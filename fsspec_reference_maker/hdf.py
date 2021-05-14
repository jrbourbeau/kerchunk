import base64
from collections import Counter
import zipfile
from typing import Union, BinaryIO
import logging
import os
import json
import numpy as np
import h5py
import zarr
from zarr.meta import encode_fill_value
from numcodecs import Zlib
import fsspec
import fsspec.utils
import fsspec.core

lggr = logging.getLogger('h5-to-zarr')


class SingleHdf5ToZarr:
    """Translate the content of one HDF5 file into Zarr metadata.

    HDF5 groups become Zarr groups. HDF5 datasets become Zarr arrays. Zarr array
    chunks remain in the HDF5 file.

    Parameters
    ----------
    h5f : file-like
        Input HDF5 file as a binary Python file-like object (duck-typed, adhering
        to BinaryIO is optional)
    url : str
        URI of the HDF5 file.
    xarray : bool, optional
        Produce attributes required by the `xarray <http://xarray.pydata.org>`_
        package to correctly identify dimensions (HDF5 dimension scales) of a
        Zarr array. Default is ``False``.
    spec : int
        The version of output to produce (see README of this repo)
    inline_threshold : int
        Include chunks smaller than this value directly in the output. Zero or negative
        to disable
    """

    def __init__(self, h5f: BinaryIO, url: str,
                 xarray: bool = False, spec=1, inline_threshold=0):
        # Open HDF5 file in read mode...
        lggr.debug(f'HDF5 file: {h5f}')
        self.input_file = h5f
        lggr.debug(f'xarray: {xarray}')
        self.spec = spec
        self.inline = inline_threshold
        self._h5f = h5py.File(h5f, mode='r')
        self._xr = xarray

        self.store = {}
        self._zroot = zarr.group(store=self.store, overwrite=True)

        self._uri = url
        lggr.debug(f'HDF5 file URI: {self._uri}')

    def translate(self):
        """Translate content of one HDF5 file into Zarr storage format.

        This method is the main entry point to execute the workflow, and
        returns a "reference" structure to be used with zarr/fsspec-reference-maker

        No data is copied out of the HDF5 file.

        :returns
        dict with references
        """
        lggr.debug('Translation begins')
        self._transfer_attrs(self._h5f, self._zroot)
        self._h5f.visititems(self._translator)
        if self.inline > 0:
            self._do_inline(self.inline)
        if self.spec < 1:
            return self.store
        else:
            for k, v in self.store.copy().items():
                if isinstance(v, list):
                    self.store[k][0] = "{{u}}"
                else:
                    self.store[k] = v.decode()
            return {
                "version": 1,
                "templates": {
                    "u": self._uri
                },
                "refs": self.store
            }

    def _do_inline(self, threshold):
        """Replace short chunks with the value of that chunk

        The chunk may need encoding with base64 if not ascii, so actual
        length may be larger than threshold.
        """
        for k, v in self.store.copy().items():
            if isinstance(v, list) and v[2] < threshold:
                self.input_file.seek(v[1])
                data = self.input_file.read(v[2])
                try:
                    # easiest way to test if data is ascii
                    data.decode('ascii')
                except UnicodeDecodeError:
                    data = b"base64:" + base64.b64encode(data)
                self.store[k] = data

    def _transfer_attrs(self, h5obj: Union[h5py.Dataset, h5py.Group],
                        zobj: Union[zarr.Array, zarr.Group]):
        """Transfer attributes from an HDF5 object to its equivalent Zarr object.

        Parameters
        ----------
        h5obj : h5py.Group or h5py.Dataset
            An HDF5 group or dataset.
        zobj : zarr.hierarchy.Group or zarr.core.Array
            An equivalent Zarr group or array to the HDF5 group or dataset with
            attributes.
        """
        for n, v in h5obj.attrs.items():
            if n in ('REFERENCE_LIST', 'DIMENSION_LIST'):
                continue

            # Fix some attribute values to avoid JSON encoding exceptions...
            if isinstance(v, bytes):
                v = v.decode('utf-8')
            elif isinstance(v, (np.ndarray, np.number)):
                if v.dtype.kind == 'S':
                    v = v.astype(str)
                if n == '_FillValue':
                    v = encode_fill_value(v, v.dtype)
                elif v.size == 1:
                    v = v.flatten()[0]
                    if isinstance(v, (np.ndarray, np.number)):
                        v = v.tolist()
                else:
                    v = v.tolist()
            if self._xr and v == 'DIMENSION_SCALE':
                continue
            try:
                zobj.attrs[n] = v
            except TypeError:
                lggr.exception(
                    f'Caught TypeError: {n}@{h5obj.name} = {v} ({type(v)})')

    def _translator(self, name: str, h5obj: Union[h5py.Dataset, h5py.Group]):
        """Produce Zarr metadata for all groups and datasets in the HDF5 file.
        """
        refs = {}
        if isinstance(h5obj, h5py.Dataset):
            lggr.debug(f'HDF5 dataset: {h5obj.name}')
            if h5obj.id.get_create_plist().get_layout() == h5py.h5d.COMPACT:
                RuntimeError(
                    f'Compact HDF5 datasets not yet supported: <{h5obj.name} '
                    f'{h5obj.shape} {h5obj.dtype} {h5obj.nbytes} bytes>')
                return

            if (h5obj.scaleoffset or h5obj.fletcher32 or h5obj.shuffle or
                    h5obj.compression in ('szip', 'lzf')):
                raise RuntimeError(
                    f'{h5obj.name} uses unsupported HDF5 filters')
            if h5obj.compression == 'gzip':
                compression = Zlib(level=h5obj.compression_opts)
            else:
                compression = None

            # Get storage info of this HDF5 dataset...
            cinfo = self._storage_info(h5obj)
            if self._xr and h5py.h5ds.is_scale(h5obj.id) and not cinfo:
                return

            # Create a Zarr array equivalent to this HDF5 dataset...
            za = self._zroot.create_dataset(h5obj.name, shape=h5obj.shape,
                                            dtype=h5obj.dtype,
                                            chunks=h5obj.chunks or False,
                                            fill_value=h5obj.fillvalue,
                                            compression=compression,
                                            overwrite=True)
            lggr.debug(f'Created Zarr array: {za}')
            self._transfer_attrs(h5obj, za)

            if self._xr:
                # Do this for xarray...
                adims = self._get_array_dims(h5obj)
                za.attrs['_ARRAY_DIMENSIONS'] = adims
                lggr.debug(f'_ARRAY_DIMENSIONS = {adims}')

            # Store chunk location metadata...
            if cinfo:
                for k, v in cinfo.items():
                    self.store[za._chunk_key(k)] = [self._uri, v['offset'], v['size']]

        elif isinstance(h5obj, h5py.Group):
            lggr.debug(f'HDF5 group: {h5obj.name}')
            zgrp = self._zroot.create_group(h5obj.name)
            self._transfer_attrs(h5obj, zgrp)

    def _get_array_dims(self, dset):
        """Get a list of dimension scale names attached to input HDF5 dataset.

        This is required by the xarray package to work with Zarr arrays. Only
        one dimension scale per dataset dimension is allowed. If dataset is
        dimension scale, it will be considered as the dimension to itself.

        Parameters
        ----------
        dset : h5py.Dataset
            HDF5 dataset.

        Returns
        -------
        list
            List with HDF5 path names of dimension scales attached to input
            dataset.
        """
        dims = list()
        rank = len(dset.shape)
        if rank:
            for n in range(rank):
                num_scales = len(dset.dims[n])
                if num_scales == 1:
                    dims.append(dset.dims[n][0].name[1:])
                elif h5py.h5ds.is_scale(dset.id):
                    dims.append(dset.name[1:])
                elif num_scales > 1:
                    raise RuntimeError(
                        f'{dset.name}: {len(dset.dims[n])} '
                        f'dimension scales attached to dimension #{n}')
        return dims

    def _storage_info(self, dset: h5py.Dataset) -> dict:
        """Get storage information of an HDF5 dataset in the HDF5 file.

        Storage information consists of file offset and size (length) for every
        chunk of the HDF5 dataset.

        Parameters
        ----------
        dset : h5py.Dataset
            HDF5 dataset for which to collect storage information.

        Returns
        -------
        dict
            HDF5 dataset storage information. Dict keys are chunk array offsets
            as tuples. Dict values are pairs with chunk file offset and size
            integers.
        """
        # Empty (null) dataset...
        if dset.shape is None:
            return dict()

        dsid = dset.id
        if dset.chunks is None:
            # Contiguous dataset...
            if dsid.get_offset() is None:
                # No data ever written...
                return dict()
            else:
                key = (0,) * (len(dset.shape) or 1)
                return {key: {'offset': dsid.get_offset(),
                              'size': dsid.get_storage_size()}}
        else:
            # Chunked dataset...
            num_chunks = dsid.get_num_chunks()
            if num_chunks == 0:
                # No data ever written...
                return dict()

            # Go over all the dataset chunks...
            stinfo = dict()
            chunk_size = dset.chunks
            for index in range(num_chunks):
                blob = dsid.get_chunk_info(index)
                key = tuple(
                    [a // b for a, b in zip(blob.chunk_offset, chunk_size)])
                stinfo[key] = {'offset': blob.byte_offset, 'size': blob.size}
            return stinfo


class MultiZarrToZarr:

    def __init__(self, path, remote_protocol,
                 remote_options=None, xarray_kwargs=None, storage_options=None):
        """

        :param path: a URL containing multiple JSONs
        :param xarray_kwargs:
        :param storage_options:
        """
        xarray_kwargs = xarray_kwargs or {}
        self.path = path
        self.xr_kwargs = xarray_kwargs
        self.storage_options = storage_options or {}
        self.remote_protocol = remote_protocol
        self.remote_options = remote_options or {}

    def translate(self, outpath):
        ds, ds0, fss = self._determine_dims()
        out = self._build_output(ds, ds0, fss)
        self.output = self._consolidate(out)
        self._write(self.output, outpath)

    @staticmethod
    def _write(refs, outpath, filetype=None):
        types = {
            "json": "json",
            "parquet": "parquet",
            "zarr": "zarr"
        }
        if filetype is None:
            ext = os.path.splitext(outpath)[1].lstrip(".")
            filetype = types[ext]
        elif filetype not in types:
            raise KeyError
        if filetype == "json":
            with open(outpath, "w") as f:
                json.dump(refs, f)
            return
        import pandas as pd
        references2 = {
            k: {"data": v.encode('ascii') if not isinstance(v, list) else None,
                "url": v[0] if isinstance(v, list) else None,
                "offset": v[1] if isinstance(v, list) else None,
                "size": v[2] if isinstance(v, list) else None}
            for k, v in refs['refs'].items()}
        # use pandas for sorting
        df = pd.DataFrame(references2.values(), index=list(references2)).sort_values("offset")

        if filetype == "zarr":
            import zarr
            import numcodecs
            # compression should be NONE, if intent is to store in single zip
            g = zarr.open_group(outpath, mode='w')
            g.attrs.update({k: v for k, v in refs.items() if k in ['version', "templates", "gen"]})
            g.array(name="key", data=df.index.values, dtype="object", compression="zstd",
                    object_codec=numcodecs.VLenUTF8())
            g.array(name="offset", data=df.offset.values, dtype="uint32", compression="zstd")
            g.array(name="size", data=df['size'].values, dtype="uint32", compression="zstd")
            g.array(name="data", data=df.url.values, dtype="object",
                    object_codec=numcodecs.VLenUTF8(), compression="gzip")
            # may be better as fixed length
            g.array(name="url", data=df.url.values, dtype="object",
                    object_codec=numcodecs.VLenUTF8(), compression='gzip')
        if filetype == "parquet":
            import fastparquet
            metadata = json.dumps(
                {k: v for k, v in refs.items() if k in ['version', "templates", "gen"]}
            )
            fastparquet.write(
                outpath,
                custom_metadata=metadata,
                compression="ZSTD"
            )

    def _consolidate(self, mapping, inline_threashold=100, template_count=5):
        import string
        counts = Counter(v[0] for v in mapping.values() if isinstance(v, list))
        # potential IndexError when more than 52 templates
        templates = {f"{string.ascii_letters[i]}": u for i, (u, v) in enumerate(counts.items())
                     if v > template_count}
        inv = {v: k for k, v in templates.items()}

        out = {}
        for k, v in mapping.items():
            if isinstance(v, list) and v[2] < inline_threashold:
                v = self.fs.cat_file(v[0], v[1], v[1] + v[2])
            if isinstance(v, bytes):
                try:
                    # easiest way to test if data is ascii
                    out[k] = v.decode('ascii')
                except UnicodeDecodeError:
                    out[k] = (b"base64:" + base64.b64encode(v)).decode()
            else:
                if v[0] in inv:
                    out[k] = ["{{" + inv[v[0]] + "}}"] + v[1:]
                else:
                    out[k] = v
        return {"version": 1, "templates": templates, "refs": out}

    def _build_output(self, ds, ds0, fss):
        import zarr
        out = {}
        ds.to_zarr(out, chunk_store={}, compute=False)  # fills in metadata&coords
        z = zarr.open_group(out, mode='a')
        for dim in self.extra_dims.union(self.concat_dims):
            # derived and concatenated dims stored as absolute data
            z[dim][:] = ds[dim].values
        for dim in self.same_dims:
            # duplicated coordinates stored as references just once
            out.update({k: v for k, v in fss[0].references.items() if k.startswith(dim)})
        for variable in ds.variables:
            if variable in ds.dims:
                # already handled
                continue
            var, var0 = ds[variable], ds0[variable]
            assert var.dims[-len(var0.dims):] == var0.dims

            concats = {d: 0 for d in self.concat_dims}
            for i, fs in enumerate(fss):
                for k, v in fs.references.items():
                    start, part = os.path.split(k)
                    if start != variable or part in ['.zgroup', '.zarray', '.zattrs']:
                        # OK, so we go through all the keys multiple times
                        continue
                    if var.shape == var0.shape:
                        out[k] = v  # copy
                    else:
                        out[f"{start}/{i}.{part}"] = v
        return out

    def _determine_dims(self):
        import xarray as xr
        with fsspec.open_files(self.path, **self.storage_options) as ofs:
            fss = [
                fsspec.filesystem(
                    "reference", fo=json.load(of),
                    remote_protocol=self.remote_protocol,
                    remote_options=self.remote_options
                ) for of in ofs
            ]
            self.fs = fss[0].fs
            mappers = [fs.get_mapper("") for fs in fss]

        ds = xr.open_mfdataset(mappers, engine="zarr", chunks={}, **self.xr_kwargs)
        ds0 = xr.open_mfdataset(mappers[:1], engine="zarr", chunks={}, **self.xr_kwargs)
        self.extra_dims = set(ds.dims) - set(ds0.dims)
        self.concat_dims = set(k for k, v in ds.dims.items() if v / ds0.dims[k] == len(mappers))
        self.same_dims = set(ds.dims) - self.extra_dims - self.concat_dims
        return ds, ds0, fss


def example_single():
    """Scans the given file and returns a dict of references"""
    url = 's3://pangeo-data-uswest2/esip/adcirc/adcirc_01d.nc'
    so = dict(
        mode='rb', anon=False, requester_pays=True,
        default_fill_cache=False, default_cache_type='none'
    )
    fsspec.utils.setup_logging(logger=lggr)
    with fsspec.open(url, **so) as f:
        h5chunks = SingleHdf5ToZarr(f, url, xarray=True)
        return h5chunks.translate()


def example_multiple():
    """Scans the set of URLs and writes a reference JSON file

    In this prototype, the outputs are wrapped in a single ZIP archive
    "out.zip".
    """
    urls = ["s3://" + p for p in [
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010000.CHRTOUT_DOMAIN1.comp',
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010100.CHRTOUT_DOMAIN1.comp',
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010200.CHRTOUT_DOMAIN1.comp',
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010300.CHRTOUT_DOMAIN1.comp',
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010400.CHRTOUT_DOMAIN1.comp',
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010500.CHRTOUT_DOMAIN1.comp',
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010600.CHRTOUT_DOMAIN1.comp',
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010700.CHRTOUT_DOMAIN1.comp',
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010800.CHRTOUT_DOMAIN1.comp',
        'noaa-nwm-retro-v2.0-pds/full_physics/2017/201704010900.CHRTOUT_DOMAIN1.comp'
    ]]
    so = dict(
        anon=True, default_fill_cache=False, default_cache_type='first'
    )
    zf = zipfile.ZipFile("out.zip", mode="w")
    for u in urls:
        with fsspec.open(u, **so) as inf:
            h5chunks = SingleHdf5ToZarr(inf, u, xarray=True, inline_threshold=100)
            with zf.open(os.path.basename(u) + ".json", 'w') as outf:
                outf.write(json.dumps(h5chunks.translate()).encode())


def example_ensamble():
    """Scan the set of URLs and create a single reference output

    This example uses the output of example_multiple
    """
    def drop_coords(ds):
        ds = ds.drop(['reference_time', 'crs'])
        return ds.reset_coords(drop=True)

    mzz = MultiZarrToZarr(
        "zip://*.json::out.zip",
        remote_protocol="s3",
        remote_options={'anon': True},
        xarray_kwargs={
            "preprocess": drop_coords,
            "decode_cf": False,
            "mask_and_scale": False,
            "decode_times": False,
            "decode_timedelta": False,
            "use_cftime": False,
            "decode_coords": False
        },
    )
    mzz.translate("output.zarr")

