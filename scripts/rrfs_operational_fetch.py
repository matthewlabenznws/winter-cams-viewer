"""Download selected NCEP operational RRFS deterministic CONUS GRIB2 messages with Herbie.

NOAA public bucket (no credentials): s3://noaa-rrfs-ops-pds/
  rrfs.YYYYMMDD/CC/rrfs.tCCz.{prslev|2dfld}.3km.fFFF.conus.grib2
  ...grib2.idx

Use Herbie indexed selective downloads for ONLY fields needed by winter plots.
Do not use GSL MPAS FTP or the retired prototype RRFS S3 bucket.

Usage:
  python scripts/rrfs_operational_fetch.py --cycle 2026110412 --fhr 6 --inventory
  python scripts/rrfs_operational_fetch.py --cycle 2026110412 --fhr 6 --output rrfs.grib2
"""
from __future__ import annotations
import argparse
from pathlib import Path
import re
import os
import shutil

BUCKET='noaa-rrfs-ops-pds'
REGION='us-east-1'


def s3_unsigned():
    from boto3.session import Session
    from botocore import UNSIGNED
    from botocore.config import Config
    return Session().client('s3', region_name=REGION,
        config=Config(signature_version=UNSIGNED,
                      retries={'mode':'standard','max_attempts':5},
                      max_pool_connections=12))


def horizon(cycle: str)->int:
    return 84 if int(cycle[-2:]) in (0,6,12,18) else 18


def is_available(model: str, cycle: str, fhr: int) -> bool:
    """True only if both official 3-km CONUS GRIB and index pairs exist."""
    if model != 'rrfs':
        raise ValueError('This source contains only operational RRFS')
    if int(cycle[-2:]) % 3:
        return False
    client = s3_unsigned()
    try:
        for kind in ('prslev', '2dfld'):
            key = grib_key(cycle, fhr, kind)
            for suffix in ('', '.idx'):
                meta = client.head_object(Bucket=BUCKET, Key=key+suffix)
                if int(meta.get('ContentLength', 0)) <= 0:
                    return False
        return True
    except Exception as exc:
        print(f'RRFS {cycle} F{fhr:03d} not fully ready: {exc}',flush=True)
        return False


def grib_key(cycle: str, fhr: int, kind: str)->str:
    if not re.fullmatch(r'\d{10}', cycle):
        raise ValueError('cycle must be YYYYMMDDHH (UTC)')
    if kind not in {'prslev','2dfld'}:
        raise ValueError('kind must be prslev or 2dfld')
    if not 0 <= fhr <= horizon(cycle):
        raise ValueError(f'Invalid FHR {fhr} for {cycle}, max {horizon(cycle)}')
    return (f'rrfs.{cycle[:8]}/{cycle[8:10]}/rrfs.t{cycle[8:10]}z.'
            f'{kind}.3km.f{fhr:03d}.conus.grib2')


# Keep S3 file existence checks lightweight for hourly scheduling; Herbie
# performs the actual selected GRIB2 download for each forecast hour.
# We use the exact Herbie inventory search strings, so Herbie selects the same
# indexed messages from its preferred operational source.

def wanted_line(description: str, kind: str) -> bool:
    parts = description.split(':')
    if len(parts) < 5:
        return False
    var = parts[3].strip().upper()
    level = parts[4].strip().lower()
    if kind == 'prslev':
        return var in {'HGT', 'TMP', 'UGRD', 'VGRD'} and bool(
            re.search(r'\b(500|700|850|925|1000)\s*(?:mb|hpa)\b', level))
    if var == 'REFC':
        return 'entire atmosphere' in level
    if var == 'REFD':
        return bool(re.search(r'\b1000\s*m\s+above\s+ground\b', level))
    if var in {'CRAIN', 'CSNOW', 'CICEP', 'CFRZR', 'APCP', 'ASNOW', 'WEASD', 'PRES'}:
        return level == 'surface'
    if var in {'PRMSL', 'MSLET'}:
        return 'mean sea' in level
    if var == 'GUST':
        return level == 'surface' or 'above ground' in level
    if var in {'TMP', 'DPT', 'RH'}:
        return bool(re.search(r'\b2\s*m\s+above\s+ground\b', level))
    if var in {'UGRD', 'VGRD'}:
        return bool(re.search(r'\b10\s*m\s+above\s+ground\b', level))
    return False


def _select_from_inventory(herbie_obj, kind: str, inventory: bool) -> str:
    """Build one Herbie regex from its exact RRFS index search_this entries.

    Herbie's search is evaluated against the `search_this` inventory column.
    Don't simply request every TMP/UGRD message: pressure-level 3-km files
    contain many levels that this viewer doesn't use.
    """
    frame = herbie_obj.inventory()
    if frame is None or frame.empty or 'search_this' not in frame:
        raise RuntimeError(f'Herbie returned no indexed RRFS {kind} messages')
    strings = [str(x) for x in frame['search_this'].dropna()]
    chosen = []
    for item in strings:
        # RRFS/Herbie NCEP entries: :VAR:level:forecast time:
        # Reformat only for reuse of our existing parameter/level filter.
        adapted = '1:0:d=cycle:' + item.lstrip(':')
        if wanted_line(adapted, kind):
            chosen.append(item)
    chosen = list(dict.fromkeys(chosen))
    print(f'Herbie RRFS {kind}: selecting {len(chosen)} of {len(strings)} indexed messages', flush=True)
    if inventory:
        for item in chosen:
            print(' ', item, flush=True)
    if not chosen:
        raise RuntimeError(f'Herbie indexed RRFS {kind}, but none of the required field patterns matched; check the printed inventory')
    # An OR of exact, escaped Herbie search_this values. This is selective even
    # when multiple accumulated/instantaneous intervals share the same variable.
    return '(?:' + '|'.join(re.escape(item) for item in chosen) + ')'


def _validate_subset(path: Path, kind: str) -> None:
    if not path.is_file() or path.stat().st_size < 1000:
        raise IOError(f'Empty or missing Herbie RRFS {kind} subset: {path}')
    with path.open('rb') as stream:
        if stream.read(4) != b'GRIB':
            raise IOError(f'Herbie RRFS {kind} subset does not begin with GRIB: {path}')
        stream.seek(-4, 2)
        if stream.read(4) != b'7777':
            raise IOError(f'Herbie RRFS {kind} subset has no terminating 7777 marker: {path}')


def download_selected_grib(model: str, cycle: str, fhr: int,
                            destination: Path, inventory: bool = False) -> Path:
    """Use Herbie for operational RRFS `prslev` + `2dfld` selective download.

    Return one concatenated GRIB2 message stream for the existing ecCodes
    plotting backend. Do not download the 2 enormous full RRFS GRIB2 files.
    Each forecast hour is downloaded once; the backend does its own crop/plots.
    """
    if model != 'rrfs':
        raise ValueError('This is the operational RRFS Herbie downloader')
    if not re.fullmatch(r'\d{10}', cycle):
        raise ValueError('cycle must be YYYYMMDDHH (UTC)')
    if int(cycle[-2:]) % 3:
        raise ValueError('Standard RRFS prslev/2dfld files are only available every three model cycles')
    if not 0 <= fhr <= horizon(cycle):
        raise ValueError(f'F{fhr:03d} exceeds the {cycle} horizon')
    from datetime import datetime
    from tempfile import TemporaryDirectory
    from herbie import Herbie

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Herbie expects a timezone-naive UTC initialization timestamp.
    target = datetime.strptime(cycle, '%Y%m%d%H')
    temporary = destination.with_suffix(destination.suffix + '.part')
    try:
        with TemporaryDirectory(prefix='herbie_rrfs_', dir=destination.parent) as workdir:
            subsets = []
            for kind in ('prslev', '2dfld'):
                print(f'Herbie RRFS {cycle} F{fhr:03d} {kind} CONUS', flush=True)
                h = Herbie(target, model='rrfs', product=kind, domain='conus',
                           fxx=fhr, priority=['aws', 'nomads'], verbose=False)
                if not getattr(h, 'grib', None):
                    raise RuntimeError(f'Herbie could not find RRFS {cycle} F{fhr:03d} {kind}')
                search = _select_from_inventory(h, kind, inventory)
                fetched = h.download(search=search, save_dir=workdir,
                                     overwrite=True, errors='raise', verbose=False)
                if fetched is None:
                    raise RuntimeError(f'Herbie returned no downloaded {kind} subset')
                path = Path(fetched)
                _validate_subset(path, kind)
                subsets.append(path)
            with temporary.open('wb') as out:
                for path in subsets:
                    with path.open('rb') as stream:
                        shutil.copyfileobj(stream, out, length=1 << 20)
        _validate_subset(temporary, 'combined')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Operational RRFS Herbie subset: {destination} ({destination.stat().st_size/(1024**2):.1f} MiB)', flush=True)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Operational RRFS Herbie subset download')
    parser.add_argument('--cycle', required=True, help='YYYYMMDDHH UTC; 00/03/06/.../21Z')
    parser.add_argument('--fhr', type=int, default=6)
    parser.add_argument('--output', type=Path, default=Path('rrfs_operational_selected.grib2'))
    parser.add_argument('--inventory', action='store_true', help='Print selected Herbie inventory entries')
    args = parser.parse_args()
    download_selected_grib('rrfs', args.cycle, args.fhr, args.output, args.inventory)
