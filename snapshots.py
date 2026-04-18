from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


CIPHERSCAN_BOOTSTRAP_PAGE_URL = 'https://crosslink.cipherscan.app/bootstrap'
CIPHERSCAN_BOOTSTRAP_INFO_URL = 'https://api.crosslink.cipherscan.app/api/crosslink/bootstrap-info'
CIPHERSCAN_BOOTSTRAP_SHA256_URL = 'https://api.crosslink.cipherscan.app/bootstrap/bootstrap.tar.gz.sha256'
CIPHERSCAN_BOOTSTRAP_DOWNLOAD_URL = 'https://api.crosslink.cipherscan.app/bootstrap/bootstrap.tar.gz'
DEFAULT_CACHE_DIR = 'zebra_crosslink_workshop_season_one_v2_37482_cache_delete_me'
SNAPSHOT_CACHE_TTL_SECONDS = 60

_snapshot_cache_lock = asyncio.Lock()
_snapshot_cache: dict[str, Any] = {
    'fetched_at': 0.0,
    'value': None,
}


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_generated_at(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    for fmt in ('%Y%m%dT%H%M%SZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def _format_dt(value: datetime | None) -> str | None:
    if not value:
        return None
    return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def _format_age(seconds: int | None) -> str:
    if seconds is None:
        return 'unknown'
    if seconds < 60:
        return f'{seconds}s ago'
    if seconds < 3600:
        return f'{seconds // 60}m ago'
    if seconds < 86400:
        return f'{seconds // 3600}h ago'
    return f'{seconds // 86400}d ago'


def _format_bytes(size_bytes: int | None) -> str:
    if size_bytes is None:
        return 'unknown'
    if size_bytes < 1024:
        return f'{size_bytes} B'
    if size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} KB'
    if size_bytes < 1024 * 1024 * 1024:
        return f'{size_bytes / (1024 * 1024):.1f} MB'
    return f'{size_bytes / (1024 * 1024 * 1024):.2f} GB'


def _parse_sha256_text(body: str | None) -> str | None:
    if not body:
        return None
    candidate = body.strip().split()[0].lower()
    if len(candidate) == 64 and all(ch in '0123456789abcdef' for ch in candidate):
        return candidate
    return None


def _build_walkthrough(meta: dict[str, Any]) -> list[dict[str, str]]:
    download_url = meta.get('download_url') or CIPHERSCAN_BOOTSTRAP_DOWNLOAD_URL
    sha256_url = meta.get('sha256_url') or CIPHERSCAN_BOOTSTRAP_SHA256_URL
    cache_dir = meta.get('cache_dir_name') or DEFAULT_CACHE_DIR

    linux_cache_root = f'~/.cache/zebra/{cache_dir}'
    mac_cache_root = f'~/Library/Caches/zebra/{cache_dir}'

    return [
        {
            'title': 'Back Up Your Seed',
            'body': 'Preserve your local secret.seed before touching the cache. The snapshot never contains it.',
            'linux': (
                'mkdir -p ~/snapshot-seed-backups\n'
                f'cp {linux_cache_root}/secret.seed ~/snapshot-seed-backups/secret.seed'
            ),
            'macos': (
                'mkdir -p ~/snapshot-seed-backups\n'
                f'cp {mac_cache_root}/secret.seed ~/snapshot-seed-backups/secret.seed'
            ),
        },
        {
            'title': 'Stop The Node',
            'body': 'Quit the GUI or stop zebrad cleanly before replacing the cache contents.',
            'linux': 'systemctl --user stop zebrad || sudo systemctl stop zebrad || pkill -f zebrad',
            'macos': 'pkill -f Crosslink || pkill -f zebrad || true',
        },
        {
            'title': 'Download And Verify',
            'body': 'Pull the archive directly from Cipherscan, then verify the checksum before extracting anything.',
            'linux': (
                f'curl -LO {download_url}\n'
                f'curl -LO {sha256_url}\n'
                'sha256sum -c bootstrap.tar.gz.sha256'
            ),
            'macos': (
                f'curl -LO {download_url}\n'
                f'curl -LO {sha256_url}\n'
                'EXPECTED="$(awk \'{print $1}\' bootstrap.tar.gz.sha256)"\n'
                'ACTUAL="$(shasum -a 256 bootstrap.tar.gz | awk \'{print $1}\')"\n'
                'test "$EXPECTED" = "$ACTUAL" && echo "bootstrap.tar.gz: OK"'
            ),
        },
        {
            'title': 'Wipe The Old Cache',
            'body': 'Remove the broken cache directory, but leave your seed backup alone.',
            'linux': f'rm -rf {linux_cache_root}\nmkdir -p ~/.cache/zebra',
            'macos': f'rm -rf {mac_cache_root}\nmkdir -p ~/Library/Caches/zebra',
        },
        {
            'title': 'Extract The Snapshot',
            'body': 'Unpack the archive into Zebra’s cache root so the snapshot directory lands in place.',
            'linux': 'tar -xzf bootstrap.tar.gz -C ~/.cache/zebra/',
            'macos': 'tar -xzf bootstrap.tar.gz -C ~/Library/Caches/zebra/',
        },
        {
            'title': 'Reinsert The Seed',
            'body': 'Put your preserved secret.seed back into the restored cache directory.',
            'linux': (
                f'cp ~/snapshot-seed-backups/secret.seed {linux_cache_root}/secret.seed\n'
                f'chmod 600 {linux_cache_root}/secret.seed'
            ),
            'macos': (
                f'cp ~/snapshot-seed-backups/secret.seed {mac_cache_root}/secret.seed\n'
                f'chmod 600 {mac_cache_root}/secret.seed'
            ),
        },
        {
            'title': 'Restart And Verify',
            'body': 'Bring the node back up, wait for peers, then verify the restored height and hash against /sync-check.',
            'linux': 'systemctl --user start zebrad || sudo systemctl start zebrad',
            'macos': 'open -a Crosslink || /Applications/Crosslink.app/Contents/MacOS/Crosslink',
        },
    ]


async def _load_cipherscan_snapshot_meta() -> dict[str, Any]:
    now = int(time.time())
    meta: dict[str, Any] = {
        'source_name': 'Cipherscan',
        'source_slug': 'cipherscan',
        'cipherscan_url': CIPHERSCAN_BOOTSTRAP_PAGE_URL,
        'page_url': CIPHERSCAN_BOOTSTRAP_PAGE_URL,
        'status_url': CIPHERSCAN_BOOTSTRAP_INFO_URL,
        'download_url': CIPHERSCAN_BOOTSTRAP_DOWNLOAD_URL,
        'sha256_url': CIPHERSCAN_BOOTSTRAP_SHA256_URL,
        'sha256': None,
        'sha256_if_fetchable': None,
        'sha256_last_modified': None,
        'sha256_last_modified_display': None,
        'generated_at': None,
        'generated_at_display': None,
        'freshness_age_seconds': None,
        'freshness_label': 'unknown',
        'freshness_basis': None,
        'current_cipherscan_tip': None,
        'tip_height': None,
        'finalized_height': None,
        'finalized_hash': None,
        'size_bytes': None,
        'size_display': 'unknown',
        'cache_dir_name': DEFAULT_CACHE_DIR,
        'contents': [],
        'excludes': [],
        'available': False,
        'errors': [],
        'last_checked_at': _format_dt(datetime.fromtimestamp(now, tz=timezone.utc)),
        'cache_ttl_seconds': SNAPSHOT_CACHE_TTL_SECONDS,
    }

    headers = {'User-Agent': 'ctaz-explorer snapshots/2026-04-18'}
    timeout = httpx.Timeout(10.0, connect=5.0)

    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        info_result, sha_result = await asyncio.gather(
            client.get(CIPHERSCAN_BOOTSTRAP_INFO_URL),
            client.get(CIPHERSCAN_BOOTSTRAP_SHA256_URL),
            return_exceptions=True,
        )

    info_payload = None
    if isinstance(info_result, Exception):
        meta['errors'].append(f'bootstrap-info fetch failed: {info_result}')
    else:
        try:
            info_result.raise_for_status()
            info_payload = info_result.json()
        except Exception as exc:
            meta['errors'].append(f'bootstrap-info parse failed: {exc}')

    if isinstance(sha_result, Exception):
        meta['errors'].append(f'sha256 fetch failed: {sha_result}')
    else:
        try:
            sha_result.raise_for_status()
            meta['sha256'] = _parse_sha256_text(sha_result.text)
            meta['sha256_if_fetchable'] = meta['sha256']
            sha_last_modified = _parse_http_date(sha_result.headers.get('Last-Modified'))
            meta['sha256_last_modified'] = sha_last_modified.isoformat() if sha_last_modified else None
            meta['sha256_last_modified_display'] = _format_dt(sha_last_modified)
        except Exception as exc:
            meta['errors'].append(f'sha256 parse failed: {exc}')

    if isinstance(info_payload, dict):
        tip_height = _coerce_int(info_payload.get('tip_height'))
        finalized_height = _coerce_int(info_payload.get('finalized_height'))
        generated_at = _parse_generated_at(info_payload.get('generated_at'))
        meta.update({
            'available': bool(info_payload.get('available')),
            'download_url': info_payload.get('download_url') or CIPHERSCAN_BOOTSTRAP_DOWNLOAD_URL,
            'sha256_url': info_payload.get('sha256_url') or CIPHERSCAN_BOOTSTRAP_SHA256_URL,
            'generated_at': generated_at.isoformat() if generated_at else None,
            'generated_at_display': _format_dt(generated_at),
            'current_cipherscan_tip': tip_height or finalized_height,
            'tip_height': tip_height,
            'finalized_height': finalized_height,
            'finalized_hash': info_payload.get('finalized_hash'),
            'size_bytes': _coerce_int(info_payload.get('size_bytes')),
            'cache_dir_name': info_payload.get('cache_dir_name') or DEFAULT_CACHE_DIR,
            'contents': list(info_payload.get('contents') or []),
            'excludes': list(info_payload.get('excludes') or []),
        })
        meta['size_display'] = _format_bytes(meta['size_bytes'])

        freshness_basis = generated_at or _parse_http_date(sha_result.headers.get('Last-Modified') if not isinstance(sha_result, Exception) else None)
        if freshness_basis:
            freshness_age = max(0, int(time.time() - freshness_basis.timestamp()))
            meta['freshness_age_seconds'] = freshness_age
            meta['freshness_label'] = _format_age(freshness_age)
            meta['freshness_basis'] = 'generated_at' if generated_at else 'sha256_last_modified'

    meta['walkthrough'] = _build_walkthrough(meta)
    return meta


async def fetch_cipherscan_snapshot_meta() -> dict[str, Any]:
    now = time.time()
    cached = _snapshot_cache.get('value')
    fetched_at = float(_snapshot_cache.get('fetched_at') or 0.0)
    if cached is not None and (now - fetched_at) < SNAPSHOT_CACHE_TTL_SECONDS:
        return copy.deepcopy(cached)

    async with _snapshot_cache_lock:
        now = time.time()
        cached = _snapshot_cache.get('value')
        fetched_at = float(_snapshot_cache.get('fetched_at') or 0.0)
        if cached is not None and (now - fetched_at) < SNAPSHOT_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached)

        meta = await _load_cipherscan_snapshot_meta()
        _snapshot_cache['value'] = meta
        _snapshot_cache['fetched_at'] = time.time()
        return copy.deepcopy(meta)


def compute_canonical_grade(
    cipherscan_tip: int | None,
    our_tip: int | None,
    *,
    cipherscan_hash: str | None = None,
    our_hash: str | None = None,
) -> dict[str, Any]:
    tip = _coerce_int(cipherscan_tip)
    ours = _coerce_int(our_tip)
    delta = (tip - ours) if tip is not None and ours is not None else None

    result: dict[str, Any] = {
        'grade': 'unknown',
        'delta': delta,
        'hash_match': None,
        'tone': 'muted',
        'note': 'missing live tip data from cipherscan or our node, so canonicality is currently unknown.',
    }

    if tip is None or ours is None:
        return result

    if cipherscan_hash and our_hash:
        result['hash_match'] = cipherscan_hash.lower() == our_hash.lower()
        if result['hash_match'] is False:
            result['note'] = (
                'our node disagrees with cipherscan on the block hash at the snapshot height. '
                'treat london as non-authoritative until the chain is reconciled.'
            )
            return result

    if abs(delta) <= 2:
        result.update({
            'grade': 'verified-canonical ±2',
            'tone': 'good',
            'note': (
                'cipherscan is within 2 blocks of our node'
                + (' and the hash matches at the advertised finalized height.' if result['hash_match'] else '.')
            ),
        })
        return result

    if delta < -2:
        result.update({
            'grade': 'stale',
            'tone': 'warn',
            'note': f'cipherscan trails our node by {abs(delta)} blocks, so this snapshot is stale against our current tip.',
        })
        return result

    result['note'] = (
        f'our node trails cipherscan by {delta} blocks, so london cannot currently verify canonicality from its own tip.'
    )
    return result


def render_snapshots_page(request, templates, snapshot_meta: dict[str, Any]):
    context = {
        'request': request,
        'snapshot': snapshot_meta,
        'walkthrough': snapshot_meta.get('walkthrough') or _build_walkthrough(snapshot_meta),
        'grade': snapshot_meta.get('grade') or {},
        'sync_check_prefill_url': snapshot_meta.get('sync_check_prefill_url') or '/sync-check',
        'sync_check_api_url': snapshot_meta.get('sync_check_api_url'),
    }
    return templates.TemplateResponse(request, 'snapshots.html', context)
