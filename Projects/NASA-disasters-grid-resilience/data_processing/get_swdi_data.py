#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Oct  1 09:28:03 2025

@author: marchett

SWDI downloader
Output: CVS file

Examples (Mar 31–Apr 4, 2023 inclusive):
  python swdi_download.py --dataset nx3hail --format csv --start 20230331 --end 20230404 -o ./out
  python swdi_download.py --dataset all     --format csv --start 20230331 --end 20230404 -o ./out --subdirs

"""


import argparse
import datetime as dt
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter, Retry

BASE = "https://www.ncei.noaa.gov/swdiws"
ALLOWED_FORMATS = {"csv", "xml", "kmz", "shp"}
COMMON_DATASETS = ["nx3tvs", "nx3meso", "nx3hail", "nx3structure"]


def yyyymmdd(s):
    return dt.datetime.strptime(s, "%Y%m%d").date()

def _session():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=4, backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"])
    )))
    return s

def _ext(fmt):
    return "zip" if fmt == "shp" else fmt

def _date_segment(start, end_inclusive):
    return f"{start.strftime('%Y%m%d')}:{end_inclusive.strftime('%Y%m%d')}"

def _save_response_to_file(resp, outfile):
   
    ctype = resp.headers.get("Content-Type", "").lower()
    if "html" in ctype:
        raise RuntimeError("Server returned HTML (likely an info or error page).")
    with open(outfile, "wb") as f:
        for chunk in resp.iter_content(chunk_size=16384):
            if chunk:
                f.write(chunk)


def _fetch_once(sess, dataset, fmt, start, end_inclusive, outdir, limit, offset):
    
    date_seg = _date_segment(start, end_inclusive)
    url = f"{BASE}/{fmt}/{dataset}/{date_seg}"
    params = {"limit": str(limit)}
    if offset is not None:
        params["offset"] = str(offset)

    outfile = outdir / f"{dataset}_{start.strftime('%Y%m%d')}-{end_inclusive.strftime('%Y%m%d')}.{_ext(fmt)}"
    with sess.get(url, params=params, stream=True, timeout=120) as r:
        r.raise_for_status()
        _save_response_to_file(r, outfile)
    return outfile


def _iter_chunks(start, end_inclusive, max_days):
    
    span_days = (end_inclusive - start).days + 1
    if span_days <= max_days:
        yield start, end_inclusive
        return
    cur = start
    step = dt.timedelta(days=max_days - 1)
    while cur <= end_inclusive:
        nxt = min(cur + step, end_inclusive)
        yield cur, nxt
        cur = nxt + dt.timedelta(days=1)


def download_swdi(datasets, fmt, start, end, outdir, 
                  limit=10000000, offset=None, chunk_days=366, 
                  subdirs=True):
    if start > end:
        raise ValueError("--start must be on or before --end (end is inclusive)")

    sess = _session()

    for ds in datasets:
        target_dir = outdir / ds if subdirs else outdir
        target_dir.mkdir(parents=True, exist_ok=True)
        print(f"[info] {ds}: {start}–{end} (inclusive), format={fmt}")

        any_saved = False
        for cstart, cend in _iter_chunks(start, end, chunk_days):
            try:
                path = _fetch_once(sess, ds, fmt, cstart, cend, target_dir, limit, offset)
                print(f"saved {path}")
                any_saved = True
            except Exception as ex:
                print(f"{ds} {cstart}–{cend}: {ex}")

        if not any_saved:
            print(f"[warn] No files saved for dataset '{ds}' in the requested window.")

    

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Download NCEI SWDI data (inclusive end date, optional chunking).")
    p.add_argument("--dataset", required=True,
                   help="SWDI dataset (e.g., warn, plsr, nx3hail, nx3meso) or 'all'")
    p.add_argument("--format", required=True, dest="fmt", choices=sorted(ALLOWED_FORMATS),
                   help="csv, xml, kmz, or shp (zip)")
    p.add_argument("--start", type=yyyymmdd, required=True, help="YYYYMMDD")
    p.add_argument("--end",   type=yyyymmdd, required=True, help="YYYYMMDD")
    p.add_argument("-o", "--outdir", default=".", help="Output directory")
    p.add_argument("--limit", type=int, default=10000000, help="Row limit")
    p.add_argument("--offset", type=int, default=None, help="Offset for pagination")
    p.add_argument("--chunk-days", type=int, default=366, help="Max days per request")
    p.add_argument("--subdirs", action="store_true", help="Save into per-dataset subfolders")
    args = p.parse_args()

    datasets = COMMON_DATASETS[:] if args.dataset.lower() == "all" else [args.dataset]
    download_swdi(
        datasets=datasets,
        fmt=args.fmt,
        start=args.start,
        end=args.end,
        outdir=Path(args.outdir),
        limit=args.limit,
        offset=args.offset,
        chunk_days=args.chunk_days,
        subdirs=args.subdirs,
    )
    
    
    
    


