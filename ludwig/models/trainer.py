#! /usr/bin/env python
# Copyright (c) 2019 Uber Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""This module contains the class and auxiliary methods of a model."""
import gc
import logging
import os
import os.path
import signal
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import psutil
import torch
from marshmallow.utils import RAISE
from marshmallow_dataclass import dataclass
from tabulate import tabulate
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

import ludwig.utils.schema_utils as schema
from ludwig.constants import COMBINED, LOSS, TEST, TRAINING, VALIDATION
from ludwig.data.dataset.base import Dataset
from ludwig.globals import (
    is_progressbar_disabled,
    MODEL_HYPERPARAMETERS_FILE_NAME,
    MODEL_WEIGHTS_FILE_NAME,
    TRAINING_CHECKPOINTS_DIR_PATH,
    TRAINING_PROGRESS_TRACKER_FILE_NAME,
)
from ludwig.models.ecd import ECD
from ludwig.models.predictor import Predictor
from ludwig.modules.metric_modules import get_improved_fun, get_initial_validation_value
from ludwig.modules.optimization_modules import (
    AdamOptimizer,
    BaseOptimizer,
    Clipper,
    ClipperDataclassField,
    create_optimizer_with_clipper,
    OptimizerDataclassField,
)
from ludwig.utils import time_utils
from ludwig.utils.checkpoint_utils import Checkpoint, CheckpointManager
from ludwig.utils.data_utils import load_json, save_json
from ludwig.utils.defaults import default_random_seed
from ludwig.utils.horovod_utils import initialize_horovod, return_first
from ludwig.utils.math_utils import exponential_decay, learning_rate_warmup, learning_rate_warmup_distributed
from ludwig.utils.misc_utils import set_random_seed

logger = logging.getLogger(__name__)


class BaseTrainer(ABC):
    @abstractmethod
    def train(self, training_set, validation_set=None, test_set=None, save_path="model", **kwargs):
        raise NotImplementedError()

    @abstractmethod
    def train_online(
        self,
        dataset,
    ):
        raise NotImplementedError()

    @property
    @abstractmethod
    def validation_field(self):
        raise NotImplementedError()

    @property
    @abstractmethod
    def validation_metric(self):
        raise NotImplementedError()

    # Remote implementations may override this
    def shutdown(self):
        pass

    # Functions needed to treat Trainer as a context manager
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()


@dataclass
class TrainerConfig:
    """TrainerConfig is a dataclass that configures most of the hyperparameters used for model training."""

    optimizer: Optional[BaseOptimizer] = OptimizerDataclassField(default={"type": "adam"})
    """Instance of `ludwig.modules.optimization_modules.BaseOptimizer` that specifies a torch-supported optimizer and
       its attributes (default: `ludwig.modules.optimization_modules.AdamOptimizer()`)."""

    epochs: int = schema.PositiveInteger(default=100)
    "Number of epochs the algorithm is intended to be run over (default: 100)."

    regularization_lambda: float = schema.FloatRange(default=0.0, min=0)
    "Strength of the $L2$ regularization (default: 0.0)."

    regularization_type: Optional[str] = schema.StringOptions(
        options=["l1", "l2", "l1_l2"], default="l2", nullable=True
    )
    "Type of regularization, one of ('l1', 'l2', 'l1_l2') (default: 'l2')."

    should_shuffle: bool = True
    "Whether to shuffle batches during training when true (default: True)."

    learning_rate: float = schema.NumericOrStringOptionsField(
        default=0.001, min=0.0, max=1.0, options=["auto"], nullable=False
    )
    """Learning rate specified in configuration, represents how much to scale the gradients by. If 'auto',
       `tune_learning_rate` must be called before training to estimate the optimal learning rate. (default: 0.001)."""

    batch_size: Union[int, str] = schema.IntegerOrStringOptionsField(
        default=128, options=["auto"], nullable=False, min_exclusive=0
    )
    "Size of batch to pass to the model for training (default: 128)."

    eval_batch_size: Union[None, int, str] = schema.IntegerOrStringOptionsField(
        default=None, options=["auto"], nullable=True, min_exclusive=0
    )
    "Size of batch to pass to the model for evaluation (default: 'auto')."

    early_stop: int = schema.IntegerRange(default=5, min=-1)
    """How many epochs without any improvement in the `validation_metric` triggers the algorithm to stop. Can be set to
       -1, which disables early_stop (default: 5)."""

    reduce_learning_rate_on_plateau: float = schema.FloatRange(default=0.0, min=0.0, max=1.0)
    """Reduces the learning rate when the algorithm hits a plateau (i.e. the performance on the validation does not
       improve). (default: 0.0)."""

    reduce_learning_rate_on_plateau_patience: int = schema.NonNegativeInteger(default=5)
    "How many epochs have to pass before the learning rate reduces (default: 5)."

    reduce_learning_rate_on_plateau_rate: float = schema.FloatRange(default=0.5, min=0.0, max=1.0)
    "Rate at which we reduce the learning rate (default: 0.5)."

    reduce_learning_rate_eval_metric: str = LOSS
    "TODO (default: 'loss')."

    reduce_learning_rate_eval_split: str = TRAINING
    "TODO (default: 'training')."

    increase_batch_size_on_plateau: int = schema.NonNegativeInteger(default=0)
    "Number to increase the batch size by on a plateau (default: 0)."

    increase_batch_size_on_plateau_patience: int = schema.NonNegativeInteger(default=5)
    "How many epochs to wait for before increasing the batch size (default: 5)."

    increase_batch_size_on_plateau_rate: float = schema.NonNegativeFloat(default=2.0)
    "Rate at which the batch size increases (default: 2.0)."

    increase_batch_size_on_plateau_max: int = schema.PositiveInteger(default=512)
    "Maximum size of the batch (default: 512)."

    increase_batch_size_eval_metric: str = LOSS
    "TODO (default: 'loss')."

    increase_batch_size_eval_split: str = TRAINING
    "TODO (default: 'training')."

    decay: bool = False
    "TODO (default: False)."

    decay_steps: int = schema.PositiveInteger(default=10000)
    "TODO (default: 10000)."

    decay_rate: float = schema.FloatRange(default=0.96, min=0.0, max=1.0)
    "TODO (default: 0.96)."

    staircase: bool = False
    "TODO (default: False)."

    gradient_clipping: Optional[Clipper] = ClipperDataclassField(default={})
    """Instance of `ludwig.modules.optimization_modules.Clipper` that sets gradient clipping params.
       (default: `ludwig.modules.optimization_modules.Clipper()`)"""

    # TODO(#1673): Need some more logic here for validating against output features
    validation_field: str = COMBINED
    "First output feature, by default it is set as the same field of the first output feature."

    validation_metric: str = LOSS
    "Metric used on `validation_field`, set by default to accuracy"

    learning_rate_warmup_epochs: float = schema.NonNegativeFloat(default=1.0)
    "Number of epochs to warmup the learning rate for"

    class Meta:
        """Sub-class specifying meta information for Marshmallow.

        Used for excluding unknown properties.
        """

        unknown = RAISE
        "Flag that sets marshmallow `load` calls to raise an error if an unknown property is passed as a parameter."


class Trainer(BaseTrainer):
    """Trainer is a class that trains a model."""

    @staticmethod
    def get_schema_cls():
        return TrainerConfig

    def __init__(
        self,
        model: ECD,
        resume: float = False,
        skip_save_model: bool = False,
        skip_save_progress: bool = False,
        skip_save_log: bool = False,
        callbacks: List = None,
        random_seed: float = default_random_seed,
        horovod: Optional[Dict] = None,
        debug: bool = False,
        device: Optional[str] = None,
        config: Optional[TrainerConfig] = TrainerConfig(),
        **kwargs,
    ):
        """Trains a model with a set of options and hyperparameters listed below. Customizable.

        :param model: Underlying Ludwig model
        :type model: `ludwig.models.ecd.ECD`
        :param resume: Resume training a model that was being trained. (default: False).
        :type resume: Boolean
        :param skip_save_model: Disables saving model weights and hyperparameters each time the model improves. By
               default Ludwig saves model weights after each epoch the validation metric (improves, but if the model is
               really big that can be time consuming. If you do not want to keep the weights and just find out what
               performance a model can get with a set of hyperparameters, use this parameter to skip it, but the model
               will not be loadable later on. (default: False).
        :type skip_save_model: Boolean
        :param skip_save_progress: Disables saving progress each epoch. By default Ludwig saves weights and stats after
               each epoch for enabling resuming of training, but if the model is really big that can be time consuming
               and will uses twice as much space, use this parameter to skip it, but training cannot be resumed later
               on. (default: False).
        :type skip_save_progress: Boolean
        :param skip_save_log: Disables saving TensorBoard logs. By default Ludwig saves logs for the TensorBoard, but if
               it is not needed turning it off can slightly increase the overall speed. (default: False).
        :type skip_save_log: Boolean
        :param callbacks: List of `ludwig.callbacks.Callback` objects that provide hooks into the Ludwig pipeline.
               (default: None).
        :type callbacks: list
        :param random_seed: Default initialization for the random seeds (default: 42).
        :type random_seed: Float
        :param horovod: Horovod parameters (default: None).
        :type horovod: dict
        :param debug: Enables debugging mode, which prints out a lot of information about the training process (default:
               False)
        :type debug: Boolean
        :param device: Device to load the model on from a saved checkpoint (default: None).
        :type device: str
        :param config: `ludwig.models.trainer.TrainerConfig` instance that specifies training hyperparameters (default:
               `ludwig.models.trainer.TrainerConfig()`).
        """
        if config is None:
            config = TrainerConfig()

        self.epochs = config.epochs
        self.regularization_lambda = config.regularization_lambda
        self.regularization_type = config.regularization_type
        self.learning_rate = config.learning_rate
        try:
            base_learning_rate = float(config.learning_rate)
        except ValueError:
            # TODO (ASN): Circle back on how we want to set default placeholder value
            base_learning_rate = 0.001  # Default initial learning rate for autoML.
        self.base_learning_rate = base_learning_rate
        self.decay = config.decay
        self.decay_rate = config.decay_rate
        self.decay_steps = config.decay_steps
        self.staircase = config.staircase
        self.batch_size = config.batch_size
        self.eval_batch_size = config.batch_size if config.eval_batch_size is None else config.eval_batch_size
        self.should_shuffle = config.should_shuffle
        # self.bucketing_field = config.bucketing_field
        self._validation_field = config.validation_field
        self._validation_metric = config.validation_metric
        self.early_stop = config.early_stop
        self.reduce_learning_rate_on_plateau = config.reduce_learning_rate_on_plateau
        self.reduce_learning_rate_on_plateau_patience = config.reduce_learning_rate_on_plateau_patience
        self.reduce_learning_rate_on_plateau_rate = config.reduce_learning_rate_on_plateau_rate
        self.reduce_learning_rate_eval_metric = config.reduce_learning_rate_eval_metric
        self.reduce_learning_rate_eval_split = config.reduce_learning_rate_eval_split
        self.increase_batch_size_on_plateau = config.increase_batch_size_on_plateau
        self.increase_batch_size_on_plateau_patience = config.increase_batch_size_on_plateau_patience
        self.increase_batch_size_on_plateau_rate = config.increase_batch_size_on_plateau_rate
        self.increase_batch_size_on_plateau_max = config.increase_batch_size_on_plateau_max
        self.increase_batch_size_eval_metric = config.increase_batch_size_eval_metric
        self.increase_batch_size_eval_split = config.increase_batch_size_eval_split
        self.learning_rate_warmup_epochs = config.learning_rate_warmup_epochs
        self.resume = resume
        self.skip_save_model = skip_save_model
        self.skip_save_progress = skip_save_progress
        self.skip_save_log = skip_save_log
        self.random_seed = random_seed
        self.horovod = horovod
        self.debug = debug
        self.received_sigint = False
        self.callbacks = callbacks or []
        self.device = device
        if self.device is None:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"

        self.model = model
        self.model = self.model.to(self.device)

        # ================ Optimizer ================
        optimizer = config.optimizer if config.optimizer is not None else AdamOptimizer()
        # Most optimizers require 'lr' parameter.  set_optimizer_learning_rate will update this during training:
        optimizer.lr = base_learning_rate
        clipper = config.gradient_clipping if config.gradient_clipping is not None else Clipper()
        self.optimizer, self.clipper = create_optimizer_with_clipper(
            model, horovod=horovod, optimizer=optimizer, clipper=clipper
        )

    def train_step(
        self, inputs: Dict[str, torch.Tensor], targets: Dict[str, torch.Tensor]
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        """Performs a single training step.

        :param inputs: A dictionary of input data, from feature name to tensor.
        :param targets: A dictionary of target data, from feature name to tensor.
        :return: A tuple of the loss and a dictionary of metrics.
        """
        self.optimizer.zero_grad()

        # Obtain model predictions and loss
        model_outputs = self.model((inputs, targets))
        loss, all_losses = self.model.train_loss(
            targets, model_outputs, self.regularization_type, self.regularization_lambda
        )

        # Begin the backward pass
        variables = self.model.parameters()
        loss.backward()

        if self.horovod:
            # Wait for gradient aggregation to complete before clipping the gradients
            self.optimizer.synchronize()

        # Clip gradients
        self.clipper.clip_grads(variables)

        # Apply gradient updates
        if self.horovod:
            # Because we already synchronized above, we can doing so here
            with self.optimizer.skip_synchronize():
                self.optimizer.step()
        else:
            self.optimizer.step()

        return loss, all_losses

    def set_base_learning_rate(self, base_learning_rate):
        """Sets the target learning rate, and updates the optimizer learning rate."""
        if self.horovod:
            base_learning_rate *= self.horovod.size()
        self.base_learning_rate = base_learning_rate  # The LR target for warmup and initial value for decay.
        self.set_optimizer_learning_rate(base_learning_rate)

    def set_optimizer_learning_rate(self, learning_rate):
        """Sets the learning rate of the optimizer."""
        for g in self.optimizer.param_groups:
            g["lr"] = learning_rate

    @classmethod
    def write_epoch_summary(
        cls,
        summary_writer,
        metrics,
        step,
    ):
        if not summary_writer:
            return

        for feature_name, output_feature in metrics.items():
            for metric in output_feature:
                metric_tag = f"{feature_name}/epoch_{metric}"
                try:
                    metric_val = output_feature[metric][-1]
                    summary_writer.add_scalar(metric_tag, metric_val, global_step=step)
                except IndexError:
                    logger.warning(f"Error computing metrics for {feature_name} {metric}.")
        summary_writer.flush()

    @classmethod
    def write_step_summary(cls, train_summary_writer, combined_loss, all_losses, step, learning_rate=None):
        if not train_summary_writer:
            return

        # combined loss
        loss_tag = "{}/step_training_loss".format("combined")
        train_summary_writer.add_scalar(loss_tag, combined_loss, global_step=step)

        # all other losses
        for feature_name, loss in all_losses.items():
            loss_tag = f"{feature_name}/step_training_loss"
            train_summary_writer.add_scalar(loss_tag, loss, global_step=step)

        if learning_rate:
            train_summary_writer.add_scalar("combined/step_learning_rate", learning_rate, global_step=step)

        train_summary_writer.flush()

    def train_for_tuning(
        self,
        dataset,
        batch_size: int,
        total_steps: int = 3,
    ):
        """Function to be used by tune_batch_size."""
        self.model.train()  # Sets model training mode.
        with dataset.initialize_batcher(batch_size=batch_size, should_shuffle=False, horovod=None) as batcher:

            step_count = 0
            while not batcher.last_batch() and step_count < total_steps:
                batch = batcher.next_batch()
                inputs = {
                    i_feat.feature_name: torch.from_numpy(batch[i_feat.proc_column]).to(self.device)
                    for i_feat in self.model.input_features.values()
                }
                targets = {
                    o_feat.feature_name: torch.from_numpy(batch[o_feat.proc_column]).to(self.device)
                    for o_feat in self.model.output_features.values()
                }

                self.train_step(inputs, targets)
                step_count += 1
        return self.model

    def tune_learning_rate(
        self,
        config,
        training_set: Dataset,
        random_seed: int = default_random_seed,
        min_lr: float = 1e-8,
        max_lr: float = 1.0,
        total_training_steps: int = 100,
        mode: str = "exponential",
        early_stop_threshold: int = 3,
        beta: float = 0.98,
    ) -> float:
        learning_rate = self.base_learning_rate

        current_learning_rate = min_lr
        losses = []
        learning_rates = []
        avg_loss = 0.0
        best_loss = 0.0
        epoch = 0
        diverging = False

        def linear_scheduler(current_learning_rate, current_step):
            scale = (current_step + 1) / total_training_steps
            return current_learning_rate + scale * (max_lr - current_learning_rate)

        def exponential_scheduler(current_learning_rate, current_step):
            scale = (current_step + 1) / total_training_steps
            return current_learning_rate * (max_lr / current_learning_rate) ** scale

        def get_optimal_lr(losses, learning_rates, skip_begin: int = 10, skip_end: int = 1):
            try:
                loss = np.array(losses[skip_begin:-skip_end])
                loss = loss[np.isfinite(loss)]
                best_lr_index = np.gradient(loss).argmin() + skip_begin
                best_lr = learning_rates[best_lr_index]
                return best_lr
            except Exception:
                return None

        self.model.train()  # Sets model training mode.
        with training_set.initialize_batcher(
            batch_size=self.batch_size, should_shuffle=self.should_shuffle, horovod=self.horovod
        ) as batcher:
            step_count = 0
            while epoch < self.epochs and step_count < total_training_steps and not diverging:
                batcher.set_epoch(epoch, self.batch_size)
                self.model.reset_metrics()
                while not batcher.last_batch() and step_count < total_training_steps:
                    batch = batcher.next_batch()
                    inputs = {
                        i_feat.feature_name: torch.from_numpy(batch[i_feat.proc_column]).to(self.device)
                        for i_feat in self.model.input_features.values()
                    }
                    targets = {
                        o_feat.feature_name: torch.from_numpy(batch[o_feat.proc_column]).to(self.device)
                        for o_feat in self.model.output_features.values()
                    }

                    loss, _ = self.train_step(
                        inputs,
                        targets,
                    )
                    # compute smoothed loss
                    avg_loss = beta * avg_loss + (1 - beta) * loss
                    smoothed_loss = avg_loss / (1 - beta ** (step_count + 1))

                    # store learning rate and loss
                    learning_rates.append(current_learning_rate)
                    losses.append(smoothed_loss)

                    # check whether loss is diverging
                    if step_count > 0 and smoothed_loss > early_stop_threshold * best_loss:
                        diverging = True
                        break
                    else:
                        if smoothed_loss < best_loss or step_count == 0:
                            best_loss = smoothed_loss

                    # compute new learning rate
                    if mode == "exponential":
                        current_learning_rate = exponential_scheduler(current_learning_rate, step_count)
                    else:
                        current_learning_rate = linear_scheduler(current_learning_rate, step_count)

                    self.set_optimizer_learning_rate(current_learning_rate)
                    step_count += 1

                epoch += 1

        optimal_lr = get_optimal_lr(losses, learning_rates)
        if optimal_lr:
            learning_rate = optimal_lr
        return learning_rate

    def tune_batch_size(
        self,
        config: Dict[str, Any],
        training_set: Dataset,
        random_seed: int = default_random_seed,
        max_trials: int = 10,
        halving_limit: int = 3,
    ) -> int:
        def _is_valid_batch_size(batch_size):
            return batch_size < len(training_set)

        # TODO (ASN) : Circle back on how we want to set default placeholder value
        # Currently, since self.batch_size is originally set to auto, we provide a
        # placeholder starting value (namely, 128)
        batch_size = 128
        skip_save_model = self.skip_save_model
        skip_save_progress = self.skip_save_progress
        skip_save_log = self.skip_save_log
        # Set temporary values
        self.skip_save_model = True
        self.skip_save_progress = True
        self.skip_save_log = True

        try:
            high = None
            count = 0
            halving_count = 0
            while halving_count < halving_limit:
                gc.collect()

                low = batch_size
                prev_batch_size = batch_size
                try:
                    self.train_for_tuning(training_set, batch_size, total_steps=3)
                    count += 1
                    if count >= max_trials:
                        break
                    if high:
                        if high - low <= 1:
                            break
                        midval = (high + low) // 2
                        batch_size = midval
                    else:
                        batch_size *= 2  # double batch size

                    if batch_size == prev_batch_size:
                        break

                except RuntimeError:
                    # PyTorch only generates Runtime errors for CUDA OOM.
                    gc.collect()
                    high = batch_size
                    halving_count += 1
                    midval = (high + low) // 2
                    batch_size = midval
                    if high - low <= 1:
                        break

                # make sure that batch size is valid (e.g. less than size of ds)
                if not _is_valid_batch_size(batch_size):
                    batch_size = min(batch_size, len(training_set))

                # edge case where bs is no longer increasing
                if batch_size == prev_batch_size:
                    break
        finally:
            # Restore original parameters to defaults
            # self.epochs = original_epochs
            self.skip_save_model = skip_save_model
            self.skip_save_progress = skip_save_progress
            self.skip_save_log = skip_save_log

        return batch_size

    def train(self, training_set, validation_set=None, test_set=None, save_path="model", **kwargs):
        """Trains a model with a set of hyperparameters listed below. Customizable.

        :param training_set: The training set
        :param validation_set: The validation dataset
        :param test_set: The test dataset
        """
        # ====== General setup =======
        output_features = self.model.output_features
        digits_per_epochs = len(str(self.epochs))
        # Only use signals when on the main thread to avoid issues with CherryPy
        # https://github.com/ludwig-ai/ludwig/issues/286
        if threading.current_thread() == threading.main_thread():
            signal.signal(signal.SIGINT, self.set_epochs_to_1_or_quit)
        should_validate = validation_set is not None and validation_set.size > 0

        metrics_names = self.get_metrics_names(output_features)

        # check if validation_field is valid
        valid_validation_field = False
        if self.validation_field == "combined":
            valid_validation_field = True
            if self.validation_metric is not LOSS and len(output_features) == 1:
                only_of = next(iter(output_features))
                if self.validation_metric in metrics_names[only_of]:
                    self._validation_field = only_of
                    logger.warning(
                        "Replacing 'combined' validation field "
                        "with '{}' as the specified validation "
                        "metric {} is invalid for 'combined' "
                        "but is valid for '{}'.".format(only_of, self.validation_metric, only_of)
                    )
        else:
            for output_feature in output_features:
                if self.validation_field == output_feature:
                    valid_validation_field = True

        if not valid_validation_field:
            raise ValueError(
                "The specified validation_field {} is not valid."
                "Available ones are: {}".format(self.validation_field, list(output_features.keys()) + ["combined"])
            )

        # check if validation_metric is valid
        valid_validation_metric = self.validation_metric in metrics_names[self.validation_field]
        if not valid_validation_metric:
            raise ValueError(
                "The specified metric {} is not valid. "
                "Available metrics for {} output feature are: {}".format(
                    self.validation_metric, self.validation_field, metrics_names[self.validation_field]
                )
            )

        # ====== Setup file names =======
        model_weights_path = model_hyperparameters_path = None
        training_checkpoints_path = training_progress_tracker_path = None
        tensorboard_log_dir = None
        if self.is_coordinator():
            os.makedirs(save_path, exist_ok=True)
            model_weights_path = os.path.join(save_path, MODEL_WEIGHTS_FILE_NAME)
            model_hyperparameters_path = os.path.join(save_path, MODEL_HYPERPARAMETERS_FILE_NAME)
            training_checkpoints_path = os.path.join(save_path, TRAINING_CHECKPOINTS_DIR_PATH)
            # training_checkpoints_prefix_path = os.path.join(
            #    training_checkpoints_path, "ckpt"
            # )
            tensorboard_log_dir = os.path.join(save_path, "logs")
        if save_path:
            training_progress_tracker_path = os.path.join(save_path, TRAINING_PROGRESS_TRACKER_FILE_NAME)

        self.callback(lambda c: c.on_trainer_train_setup(self, save_path), coordinator_only=False)

        # ====== Setup session =======
        checkpoint = checkpoint_manager = None
        if self.is_coordinator():
            checkpoint = Checkpoint(model=self.model, optimizer=self.optimizer)
            checkpoint_manager = CheckpointManager(
                checkpoint, training_checkpoints_path, device=self.device, max_to_keep=1
            )

        train_summary_writer = None
        validation_summary_writer = None
        test_summary_writer = None
        if self.is_coordinator() and not self.skip_save_log and tensorboard_log_dir:
            train_summary_writer = SummaryWriter(os.path.join(tensorboard_log_dir, TRAINING))
            if validation_set is not None and validation_set.size > 0:
                validation_summary_writer = SummaryWriter(os.path.join(tensorboard_log_dir, VALIDATION))
            if test_set is not None and test_set.size > 0:
                test_summary_writer = SummaryWriter(os.path.join(tensorboard_log_dir, TEST))

        # ================ Resume logic ================
        if self.resume:
            progress_tracker = self.resume_training_progress_tracker(training_progress_tracker_path)
            if self.is_coordinator():
                self.resume_weights_and_optimzier(training_checkpoints_path, checkpoint)
        else:
            (train_metrics, vali_metrics, test_metrics) = self.initialize_training_metrics(output_features)

            progress_tracker = ProgressTracker(
                batch_size=self.batch_size,
                epoch=0,
                steps=0,
                last_improvement_epoch=0,
                last_learning_rate_reduction_epoch=0,
                last_increase_batch_size_epoch=0,
                learning_rate=self.base_learning_rate,
                best_eval_metric=get_initial_validation_value(self.validation_metric),
                best_reduce_learning_rate_eval_metric=get_initial_validation_value(
                    self.reduce_learning_rate_eval_metric
                ),
                last_reduce_learning_rate_eval_metric_improvement=0,
                best_increase_batch_size_eval_metric=get_initial_validation_value(self.increase_batch_size_eval_metric),
                last_increase_batch_size_eval_metric_improvement=0,
                num_reductions_learning_rate=0,
                num_increases_batch_size=0,
                train_metrics=train_metrics,
                vali_metrics=vali_metrics,
                test_metrics=test_metrics,
                last_improvement=0,
                last_learning_rate_reduction=0,
                last_increase_batch_size=0,
            )

        if self.horovod:
            # Horovod: broadcast initial variable states from rank 0 to all other processes.
            # This is necessary to ensure consistent initialization of all workers when
            # training is started with random weights or restored from a checkpoint.
            self.horovod.broadcast_parameters(self.model.state_dict(), root_rank=0)
            self.horovod.broadcast_optimizer_state(self.optimizer, root_rank=0)

        set_random_seed(self.random_seed)
        with training_set.initialize_batcher(
            batch_size=self.batch_size,
            should_shuffle=self.should_shuffle,
            seed=self.random_seed,
            horovod=self.horovod,
        ) as batcher:

            # ================ Training Loop ================
            while progress_tracker.epoch < self.epochs:
                # note that batch size may change over epochs
                batcher.set_epoch(progress_tracker.epoch, progress_tracker.batch_size)

                # epoch init
                start_time = time.time()
                if self.is_coordinator():
                    logger.info(
                        "Epoch {epoch:{digits}d}".format(epoch=progress_tracker.epoch + 1, digits=digits_per_epochs)
                    )

                # Reset the metrics at the start of the next epoch
                self.model.train()  # Sets model to training mode.
                self.model.reset_metrics()

                # ================ Train ================
                progress_bar = None
                if self.is_coordinator():
                    progress_bar = tqdm(
                        desc="Training",
                        total=batcher.steps_per_epoch,
                        file=sys.stdout,
                        disable=is_progressbar_disabled(),
                    )

                self.callback(lambda c: c.on_epoch_start(self, progress_tracker, save_path))

                # training step loop
                self._train_loop(batcher, progress_tracker, save_path, train_summary_writer, progress_bar)

                # ================ Post Training Epoch ================
                if self.is_coordinator():
                    progress_bar.close()

                progress_tracker.epoch += 1

                # ================ Eval ================
                # init tables
                tables = OrderedDict()
                for output_feature_name, output_feature in output_features.items():
                    tables[output_feature_name] = [[output_feature_name] + metrics_names[output_feature_name]]
                tables[COMBINED] = [[COMBINED, LOSS]]

                # eval metrics on train
                self.eval_batch_size = max(self.eval_batch_size, progress_tracker.batch_size)
                self.evaluation(
                    training_set,
                    "train",
                    progress_tracker.train_metrics,
                    tables,
                    self.eval_batch_size,
                )

                self.write_epoch_summary(
                    summary_writer=train_summary_writer,
                    metrics=progress_tracker.train_metrics,
                    step=progress_tracker.epoch,
                )

                if validation_set is not None:
                    self.callback(lambda c: c.on_validation_start(self, progress_tracker, save_path))

                    # eval metrics on validation set
                    self.evaluation(
                        validation_set,
                        "vali",
                        progress_tracker.vali_metrics,
                        tables,
                        self.eval_batch_size,
                    )

                    self.write_epoch_summary(
                        summary_writer=validation_summary_writer,
                        metrics=progress_tracker.vali_metrics,
                        step=progress_tracker.epoch,
                    )

                    self.callback(lambda c: c.on_validation_end(self, progress_tracker, save_path))

                if test_set is not None:
                    self.callback(lambda c: c.on_test_start(self, progress_tracker, save_path))

                    # eval metrics on test set
                    self.evaluation(
                        test_set,
                        TEST,
                        progress_tracker.test_metrics,
                        tables,
                        self.eval_batch_size,
                    )

                    self.write_epoch_summary(
                        summary_writer=test_summary_writer,
                        metrics=progress_tracker.test_metrics,
                        step=progress_tracker.epoch,
                    )

                    self.callback(lambda c: c.on_test_end(self, progress_tracker, save_path))

                elapsed_time = (time.time() - start_time) * 1000.0

                if self.is_coordinator():
                    logger.info(f"Took {time_utils.strdelta(elapsed_time)}")

                # metric prints
                if self.is_coordinator():
                    for output_feature, table in tables.items():
                        logger.info(tabulate(table, headers="firstrow", tablefmt="fancy_grid", floatfmt=".4f"))

                # ================ Validation Logic ================
                if should_validate:
                    should_break = self.check_progress_on_validation(
                        progress_tracker,
                        self.validation_field,
                        self.validation_metric,
                        model_weights_path,
                        model_hyperparameters_path,
                        self.reduce_learning_rate_on_plateau,
                        self.reduce_learning_rate_on_plateau_patience,
                        self.reduce_learning_rate_on_plateau_rate,
                        self.reduce_learning_rate_eval_metric,
                        self.reduce_learning_rate_eval_split,
                        self.increase_batch_size_on_plateau,
                        self.increase_batch_size_on_plateau_patience,
                        self.increase_batch_size_on_plateau_rate,
                        self.increase_batch_size_on_plateau_max,
                        self.increase_batch_size_eval_metric,
                        self.increase_batch_size_eval_split,
                        self.early_stop,
                        self.skip_save_model,
                    )
                    if should_break:
                        break
                else:
                    # there's no validation, so we save the model at each iteration
                    if self.is_coordinator() and not self.skip_save_model:
                        torch.save(self.model.state_dict(), model_weights_path)

                # ========== Save training progress ==========
                if self.is_coordinator():
                    if not self.skip_save_progress:
                        checkpoint_manager.save(progress_tracker.epoch)
                        progress_tracker.save(os.path.join(save_path, TRAINING_PROGRESS_TRACKER_FILE_NAME))
                    logger.info("")

                self.callback(lambda c: c.on_epoch_end(self, progress_tracker, save_path))

        if train_summary_writer is not None:
            train_summary_writer.close()
        if validation_summary_writer is not None:
            validation_summary_writer.close()
        if test_summary_writer is not None:
            test_summary_writer.close()

        return (
            self.model,
            progress_tracker.train_metrics,
            progress_tracker.vali_metrics,
            progress_tracker.test_metrics,
        )

    def _train_loop(self, batcher, progress_tracker, save_path, train_summary_writer, progress_bar):
        while not batcher.last_batch():
            self.callback(lambda c: c.on_batch_start(self, progress_tracker, save_path))

            # Set learning rate for this batch
            current_learning_rate = progress_tracker.learning_rate

            if self.decay:
                current_learning_rate = exponential_decay(
                    current_learning_rate,
                    self.decay_rate,
                    self.decay_steps,
                    progress_tracker.steps,
                    self.staircase,
                )

            if self.horovod:
                current_learning_rate = (
                    learning_rate_warmup_distributed(
                        current_learning_rate,
                        progress_tracker.epoch,
                        self.learning_rate_warmup_epochs,
                        self.horovod.size(),
                        batcher.step,
                        batcher.steps_per_epoch,
                    )
                    * self.horovod.size()
                )
            else:
                current_learning_rate = learning_rate_warmup(
                    current_learning_rate,
                    progress_tracker.epoch,
                    self.learning_rate_warmup_epochs,
                    batcher.step,
                    batcher.steps_per_epoch,
                )
            self.set_optimizer_learning_rate(current_learning_rate)

            # obtain batch
            batch = batcher.next_batch()

            # Move tensors to cuda here.
            inputs = {
                i_feat.feature_name: torch.from_numpy(batch[i_feat.proc_column]).to(self.device)
                for i_feat in self.model.input_features.values()
            }
            targets = {
                o_feat.feature_name: torch.from_numpy(batch[o_feat.proc_column]).to(self.device)
                for o_feat in self.model.output_features.values()
            }

            # Reintroduce for tensorboard graph
            # if first_batch and self.is_coordinator() and not skip_save_log:
            #    tf.summary.trace_on(graph=True, profiler=True)

            loss, all_losses = self.train_step(
                inputs,
                targets,
            )

            # Reintroduce for tensorboard graph
            # if first_batch and self.is_coordinator() and not skip_save_log:
            #     with train_summary_writer.as_default():
            #         tf.summary.trace_export(
            #             name="Model",
            #             step=0,
            #             profiler_outdir=tensorboard_log_dir
            #         )

            if self.is_coordinator() and not self.skip_save_log:
                self.write_step_summary(
                    train_summary_writer=train_summary_writer,
                    combined_loss=loss,
                    all_losses=all_losses,
                    step=progress_tracker.steps,
                    learning_rate=current_learning_rate,
                )

            progress_tracker.steps += 1
            if self.is_coordinator():
                progress_bar.update(1)
                logger.debug(
                    f"training: completed batch {progress_bar.n} "
                    f"memory used: "
                    f"{psutil.Process(os.getpid()).memory_info()[0] / 1e6:0.2f}MB"
                )

            self.callback(lambda c: c.on_batch_end(self, progress_tracker, save_path))

    def train_online(self, dataset):
        self.model.train()  # Sets model training mode.
        with dataset.initialize_batcher(
            batch_size=self.batch_size, should_shuffle=self.should_shuffle, horovod=self.horovod
        ) as batcher:

            # training step loop
            progress_bar = tqdm(
                desc="Training online",
                total=batcher.steps_per_epoch,
                file=sys.stdout,
                disable=is_progressbar_disabled(),
            )

            while not batcher.last_batch():
                batch = batcher.next_batch()
                inputs = {
                    i_feat.feature_name: torch.from_numpy(batch[i_feat.proc_column]).to(self.device)
                    for i_feat in self.model.input_features.values()
                }
                targets = {
                    o_feat.feature_name: torch.from_numpy(batch[o_feat.proc_column]).to(self.device)
                    for o_feat in self.model.output_features.values()
                }

                self.train_step(
                    inputs,
                    targets,
                )

                progress_bar.update(1)

            progress_bar.close()
        return self.model

    @property
    def validation_field(self):
        return self._validation_field

    @property
    def validation_metric(self):
        return self._validation_metric

    def append_metrics(self, dataset_name, results, metrics_log, tables):
        for output_feature in self.model.output_features:
            scores = [dataset_name]

            # collect metric names based on output features metrics to
            # ensure consistent order of reporting metrics
            metric_names = self.model.output_features[output_feature].metric_functions.keys()

            for metric in metric_names:
                if metric in results[output_feature]:
                    # Some metrics may have been excepted and excluded from results.
                    score = results[output_feature][metric]
                    metrics_log[output_feature][metric].append(score)
                    scores.append(score)

            tables[output_feature].append(scores)

        metrics_log[COMBINED][LOSS].append(results[COMBINED][LOSS])
        tables[COMBINED].append([dataset_name, results[COMBINED][LOSS]])

        return metrics_log, tables

    def evaluation(
        self,
        dataset,
        dataset_name,
        metrics_log,
        tables,
        batch_size=128,
    ):
        predictor = Predictor(self.model, batch_size=batch_size, horovod=self.horovod, debug=self.debug)
        metrics, predictions = predictor.batch_evaluation(dataset, collect_predictions=False, dataset_name=dataset_name)

        self.append_metrics(dataset_name, metrics, metrics_log, tables)

        return metrics_log, tables

    def check_progress_on_validation(
        self,
        progress_tracker,
        validation_output_feature_name,
        validation_metric,
        model_weights_path,
        model_hyperparameters_path,
        reduce_learning_rate_on_plateau,
        reduce_learning_rate_on_plateau_patience,
        reduce_learning_rate_on_plateau_rate,
        reduce_learning_rate_eval_metric,
        reduce_learning_rate_eval_split,
        increase_batch_size_on_plateau,
        increase_batch_size_on_plateau_patience,
        increase_batch_size_on_plateau_rate,
        increase_batch_size_on_plateau_max,
        increase_batch_size_eval_metric,
        increase_batch_size_eval_split,
        early_stop,
        skip_save_model,
    ):
        should_break = False
        # record how long its been since an improvement
        improved = get_improved_fun(validation_metric)
        vali_metric = progress_tracker.vali_metrics[validation_output_feature_name]
        if improved(vali_metric[validation_metric][-1], progress_tracker.best_eval_metric):
            progress_tracker.last_improvement_epoch = progress_tracker.epoch
            progress_tracker.best_eval_metric = progress_tracker.vali_metrics[validation_output_feature_name][
                validation_metric
            ][-1]
            if self.is_coordinator() and not skip_save_model:
                torch.save(self.model.state_dict(), model_weights_path)
                logger.info(
                    f"Validation {validation_metric} on {validation_output_feature_name} improved, model saved."
                )

        progress_tracker.last_improvement = progress_tracker.epoch - progress_tracker.last_improvement_epoch
        if progress_tracker.last_improvement != 0 and self.is_coordinator():
            logger.info(
                f"Last improvement of {validation_output_feature_name} validation {validation_metric} happened "
                + f"{progress_tracker.last_improvement} epoch(s) ago."
            )

        # ========== Reduce Learning Rate Plateau logic ========
        if reduce_learning_rate_on_plateau > 0:
            self.reduce_learning_rate(
                progress_tracker,
                validation_output_feature_name,
                reduce_learning_rate_on_plateau,
                reduce_learning_rate_on_plateau_patience,
                reduce_learning_rate_on_plateau_rate,
                reduce_learning_rate_eval_metric,
                reduce_learning_rate_eval_split,
            )
            progress_tracker.last_learning_rate_reduction = (
                progress_tracker.epoch - progress_tracker.last_learning_rate_reduction_epoch
            )
            if (
                progress_tracker.last_learning_rate_reduction > 0
                and progress_tracker.last_reduce_learning_rate_eval_metric_improvement > 0
                and not progress_tracker.num_reductions_learning_rate >= reduce_learning_rate_on_plateau
            ):
                logger.info(
                    f"Last learning rate reduction happened {progress_tracker.last_learning_rate_reduction} epoch(s) "
                    f"ago, improvement of {validation_output_feature_name} {reduce_learning_rate_eval_split} "
                    f"{reduce_learning_rate_eval_metric} happened "
                    f"{progress_tracker.last_reduce_learning_rate_eval_metric_improvement} epoch(s) ago."
                )

        # ========== Increase Batch Size Plateau logic =========
        if increase_batch_size_on_plateau > 0:
            self.increase_batch_size(
                progress_tracker,
                validation_output_feature_name,
                increase_batch_size_on_plateau,
                increase_batch_size_on_plateau_patience,
                increase_batch_size_on_plateau_rate,
                increase_batch_size_on_plateau_max,
                increase_batch_size_eval_metric,
                increase_batch_size_eval_split,
            )
            progress_tracker.last_increase_batch_size = (
                progress_tracker.epoch - progress_tracker.last_increase_batch_size_epoch
            )
            if (
                progress_tracker.last_increase_batch_size > 0
                and progress_tracker.last_increase_batch_size_eval_metric_improvement > 0
                and not progress_tracker.num_increases_batch_size >= increase_batch_size_on_plateau
                and not progress_tracker.batch_size >= increase_batch_size_on_plateau_max
            ):
                logger.info(
                    "Last batch size increase "
                    f"happened {progress_tracker.last_increase_batch_size} epoch(s) ago, "
                    f"improvement of {validation_output_feature_name} {increase_batch_size_eval_split} "
                    f"{increase_batch_size_eval_metric} happened "
                    f"{progress_tracker.last_increase_batch_size_eval_metric_improvement} epoch(s) ago."
                )

        # ========== Early Stop logic ==========
        if 0 < early_stop <= progress_tracker.last_improvement:
            if self.is_coordinator():
                logger.info(
                    "\nEARLY STOPPING due to lack of "
                    "validation improvement, "
                    f"it has been {progress_tracker.epoch - progress_tracker.last_improvement_epoch} epoch(s) since "
                    "last validation improvement.\n"
                )
            should_break = True
        return should_break

    def set_epochs_to_1_or_quit(self, signum, frame):
        if not self.received_sigint:
            self.epochs = 1
            self.received_sigint = True
            logger.critical("\nReceived SIGINT, will finish this epoch and then conclude " "the training")
            logger.critical("Send another SIGINT to immediately interrupt the process")
        else:
            logger.critical("\nReceived a second SIGINT, will now quit")
            sys.exit(1)

    def quit_training(self, signum, frame):
        logger.critical("Received SIGQUIT, will kill training")
        sys.exit(1)

    def resume_training_progress_tracker(self, training_progress_tracker_path):
        if self.is_coordinator():
            logger.info(f"Resuming training of model: {training_progress_tracker_path}")
        progress_tracker = ProgressTracker.load(training_progress_tracker_path)
        return progress_tracker

    def initialize_training_metrics(self, output_features):
        train_metrics = OrderedDict()
        vali_metrics = OrderedDict()
        test_metrics = OrderedDict()

        for output_feature_name, output_feature in output_features.items():
            train_metrics[output_feature_name] = OrderedDict()
            vali_metrics[output_feature_name] = OrderedDict()
            test_metrics[output_feature_name] = OrderedDict()
            for metric in output_feature.metric_functions:
                train_metrics[output_feature_name][metric] = []
                vali_metrics[output_feature_name][metric] = []
                test_metrics[output_feature_name][metric] = []

        for metrics in [train_metrics, vali_metrics, test_metrics]:
            metrics[COMBINED] = {LOSS: []}

        return train_metrics, vali_metrics, test_metrics

    def get_metrics_names(self, output_features):
        metrics_names = {}
        for output_feature_name, output_feature in output_features.items():
            for metric in output_feature.metric_functions:
                metrics = metrics_names.get(output_feature_name, [])
                metrics.append(metric)
                metrics_names[output_feature_name] = metrics
        metrics_names[COMBINED] = [LOSS]
        return metrics_names

    def resume_weights_and_optimzier(
        self,
        model_weights_progress_path: str,
        checkpoint: Checkpoint,
    ):
        CheckpointManager.load_latest_checkpoint(checkpoint, model_weights_progress_path, self.device)

    def reduce_learning_rate(
        self,
        progress_tracker,
        validation_output_feature_name,
        reduce_learning_rate_on_plateau,
        reduce_learning_rate_on_plateau_patience,
        reduce_learning_rate_on_plateau_rate,
        reduce_learning_rate_eval_metric=LOSS,
        reduce_learning_rate_eval_split=TRAINING,
    ):
        if not (progress_tracker.num_reductions_learning_rate >= reduce_learning_rate_on_plateau):

            if reduce_learning_rate_eval_split == TRAINING:
                split_metrics = progress_tracker.train_metrics
            elif reduce_learning_rate_eval_split == VALIDATION:
                split_metrics = progress_tracker.vali_metrics
            else:  # if reduce_learning_rate_eval_split == TEST:
                split_metrics = progress_tracker.test_metrics

            validation_metric = reduce_learning_rate_eval_metric
            last_metric_value = split_metrics[validation_output_feature_name][validation_metric][-1]

            improved = get_improved_fun(validation_metric)
            is_improved = improved(last_metric_value, progress_tracker.best_reduce_learning_rate_eval_metric)
            if is_improved:
                # we update the best metric value and set it to the current one
                # and reset last improvement epoch count
                progress_tracker.best_reduce_learning_rate_eval_metric = last_metric_value
                progress_tracker.last_reduce_learning_rate_eval_metric_improvement = 0
            else:
                progress_tracker.last_reduce_learning_rate_eval_metric_improvement += 1
                if not is_improved and (
                    # learning rate reduction happened more than N epochs ago
                    progress_tracker.last_learning_rate_reduction >= reduce_learning_rate_on_plateau_patience
                    # No improvement of the evaluation metric since more than N epochs ago
                    and progress_tracker.last_reduce_learning_rate_eval_metric_improvement
                    >= reduce_learning_rate_on_plateau_patience
                ):
                    progress_tracker.learning_rate *= reduce_learning_rate_on_plateau_rate

                    if self.is_coordinator():
                        logger.info(
                            f"PLATEAU REACHED, reducing learning rate to {progress_tracker.learning_rate} due to lack "
                            f"of improvement of {validation_output_feature_name} {reduce_learning_rate_eval_split} "
                            f"{validation_metric}."
                        )

                    progress_tracker.last_learning_rate_reduction_epoch = progress_tracker.epoch
                    progress_tracker.last_learning_rate_reduction = 0
                    progress_tracker.num_reductions_learning_rate += 1

                    if progress_tracker.num_reductions_learning_rate >= reduce_learning_rate_on_plateau:
                        if self.is_coordinator():
                            logger.info(
                                f"Learning rate was already reduced {progress_tracker.num_reductions_learning_rate} "
                                "time(s), not reducing it anymore."
                            )

    def increase_batch_size(
        self,
        progress_tracker,
        validation_output_feature_name,
        increase_batch_size_on_plateau,
        increase_batch_size_on_plateau_patience,
        increase_batch_size_on_plateau_rate,
        increase_batch_size_on_plateau_max,
        increase_batch_size_eval_metric=LOSS,
        increase_batch_size_eval_split=TRAINING,
    ):
        if (
            not progress_tracker.num_increases_batch_size >= increase_batch_size_on_plateau
            and not progress_tracker.batch_size == increase_batch_size_on_plateau_max
        ):

            if increase_batch_size_eval_split == TRAINING:
                split_metrics = progress_tracker.train_metrics
            elif increase_batch_size_eval_split == VALIDATION:
                split_metrics = progress_tracker.vali_metrics
            else:  # if increase_batch_size_eval_split == TEST:
                split_metrics = progress_tracker.test_metrics

            validation_metric = increase_batch_size_eval_metric
            last_metric_value = split_metrics[validation_output_feature_name][validation_metric][-1]

            improved = get_improved_fun(validation_metric)
            is_improved = improved(last_metric_value, progress_tracker.best_increase_batch_size_eval_metric)
            if is_improved:
                # We update the best metric value and set it to the current one, and reset last
                # improvement epoch count
                progress_tracker.best_increase_batch_size_eval_metric = last_metric_value
                progress_tracker.last_increase_batch_size_eval_metric_improvement = 0
            else:
                progress_tracker.last_increase_batch_size_eval_metric_improvement += 1
                if not is_improved and (
                    # Batch size increase happened more than N epochs ago
                    progress_tracker.last_increase_batch_size >= increase_batch_size_on_plateau_patience
                    and (
                        # No improvement of the evaluation metric since more than N epochs ago
                        progress_tracker.last_increase_batch_size_eval_metric_improvement
                        >= increase_batch_size_on_plateau_patience
                    )
                ):
                    progress_tracker.batch_size = min(
                        (increase_batch_size_on_plateau_rate * progress_tracker.batch_size),
                        increase_batch_size_on_plateau_max,
                    )

                    if self.is_coordinator():
                        logger.info(
                            f"PLATEAU REACHED, increasing batch size to {progress_tracker.batch_size} due to lack of "
                            f"improvement of {validation_output_feature_name} {increase_batch_size_eval_split} "
                            f"{validation_metric}."
                        )

                    progress_tracker.last_increase_batch_size_epoch = progress_tracker.epoch
                    progress_tracker.last_increase_batch_size = 0
                    progress_tracker.num_increases_batch_size += 1

                    if progress_tracker.num_increases_batch_size >= increase_batch_size_on_plateau:
                        if self.is_coordinator():
                            logger.info(
                                f"Batch size was already increased {progress_tracker.num_increases_batch_size} times, "
                                "not increasing it anymore."
                            )
                    elif progress_tracker.batch_size >= increase_batch_size_on_plateau_max:
                        if self.is_coordinator():
                            logger.info(
                                f"Batch size was already increased {progress_tracker.num_increases_batch_size} times, "
                                f"currently it is {progress_tracker.batch_size}, the maximum allowed."
                            )

    def is_coordinator(self):
        if not self.horovod:
            return True
        return self.horovod.rank() == 0

    def callback(self, fn, coordinator_only=True):
        if not coordinator_only or self.is_coordinator():
            for callback in self.callbacks:
                fn(callback)


class RemoteTrainer(Trainer):
    def __init__(self, gpus=None, gpu_memory_limit=None, allow_parallel_threads=True, **kwargs):
        horovod = initialize_horovod()
        config, kwargs = schema.load_config_with_kwargs(Trainer.get_schema_cls(), kwargs)
        super().__init__(horovod=horovod, config=config, **kwargs)

        # Only return results from rank 0 to reduce network overhead
        self.train = return_first(self.train)
        self.train_online = return_first(self.train_online)


class ProgressTracker:
    def __init__(
        self,
        epoch,
        batch_size,
        steps,
        last_improvement_epoch,
        last_learning_rate_reduction_epoch,
        last_increase_batch_size_epoch,
        best_eval_metric,
        best_reduce_learning_rate_eval_metric,
        last_reduce_learning_rate_eval_metric_improvement,
        best_increase_batch_size_eval_metric,
        last_increase_batch_size_eval_metric_improvement,
        learning_rate,
        num_reductions_learning_rate,
        num_increases_batch_size,
        train_metrics,
        vali_metrics,
        test_metrics,
        last_improvement,
        last_learning_rate_reduction,
        last_increase_batch_size,
    ):
        self.batch_size = batch_size
        self.epoch = epoch
        self.steps = steps
        self.last_improvement_epoch = last_improvement_epoch
        self.last_improvement = last_improvement
        self.last_learning_rate_reduction_epoch = last_learning_rate_reduction_epoch
        self.last_learning_rate_reduction = last_learning_rate_reduction
        self.last_increase_batch_size_epoch = last_increase_batch_size_epoch
        self.last_increase_batch_size = last_increase_batch_size
        self.learning_rate = learning_rate
        self.best_eval_metric = best_eval_metric
        self.best_reduce_learning_rate_eval_metric = best_reduce_learning_rate_eval_metric
        self.last_reduce_learning_rate_eval_metric_improvement = last_reduce_learning_rate_eval_metric_improvement
        self.best_increase_batch_size_eval_metric = best_increase_batch_size_eval_metric
        self.last_increase_batch_size_eval_metric_improvement = last_increase_batch_size_eval_metric_improvement
        self.num_reductions_learning_rate = num_reductions_learning_rate
        self.num_increases_batch_size = num_increases_batch_size
        self.train_metrics = train_metrics
        self.vali_metrics = vali_metrics
        self.test_metrics = test_metrics

    def save(self, filepath):
        save_json(filepath, self.__dict__)

    @staticmethod
    def load(filepath):
        loaded = load_json(filepath)
        return ProgressTracker(**loaded)

    def log_metrics(self, idx=-1):
        log_metrics = {}
        for item_name in [
            "batch_size",
            "epoch",
            "steps",
            "last_improvement_epoch",
            "learning_rate",
            "best_valid_metric",
            "num_reductions_lr",
            "num_increases_bs",
            "train_metrics",
            "vali_metrics",
            "test_metrics",
        ]:
            try:
                item = getattr(self, item_name)
                if isinstance(item, dict):
                    for key in item:
                        if isinstance(item[key], dict):
                            for key2 in item[key]:
                                log_metrics[item_name + "." + key + "." + key2] = item[key][key2][idx]
                        else:
                            log_metrics[item_name + "." + key] = item[key][idx]
                elif item is not None:
                    log_metrics[item_name] = item
            except Exception:
                logger.info(f"skip logging '{item_name}'")
        return log_metrics
