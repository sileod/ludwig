import contextlib
import logging
from typing import (
    Callable,
    Optional,
    List,
    Union,
    Any,
    Dict,
    Iterable,
    TYPE_CHECKING,
)

from ray.data.datasource.partitioning import PathPartitionFilter

if TYPE_CHECKING:
    import pyarrow

from ray.data.block import Block
from ray.data.context import DatasetContext
from ray.data.impl.output_buffer import BlockOutputBuffer
from ray.data.datasource.binary_datasource import BinaryDatasource
from ray.data.datasource.datasource import ReadTask
from ray.data.datasource.file_based_datasource import (
    _resolve_paths_and_filesystem,
    _wrap_s3_serialization_workaround,
    _S3FileSystemWrapper,
)
from ray.data.datasource.file_meta_provider import (
    BaseFileMetadataProvider,
    DefaultFileMetadataProvider,
)
from ray.data.impl.util import _check_pyarrow_version

from ludwig.utils.strings_utils import is_nan_or_none

logger = logging.getLogger(__name__)


class BinaryNaNCompatibleDatasource(BinaryDatasource):
    """Binary datasource, for reading and writing binary files. Ignores NaNs and None values.

    Examples:
        >>> import ray
        >>> from ray.data.datasource import BinaryDatasource
        >>> source = BinaryDatasource() # doctest: +SKIP
        >>> ray.data.read_datasource( # doctest: +SKIP
        ...     source, paths=["/path/to/dir", None]).take()
        [b"file_data", ...]
    """

    def prepare_read(
        self,
        parallelism: int,
        paths: Union[str, List[str]],
        filesystem: Optional["pyarrow.fs.FileSystem"] = None,
        schema: Optional[Union[type, "pyarrow.lib.Schema"]] = None,
        open_stream_args: Optional[Dict[str, Any]] = None,
        meta_provider: BaseFileMetadataProvider = DefaultFileMetadataProvider(),
        partition_filter: PathPartitionFilter = None,
        # TODO(ekl) deprecate this once read fusion is available.
        _block_udf: Optional[Callable[[Block], Block]] = None,
        **reader_args,
    ) -> List[ReadTask]:
        """Creates and returns read tasks for a file-based datasource."""
        _check_pyarrow_version()
        import numpy as np

        read_stream = self._read_stream

        filesystem = _wrap_s3_serialization_workaround(filesystem)

        if open_stream_args is None:
            open_stream_args = {}

        def read_files(
            read_paths: List[str],
            fs: Union["pyarrow.fs.FileSystem", _S3FileSystemWrapper],
        ) -> Iterable[Block]:
            logger.debug(f"Reading {len(read_paths)} files.")
            if isinstance(fs, _S3FileSystemWrapper):
                fs = fs.unwrap()
            ctx = DatasetContext.get_current()
            output_buffer = BlockOutputBuffer(block_udf=_block_udf, target_max_block_size=ctx.target_max_block_size)
            for read_path in read_paths:
                if not is_nan_or_none(read_path):
                    compression = open_stream_args.pop("compression", None)
                    if compression is None:
                        import pyarrow as pa

                        try:
                            # If no compression manually given, try to detect
                            # compression codec from path.
                            compression = pa.Codec.detect(read_path).name
                        except (ValueError, TypeError):
                            # Arrow's compression inference on the file path
                            # doesn't work for Snappy, so we double-check ourselves.
                            import pathlib

                            suffix = pathlib.Path(read_path).suffix
                            if suffix and suffix[1:] == "snappy":
                                compression = "snappy"
                            else:
                                compression = None
                    if compression == "snappy":
                        # Pass Snappy compression as a reader arg, so datasource subclasses
                        # can manually handle streaming decompression in
                        # self._read_stream().
                        reader_args["compression"] = compression
                        reader_args["filesystem"] = fs
                    elif compression is not None:
                        # Non-Snappy compression, pass as open_input_stream() arg so Arrow
                        # can take care of streaming decompression for us.
                        open_stream_args["compression"] = compression

                with self._open_input_source(fs, read_path, **open_stream_args) as f:
                    for data in read_stream(f, read_path, **reader_args):
                        output_buffer.add_block(data)
                        if output_buffer.has_next():
                            yield output_buffer.next()
            output_buffer.finalize()
            if output_buffer.has_next():
                yield output_buffer.next()

        # fix https://github.com/ray-project/ray/issues/24296
        parallelism = min(parallelism, len(paths))

        read_tasks = []
        for raw_paths in np.array_split(paths, parallelism):
            # Paths must be resolved and expanded
            read_paths = []
            file_sizes = []
            for raw_path in raw_paths:
                if is_nan_or_none(raw_path):
                    read_paths.append(raw_path)
                    file_sizes.append(None)  # unknown file size is None
                else:
                    resolved_path, filesystem = _resolve_paths_and_filesystem([raw_path], filesystem)
                    read_path, file_size = meta_provider.expand_paths(resolved_path, filesystem)
                    if partition_filter is not None:
                        read_path = partition_filter(read_path)
                    read_paths.append(read_path[0])
                    file_sizes.append(file_size[0])

            if len(read_paths) <= 0:
                continue

            meta = meta_provider(
                read_paths,
                schema,
                rows_per_file=self._rows_per_file(),
                file_sizes=file_sizes,
            )
            read_task = ReadTask(lambda read_paths=read_paths: read_files(read_paths, filesystem), meta)
            read_tasks.append(read_task)

        return read_tasks

    def _open_input_source(
        self,
        filesystem: "pyarrow.fs.FileSystem",
        path: str,
        **open_args,
    ) -> "pyarrow.NativeFile":
        """Opens a source path for reading and returns the associated Arrow NativeFile.

        The default implementation opens the source path as a sequential input stream.

        Implementations that do not support streaming reads (e.g. that require random
        access) should override this method.
        """
        if is_nan_or_none(path):
            return contextlib.nullcontext()
        return filesystem.open_input_stream(path, **open_args)

    def _read_file(self, f: Union["pyarrow.NativeFile", contextlib.nullcontext], path: str, **reader_args):
        if is_nan_or_none(path):
            if reader_args.get("include_paths", False):
                return [(path, None)]
            return [None]
        return super()._read_file(f, path, **reader_args)
