"""Retrieve SuperMAG magnetometer data for Missouri station R01 on selected dates."""

import pandas as pd

if __package__:
    from .supermag_data import DEFAULT_FLAGS, SUPERMAG_CACHE_DIR, fetch_supermag_station
else:
    from supermag_data import DEFAULT_FLAGS, SUPERMAG_CACHE_DIR, fetch_supermag_station

STATION = "R01"

# Each entry is a UTC day, [YYYY, MM, DD, hh, mm]
DEFAULT_DATES = [
    [2024, 7, 8, 0, 0],
    [2025, 5, 1, 0, 0],
    [2025, 5, 8, 0, 0],
    [2025, 5, 15, 0, 0],
    [2025, 5, 22, 0, 0],
    [2025, 5, 29, 0, 0],
]


def get_r01_data(userid=None, dates=None, station=STATION, extent=86400,
                 flags=DEFAULT_FLAGS, *, cache_dir=SUPERMAG_CACHE_DIR,
                 allow_download=False, api_client=None):
    """Fetch 1-minute SuperMAG data for `station` (default R01) for each date.

    Parameters
    ----------
    userid : str
        Your SuperMAG user id.
    dates : list of [YYYY, MM, DD, hh, mm], optional
        Event start times (UTC). Defaults to July 8, 2024 and
        May 1, 8, 15, 22, 29, 2025.
    station : str
        IAGA station code (default "R01").
    extent : int
        Length of each request in seconds (86400 = one full day).
    flags : str
        Explicit processing flags (default "all,baseline=yearly").
    cache_dir : path-like
        Project SuperMAG_data root; requests are stored under api_cache/.
    allow_download : bool
        False by default: reuse cache only. Set True to request missing days.

    Returns
    -------
    combined : pandas.DataFrame
        All days concatenated, with a 'date' column (UTC datetime of tval)
        and a 'request_start' column identifying which request each row came from.
    failures : dict
        {request_start: error message} for any days that failed.
    """
    dates = DEFAULT_DATES if dates is None else dates
    frames, failures = [], {}

    for start in dates:
        label = "%04d-%02d-%02d" % tuple(start[:3])
        try:
            df = fetch_supermag_station(
                station, start, extent, flags=flags, userid=userid,
                cache_dir=cache_dir, allow_download=allow_download,
                api_client=api_client,
            ).reset_index()
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            # Do not retain service responses/URLs, which can expose access IDs.
            failures[label] = type(exc).__name__
            continue

        df["date"] = df["timestamp"]
        df["request_start"] = label
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return combined, failures


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true',
                        help='Opt in to missing-day requests; reads SUPERMAG_USERID from local environment.')
    parser.add_argument('--station', default=STATION)
    args = parser.parse_args()
    data, failed = get_r01_data(station=args.station, allow_download=args.download)
    if not data.empty:
        print(data.groupby("request_start").size())
    if failed:
        print("Failed requests:", failed)