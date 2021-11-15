"""``H5pyDataSet`` loads/saves data from/to a hdf file using an underlying
filesystem (e.g.: local, S3, GCS). It uses h5py.File to handle the hdf file.
"""
from copy import deepcopy
from pathlib import PurePosixPath
from threading import Lock
from typing import Any, Dict
import tempfile
from io import BytesIO

import fsspec
import h5py

from kedro.io.core import (
    AbstractVersionedDataSet,
    DataSetError,
    Version,
    get_filepath_str,
    get_protocol_and_path,
)

class H5pyDataSet(AbstractVersionedDataSet):
    """``H5pyDataSet`` loads/saves data from/to a hdf file using an underlying
    filesystem (e.g. local, S3, GCS). It uses h5py.File to handle the hdf file.

    Example adding a catalog entry with
    `YAML API <https://kedro.readthedocs.io/en/stable/05_data/\
        01_data_catalog.html#using-the-data-catalog-with-the-yaml-api>`_:

    .. code-block:: yaml

        >>> hdf_dataset:
        >>>   type: hdf5.H5pyDataSet
        >>>   filepath: s3://my_bucket/raw/sensor_reading.h5
        >>>   credentials: aws_s3_creds

    Example using Python API:
    ::

        >>> from kedro.extras.datasets.hdf5 import H5pyDataSet
        >>> import pandas as pd
        >>>
        >>> data = pd.DataFrame({'col1': [1, 2], 'col2': [4, 5],
        >>>                      'col3': [5, 6]})
        >>>
        >>> # data_set = H5pyDataSet(filepath="gcs://bucket/test.hdf")
        >>> data_set = H5pyDataSet(filepath="test.h5")
        >>> data_set.save(data)
        >>> reloaded = data_set.load()
        >>> assert data.equals(reloaded)

    """

    # _lock is a class attribute that will be shared across all the instances.
    # It is used to make dataset safe for threads.
    _lock = Lock()
    DEFAULT_LOAD_ARGS = {}  # type: Dict[str, Any]
    DEFAULT_SAVE_ARGS = {}  # type: Dict[str, Any]

    # pylint: disable=protected-access
    @staticmethod
    def __h5py_from_binary(binary, load_args):
        file_access_property_list = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
        file_access_property_list.set_fapl_core(backing_store=False)
        file_access_property_list.set_file_image(binary)

        file_id_args = {
            'fapl': file_access_property_list,
            'flags': h5py.h5f.ACC_RDONLY,
            'name': next(tempfile._get_candidate_names()).encode(),
        }
        h5_file_args = {
            'backing_store': False,
            'driver': 'core',
            'mode': 'r'
        }

        file_id = h5py.h5f.open(**file_id_args)
        return h5py.File(file_id, **h5_file_args, **load_args)

    @staticmethod
    def __h5py_to_binary(h5f, save_args):
        bio = BytesIO()
        with h5py.File(bio, 'w', **save_args) as biof:
            for _, value in h5f.items():
                h5f.copy(value, biof,
                        expand_soft=True,
                        expand_external=True,
                        expand_refs=True)
            biof.close()
            return bio.getvalue()


    # pylint: disable=too-many-arguments
    def __init__(
        self,
        filepath: str,
        load_args: Dict[str, Any] = None,
        save_args: Dict[str, Any] = None,
        version: Version = None,
        credentials: Dict[str, Any] = None,
        fs_args: Dict[str, Any] = None,
    ) -> None:
        """Creates a new instance of ``H5pyDataSet`` pointing to a concrete hdf file
        on a specific filesystem.

        Args:
            filepath: Filepath in POSIX format to a hdf file prefixed with a protocol like `s3://`.
                If prefix is not provided, `file` protocol (local filesystem) will be used.
                The prefix should be any protocol supported by ``fsspec``.
                Note: `http(s)` doesn't support versioning.
            load_args: h5py options for loading hdf files.
                You can find all available arguments at:
                https://docs.h5py.org/en/stable/high/file.html#h5py.File
                All defaults are preserved.
            save_args: h5py options for saving hdf files.
                You can find all available arguments at:
                https://docs.h5py.org/en/stable/high/file.html#h5py.File
                All defaults are preserved.
            version: If specified, should be an instance of
                ``kedro.io.core.Version``. If its ``load`` attribute is
                None, the latest version will be loaded. If its ``save``
                attribute is None, save version will be autogenerated.
            credentials: Credentials required to get access to the underlying filesystem.
                E.g. for ``GCSFileSystem`` it should look like `{"token": None}`.
            fs_args: Extra arguments to pass into underlying filesystem class constructor
                (e.g. `{"project": "my-project"}` for ``GCSFileSystem``), as well as
                to pass to the filesystem's `open` method through nested keys
                `open_args_load` and `open_args_save`.
                Here you can find all available arguments for `open`:
                https://filesystem-spec.readthedocs.io/en/latest/api.html#fsspec.spec.AbstractFileSystem.open
                All defaults are preserved, except `mode`, which is set `wb` when saving.
        """
        _fs_args = deepcopy(fs_args) or {}
        _fs_open_args_load = _fs_args.pop("open_args_load", {})
        _fs_open_args_save = _fs_args.pop("open_args_save", {})
        _credentials = deepcopy(credentials) or {}

        protocol, path = get_protocol_and_path(filepath, version)
        if protocol == "file":
            _fs_args.setdefault("auto_mkdir", True)

        self._protocol = protocol
        self._fs = fsspec.filesystem(self._protocol, **_credentials, **_fs_args)

        super().__init__(
            filepath=PurePosixPath(path),
            version=version,
            exists_function=self._fs.exists,
            glob_function=self._fs.glob,
        )

        # Handle default load and save arguments
        self._load_args = deepcopy(self.DEFAULT_LOAD_ARGS)
        if load_args is not None:
            self._load_args.update(load_args)
        self._save_args = deepcopy(self.DEFAULT_SAVE_ARGS)
        if save_args is not None:
            self._save_args.update(save_args)

        _fs_open_args_save.setdefault("mode", "wb")
        self._fs_open_args_load = _fs_open_args_load
        self._fs_open_args_save = _fs_open_args_save

    def _describe(self) -> Dict[str, Any]:
        return dict(
            filepath=self._filepath,
            protocol=self._protocol,
            load_args=self._load_args,
            save_args=self._save_args,
            version=self._version,
        )

    def _load(self) -> h5py.File:
        load_path = get_filepath_str(self._get_load_path(), self._protocol)

        with self._fs.open(load_path, **self._fs_open_args_load) as fs_file:
            binary_data = fs_file.read()

        with H5pyDataSet._lock:
            return H5pyDataSet.__h5py_from_binary(binary_data, self._load_args)

    def _save(self, data: h5py.File) -> None:
        save_path = get_filepath_str(self._get_save_path(), self._protocol)

        with H5pyDataSet._lock:
            binary_data = H5pyDataSet.__h5py_to_binary(data, self._save_args)

        with self._fs.open(save_path, **self._fs_open_args_save) as fs_file:
            fs_file.write(binary_data)

        self._invalidate_cache()

    def _exists(self) -> bool:
        try:
            load_path = get_filepath_str(self._get_load_path(), self._protocol)
        except DataSetError:
            return False

        return self._fs.exists(load_path)

    def _release(self) -> None:
        super()._release()
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        """Invalidate underlying filesystem caches."""
        filepath = get_filepath_str(self._filepath, self._protocol)
        self._fs.invalidate_cache(filepath)
