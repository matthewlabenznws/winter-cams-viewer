"""Publish site-staged RRFS-MPAS winter PNGs and manifests to AWS S3.

Order matters: upload graphics -> individual manifest -> latest merged runs.json.
No delete operations, and never touch the existing severe CAMs paths.

Usage:
  python scripts/publish_rrfs_mpas_winter.py --stage ./output/site_export
  python scripts/publish_rrfs_mpas_winter.py --stage ./output/site_export --dry-run

Required: AWS_BUCKET and AWS_REGION. Credentials use the standard AWS SDK
chain (GitHub Actions can provide AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY).
Do not set an R2/third-party endpoint; boto3 selects AWS S3 automatically.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

PREFIX = 'runs/cams/rrfs_mpas/winter'
INDEX_KEY = PREFIX + '/runs.json'


def get_site_root(stage: Path) -> Path:
    root = stage / 'runs' / 'cams' / 'rrfs_mpas' / 'winter'
    if not root.is_dir():
        raise FileNotFoundError(f'Missing staging directory: {root}')
    return root


def validate_local_run(root: Path) -> tuple[list[Path],list[dict]]:
    """Validate every file referenced by the current local per-FHR manifests."""
    manifests = sorted(root.glob('*/manifest_f[0-9][0-9][0-9].json'))
    if not manifests:
        raise RuntimeError('No forecast-hour manifests found; refusing to upload a blank index')
    pngs = []
    entries = []
    for mfile in manifests:
        m = json.loads(mfile.read_text(encoding='utf-8'))
        cycle = str(m['cycle'])
        fhr = int(m['forecast_hour'])
        if not re.fullmatch(r'\d{10}', cycle) or not (0 <= fhr <= 120):
            raise ValueError(f'Invalid cycle or FHR in manifest: {mfile}')
        if m['model'] != 'rrfs_mpas':
            raise ValueError(f'Unexpected model in {mfile}')
        if mfile.relative_to(root).as_posix() != f'{cycle}/manifest_f{fhr:03d}.json':
            raise ValueError(f'Manifest path mismatches metadata: {mfile}')
        count = 0
        for domain, products in m['domains'].items():
            if domain not in ('lbf','regional','central_plains'):
                raise ValueError(f'Unexpected domain: {domain}')
            for product, rel in products.items():
                # Prevent accidental path traversal and accidental uploads to severe paths.
                relative = Path(rel)
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError(f'Unsafe PNG filename: {rel}')
                if not rel.startswith(f'{cycle}/{domain}/') or not rel.endswith('.png'):
                    raise ValueError(f'Unexpected PNG filename: {rel}')
                file = root / relative
                if not file.is_file() or file.stat().st_size == 0:
                    raise FileNotFoundError(f'Manifest lists a missing/empty PNG: {file}')
                pngs.append(file)
                count += 1
        if not count:
            raise RuntimeError(f'Manifest {mfile.name} has no PNG outputs')
        entries.append({
            'cycle':cycle,'forecast_hour':fhr,
            'valid_time_utc':m['valid_time_utc'],
            'manifest':f'{cycle}/manifest_f{fhr:03d}.json',
            'domains':list(m['domains']),
        })
    return sorted(set(pngs)), entries


def merge_index(remote: dict | None, new_entries: list[dict], max_entries: int=1800) -> dict:
    """Keep previously uploaded cycle/FHR entries; never replace with only current hour."""
    old = remote if isinstance(remote, dict) else {}
    prior = old.get('runs', [])
    unique: dict[tuple[str,int],dict] = {}
    for entry in prior if isinstance(prior, list) else []:
        if isinstance(entry, dict) and re.fullmatch(r'\d{10}',str(entry.get('cycle',''))):
            try:
                unique[(entry['cycle'],int(entry['forecast_hour']))] = entry
            except (KeyError,ValueError,TypeError):
                pass
    for entry in new_entries:
        unique[(entry['cycle'],entry['forecast_hour'])] = entry
    sorted_entries = sorted(unique.values(),key=lambda x:(x['cycle'],int(x['forecast_hour'])),reverse=True)
    return {'schema_version':1,'model':'rrfs_mpas','experimental':True,
            'updated_utc':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
            'runs':sorted_entries[:max_entries]}


def connect_s3():
    """Use the actual AWS S3 regional endpoint and standard AWS credentials."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError('boto3 is needed for S3 publishing') from exc
    missing = [key for key in ('AWS_BUCKET', 'AWS_REGION') if not os.getenv(key)]
    if missing:
        raise RuntimeError('Missing GitHub S3 setting(s): ' + ', '.join(missing))
    # Never set endpoint_url here. We are publishing to AWS S3, not R2.
    # Default credential chain supports environment keys and future OIDC roles.
    return boto3.client(
        's3', region_name=os.environ['AWS_REGION'],
        config=Config(signature_version='s3v4', retries={'max_attempts': 5, 'mode': 'standard'}),
    )


def load_remote_index(client,bucket) -> dict:
    try:
        return json.loads(client.get_object(Bucket=bucket,Key=INDEX_KEY)['Body'].read().decode('utf-8'))
    except client.exceptions.NoSuchKey:
        return {}
    except Exception as exc:
        # Botocore returns ClientError (NoSuchKey or HTTP 404) for absent objects.
        from botocore.exceptions import ClientError
        if isinstance(exc,ClientError) and exc.response.get('Error',{}).get('Code') in ('NoSuchKey','404','NotFound'):
            return {}
        raise


def publish(stage: Path, dry_run=False) -> dict:
    root=get_site_root(stage)
    pngs,entries=validate_local_run(root)
    manifest_files=[root / e['manifest'] for e in entries]
    print(f'Validated {len(pngs)} PNGs and {len(manifest_files)} manifests for {len(entries)} FHRs')
    if dry_run:
        print('DRY RUN — no S3 connection or writes')
        return merge_index(None,entries)
    s3=connect_s3()
    bucket=os.environ['AWS_BUCKET']
    for file in pngs:
        key=f'{PREFIX}/{file.relative_to(root).as_posix()}'
        s3.upload_file(str(file),bucket,key,ExtraArgs={
            'ContentType':'image/png',
            'CacheControl':'public, max-age=1800'})
        print('Uploaded PNG:',key)
    for i, file in enumerate(manifest_files):
        key=f'{PREFIX}/{file.relative_to(root).as_posix()}'
        manifest=json.loads(file.read_text(encoding='utf-8'))
        # A later workflow may publish additional domains for a forecast hour.
        # Preserve earlier published LBF/regional/central_plains products instead
        # of replacing their manifest with the newly generated domain alone.
        try:
            prior=json.loads(s3.get_object(Bucket=bucket,Key=key)['Body'].read())
        except Exception as exc:
            from botocore.exceptions import ClientError
            if isinstance(exc,ClientError) and exc.response.get('Error',{}).get('Code') in ('404','NoSuchKey','NotFound'):
                prior={}
            else:
                raise
        if (prior.get('model') == manifest['model'] and
            prior.get('cycle') == manifest['cycle'] and
            prior.get('forecast_hour') == manifest['forecast_hour']):
            old_domains=prior.get('domains', {})
            if isinstance(old_domains,dict):
                combined={domain:dict(products) for domain,products in old_domains.items()
                          if isinstance(products,dict)}
                for domain,products in manifest['domains'].items():
                    combined.setdefault(domain,{}).update(products)
                manifest['domains']=combined
                fields=set(prior.get('available_fields', []))
                fields.update(manifest.get('available_fields', []))
                manifest['available_fields']=sorted(fields)
        entries[i]['domains']=list(manifest['domains'])
        s3.put_object(Bucket=bucket,Key=key,
            Body=json.dumps(manifest,indent=2).encode('utf-8'),
            ContentType='application/json',CacheControl='no-store, max-age=0')
        print('Uploaded manifest (merged existing domains):',key)
    # Index is intentionally LAST: the viewer won't see incomplete new hours.
    merged=merge_index(load_remote_index(s3,bucket),entries)
    s3.put_object(Bucket=bucket, Key=INDEX_KEY,
        Body=json.dumps(merged,indent=2).encode('utf-8'),
        ContentType='application/json',CacheControl='no-store, max-age=0')
    print(f'Uploaded runs.json LAST ({len(merged["runs"])} entries)')
    return merged


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage',required=True,type=Path)
    p.add_argument('--dry-run',action='store_true')
    args=p.parse_args()
    publish(args.stage,args.dry_run)

if __name__=='__main__':
    main()
