"""HRRR winter data access through Herbie (2026.9.x API).

The winter plotter consumes ONE concatenated GRIB2 file for a cycle / FHR.
This module uses Herbie's *index based subset downloads* for two products:
    - product='prs' : 1000, 925, 850, 700, 500 mb T, U, V, HGT
    - product='sfc' : 2-m T/Td/RH, 10-m winds, MSLP, reflectivity,
                      precipitation type, QPF, snow, gust, surface pressure.
The downloaded GRIB messages are concatenated without changing their GRIB2
headers, so the existing ecCodes field decoder / wind rotation stays intact.

Availability is probed for BOTH model products. Herbie handles the choice of
AWS / Google / NOMADS sources and the message byte ranges. No direct NOAA
HTTP Range code is used by this HRRR downloader.

Install on the workflow runner:
    pip install 'herbie-data==2026.9.2'

This file is deliberately independent of scripts/winter_data_fetch.py, which
may still be used by the separate experimental/operational RRFS pipeline.
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import tempfile

MODEL = 'hrrr'
PRODUCTS = ('prs', 'sfc')
# The fields the full hrrr_winter.py plotter actually uses. Search matches the
# 'search_this' text in Herbie's NCEP/wgrib2 index, NOT ecCodes short names.
PRESSURE_SEARCH = (
    r':(?:TMP|UGRD|VGRD|HGT):(?:1000|925|850|700|500) mb:'
)
SURFACE_SEARCH = (
    r':(?:TMP|DPT|RH):2 m above ground:'
    r'|:(?:UGRD|VGRD):10 m above ground:'
    r'|:(?:PRMSL|MSLMA|MSLET):(?:mean sea level|MSL):'
    r'|:(?:PRES|GUST|CRAIN|CSNOW|CICEP|CFRZR|APCP|ASNOW|WEASD):(?:surface|10 m above ground):'
    r'|:REFC:'
    r'|:REFD:(?:1000 m above ground|1 km above ground):'
)
# Allow site owner to constrain Herbie's fallback sources if desired.
DEFAULT_PRIORITY = ('aws', 'google', 'nomads')


def cycle_date(cycle: str, fhr: int) -> datetime:
    if not re.fullmatch(r'\d{10}', cycle):
        raise ValueError('HRRR cycle must be YYYYMMDDHH')
    dt = datetime.strptime(cycle, '%Y%m%d%H')
    horizon = 48 if dt.hour in (0, 6, 12, 18) else 18
    if not (0 <= fhr <= horizon):
        raise ValueError(f'HRRR {cycle} supports F000–F{horizon:03d}, not F{fhr:03d}')
    return dt


def _make_herbie(cycle: str, fhr: int, product: str, save_dir: Path | None = None):
    # Import lazily, so the selection logic and CLI help work in a local test
    # environment without optional Herbie and cfgrib binaries installed.
    from herbie import Herbie

    priority = tuple(x.strip().lower() for x in
                     os.getenv('WINTER_HERBIE_PRIORITY', ','.join(DEFAULT_PRIORITY)).split(',')
                     if x.strip())
    if not priority:
        raise ValueError('WINTER_HERBIE_PRIORITY did not specify any sources')
    kwargs = dict(model=MODEL, product=product, fxx=int(fhr),
                  priority=list(priority), verbose=False)
    if save_dir is not None:
        kwargs['save_dir'] = str(save_dir)
    return Herbie(cycle_date(cycle, fhr), **kwargs)


def _ready(H) -> bool:
    # Require both GRIB and index to be found by Herbie. A cycle timestamp
    # alone is not proof that its GRIB files are available.
    return bool(H) and bool(getattr(H, 'grib', None)) and bool(getattr(H, 'idx', None))


def is_available(model: str, cycle: str, fhr: int) -> bool:
    """Probe for matching HRRR prs+sfc GRIB and .idx files via Herbie."""
    if model.lower() != MODEL:
        raise ValueError('This module retrieves HRRR only')
    try:
        cycle_date(cycle, fhr)
        return all(_ready(_make_herbie(cycle, fhr, product))
                   for product in PRODUCTS)
    except Exception as exc:
        print(f'Herbie availability: {cycle} F{fhr:03d} not ready: {exc}', flush=True)
        return False


def _download_product(cycle: str, fhr: int, product: str,
                      cache_dir: Path) -> Path:
    H = _make_herbie(cycle, fhr, product, cache_dir)
    if not _ready(H):
        raise RuntimeError(f'Herbie has not located both GRIB and idx: {cycle} F{fhr:03d} {product}')
    search = PRESSURE_SEARCH if product == 'prs' else SURFACE_SEARCH
    # This is a real regex on the NCEP index; show a preview of matches in logs
    # and abort if a product has no matching messages.
    rows = H.inventory(search=search, verbose=False)
    if rows is None or len(rows) == 0:
        raise RuntimeError(f'Herbie index matched no {product} variables for {cycle} F{fhr:03d}')
    if product == 'prs':
        # Fail visibly rather than generate deceptively incomplete upper-air
        # maps when a feed or index layout changes.
        matches = rows['search_this'].astype(str).tolist()
        for var in ('TMP', 'UGRD', 'VGRD', 'HGT'):
            if not any(re.search(rf':{var}:(?:925|850|700|500) mb:', m) for m in matches):
                raise RuntimeError(f'HRRR prs F{fhr:03d} lacks {var} pressure-level messages')
    print(f'Herbie {cycle} F{fhr:03d} {product}: '
          f'{len(rows)} selected GRIB messages from {getattr(H, "grib_source", "source")}', flush=True)
    path = H.download(search=search, save_dir=cache_dir,
                      overwrite=True, verbose=False, errors='raise')
    if path is None or not Path(path).is_file() or Path(path).stat().st_size < 1000:
        raise RuntimeError(f'Herbie {product} subset download returned no usable GRIB: {path}')
    return Path(path)


def download_selected_grib(model: str, cycle: str, fhr: int,
                           destination: Path) -> Path:
    """Return the same combined GRIB2 file that hrrr_winter.py expects.

    Downloaded subsets are joined in prs+sfc order. A partially downloaded
    or partially concatenated file is NEVER passed to the plotter.
    """
    if model.lower() != MODEL:
        raise ValueError('This module retrieves HRRR only')
    cycle_date(cycle, fhr)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Scratch is intentionally per-FHR, removed even on errors / cancellations.
    with tempfile.TemporaryDirectory(prefix='hrrr_herbie_',
                                     dir=destination.parent) as scratch:
        cache = Path(scratch)
        parts = [_download_product(cycle, fhr, product, cache)
                 for product in PRODUCTS]
        tmp = cache / 'combined.partial.grib2'
        with tmp.open('wb') as output:
            for part in parts:
                with part.open('rb') as source:
                    shutil.copyfileobj(source, output, length=8 * 1024 * 1024)
        if tmp.stat().st_size < 2000:
            raise RuntimeError('Combined Herbie GRIB2 unexpectedly small')
        with tmp.open('rb') as sample:
            if sample.read(4) != b'GRIB':
                raise RuntimeError('Herbie output is not a GRIB2 message stream')
        tmp.replace(destination)
    print(f'HRRR Herbie combined file ready: {destination} '
          f'({destination.stat().st_size / 2**20:.1f} MiB)', flush=True)
    return destination


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='Download one HRRR FHR with Herbie')
    p.add_argument('--cycle', required=True, help='YYYYMMDDHH')
    p.add_argument('--forecast-hour', type=int, default=6)
    p.add_argument('--out', type=Path, default=Path('hrrr_selected.grib2'))
    p.add_argument('--probe', action='store_true', help='Check for prs and sfc without downloading')
    args = p.parse_args()
    if args.probe:
        status = is_available('hrrr', args.cycle, args.forecast_hour)
        print(f'HRRR F{args.forecast_hour:03d} ready: {status}')
        raise SystemExit(0 if status else 2)
    download_selected_grib('hrrr', args.cycle, args.forecast_hour, args.out)
