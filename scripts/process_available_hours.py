"""Discover and render RRFS-MPAS forecast hours without inventing missing FHRs.

The existing rrfs_mpas_winter.py generates one forecast hour and writes its
PNG files + manifest to MPAS_OUT_DIR/site_export. This orchestrator chooses
actual available hours from 3-hourly GSL FTP cycles and runs that plotting script
once per hour, accumulating all results in the same site_export directory.

With --publish-each-hour, each successful hour is uploaded and indexed in S3
before the next GRIB download begins. Published PNGs are never deleted.

For long runs, print per-hour QPF availability diagnostics and delete the original
GRIB2 input after successfully staging that forecast hour. This prevents filling
the GitHub runner disk with dozens of large raw files.


Example (initial smoke test):
  python scripts/process_available_hours.py --cycle AUTO --hours 006 --max-hours 1

Example (whole latest allowed 3-hourly cycle, with no batch cap):
  python scripts/process_available_hours.py --cycle AUTO --hours ALL --max-hours 0

Example (incremental from recent runs, skip already-published hours):
  python scripts/process_available_hours.py --cycle RECENT --recent-cycles 8 \\
    --hours ALL --max-hours 6 --skip-published

Use --list-only to inspect the plan without downloads or plotting.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from ftplib import FTP
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

FTP_HOST = 'gsdftp.fsl.noaa.gov'
FTP_DIR = '/ncar/upp/det'
PREFIX = 'runs/cams/rrfs_mpas/winter'
FILE_RE = re.compile(
    r'(?P<date>\d{8})(?P<cycle>\d{2})\.rrfs\.t(?P=cycle)z'
    r'\.testbed\.f(?P<fhr>\d{3})\.conus\.grib2$'
)
VALID_DOMAINS = {'lbf', 'regional', 'central_plains'}
# Only run model cycles initialized at 00Z, 03Z, ..., 21Z.
# Forecast lead hours remain dynamically discovered; this is NOT an FHR limit.
ALLOWED_CYCLE_HOURS = frozenset(range(0, 24, 3))


def list_ftp_files(email: str, retries: int = 3) -> list[str]:
    for attempt in range(1, retries + 1):
        try:
            with FTP(FTP_HOST, timeout=120) as ftp:
                ftp.set_pasv(True)
                ftp.login('anonymous', email)
                ftp.cwd(FTP_DIR)
                return ftp.nlst()
        except Exception as exc:
            if attempt == retries:
                raise RuntimeError(f'GSL FTP inventory unavailable after {retries} attempts') from exc
            print(f'FTP inventory attempt {attempt} failed ({exc}); retrying', flush=True)
            time.sleep(2 * attempt)
    raise AssertionError('Unreachable')


def parse_inventory(names: list[str]) -> dict[str, list[int]]:
    available: dict[str, set[int]] = defaultdict(set)
    for name in names:
        match = FILE_RE.fullmatch(Path(name).name)
        if match and int(match['cycle']) in ALLOWED_CYCLE_HOURS:
            available[match['date'] + match['cycle']].add(int(match['fhr']))
    return {cycle: sorted(hours) for cycle, hours in sorted(available.items(), reverse=True)}


def parse_requested_hours(spec: str) -> set[int] | None:
    if spec.strip().upper() == 'ALL':
        return None
    result: set[int] = set()
    for item in spec.split(','):
        item = item.strip()
        if not item:
            continue
        match = re.fullmatch(r'(\d{1,3})\s*-\s*(\d{1,3})', item)
        if match:
            start, end = map(int, match.groups())
            if end < start:
                raise ValueError(f'Backward forecast-hour range: {item}')
            result.update(range(start, end + 1))
        elif re.fullmatch(r'\d{1,3}', item):
            result.add(int(item))
        else:
            raise ValueError(f'Invalid forecast hour selection: {item!r}')
    if not result or max(result) > 120:
        raise ValueError('Specify ALL or valid forecast hours between 000 and 120')
    return result


def selected_cycles(inventory: dict[str,list[int]], cycle_spec: str, recent_cycles: int,
                    requested: set[int] | None = None) -> list[str]:
    if not inventory:
        raise RuntimeError('No RRFS-MPAS testbed files found in GSL FTP listing')
    spec = cycle_spec.upper().strip()
    if spec == 'AUTO':
        # For a specific FHR (e.g. F006), choose the newest cycle which
        # actually has that FHR, not a newer cycle with F000 only.
        for cycle, hours in inventory.items():
            if requested is None or requested.intersection(hours):
                return [cycle]
        raise RuntimeError('No GSL cycle contains the requested forecast hour(s)')
    if spec == 'RECENT':
        return [cycle for cycle in list(inventory)[:recent_cycles]
                if requested is None or requested.intersection(inventory[cycle])]
    if not re.fullmatch(r'\d{10}', spec):
        raise ValueError('Cycle must be AUTO, RECENT, or YYYYMMDDHH')
    if int(spec[-2:]) not in ALLOWED_CYCLE_HOURS:
        raise ValueError('Only 3-hourly RRFS-MPAS cycles are supported: '
                         '00Z, 03Z, 06Z, 09Z, 12Z, 15Z, 18Z, 21Z')
    if spec not in inventory:
        raise RuntimeError(f'Selected cycle {spec} not in GSL FTP listing')
    return [spec]


def published_domains(index: dict[str, Any]) -> dict[tuple[str,int],set[str]]:
    found: dict[tuple[str,int],set[str]] = defaultdict(set)
    for row in index.get('runs', []):
        if not isinstance(row, dict):
            continue
        try:
            cycle, fhr = str(row['cycle']), int(row['forecast_hour'])
            if not re.fullmatch(r'\d{10}', cycle):
                continue
            if isinstance(row.get('domains'), list):
                found[(cycle, fhr)].update(str(d) for d in row['domains'])
        except (KeyError, ValueError, TypeError):
            continue
    return dict(found)


def get_s3_index() -> dict[str, Any]:
    # Publishing jobs use S3 directly to avoid re-rendering hours already online.
    # Fail clearly on AccessDenied rather than silently reprocessing everything.
    import boto3
    from botocore.exceptions import ClientError
    bucket = os.environ.get('AWS_BUCKET')
    if not bucket:
        raise RuntimeError('AWS_BUCKET must be configured to skip published hours')
    s3 = boto3.client('s3', region_name=os.environ.get('AWS_REGION', 'us-east-2'))
    try:
        response = s3.get_object(Bucket=bucket, Key=PREFIX + '/runs.json')
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') in {'404', 'NoSuchKey', 'NotFound'}:
            return {}
        raise
    return json.loads(response['Body'].read())


def build_plan(inventory: dict[str,list[int]], cycle_spec: str,
               requested: set[int] | None, recent_cycles: int,
               max_hours: int, already: dict[tuple[str,int],set[str]],
               domains: set[str]) -> tuple[list[tuple[str,int]],int,int]:
    cycles = selected_cycles(inventory, cycle_spec, recent_cycles, requested)
    jobs: list[tuple[str,int]] = []
    matched = 0
    skipped = 0
    # Prefer the latest run; each job is bound to one fixed cycle and hour.
    for cycle in cycles:
        for fhr in inventory[cycle]:
            # F000 is disabled in the viewer, so skip it for ALL runs.
            # It can still be rendered for debugging if explicitly requested.
            if fhr == 0 and (requested is None or 0 not in requested):
                continue
            if requested is not None and fhr not in requested:
                continue
            matched += 1
            if domains.issubset(already.get((cycle, fhr), set())):
                skipped += 1
                continue
            jobs.append((cycle, fhr))
    if requested is not None and matched == 0:
        raise RuntimeError('No requested forecast hours are available on GSL FTP')
    return (jobs[:max_hours] if max_hours else jobs), matched, skipped


def run_jobs(plan: list[tuple[str,int]], domains: str, script: Path,
             output: Path, keep_going: bool, publish_each_hour: bool = False) -> tuple[list[tuple[str,int]], list[tuple[str,int,str]]]:
    output.mkdir(parents=True,exist_ok=True)
    good: list[tuple[str,int]] = []
    bad: list[tuple[str,int,str]] = []
    missing_qpf: list[tuple[str,int,list[str]]] = []
    with_qpf = 0
    for i, (cycle, hour) in enumerate(plan, start=1):
        print(f'\n========== Hour {i}/{len(plan)}: {cycle} F{hour:03d} ==========', flush=True)
        env = dict(os.environ,
                   MPAS_CYCLE=cycle,
                   MPAS_FHR=f'{hour:03d}',
                   MPAS_DOMAINS=domains,
                   MPAS_OUT_DIR=str(output))
        try:
            subprocess.run([sys.executable, str(script)], env=env, check=True)
            manifest = output / 'site_export' / PREFIX / cycle / f'manifest_f{hour:03d}.json'
            if not manifest.is_file():
                raise RuntimeError(f'Plot completed but did not create {manifest}')
            # QPF is only published when the GRIB has a true cumulative
            # 0-to-forecast-hour accumulation. Report missing products explicitly,
            # rather than letting the frontend silently remove them from its menu.
            data = json.loads(manifest.read_text(encoding='utf-8'))
            domain_products = data.get('domains', {})
            missing_domains = [d for d in domains.split(',') if
                               'qpf' not in domain_products.get(d.strip(), {})]
            if missing_domains:
                print(f'QPF CHECK {cycle} F{hour:03d}: NOT STAGED '
                      f'(domains: {", ".join(missing_domains)})', flush=True)
                if hour != 0:
                    missing_qpf.append((cycle, hour, missing_domains))
            else:
                with_qpf += 1
                print(f'QPF CHECK {cycle} F{hour:03d}: OK '
                      '(true cumulative QPF PNG staged)', flush=True)
            # A full 48-hour run may contain tens of gigabytes of raw GRIB
            # downloads; the rendered PNGs are retained in site_export.
            grib_file = output / (f'{cycle[:8]}.rrfs.t{cycle[8:]}z.'
                                  f'testbed.f{hour:03d}.conus.grib2')
            if grib_file.is_file():
                size_mb = grib_file.stat().st_size / (1024 ** 2)
                grib_file.unlink()
                print(f'Cleaned raw GRIB F{hour:03d} ({size_mb:.1f} MiB); '
                      'staged PNGs retained', flush=True)
            if publish_each_hour:
                publisher = Path(__file__).resolve().with_name('publish_rrfs_mpas_winter.py')
                cmd = [sys.executable, str(publisher),
                       '--stage', str(output / 'site_export'),
                       '--cycle', cycle, '--forecast-hour', str(hour)]
                print(f'Publishing {cycle} F{hour:03d} to AWS S3 now...', flush=True)
                subprocess.run(cmd, check=True)
                print(f'PUBLISHED {cycle} F{hour:03d}: PNGs + merged manifest + runs.json', flush=True)
            good.append((cycle, hour))
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            bad.append((cycle, hour, str(exc)))
            print(f'WARNING: {cycle} F{hour:03d} failed: {exc}', flush=True)
            if not keep_going:
                break
    print(f'\nQPF SUMMARY: {with_qpf} hour(s) with QPF; '
          f'{len(missing_qpf)} forecast hour(s) missing QPF (excluding F000).',
          flush=True)
    for cycle, hour, missing_domains in missing_qpf:
        print(f'  MISSING QPF: {cycle} F{hour:03d} '
              f'[{", ".join(missing_domains)}]', flush=True)
    if missing_qpf:
        print('Check GRIB precipitation stepType and stepRange. The plotter '
              'currently requires an exact 0-F cumulative QPF field. '
              'Do NOT substitute the latest 1-hour increment as a total.', flush=True)
    return good, bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycle', default=os.getenv('MPAS_CYCLE', 'AUTO'),
                        help='AUTO, RECENT, or YYYYMMDDHH; only cycles 00/03/.../21Z')
    parser.add_argument('--hours', default='ALL', help='ALL, 006, 000-048, or 000,003,006')
    parser.add_argument('--recent-cycles', type=int, default=8,
                        help='How many cycles to include when --cycle RECENT')
    parser.add_argument('--max-hours', type=int, default=6,
                        help='Limit hours per workflow run; 0 = all available')
    parser.add_argument('--domains', default=os.getenv('MPAS_DOMAINS', 'lbf'))
    parser.add_argument('--skip-published', action='store_true',
                        help='Read existing S3 run index; process only missing cycle/hour/domain')
    parser.add_argument('--publish-each-hour', action='store_true',
                        help='Upload each completed hour to S3 and update runs.json before the next hour')
    parser.add_argument('--list-only', action='store_true')
    parser.add_argument('--inventory-file', type=Path,
                        help='Optional local FTP listing fixture for offline testing')
    parser.add_argument('--keep-going', action='store_true',
                        help='Continue with other hours when one download/plot fails')
    parser.add_argument('--output-dir', type=Path,
                        default=Path(os.getenv('MPAS_OUT_DIR', './rrfs_mpas_winter_output')))
    parser.add_argument('--script', type=Path, default=Path('rrfs_mpas_winter.py'))
    args = parser.parse_args(argv)
    if args.max_hours < 0 or args.recent_cycles < 1:
        parser.error('--max-hours must be >= 0 and --recent-cycles must be >= 1')
    domains = {item.strip() for item in args.domains.split(',') if item.strip()}
    if not domains or not domains.issubset(VALID_DOMAINS):
        parser.error('Invalid --domains (use lbf,regional,central_plains)')
    requested = parse_requested_hours(args.hours)
    if args.inventory_file:
        names = args.inventory_file.read_text().splitlines()
    else:
        names = list_ftp_files(os.getenv('GSL_FTP_EMAIL','your_email@example.com'))
    inventory = parse_inventory(names)
    print('Discovered cycles and file counts:',
          ', '.join(f'{cycle} ({len(hours)} hours)' for cycle,hours in list(inventory.items())[:12]),flush=True)
    already = published_domains(get_s3_index()) if args.skip_published else {}
    plan, matched, skipped = build_plan(
        inventory, args.cycle, requested, args.recent_cycles,
        args.max_hours, already, domains)
    print(f'FTP hours matching selection: {matched}; already published for domains: {skipped};'
          f' selected this run: {len(plan)}',flush=True)
    print('Selected:', ', '.join(f'{cycle} F{hour:03d}' for cycle,hour in plan) or '(none)',flush=True)
    if args.list_only or not plan:
        if not plan:
            print('No pending forecast hours. This is normal if they have all been published.')
        return 0
    good,bad = run_jobs(plan, args.domains, args.script, args.output_dir,
                        args.keep_going, publish_each_hour=args.publish_each_hour)
    print(f'Completed {len(good)} forecast hours; failed {len(bad)}',flush=True)
    for cycle,fhr,message in bad:
        print(f'FAILED {cycle} F{fhr:03d}: {message}',flush=True)
    if bad:
        # Do not report a green publishing job when any selected hour failed.
        # Already uploaded hours remain online and are not rolled back.
        return 1
    return 0
if __name__=='__main__':
    raise SystemExit(main())
