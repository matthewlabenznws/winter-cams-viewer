"""Hourly (00Z..23Z) HRRR Winter CAMs: Herbie retrieval, S3 resume, per-hour publishing.

SAFE FIRST TEST:
  python scripts/process_hrrr_hourly.py --cycle AUTO --hours 006 --max-hours 1

AUTOMATIC (after reviewing maps):
  python scripts/process_hrrr_hourly.py --cycle PRIORITY --hours ALL \
    --max-hours 0 --max-wall-minutes 45 --skip-published --publish \
    --wait-for-new-hours --idle-wait-minutes 15 --poll-seconds 90 --keep-going

NOAA public S3 listing discovers actual published prs+sfc GRIB2 .idx files
efficiently; Herbie verifies availability and downloads the selected messages. No hypothetical forecast hour is invented.
Non-extended hourly cycles go through F018; 00/06/12/18 through F048.
Each completed FHR is published with a manifest/index checkpoint; already
published FHR/domain pairs are skipped at the next run. PRIORITY + ALL is
STRICTLY GAP AWARE: within each individual cycle, F003 is never plotted until
F002 has been published for all requested domains. While plotting F002, new
F003 availability is detected immediately when F002 finishes. When the queue
runs dry, the runner polls for newly arriving next-hours for up to its idle
wait window, without preventing other ready cycles from running. A later cron
run resumes ALL missing hours from S3; no forecast-hour count cap is applied.
Plot artifacts are removed locally after publication to save CI storage.
This does NOT modify RRFS-MPAS scripts or S3 keys.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

BASE = Path(__file__).resolve().parents[1]
PREFIX = 'runs/cams/hrrr/winter'
SOURCE_BUCKET = 'noaa-hrrr-bdp-pds'
SOURCE_REGION = 'us-east-1'
VALID_DOMAINS = {'lbf', 'regional', 'central_plains'}
FILE_PATTERN = re.compile(r'^hrrr\.t(?P<hh>\d{2})z\.(?P<kind>wrfprsf|wrfsfcf)(?P<fhr>\d{2})\.grib2\.idx$')


def horizon(cycle: str) -> int:
    return 48 if int(cycle[-2:]) in (0, 6, 12, 18) else 18


def parse_requested_hours(spec: str) -> set[int] | None:
    if spec.strip().upper() == 'ALL':
        return None
    result: set[int] = set()
    for part in spec.split(','):
        part = part.strip()
        if re.fullmatch(r'\d{1,3}', part):
            result.add(int(part))
        elif re.fullmatch(r'\d{1,3}-\d{1,3}', part):
            a, b = map(int, part.split('-'))
            if b < a:
                raise ValueError(f'Backward forecast-hour range: {part}')
            result.update(range(a, b + 1))
        else:
            raise ValueError(f'Invalid forecast-hour selector: {part}')
    if not result or min(result) < 0 or max(result) > 48:
        raise ValueError('Hours must be ALL, F000..F048, or a comma/range selection')
    return result


def parse_key(key: str):
    """Return (cycle, fhr, product) for a NOAA .idx key or None."""
    bits = key.split('/')
    if len(bits) != 3 or bits[1] != 'conus':
        return None
    date = bits[0].removeprefix('hrrr.')
    if not re.fullmatch(r'\d{8}', date):
        return None
    m = FILE_PATTERN.fullmatch(bits[2])
    if not m:
        return None
    cycle = date + m['hh']
    if int(m['fhr']) > horizon(cycle):
        return None
    return cycle, int(m['fhr']), m['kind']


def discover_source(lookback_hours: int, now: datetime | None = None) -> dict[str, list[int]]:
    """List NOAA's public archive, and accept only wrfprs/wrfsfc paired hours."""
    from boto3.session import Session
    from botocore import UNSIGNED
    from botocore.config import Config
    client = Session().client('s3', region_name=SOURCE_REGION,
                              config=Config(signature_version=UNSIGNED,
                                            retries={'mode': 'standard', 'max_attempts': 4}))
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    start_date = cutoff.date()
    end_date = now.date()
    keys_seen: dict[tuple[str, int], set[str]] = defaultdict(set)
    current = start_date
    while current <= end_date:
        prefix = f'hrrr.{current:%Y%m%d}/conus/hrrr.t'
        pager = client.get_paginator('list_objects_v2')
        count = 0
        for page in pager.paginate(Bucket=SOURCE_BUCKET, Prefix=prefix):
            for item in page.get('Contents', []):
                candidate = parse_key(item['Key'])
                if candidate:
                    cyc, fhr, kind = candidate
                    cyc_dt = datetime.strptime(cyc, '%Y%m%d%H').replace(tzinfo=timezone.utc)
                    if cutoff <= cyc_dt <= now:
                        keys_seen[(cyc, fhr)].add(kind)
                        count += 1
        print(f'NOAA HRRR listing {current:%Y-%m-%d}: {count} indexed GRIB files', flush=True)
        current += timedelta(days=1)
    available: dict[str, list[int]] = defaultdict(list)
    for (cyc, fhr), kinds in keys_seen.items():
        if {'wrfprsf', 'wrfsfcf'}.issubset(kinds):
            available[cyc].append(fhr)
    return {c: sorted(h) for c, h in sorted(available.items(), reverse=True)}


def published_domains(index: dict[str, Any]) -> dict[tuple[str, int], set[str]]:
    done: dict[tuple[str, int], set[str]] = defaultdict(set)
    for item in index.get('runs', []):
        try:
            cycle = str(item['cycle'])
            fhr = int(item['forecast_hour'])
            if re.fullmatch(r'\d{10}', cycle):
                done[(cycle, fhr)].update(item.get('domains', []))
        except (TypeError, ValueError, KeyError, AttributeError):
            pass
    return done


def remote_index() -> dict:
    import boto3
    from botocore.exceptions import ClientError
    if not os.getenv('AWS_BUCKET'):
        raise RuntimeError('AWS_BUCKET is needed for --skip-published')
    s3 = boto3.client('s3', region_name=os.environ.get('AWS_REGION', 'us-east-2'))
    try:
        return json.loads(s3.get_object(Bucket=os.environ['AWS_BUCKET'], Key=PREFIX+'/runs.json')['Body'].read())
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
            return {}
        raise


def next_unpublished_hour(cycle: str, done: dict[tuple[str, int], set[str]],
                          domains: set[str], requested_hours: set[int] | None) -> int | None:
    """Earliest uncompleted requested FHR. Never assume unavailable == completed."""
    levels = (range(1, horizon(cycle) + 1) if requested_hours is None else
              sorted(h for h in requested_hours if 0 <= h <= horizon(cycle)))
    for fhr in levels:
        if not domains.issubset(done.get((cycle, fhr), set())):
            return fhr
    return None


def build_plan(inventory: dict[str, list[int]], cycle_spec: str,
               requested_hours: set[int] | None, done: dict[tuple[str, int], set[str]],
               domains: set[str], max_hours: int = 0) -> tuple[list[tuple[str, int]], int]:
    """Queue only contiguous ready forecast hours PER CYCLE for PRIORITY/ALL.

    Example: F001 and F003 are on NOAA, but F002 is not. Schedule F001, then
    halt THAT cycle at F002. Never silently schedule F003 ahead of it. Other
    cycles remain eligible to run while this one waits.
    """
    spec = cycle_spec.strip().upper()
    if spec == 'AUTO':
        cycles = sorted((c for c, hrs in inventory.items()
                  if any(h > 0 and (requested_hours is None or h in requested_hours) for h in hrs)), reverse=True)[:1]
    elif spec == 'PRIORITY':
        cycles = sorted(inventory, reverse=True)
    else:
        if not re.fullmatch(r'\d{10}', spec):
            raise ValueError('Cycle must be AUTO, PRIORITY or YYYYMMDDHH')
        cycles = [spec] if spec in inventory else []
    if not cycles:
        return [], 0
    long_queue: list[tuple[str, int]] = []
    short_queue: list[tuple[str, int]] = []
    normal: list[tuple[str, int]] = []
    skipped = 0
    strict_order = requested_hours is None  # Production ALL; explicit hour test independent.
    for cycle in cycles:
        available = set(inventory[cycle])
        hours = (range(1, horizon(cycle) + 1) if requested_hours is None
                 else sorted(h for h in requested_hours if h <= horizon(cycle)))
        for fhr in hours:
            if domains.issubset(done.get((cycle, fhr), set())):
                skipped += 1
                continue
            if fhr not in available:
                if strict_order:
                    break  # Do not jump an unpublished/missing forecast hour.
                continue
            item = (cycle, fhr)
            if spec == 'PRIORITY':
                (long_queue if int(cycle[-2:]) in (0, 6, 12, 18) else short_queue).append(item)
            else:
                normal.append(item)
    if spec != 'PRIORITY':
        plan = sorted(normal, key=lambda item: (item[0], -item[1]), reverse=True)
    else:
        long_queue.sort(key=lambda item: (item[0], item[1]))
        short_queue.sort(key=lambda item: (item[0], item[1]), reverse=True)
        plan = []
        a = b = 0
        while a < len(long_queue) or b < len(short_queue):
            if a < len(long_queue):
                plan.append(long_queue[a]); a += 1
            for _ in range(2):
                if b < len(short_queue):
                    plan.append(short_queue[b]); b += 1
    return (plan[:max_hours] if max_hours else plan), skipped


def choose_next(plan: list[tuple[str, int]], spec: str, phase: int) -> tuple[str, int]:
    """Phase prevents recomputing the plan from starving short hourly cycles."""
    if spec.strip().upper() != 'PRIORITY':
        return plan[0]
    prefer_long = phase % 3 == 0
    for cycle, fhr in plan:
        is_long = int(cycle[-2:]) in (0, 6, 12, 18)
        if is_long == prefer_long:
            return cycle, fhr
    return plan[0]


def refresh_expected_hours(inventory: dict[str, list[int]],
                           done: dict[tuple[str, int], set[str]], domains: set[str],
                           requested: set[int] | None, blocked_cycles: set[str] | None = None,
                           only_cycle: str | None = None) -> list[tuple[str, int]]:
    """Probe ONLY next needed hour per cycle, not hypothetical later hours.

    Even if NOAA lists F005, F004 is required first. Check the GRIB and .idx
    files themselves rather than assuming a file is ready from its cycle time.
    """
    from scripts.herbie_hrrr_fetch import is_available
    found: list[tuple[str, int]] = []
    if only_cycle is not None:
        cycles = [only_cycle] if only_cycle in inventory else []
    else:
        # Oldest extended cycles first, then newest ordinary cycles.
        long_cycles = sorted(c for c in inventory if int(c[-2:]) in (0, 6, 12, 18))
        short_cycles = sorted((c for c in inventory if int(c[-2:]) not in (0, 6, 12, 18)),
                              reverse=True)
        cycles = long_cycles + short_cycles
    for cycle in cycles:
        if blocked_cycles and cycle in blocked_cycles:
            continue
        needed = next_unpublished_hour(cycle, done, domains, requested)
        if needed is None or needed in inventory[cycle]:
            continue
        if is_available('hrrr', cycle, needed):
            inventory[cycle] = sorted(set(inventory[cycle]) | {needed})
            found.append((cycle, needed))
            print(f'NEW FILE READY: HRRR {cycle} F{needed:03d}', flush=True)
    return found


def cleanup_published(output: Path, manifest: Path):
    """Delete generated files *after* successful upload, keep local index metadata."""
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
        src = output / 'site_export' / PREFIX
        fhr = int(data['forecast_hour'])
        cycle = str(data['cycle'])
        for domain, products in data.get('domains', {}).items():
            for product_id, rel in products.items():
                png = src / rel
                if png.is_file() and src in png.parents:
                    png.unlink()
                original = output / f'hrrr_{product_id}_{domain}_{cycle}_f{fhr:03d}.png'
                if original.is_file():
                    original.unlink()
        manifest.unlink(missing_ok=True)
    except Exception as exc:
        print(f'Nonfatal cleanup warning: {exc}', flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cycle', default='AUTO', help='AUTO latest, PRIORITY all recent, or YYYYMMDDHH')
    p.add_argument('--hours', default='006', help='ALL or 006, 006-012, etc.')
    p.add_argument('--domains', default='lbf,regional,central_plains')
    p.add_argument('--max-hours', type=int, default=0, help='0 = no forecast-hour count cap')
    p.add_argument('--max-wall-minutes', type=int, default=45,
                   help='Wall time budget; checks between completed hours')
    p.add_argument('--lookback-hours', type=int, default=96)
    p.add_argument('--publish', action='store_true')
    p.add_argument('--skip-published', action='store_true')
    p.add_argument('--keep-going', action='store_true')
    p.add_argument('--list-only', action='store_true')
    p.add_argument('--keep-grib', action='store_true')
    p.add_argument('--wait-for-new-hours', action='store_true',
                   help='Poll for the next missing FHR after each finished hour and when idle')
    p.add_argument('--poll-seconds', type=int, default=90)
    p.add_argument('--idle-wait-minutes', type=int, default=15,
                   help='Maximum time to stay idle waiting for a newly released FHR')
    args = p.parse_args(argv)
    if (args.max_hours < 0 or args.max_wall_minutes < 0 or args.lookback_hours < 0 or
        args.poll_seconds < 15 or args.idle_wait_minutes < 0):
        p.error('Limits must be >= 0, and poll-seconds must be >= 15')
    domains = [x.strip() for x in args.domains.split(',') if x.strip()]
    if not domains or any(d not in VALID_DOMAINS for d in domains):
        p.error('Domains must come from lbf,regional,central_plains')
    requested = parse_requested_hours(args.hours)
    inventory = discover_source(args.lookback_hours)
    print(f'NOAA published {sum(map(len, inventory.values()))} paired forecast-hour sets '
          f'from {len(inventory)} hourly HRRR cycles', flush=True)
    if not inventory:
        raise RuntimeError('No HRRR source inventory available. Check NOAA public bucket.')
    done = published_domains(remote_index()) if args.skip_published else {}
    chosen_domains = set(domains)
    plan, skipped = build_plan(inventory, args.cycle, requested, done,
                               chosen_domains, args.max_hours)
    print(f'Already published for ALL selected domains: {skipped}', flush=True)
    print(f'Initially ready {len(plan)} forecast hours (first 20): ' +
          ', '.join(f'{cyc}/F{hr:03d}' for cyc, hr in plan[:20]), flush=True)
    if args.list_only:
        print('List-only: no downloads, plots, or S3 writes.')
        return 0
    if not plan and not args.wait_for_new_hours:
        print('No currently available/unpublished requested hours. Cron will check again.')
        return 0
    output = Path(os.getenv('WINTER_OUT_DIR', BASE / 'hrrr_winter_output')).resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    last_progress = started
    last_check = started - args.poll_seconds
    successful: list[tuple[str, int]] = []
    failed: list[tuple[str, int, str]] = []
    blocked_cycles: set[str] = set()
    phase = 0
    spec = args.cycle.strip().upper()
    processed = 0
    from scripts.herbie_hrrr_fetch import is_available

    while True:
        elapsed = time.monotonic() - started
        if args.max_wall_minutes and elapsed >= args.max_wall_minutes * 60:
            print('Time budget reached; next cron run resumes missing hours from S3.', flush=True)
            break
        if args.max_hours and processed >= args.max_hours:
            print('User-specified forecast-hour count reached.', flush=True)
            break

        active_inventory = {cycle: hours for cycle, hours in inventory.items()
                            if cycle not in blocked_cycles}
        plan, _ = build_plan(active_inventory, args.cycle, requested, done,
                             chosen_domains, 0)
        if not plan:
            if not args.wait_for_new_hours:
                print('No more ready forecast hours; return to cron.', flush=True)
                break
            idle_seconds = time.monotonic() - last_progress
            if idle_seconds >= args.idle_wait_minutes * 60:
                print(f'No new hours for {args.idle_wait_minutes} minutes; '
                      'unavailable hours will be retried by the next cron run.', flush=True)
                break
            # Probe for the *next missing* FHR from every still-active cycle.
            # This also catches an hour that appears AFTER the inventory scan.
            print('Waiting for unpublished forecast hours; checking NOAA again...', flush=True)
            fresh = refresh_expected_hours(inventory, done, chosen_domains, requested,
                                           blocked_cycles=blocked_cycles)
            if fresh:
                last_progress = time.monotonic()
                continue
            sleep_for = min(args.poll_seconds,
                            args.idle_wait_minutes * 60 - idle_seconds,
                            (args.max_wall_minutes * 60 - (time.monotonic() - started)
                             if args.max_wall_minutes else args.poll_seconds))
            if sleep_for > 0:
                time.sleep(sleep_for)
            continue

        cycle, fhr = choose_next(plan, spec, phase)
        phase += 1
        print(f'\nPROCESS HRRR {cycle} F{fhr:03d} domains={domains}', flush=True)
        try:
            # A cached public index is not enough: require source availability.
            if not is_available('hrrr', cycle, fhr):
                # No permanently skipped hour. Pause THIS cycle until a later
                # poll / cron run; unrelated cycles can still publish now.
                print(f'Source NOT READY: {cycle} F{fhr:03d}; hold this cycle.', flush=True)
                inventory[cycle] = [h for h in inventory[cycle] if h != fhr]
                if not args.wait_for_new_hours:
                    blocked_cycles.add(cycle)
                continue
            env = dict(os.environ, WINTER_CYCLE=cycle, WINTER_FHR=f'{fhr:03d}',
                       WINTER_DOMAINS=','.join(domains), WINTER_OUT_DIR=str(output))
            subprocess.run([sys.executable, str(BASE/'hrrr_winter.py')],
                           cwd=BASE, env=env, check=True)
            manifest = output/'site_export'/PREFIX/cycle/f'manifest_f{fhr:03d}.json'
            if not manifest.is_file():
                raise RuntimeError(f'Manifest not staged: {manifest}')
            product_info = json.loads(manifest.read_text())['domains']
            missing = [d for d in domains if not product_info.get(d)]
            if missing:
                raise RuntimeError('No products staged for domain(s): '+', '.join(missing))
            if args.publish:
                publisher = BASE/'scripts'/'publish_hrrr_winter.py'
                subprocess.run([sys.executable, str(publisher), '--stage',
                                str(output/'site_export'), '--cycle', cycle,
                                '--forecast-hour', str(fhr)], cwd=BASE, env=env, check=True)
                cleanup_published(output, manifest)
            if not args.keep_grib:
                (output/f'hrrr_selected_{cycle}_f{fhr:03d}.grib2').unlink(missing_ok=True)
            # Mark completed in memory after successful publish or dry-run.
            done.setdefault((cycle, fhr), set()).update(domains)
            successful.append((cycle, fhr))
            processed += 1
            last_progress = time.monotonic()
            print(f'DONE {cycle} F{fhr:03d} | {"published S3" if args.publish else "local preview"}', flush=True)

            # IMPORTANT: Fhr+1 may have arrived during the image creation.
            # Probe NOW rather than using the stale one-time .idx listing.
            if args.wait_for_new_hours:
                fresh = refresh_expected_hours(inventory, done, chosen_domains, requested,
                                               only_cycle=cycle)
                if fresh:
                    last_progress = time.monotonic()
        except (subprocess.CalledProcessError, RuntimeError, ValueError, OSError) as exc:
            print(f'FAILED {cycle} F{fhr:03d}: {exc}', flush=True)
            failed.append((cycle, fhr, str(exc)))
            blocked_cycles.add(cycle)
            if not args.keep_going:
                break
    print(f'HRRR completed {len(successful)}, failed {len(failed)} forecast-hour tasks', flush=True)
    if failed:
        for cyc, fhr, err in failed[:10]:
            print(f'  {cyc} F{fhr:03d}: {err}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
