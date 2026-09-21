# src/xrgrib/backend.py
import threading

import eccodes
import numpy as np
import xarray as xr
from xarray.backends import BackendArray, BackendEntrypoint
from xarray.core import indexing


def _message_values(path, offset, shape):
    """Decode the values of the single GRIB message stored at a byte offset."""
    with open(path, "rb") as f:
        f.seek(int(offset))
        gid = eccodes.codes_grib_new_from_file(f)
        if gid is None:
            raise IOError(f"No GRIB message at offset {offset} in {path!r}")
        try:
            values = eccodes.codes_get_array(gid, "values").astype(np.float32)
            missing = eccodes.codes_get_double(gid, "missingValue")
        finally:
            eccodes.codes_release(gid)
    return np.where(values == missing, np.nan, values).reshape(shape)


class GribBackendArray(BackendArray):
    """Lazy (time, latitude, longitude) view over the messages of one variable."""

    def __init__(self, path, offsets, shape, lock):
        self.path = path
        self.offsets = offsets  # one byte offset (or None) per time step
        self.shape = shape
        self.dtype = np.dtype("float32")
        self.lock = lock

    def __getitem__(self, key):
        return indexing.explicit_indexing_adapter(
            key, self.shape, indexing.IndexingSupport.BASIC, self._raw_indexing_method
        )

    def _raw_indexing_method(self, key):
        t_key, y_key, x_key = key
        t_indices = np.arange(self.shape[0])[t_key]
        scalar_time = np.ndim(t_indices) == 0
        t_indices = np.atleast_1d(t_indices)
        grid_shape = self.shape[1:]

        slices = []
        with self.lock:
            for t in t_indices:
                offset = self.offsets[int(t)]
                if offset is None:
                    field = np.full(grid_shape, np.nan, dtype=np.float32)
                else:
                    field = _message_values(self.path, offset, grid_shape)
                slices.append(field[y_key, x_key])

        out = np.stack(slices, axis=0)
        return out[0] if scalar_time else out


class XrgribBackend(BackendEntrypoint):
    description = "Minimal lazy GRIB reader built on eccodes"
    open_dataset_parameters = ("filename_or_obj", "drop_variables", "use_long_names")

    def open_dataset(self, filename_or_obj, *, drop_variables=None, use_long_names=False):
        path = str(filename_or_obj)
        drop_variables = set(drop_variables or ())

        index = {}  # var_name -> {validity time: byte offset}
        var_attrs = {}
        lats = lons = Ni = Nj = None

        # Pass 1: header-only scan, so the 7 GB of packed values is never touched.
        with open(path, "rb") as f:
            while True:
                gid = eccodes.codes_grib_new_from_file(f, headers_only=True)
                if gid is None:
                    break
                try:
                    short_name = eccodes.codes_get(gid, "shortName")
                    long_name = eccodes.codes_get(gid, "name")
                    var_name = long_name if use_long_names else short_name
                    if var_name in drop_variables:
                        continue

                    if lats is None:
                        Ni = eccodes.codes_get(gid, "Ni")
                        Nj = eccodes.codes_get(gid, "Nj")
                        lats = eccodes.codes_get_array(gid, "latitudes").reshape(Nj, Ni)[:, 0]
                        lons = eccodes.codes_get_array(gid, "longitudes").reshape(Nj, Ni)[0, :]

                    date = eccodes.codes_get(gid, "validityDate")
                    time = eccodes.codes_get(gid, "validityTime")
                    stamp = np.datetime64(
                        f"{date // 10000:04d}-{date // 100 % 100:02d}-{date % 100:02d}"
                        f"T{time // 100:02d}:{time % 100:02d}:00",
                        "ns",  # must match the dtype of the time coord used for lookup
                    )

                    index.setdefault(var_name, {})[stamp] = int(eccodes.codes_get(gid, "offset"))
                    var_attrs.setdefault(
                        var_name,
                        {
                            "long_name": long_name,
                            "units": eccodes.codes_get(gid, "units"),
                            "short_name": short_name,
                        },
                    )
                finally:
                    eccodes.codes_release(gid)

        if not index:
            raise ValueError(f"No GRIB messages decoded from {path!r}")

        times = np.array(sorted({t for v in index.values() for t in v}), dtype="datetime64[ns]")
        shape = (len(times), Nj, Ni)
        lock = threading.Lock()

        data_vars = {}
        for var_name, by_time in index.items():
            offsets = [by_time.get(t) for t in times]
            data_vars[var_name] = xr.Variable(
                ("time", "latitude", "longitude"),
                indexing.LazilyIndexedArray(GribBackendArray(path, offsets, shape, lock)),
                var_attrs[var_name],
                encoding={"preferred_chunks": {"time": 1}},
            )

        ds = xr.Dataset(
            data_vars,
            coords={"time": times, "latitude": lats, "longitude": lons},
        )
        ds.set_close(lambda: None)
        return ds

    def guess_can_open(self, filename_or_obj):
        try:
            return str(filename_or_obj).endswith((".grib", ".grib2", ".grb", ".grb2"))
        except TypeError:
            return False
