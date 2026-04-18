import asyncio
import json
import logging
import os
import pathlib
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from rpc import ZebradRPC
from snapshots import (
    compute_canonical_grade,
    fetch_cipherscan_snapshot_meta,
    render_snapshots_page,
)
from zap1_live import (
    close as close_zap1_live,
    fetch_anchor_registry as fetch_zap1_live_anchors,
    fetch_leaf_by_hash as fetch_zap1_live_leaf_by_hash,
    fetch_leaf_registry as fetch_zap1_live_leaves,
    fetch_status as fetch_zap1_live_status,
)

app = FastAPI(title='ctaz-explorer', docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory='templates')
app.mount('/static', StaticFiles(directory='static'), name='static')
rpc = ZebradRPC()
rpc_zcash = ZebradRPC(url=os.environ.get('ZCASH_MAINNET_RPC_URL', 'http://127.0.0.1:8232'))
logger = logging.getLogger(__name__)
CHAIN_ROUTE_STATUS = {
    'ctaz': {'ok': True, 'message': ''},
    'zcash': {'ok': True, 'message': ''},
}
EXPECTED_CHAIN_IDS = {
    'ctaz': 'ctaz-s1',
    'zcash': 'main',
}
EXPECTED_GENESIS_HASHES = {
    'ctaz': os.environ.get('CTAZ_EXPECTED_GENESIS_HASH', '05a60a92d99d85997cce3b87616c089f6124d7342af37106edc76126334a2c38').lower(),
    'zcash': os.environ.get('ZCASH_MAINNET_GENESIS_HASH', '00040fe8ec8471911baa1db1266ea15dd06b4a8a5c453883c000b031973dce08').lower(),
}


NO_STORE_PREFIXES = (
    '/api/', '/block/', '/tx/', '/verify', '/finalizers',
    '/anchors', '/vaults', '/events', '/params', '/tfl',
    '/tip', '/finalized', '/gap', '/feed.xml',
    '/.well-known/explorer', '/z/', '/tip.z', '/snapshots',
)


@app.middleware('http')
async def no_store_live_surfaces(request: Request, call_next):
    path = request.url.path
    chain_key = route_chain_key(path)
    issue = CHAIN_ROUTE_STATUS.get(chain_key) if chain_key else None
    if issue and not issue.get('ok', True):
        response = make_error_response(request, 503, issue.get('message'))
    else:
        response = await call_next(request)
    if path in {'/', '/z', '/robots.txt', '/sitemap.xml'} or any(path.startswith(p) for p in NO_STORE_PREFIXES):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Robots-Tag'] = 'noarchive'
    return response


CHAINS = {
    'ctaz': {
        'id': 'ctaz-s1',
        'label': 'crosslink s1 feature net',
        'short': 'cTAZ',
        'rpc': rpc,
        'unit': 'ctaz',
        'has_bft': True,
        'data_suffix': '',
        'path_prefix': '',
    },
    'zcash': {
        'id': 'zcash-mainnet',
        'label': 'zcash mainnet',
        'short': 'ZEC',
        'rpc': rpc_zcash,
        'unit': 'zec',
        'has_bft': False,
        'data_suffix': '-zcash',
        'path_prefix': '/z',
    },
}
templates.env.globals['CHAINS'] = CHAINS

OPERATOR_FINALIZER_PUBKEY = 'bb93fde13cfc03f430af8d03f9114f711897170c18192a3524e48251d8f77e64'

POOL_META = {
    'orchard': {'label': 'orchard', 'kind': 'shielded', 'class': 'pool-orchard'},
    'sapling': {'label': 'sapling', 'kind': 'shielded', 'class': 'pool-sapling'},
    'sprout': {'label': 'sprout', 'kind': 'shielded', 'class': 'pool-sapling'},
    'transparent': {'label': 'transparent', 'kind': 'transparent', 'class': 'pool-transparent'},
    'lockbox': {'label': 'lockbox', 'kind': 'lockbox', 'class': 'pool-transparent'},
}
POOL_HISTORY_WINDOW = 200

import time as _time

_pool_cache = {}
_POOL_CACHE_TTL = 30
_POOL_CACHE_BUCKET_SIZE = 10
_POOL_HISTORY_RPC_CONCURRENCY = 20

async def cached_pool_history(pool_id: str, tip: int, chain_key: str = 'ctaz'):
    now = _time.time()
    tip_bucket = tip // _POOL_CACHE_BUCKET_SIZE
    for bucket in (tip_bucket, tip_bucket - 1):
        if bucket < 0:
            continue
        key = (pool_id, bucket, chain_key)
        entry = _pool_cache.get(key)
        if not entry:
            continue
        if (now - entry['cached_at']) >= _POOL_CACHE_TTL:
            _pool_cache.pop(key, None)
            continue
        if 0 <= (tip - entry['tip']) <= _POOL_CACHE_BUCKET_SIZE:
            return entry['series']
    series = await fetch_pool_history_chain(pool_id, tip, chain_key)
    _pool_cache[(pool_id, tip_bucket, chain_key)] = {
        'cached_at': now,
        'tip': tip,
        'series': series,
    }
    return series


async def fetch_pool_history_chain(pool_id: str, tip: int, chain_key: str, window: int = POOL_HISTORY_WINDOW):
    start = max(0, tip - window + 1)
    heights = list(range(start, tip + 1))
    rpc_call = safe_call if chain_key == 'ctaz' else lambda method, params=None: safe_call_on(chain_key, method, params)
    blocks = await fetch_blocks_by_height_range(
        heights,
        rpc_call,
    )
    return build_pool_history_series(pool_id, heights, blocks)


async def fetch_blocks_by_height_range(heights, rpc_call):
    sem = asyncio.Semaphore(_POOL_HISTORY_RPC_CONCURRENCY)

    async def fetch_hash(height):
        async with sem:
            return await rpc_call('getblockhash', [height])

    block_hashes = await asyncio.gather(*(fetch_hash(height) for height in heights))

    async def fetch_block(block_hash):
        if not block_hash:
            return None
        async with sem:
            return await rpc_call('getblock', [block_hash, 1])

    return await asyncio.gather(*(fetch_block(block_hash) for block_hash in block_hashes))


def build_pool_history_series(pool_id: str, heights, blocks):
    series = []
    for h, block in zip(heights, blocks):
        if not block:
            continue
        pools = {p['id']: p for p in block.get('valuePools', [])}
        pool = pools.get(pool_id)
        if not pool:
            continue
        series.append({
            'height': h,
            'time': block.get('time'),
            'value_zat': int(pool.get('chainValueZat', 0) or 0),
            'delta_zat': int(pool.get('valueDeltaZat', 0) or 0),
            'tx_count': len(block.get('tx', [])),
        })
    return series


DATA_DIR = pathlib.Path(os.environ.get('CTAZ_DATA_DIR', 'data'))


def load_registry(name: str):
    path = DATA_DIR / name
    try:
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        return []


ZAP1_EVENT_LABELS = {
    '01': 'program entry',
    '02': 'ownership attest',
    '03': 'contract anchor',
    '04': 'deployment',
    '05': 'hosting payment',
    '06': 'shield renewal',
    '07': 'transfer',
    '08': 'exit',
    '09': 'merkle root',
    '0a': 'staking deposit',
    '0b': 'staking withdraw',
    '0c': 'staking reward',
    '0d': 'governance proposal',
    '0e': 'governance vote',
    '0f': 'governance result',
    '40': 'agent register',
    '41': 'agent policy',
    '42': 'agent action',
}


def lookup_zap1_anchor(txid: str):
    registry = load_registry('zap1-anchors.json')
    for entry in registry:
        if entry.get('txid', '').lower() == txid.lower() and entry.get('network') == 'ctaz-s1':
            return entry
    return None


def lookup_vault(pubkey: str):
    registry = load_registry('vaults.json')
    for entry in registry:
        if entry.get('pubkey', '').lower() == pubkey.lower():
            return entry
    return None


def lookup_zeven_event(txid: str):
    registry = load_registry('zeven-events.json')
    for entry in registry:
        if entry.get('txid', '').lower() == txid.lower() and entry.get('network') == 'ctaz-s1':
            return entry
    return None


def humanize_ts(ts):
    try:
        import time
        delta = int(time.time()) - int(ts)
        if delta < 60: return f'{delta}s ago'
        if delta < 3600: return f'{delta // 60}m ago'
        if delta < 86400: return f'{delta // 3600}h ago'
        return f'{delta // 86400}d ago'
    except Exception:
        return str(ts)


def zats_to_ctaz(zats):
    try:
        return f'{int(zats) / 1e8:.4f}'
    except Exception:
        return '0'


def short_hash(h, n=16):
    if not h:
        return ''
    s = str(h)
    if len(s) <= n:
        return s
    return s[:n] + '…'


def bytes_to_hex(b):
    if isinstance(b, list):
        return ''.join(f'{x:02x}' for x in b)
    return str(b) if b else ''


def human_size(n):
    try:
        n = int(n)
        for unit in ['B', 'KB', 'MB']:
            if n < 1024:
                return f'{n:.0f}{unit}' if unit == 'B' else f'{n:.1f}{unit}'
            n /= 1024
        return f'{n:.1f}GB'
    except Exception:
        return '—'


templates.env.filters['ctaz'] = zats_to_ctaz
templates.env.filters['short'] = short_hash
templates.env.filters['ago'] = humanize_ts
templates.env.filters['hexbytes'] = bytes_to_hex
templates.env.filters['hsize'] = human_size


def fmt_amount(x, places=2):
    try:
        return f'{float(x):,.{places}f}'
    except Exception:
        return str(x)


def iso_time(ts):
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    except Exception:
        return str(ts)


def reverse_hex(h):
    try:
        return bytes.fromhex(str(h))[::-1].hex()
    except Exception:
        return str(h)


templates.env.filters['amt'] = fmt_amount
templates.env.filters['iso'] = iso_time
templates.env.filters['revhex'] = reverse_hex

from finalizer_participation import get_tracker


@app.on_event('startup')
async def _start_participation_tracker():
    await get_tracker().start()
templates.env.globals['operator_pubkey'] = OPERATOR_FINALIZER_PUBKEY


async def safe_call(method, params=None):
    try:
        return await rpc.call(method, params)
    except Exception:
        return None





STAKING_CYCLE_BLOCKS = 150
STAKING_WINDOW_BLOCKS = 70


def staking_day_state(tip: int):
    """per workshop faq: 70-block window out of every 150 pow blocks.
    assumes cycle starts at block 0. offset may shift; label as approximate.
    """
    try:
        t = int(tip)
    except Exception:
        return None
    cycle_pos = t % STAKING_CYCLE_BLOCKS
    in_window = cycle_pos < STAKING_WINDOW_BLOCKS
    return {
        'live': in_window,
        'cycle_pos': cycle_pos,
        'cycle_size': STAKING_CYCLE_BLOCKS,
        'window_size': STAKING_WINDOW_BLOCKS,
        'blocks_remaining': STAKING_WINDOW_BLOCKS - cycle_pos if in_window else None,
        'next_in': None if in_window else STAKING_CYCLE_BLOCKS - cycle_pos,
    }


def load_finalizer_labels():
    """Load opt-in community labels for finalizer pub_keys in node-log byte order."""
    try:
        with open('/root/ctaz-explorer/data/finalizer-labels.json') as f:
            return json.load(f)
    except Exception:
        return {}


async def get_bft_chain_tip():
    import struct
    fp = await safe_call('get_tfl_fat_pointer_to_bft_chain_tip')
    if not fp:
        return None
    vote = fp.get('vote_for_block_without_finalizer_public_key') or []
    if len(vote) < 44:
        return None
    try:
        pos_height = struct.unpack('<Q', bytes(vote[32:40]))[0]
    except Exception:
        pos_height = None
    signer_count = len(fp.get('signatures') or [])
    return {
        'pos_height': pos_height,
        'signer_count': signer_count,
    }


async def safe_call_on(chain_key: str, method, params=None):
    chain = CHAINS.get(chain_key)
    if not chain:
        return None
    try:
        return await chain['rpc'].call(method, params)
    except Exception:
        return None


def load_chain_registry(name: str, chain_key: str):
    suffix = CHAINS.get(chain_key, {}).get('data_suffix', '')
    base = name.replace('.json', '')
    path = DATA_DIR / f'{base}{suffix}.json'
    try:
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        return []


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {'0', 'false', 'no', 'off'}


def zap1_live_enabled() -> bool:
    return env_flag('ZAP1_LIVE_API', True)


async def load_zcash_zap1_anchors():
    if zap1_live_enabled():
        try:
            return await fetch_zap1_live_anchors()
        except Exception as exc:
            logger.warning('falling back to static zcash ZAP1 anchors: %s', exc)
    return load_chain_registry('zap1-anchors.json', 'zcash')


async def load_zcash_zap1_leaves():
    if zap1_live_enabled():
        try:
            return await fetch_zap1_live_leaves()
        except Exception as exc:
            logger.warning('falling back to static zcash ZAP1 leaves: %s', exc)
    return load_chain_registry('zap1-leaves.json', 'zcash')


async def load_zcash_zap1_leaf_count() -> int:
    if zap1_live_enabled():
        try:
            status = await fetch_zap1_live_status()
            leaf_count = status.get('leaf_count')
            if leaf_count is not None:
                return int(leaf_count)
        except Exception as exc:
            logger.warning('falling back to derived zcash ZAP1 leaf count: %s', exc)
    return len(await load_zcash_zap1_leaves())


async def load_zcash_zap1_leaf(leaf_hash: str):
    leaves = await load_zcash_zap1_leaves()
    for leaf in leaves:
        if leaf.get('leaf_hash', '').lower() == leaf_hash.lower():
            return leaf
    if zap1_live_enabled():
        try:
            return await fetch_zap1_live_leaf_by_hash(leaf_hash)
        except Exception as exc:
            logger.warning('zcash ZAP1 leaf lookup failed for %s: %s', leaf_hash, exc)
    return None


VERIFY_NOT_FOUND_MESSAGE = 'no ZAP1 attestation found for this txid on zcash-mainnet or ctaz-s1'
VERIFY_SERVICE_ERROR_MESSAGE = 'unable to reach the ZAP1 attestation service right now'


def normalize_zap1_anchor_record(anchor: dict) -> dict:
    anchors = anchor.get('anchors') or {}
    mainnet = anchors.get('mainnet') or {}
    ctaz = anchors.get('ctaz') or {}

    mainnet_height = mainnet.get('height')
    if mainnet_height is None:
        mainnet_height = anchor.get('height')
    if mainnet_height is None:
        mainnet_height = anchor.get('block_height')

    ctaz_height = ctaz.get('height')
    if ctaz_height is None:
        ctaz_height = anchor.get('height_ctaz')
    if ctaz_height is None:
        ctaz_height = anchor.get('ctaz_height')
    if ctaz_height is None:
        ctaz_height = anchor.get('anchor_height_ctaz')

    return {
        'root': anchor.get('root') or anchor.get('payload_hash'),
        'leaf_count': anchor.get('leaf_count'),
        'created_at': anchor.get('created_at'),
        'mainnet': {
            'txid': mainnet.get('txid') or anchor.get('txid'),
            'height': mainnet_height,
        },
        'ctaz': {
            'txid': (
                ctaz.get('txid')
                or anchor.get('txid_ctaz')
                or anchor.get('ctaz_txid')
                or anchor.get('anchor_txid_ctaz')
            ),
            'height': ctaz_height,
        },
    }


def lookup_ctaz_cert_for_height(height: int) -> dict | None:
    tracker = get_tracker()
    matches = []
    for cert_id, cert in tracker.certs_seen.items():
        try:
            pos_height = int(cert.get('pos_height'))
            finalized_pow_height = int(cert.get('finalized_pow_height'))
            signer_count = int(cert.get('signer_count') or 0)
        except (TypeError, ValueError):
            continue
        if finalized_pow_height < int(height):
            continue
        matches.append((pos_height, cert_id, signer_count, finalized_pow_height))
    if not matches:
        return None
    pos_height, cert_id, signer_count, finalized_pow_height = min(matches, key=lambda item: item[0])
    return {
        'id': cert_id,
        'pos_height': pos_height,
        'signer_count': signer_count,
        'finalized_pow_height': finalized_pow_height,
    }


def block_href_for_network(network_label: str, height) -> str | None:
    if height is None:
        return None
    prefix = '/block' if network_label == 'ctaz-s1' else '/z/block'
    return f'{prefix}/{height}'


def build_verify_examples(anchors: list[dict], limit: int = 3) -> list[dict]:
    records = [normalize_zap1_anchor_record(anchor) for anchor in anchors]
    records.sort(
        key=lambda record: max(
            int(record['ctaz']['height'] or 0),
            int(record['mainnet']['height'] or 0),
        ),
        reverse=True,
    )
    examples = []
    seen = set()
    for record in records:
        for network_label, chain_key in (('ctaz-s1', 'ctaz'), ('zcash-mainnet', 'mainnet')):
            txid = record[chain_key].get('txid')
            height = record[chain_key].get('height')
            if not txid or txid in seen:
                continue
            examples.append({
                'txid': txid,
                'network': network_label,
                'height': height,
                'block_href': block_href_for_network(network_label, height),
            })
            seen.add(txid)
            break
        if len(examples) >= limit:
            break
    return examples


async def load_verify_examples(limit: int = 3) -> list[dict]:
    try:
        live_anchors = await fetch_zap1_live_anchors()
        examples = build_verify_examples(live_anchors, limit=limit)
        if examples:
            return examples
    except Exception as exc:
        logger.warning('ZAP1 verify examples falling back to static data: %s', exc)
    return build_verify_examples(load_chain_registry('zap1-anchors.json', 'zcash'), limit=limit)


async def lookup_live_zap1_attestation(txid: str) -> dict | None:
    for anchor in await fetch_zap1_live_anchors():
        record = normalize_zap1_anchor_record(anchor)
        matched_network = None
        if (record['ctaz'].get('txid') or '').lower() == txid:
            matched_network = 'ctaz-s1'
        elif (record['mainnet'].get('txid') or '').lower() == txid:
            matched_network = 'zcash-mainnet'
        if matched_network is None:
            continue

        primary_network = 'ctaz-s1' if record['ctaz'].get('height') is not None else 'zcash-mainnet'
        primary_height = record['ctaz'].get('height') if primary_network == 'ctaz-s1' else record['mainnet'].get('height')
        ctaz_cert = None
        if record['ctaz'].get('height') is not None:
            try:
                ctaz_cert = lookup_ctaz_cert_for_height(int(record['ctaz']['height']))
            except (TypeError, ValueError):
                ctaz_cert = None

        if primary_network == 'ctaz-s1' and ctaz_cert:
            summary = (
                f'attested at block {primary_height}, cert {ctaz_cert["id"]}, '
                f'signed by {ctaz_cert["signer_count"]} finalizers'
            )
        elif primary_network == 'ctaz-s1' and primary_height is not None:
            summary = f'attested on ctaz-s1 at block {primary_height}'
        elif primary_height is not None:
            summary = f'attested on zcash-mainnet at block {primary_height}'
        else:
            summary = f'attested on {primary_network}'

        return {
            'query_txid': txid,
            'matched_network': matched_network,
            'primary_network': primary_network,
            'summary': summary,
            'block_height': primary_height,
            'block_href': block_href_for_network(primary_network, primary_height),
            'root': record.get('root'),
            'leaf_count': record.get('leaf_count'),
            'created_at': record.get('created_at'),
            'mainnet': {
                'txid': record['mainnet'].get('txid'),
                'height': record['mainnet'].get('height'),
                'block_href': block_href_for_network('zcash-mainnet', record['mainnet'].get('height')),
            },
            'ctaz': {
                'txid': record['ctaz'].get('txid'),
                'height': record['ctaz'].get('height'),
                'block_href': block_href_for_network('ctaz-s1', record['ctaz'].get('height')),
            },
            'ctaz_cert': ctaz_cert,
        }
    return None


def route_chain_key(path: str):
    if path in {'/snapshots', '/api/snapshots'}:
        return None
    if path == '/z' or path == '/tip.z' or path.startswith('/z/') or path.startswith('/api/z/'):
        return 'zcash'
    if path in {
        '/',
        '/search',
        '/verify',
        '/why',
        '/params',
        '/stake',
        '/anchors',
        '/vaults',
        '/events',
        '/finalizers',
        '/health',
        '/tip',
        '/finalized',
        '/gap',
        '/feed.xml',
        '/.well-known/explorer',
    }:
        return 'ctaz'
    if path.startswith(('/api/', '/block/', '/tx/', '/address/', '/pool/', '/finalizers/')):
        return 'ctaz'
    return None


def error_default_message(status_code: int):
    defaults = {
        404: 'page not found',
        500: 'internal server error',
        503: 'upstream node not ready',
    }
    return defaults.get(status_code, 'something went wrong')


def make_error_response(request: Request, status_code: int, message: str | None = None):
    message = message or error_default_message(status_code)
    if request.url.path.startswith('/api/'):
        return JSONResponse(status_code=status_code, content={'error': message})
    template_name = {
        404: '404.html',
        503: '503.html',
    }.get(status_code, 'error.html')
    return templates.TemplateResponse(request, template_name, {
        'request': request,
        'status_code': status_code,
        'message': message,
    }, status_code=status_code)


async def inspect_chain_backend(chain_key: str):
    label = CHAINS[chain_key]['label']
    chaininfo = await safe_call_on(chain_key, 'getblockchaininfo')
    if not chaininfo:
        message = f'{label} routes disabled: backend check failed'
        logger.error('%s rpc startup check failed: no getblockchaininfo result', chain_key)
        return {'ok': False, 'message': message}
    observed_chain = str(chaininfo.get('chain') or '').lower()
    observed_genesis = await safe_call_on(chain_key, 'getblockhash', [0])
    expected_chain = EXPECTED_CHAIN_IDS[chain_key]
    expected_genesis = EXPECTED_GENESIS_HASHES[chain_key]
    chain_ok = observed_chain == expected_chain
    genesis_ok = bool(observed_genesis) and observed_genesis.lower() == expected_genesis
    if chain_ok or genesis_ok:
        if not chain_ok and genesis_ok:
            logger.warning(
                '%s rpc reported chain=%s but genesis matched %s, accepting routes',
                chain_key,
                observed_chain or 'unknown',
                expected_genesis,
            )
        else:
            logger.info('%s rpc startup check passed with chain=%s', chain_key, observed_chain or 'unknown')
        return {'ok': True, 'message': ''}
    message = f'{label} routes disabled: expected {expected_chain}, got {observed_chain or "unknown"}'
    logger.error(
        '%s rpc startup check failed: expected chain=%s genesis=%s, got chain=%s genesis=%s',
        chain_key,
        expected_chain,
        expected_genesis,
        observed_chain or 'unknown',
        observed_genesis or 'unknown',
    )
    return {'ok': False, 'message': message}


async def refresh_chain_route_status():
    ctaz_status, zcash_status = await asyncio.gather(
        inspect_chain_backend('ctaz'),
        inspect_chain_backend('zcash'),
    )
    CHAIN_ROUTE_STATUS['ctaz'] = ctaz_status
    CHAIN_ROUTE_STATUS['zcash'] = zcash_status


@app.on_event('startup')
async def verify_chain_backends():
    await refresh_chain_route_status()


@app.on_event('shutdown')
async def shutdown_clients():
    await asyncio.gather(
        rpc.close(),
        rpc_zcash.close(),
        close_zap1_live(),
        return_exceptions=True,
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error('unhandled exception on %s', request.url.path, exc_info=(type(exc), exc, exc.__traceback__))
    try:
        return make_error_response(request, 500, 'internal server error')
    except Exception:
        return JSONResponse(status_code=500, content={'error': 'internal server error'})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    try:
        return make_error_response(request, exc.status_code, exc.detail or error_default_message(exc.status_code))
    except Exception:
        return JSONResponse(status_code=exc.status_code, content={'error': exc.detail})


async def fetch_recent_block_with_finality(h):
    h_hash = await safe_call('getblockhash', [h])
    if not h_hash:
        return None
    block, finality = await asyncio.gather(
        safe_call('getblock', [h_hash, 1]),
        safe_call('get_tfl_block_finality_from_hash', [h_hash]),
    )
    if not block:
        return None
    return {
        'height': h,
        'hash': h_hash,
        'tx_count': len(block.get('tx', [])),
        'time': block.get('time'),
        'finality': finality or 'Unknown',
    }


def finality_class(finality):
    if finality == 'Finalized':
        return 'finalized'
    if finality == 'NotYetFinalized':
        return 'notyet'
    return 'unknown'


def finality_label(finality):
    if finality == 'Finalized':
        return 'bft finalized'
    if finality == 'NotYetFinalized':
        return 'pow only'
    return 'unknown'


templates.env.filters['fincls'] = finality_class
templates.env.filters['finlbl'] = finality_label


def is_hex64(value: str):
    return len(value) == 64 and all(c in '0123456789abcdef' for c in value.lower())


def tx_value_flow(tx):
    transparent_out_zat = 0
    for v in tx.get('vout', []) or []:
        try:
            transparent_out_zat += int(v.get('valueZat', 0))
        except Exception:
            pass
    orchard = tx.get('orchard') or {}
    orchard_balance_zat = int(orchard.get('valueBalanceZat', 0) or 0)
    actions = len(orchard.get('actions', []) or [])
    sapling_spend = len(tx.get('vShieldedSpend', []) or [])
    sapling_output = len(tx.get('vShieldedOutput', []) or [])
    is_coinbase = bool(tx.get('vin') and tx['vin'][0].get('coinbase'))
    return {
        'transparent_out_zat': transparent_out_zat,
        'orchard_balance_zat': orchard_balance_zat,
        'orchard_actions': actions,
        'sapling_spend': sapling_spend,
        'sapling_output': sapling_output,
        'is_coinbase': is_coinbase,
        'is_shielded': actions > 0 or sapling_spend > 0 or sapling_output > 0,
    }


@app.get('/')
async def home(request: Request, order: str = 'asc'):
    info, chaininfo, roster, final_hh, bft = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('getblockchaininfo'),
        safe_call('get_tfl_roster_zats'),
        safe_call('get_tfl_final_block_height_and_hash'),
        get_bft_chain_tip(),
    )
    if not info or not chaininfo:
        raise HTTPException(status_code=503, detail="node not ready")
    roster = roster or []
    tip = info['blocks']
    recent_raw = await asyncio.gather(*[fetch_recent_block_with_finality(h) for h in range(max(0, tip - 9), tip + 1)])
    recent = [b for b in recent_raw if b is not None]
    total_vp = sum(int(m.get('voting_power', 0)) for m in roster)
    pools = {p['id']: p for p in chaininfo.get('valuePools', [])}
    orchard = pools.get('orchard', {}).get('chainValue', 0)
    transparent = pools.get('transparent', {}).get('chainValue', 0)
    finalized_height = final_hh.get('height') if final_hh and isinstance(final_hh, dict) else None
    finality_gap = (tip - finalized_height) if finalized_height is not None else None
    anchors = [e for e in load_registry('zap1-anchors.json') if e.get('network') == 'ctaz-s1']
    vaults = [e for e in load_registry('vaults.json') if e.get('network') == 'ctaz-s1']
    events = [e for e in load_registry('zeven-events.json') if e.get('network') == 'ctaz-s1']
    return templates.TemplateResponse(request, 'home.html', {
        'request': request,
        'tip': tip,
        'connections': info.get('connections', 0),
        'chaininfo': chaininfo,
        'roster': roster,
        'total_vp': total_vp,
        'finalized': final_hh,
        'finalized_height': finalized_height,
        'finality_gap': finality_gap,
        'orchard': orchard,
        'transparent': transparent,
        'recent': recent if order == 'asc' else list(reversed(recent)),
        'order': order,
        'anchor_count': len(anchors),
        'vault_count': len(vaults),
        'event_count': len(events),
        'auto_refresh_s': 30,
        'bft': bft,
        'pow_pos_map': get_tracker().get_pow_to_pos_map([b['height'] for b in recent]),
        'staking': staking_day_state(tip),
        'leet_capture': get_tracker().get_recent_leet_capture(),
        'labels': load_finalizer_labels(),
    })


@app.get('/block/{hash_or_height}')
async def block_view(request: Request, hash_or_height: str):
    if hash_or_height.isdigit():
        h_hash = await safe_call('getblockhash', [int(hash_or_height)])
    else:
        if not is_hex64(hash_or_height):
            raise HTTPException(status_code=404, detail='block hash must be 64 hex characters or a decimal height')
        h_hash = hash_or_height
    if not h_hash:
        raise HTTPException(status_code=404, detail='block not found')
    block = await safe_call('getblock', [h_hash, 1])
    if not block:
        raise HTTPException(status_code=404, detail='block not found')
    finality = await safe_call('get_tfl_block_finality_from_hash', [h_hash])
    height = block.get('height')
    next_hash = None
    if height is not None:
        next_hash = await safe_call('getblockhash', [height + 1])
    pool_deltas = []
    for p in block.get('valuePools', []) or []:
        delta = int(p.get('valueDeltaZat', 0) or 0)
        if delta != 0 or p.get('monitored'):
            pool_deltas.append({
                'id': p.get('id'),
                'delta_zat': delta,
                'total_zat': int(p.get('chainValueZat', 0) or 0),
            })
    return templates.TemplateResponse(request, 'block.html', {
        'request': request,
        'block': block,
        'finality': finality or 'Unknown',
        'next_hash': next_hash,
        'pool_deltas': pool_deltas,
    })


@app.get('/tx/{txid}')
async def tx_view(request: Request, txid: str):
    if not is_hex64(txid):
        raise HTTPException(status_code=404, detail='txid must be 64 hex characters')
    tx = await safe_call('getrawtransaction', [txid, 1])
    if not tx:
        raise HTTPException(status_code=404, detail='transaction not found')
    finality = await safe_call('get_tfl_tx_finality_from_hash', [txid])
    flow = tx_value_flow(tx)
    zap1_anchor = lookup_zap1_anchor(txid)
    if zap1_anchor:
        zap1_anchor = dict(zap1_anchor)
        zap1_anchor['event_label'] = ZAP1_EVENT_LABELS.get(zap1_anchor.get('event_type', '').lower(), 'unknown event')
    zeven_event = lookup_zeven_event(txid)
    return templates.TemplateResponse(request, 'tx.html', {
        'request': request,
        'tx': tx,
        'txid': txid,
        'finality': finality or 'Unknown',
        'flow': flow,
        'zap1_anchor': zap1_anchor,
        'zeven_event': zeven_event,
    })


@app.get('/address/{addr}')
async def address_view(request: Request, addr: str):
    balance = await safe_call('getaddressbalance', [{'addresses': [addr]}])
    if balance is None:
        raise HTTPException(status_code=400, detail='invalid address')
    txids = await safe_call('getaddresstxids', [{'addresses': [addr]}]) or []
    return templates.TemplateResponse(request, 'address.html', {
        'request': request,
        'addr': addr,
        'balance': balance,
        'txids': txids[-50:][::-1],
    })


@app.get('/finalizers')
async def finalizers_view(request: Request):
    roster, finalized = await asyncio.gather(
        safe_call('get_tfl_roster_zats'),
        safe_call('get_tfl_final_block_height_and_hash'),
    )
    roster = roster or []
    total_vp = sum(int(m.get('voting_power', 0)) for m in roster)
    roster_sorted = sorted(roster, key=lambda m: int(m.get('voting_power', 0)), reverse=True)
    return templates.TemplateResponse(request, 'finalizers.html', {
        'request': request,
        'roster': roster_sorted,
        'total_vp': total_vp,
        'finalized': finalized,
        'labels': load_finalizer_labels(),
    })


@app.get('/stake')
async def stake_view(request: Request):
    roster = await safe_call('get_tfl_roster_zats') or []
    in_roster = any(reverse_hex(m.get('pub_key', '')) == OPERATOR_FINALIZER_PUBKEY for m in roster)
    our_stake = 0
    for m in roster:
        if reverse_hex(m.get('pub_key', '')) == OPERATOR_FINALIZER_PUBKEY:
            our_stake = int(m.get('voting_power', 0))
            break
    info = await safe_call('getinfo')
    staking = staking_day_state(info['blocks']) if info else None
    return templates.TemplateResponse(request, 'stake.html', {
        'request': request,
        'pubkey': OPERATOR_FINALIZER_PUBKEY,
        'in_roster': in_roster,
        'our_stake': our_stake,
        'staking': staking,
    })


async def fetch_block_by_height(h):
    h_hash = await safe_call('getblockhash', [h])
    if not h_hash:
        return None
    return await safe_call('getblock', [h_hash, 1])


async def fetch_pool_history(pool_id: str, tip: int, window: int = POOL_HISTORY_WINDOW):
    start = max(0, tip - window + 1)
    heights = list(range(start, tip + 1))
    sem = asyncio.Semaphore(20)
    async def fetch(h):
        async with sem:
            return await fetch_block_by_height(h)
    blocks = await asyncio.gather(*[fetch(h) for h in heights])
    series = []
    for h, block in zip(heights, blocks):
        if not block:
            continue
        pools = {p['id']: p for p in block.get('valuePools', [])}
        pool = pools.get(pool_id)
        if not pool:
            continue
        series.append({
            'height': h,
            'time': block.get('time'),
            'value_zat': int(pool.get('chainValueZat', 0) or 0),
            'delta_zat': int(pool.get('valueDeltaZat', 0) or 0),
            'tx_count': len(block.get('tx', [])),
        })
    return series


def build_sparkline(values, width=600, height=80, stroke='#cf9b22'):
    if not values or len(values) < 2:
        return ''
    vmin = min(values)
    vmax = max(values)
    span = max(vmax - vmin, 1)
    n = len(values)
    points = []
    for i, v in enumerate(values):
        x = (i / (n - 1)) * width
        y = height - ((v - vmin) / span) * (height - 8) - 4
        points.append(f'{x:.1f},{y:.1f}')
    poly = ' '.join(points)
    return (
        f'<svg class="sparkline" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="none" aria-label="cumulative pool value">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.8" points="{poly}" stroke-linejoin="round" stroke-linecap="round"/>'
        f'</svg>'
    )


@app.get('/pool/{pool_id}')
async def pool_view(request: Request, pool_id: str):
    if pool_id not in POOL_META:
        raise HTTPException(status_code=404, detail='unknown pool')
    info, chaininfo = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('getblockchaininfo'),
    )
    if not info:
        raise HTTPException(status_code=503, detail='node not ready')
    tip = info.get('blocks', 0)
    series = await fetch_pool_history(pool_id, tip)
    values_zat = [s['value_zat'] for s in series]
    sparkline_svg = build_sparkline(values_zat)
    chain_pool = {}
    for p in (chaininfo or {}).get('valuePools', []) or []:
        if p.get('id') == pool_id:
            chain_pool = p
            break
    recent_nonzero = [s for s in reversed(series) if s['delta_zat'] != 0][:20]
    window_delta = values_zat[-1] - values_zat[0] if len(values_zat) >= 2 else 0
    return templates.TemplateResponse(request, 'pool.html', {
        'request': request,
        'pool_id': pool_id,
        'pool_meta': POOL_META[pool_id],
        'tip': tip,
        'series_len': len(series),
        'current_zat': int(chain_pool.get('chainValueZat', 0) or 0),
        'chain_value': chain_pool.get('chainValue'),
        'monitored': chain_pool.get('monitored', False),
        'window_delta_zat': window_delta,
        'sparkline_svg': sparkline_svg,
        'recent_nonzero': recent_nonzero,
        'window': POOL_HISTORY_WINDOW,
    })


@app.get('/api/pool/{pool_id}')
async def api_pool(pool_id: str):
    if pool_id not in POOL_META:
        return JSONResponse(status_code=404, content={'error': 'unknown pool'})
    info = await safe_call('getinfo')
    if not info:
        return JSONResponse(status_code=503, content={'error': 'node not ready'})
    tip = info.get('blocks', 0)
    series = await fetch_pool_history(pool_id, tip)
    return {
        'pool': pool_id,
        'kind': POOL_META[pool_id]['kind'],
        'tip': tip,
        'window': POOL_HISTORY_WINDOW,
        'series': series,
    }


@app.get('/why')
async def why_view(request: Request):
    return templates.TemplateResponse(request, 'why.html', {'request': request})


@app.get('/params')
async def params_view(request: Request):
    info, chaininfo, final_hh, roster = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('getblockchaininfo'),
        safe_call('get_tfl_final_block_height_and_hash'),
        safe_call('get_tfl_roster_zats'),
    )
    return templates.TemplateResponse(request, 'params.html', {
        'request': request,
        'info': info or {},
        'chaininfo': chaininfo or {},
        'finalized': final_hh,
        'roster_len': len(roster or []),
    })


@app.get('/anchors')
async def anchors_view(request: Request):
    registry = [e for e in load_registry('zap1-anchors.json') if e.get('network') == 'ctaz-s1']
    for e in registry:
        e['event_label'] = ZAP1_EVENT_LABELS.get(e.get('event_type', '').lower(), 'unknown event')
    registry.sort(key=lambda e: int(e.get('block_height') or 0), reverse=True)
    return templates.TemplateResponse(request, 'anchors.html', {
        'request': request,
        'anchors': registry,
        'event_labels': ZAP1_EVENT_LABELS,
    })


@app.get('/api/anchors')
async def api_anchors():
    registry = [e for e in load_registry('zap1-anchors.json') if e.get('network') == 'ctaz-s1']
    registry.sort(key=lambda e: int(e.get('block_height') or 0), reverse=True)
    return {
        'protocol': 'ZAP1',
        'network': 'ctaz-s1',
        'count': len(registry),
        'event_types_supported': ZAP1_EVENT_LABELS,
        'anchors': registry,
    }


@app.get('/vaults')
async def vaults_view(request: Request):
    registry = [e for e in load_registry('vaults.json') if e.get('network') == 'ctaz-s1']
    return templates.TemplateResponse(request, 'vaults.html', {
        'request': request,
        'vaults': registry,
    })


@app.get('/api/vaults')
async def api_vaults():
    registry = [e for e in load_registry('vaults.json') if e.get('network') == 'ctaz-s1']
    return {'network': 'ctaz-s1', 'count': len(registry), 'vaults': registry}


@app.get('/events')
async def events_view(request: Request):
    registry = [e for e in load_registry('zeven-events.json') if e.get('network') == 'ctaz-s1']
    registry.sort(key=lambda e: int(e.get('block_height') or 0), reverse=True)
    return templates.TemplateResponse(request, 'events.html', {
        'request': request,
        'events': registry,
    })


@app.get('/api/events')
async def api_events():
    registry = [e for e in load_registry('zeven-events.json') if e.get('network') == 'ctaz-s1']
    registry.sort(key=lambda e: int(e.get('block_height') or 0), reverse=True)
    return {'network': 'ctaz-s1', 'count': len(registry), 'events': registry}


async def build_verification(txid: str):
    tx, finality = await asyncio.gather(
        safe_call('getrawtransaction', [txid, 1]),
        safe_call('get_tfl_tx_finality_from_hash', [txid]),
    )
    if not tx:
        return None
    block_finality = None
    if tx.get('blockhash'):
        block_finality = await safe_call('get_tfl_block_finality_from_hash', [tx['blockhash']])
    flow = tx_value_flow(tx)
    zap1 = lookup_zap1_anchor(txid)
    if zap1:
        zap1 = dict(zap1)
        zap1['event_label'] = ZAP1_EVENT_LABELS.get(zap1.get('event_type', '').lower(), 'unknown event')
    zeven = lookup_zeven_event(txid)
    return {
        'txid': txid,
        'found': True,
        'block': {
            'hash': tx.get('blockhash'),
            'height': tx.get('height'),
            'time': tx.get('time'),
            'confirmations': tx.get('confirmations'),
        },
        'finality': {
            'tx': finality or 'Unknown',
            'block': block_finality or 'Unknown',
        },
        'flow': flow,
        'zap1_anchor': zap1,
        'vault': None,
        'vault_status': 'stub',
        'vault_message': 'tx-addressable vault matching is not implemented yet',
        'zeven_event': zeven,
        'size': tx.get('size'),
        'version': tx.get('version'),
    }


@app.get('/verify')
async def verify_view(request: Request, q: str = ''):
    q = q.strip()
    attestation = None
    not_found = None
    error = None
    examples = await load_verify_examples()
    if q:
        q = q.lower()
        if not is_hex64(q):
            error = 'txid must be 64 hex characters'
        else:
            try:
                attestation = await lookup_live_zap1_attestation(q)
            except Exception as exc:
                logger.warning('ZAP1 verify lookup failed for %s: %s', q, exc)
                error = VERIFY_SERVICE_ERROR_MESSAGE
            if attestation is None and error is None:
                not_found = VERIFY_NOT_FOUND_MESSAGE
    return templates.TemplateResponse(request, 'verify.html', {
        'request': request,
        'q': q,
        'attestation': attestation,
        'examples': examples,
        'error': error,
        'not_found': not_found,
    })


@app.get('/api/verify/{txid}')
async def api_verify(txid: str):
    if not is_hex64(txid):
        return JSONResponse(status_code=400, content={'error': 'txid must be 64 hex characters'})
    result = await build_verification(txid)
    if result is None:
        return JSONResponse(status_code=404, content={'txid': txid, 'found': False})
    return result


@app.get('/search')
async def search(request: Request, q: str = ''):
    q = q.strip()
    if not q:
        return RedirectResponse(url='/')
    if q.isdigit():
        return RedirectResponse(url=f'/block/{q}')
    is_hex64 = len(q) == 64 and all(c in '0123456789abcdef' for c in q.lower())
    if is_hex64:
        block = await safe_call('getblock', [q, 1])
        if block:
            return RedirectResponse(url=f'/block/{q}')
        return RedirectResponse(url=f'/tx/{q}')
    if q.startswith(('t1', 't2', 't3', 'tm', 'u1', 'utest', 'zs', 'ztestsapling')):
        return RedirectResponse(url=f'/address/{q}')
    return RedirectResponse(url='/')


@app.get('/z')
async def zcash_home(request: Request):
    info, chaininfo, anchors, leaf_count = await asyncio.gather(
        safe_call_on('zcash', 'getinfo'),
        safe_call_on('zcash', 'getblockchaininfo'),
        load_zcash_zap1_anchors(),
        load_zcash_zap1_leaf_count(),
    )
    if not info or not chaininfo:
        raise HTTPException(status_code=503, detail='zcash mainnet node not ready')
    tip = info['blocks']
    recent_heights = list(range(max(0, tip - 9), tip + 1))
    async def fetch_zcash_block(h):
        h_hash = await safe_call_on('zcash', 'getblockhash', [h])
        if not h_hash:
            return None
        block = await safe_call_on('zcash', 'getblock', [h_hash, 1])
        if not block:
            return None
        return {
            'height': h,
            'hash': h_hash,
            'tx_count': len(block.get('tx', [])),
            'time': block.get('time'),
        }
    recent_raw = await asyncio.gather(*[fetch_zcash_block(h) for h in recent_heights])
    recent = [b for b in recent_raw if b is not None]
    pools = {p['id']: p for p in chaininfo.get('valuePools', [])}
    orchard = pools.get('orchard', {}).get('chainValue', 0)
    transparent = pools.get('transparent', {}).get('chainValue', 0)
    sapling = pools.get('sapling', {}).get('chainValue', 0)
    lockbox = pools.get('lockbox', {}).get('chainValue', 0)
    anchors = [e for e in anchors if e.get('network') == 'zcash-mainnet']
    anchors_preview = sorted(
        anchors,
        key=lambda e: int(e.get('block_height') or 0),
        reverse=True,
    )[:5]
    return templates.TemplateResponse(request, 'zcash_home.html', {
        'request': request,
        'chain': CHAINS['zcash'],
        'tip': tip,
        'connections': info.get('connections', 0),
        'subversion': info.get('subversion', ''),
        'chaininfo': chaininfo,
        'orchard': orchard,
        'transparent': transparent,
        'sapling': sapling,
        'lockbox': lockbox,
        'recent': list(reversed(recent)),
        'leaf_count': leaf_count,
        'anchor_count': len(anchors),
        'anchors_preview': anchors_preview,
    })


@app.get('/z/block/{hash_or_height}')
async def zcash_block(request: Request, hash_or_height: str):
    if hash_or_height.isdigit():
        h_hash = await safe_call_on('zcash', 'getblockhash', [int(hash_or_height)])
    else:
        if not is_hex64(hash_or_height):
            raise HTTPException(status_code=404, detail='block hash must be 64 hex characters or a decimal height')
        h_hash = hash_or_height
    if not h_hash:
        raise HTTPException(status_code=404, detail='block not found on zcash mainnet')
    block = await safe_call_on('zcash', 'getblock', [h_hash, 1])
    if not block:
        raise HTTPException(status_code=404, detail='block not found on zcash mainnet')
    height = block.get('height')
    next_hash = None
    if height is not None:
        next_hash = await safe_call_on('zcash', 'getblockhash', [height + 1])
    pool_deltas = []
    for p in block.get('valuePools', []) or []:
        delta = int(p.get('valueDeltaZat', 0) or 0)
        if delta != 0 or p.get('monitored'):
            pool_deltas.append({
                'id': p.get('id'),
                'delta_zat': delta,
                'total_zat': int(p.get('chainValueZat', 0) or 0),
            })
    return templates.TemplateResponse(request, 'zcash_block.html', {
        'request': request,
        'chain': CHAINS['zcash'],
        'block': block,
        'next_hash': next_hash,
        'pool_deltas': pool_deltas,
    })


@app.get('/z/tx/{txid}')
async def zcash_tx(request: Request, txid: str):
    if not is_hex64(txid):
        raise HTTPException(status_code=404, detail='txid must be 64 hex characters')
    tx = await safe_call_on('zcash', 'getrawtransaction', [txid, 1])
    if not tx:
        raise HTTPException(status_code=404, detail='transaction not found on zcash mainnet')
    flow = tx_value_flow(tx)
    anchors = await load_zcash_zap1_anchors()
    zap1_anchor = None
    for entry in anchors:
        if entry.get('txid', '').lower() == txid.lower() and entry.get('network') == 'zcash-mainnet':
            zap1_anchor = dict(entry)
            zap1_anchor['event_label'] = ZAP1_EVENT_LABELS.get(zap1_anchor.get('event_type', '').lower(), 'unknown event')
            break
    return templates.TemplateResponse(request, 'zcash_tx.html', {
        'request': request,
        'chain': CHAINS['zcash'],
        'tx': tx,
        'txid': txid,
        'flow': flow,
        'zap1_anchor': zap1_anchor,
    })


@app.get('/z/anchors')
async def zcash_anchors(request: Request):
    registry = [e for e in await load_zcash_zap1_anchors() if e.get('network') == 'zcash-mainnet']
    for e in registry:
        e['event_label'] = ZAP1_EVENT_LABELS.get(e.get('event_type', '').lower(), 'unknown event')
    registry.sort(key=lambda e: int(e.get('block_height') or 0), reverse=True)
    return templates.TemplateResponse(request, 'zcash_anchors.html', {
        'request': request,
        'chain': CHAINS['zcash'],
        'anchors': registry,
        'event_labels': ZAP1_EVENT_LABELS,
    })


@app.get('/api/z/tip')
async def api_zcash_tip():
    info = await safe_call_on('zcash', 'getinfo')
    if not info:
        return JSONResponse(status_code=503, content={'error': 'zcash mainnet node not ready'})
    return {
        'chain': 'zcash-mainnet',
        'tip': info.get('blocks'),
        'subversion': info.get('subversion'),
        'connections': info.get('connections', 0),
    }


@app.get('/api/z/anchors')
async def api_zcash_anchors():
    registry = [e for e in await load_zcash_zap1_anchors() if e.get('network') == 'zcash-mainnet']
    registry.sort(key=lambda e: int(e.get('block_height') or 0), reverse=True)
    return {
        'protocol': 'ZAP1',
        'network': 'zcash-mainnet',
        'count': len(registry),
        'event_types_supported': ZAP1_EVENT_LABELS,
        'anchors': registry,
    }




@app.get('/z/pool/{pool_id}')
async def zcash_pool_view(request: Request, pool_id: str):
    if pool_id not in POOL_META:
        raise HTTPException(status_code=404, detail='unknown pool')
    info = await safe_call_on('zcash', 'getinfo')
    if not info:
        raise HTTPException(status_code=503, detail='zcash mainnet node not ready')
    tip = info.get('blocks', 0)
    series = await cached_pool_history(pool_id, tip, 'zcash')
    values_zat = [s['value_zat'] for s in series]
    sparkline_svg = build_sparkline(values_zat)
    chaininfo = await safe_call_on('zcash', 'getblockchaininfo')
    chain_pool = {}
    for p in (chaininfo or {}).get('valuePools', []) or []:
        if p.get('id') == pool_id:
            chain_pool = p
            break
    recent_nonzero = [s for s in reversed(series) if s['delta_zat'] != 0][:20]
    window_delta = values_zat[-1] - values_zat[0] if len(values_zat) >= 2 else 0
    return templates.TemplateResponse(request, 'zcash_pool.html', {
        'request': request,
        'chain': CHAINS['zcash'],
        'pool_id': pool_id,
        'pool_meta': POOL_META[pool_id],
        'tip': tip,
        'series_len': len(series),
        'current_zat': int(chain_pool.get('chainValueZat', 0) or 0),
        'chain_value': chain_pool.get('chainValue'),
        'monitored': chain_pool.get('monitored', False),
        'window_delta_zat': window_delta,
        'sparkline_svg': sparkline_svg,
        'recent_nonzero': recent_nonzero,
        'window': POOL_HISTORY_WINDOW,
    })


@app.get('/z/verify')
async def zcash_verify_view(request: Request, q: str = ''):
    q = q.strip()
    result = None
    error = None
    if q:
        if not is_hex64(q):
            error = 'txid must be 64 hex characters'
        else:
            tx = await safe_call_on('zcash', 'getrawtransaction', [q, 1])
            if not tx:
                error = 'transaction not found on zcash mainnet'
            else:
                flow = tx_value_flow(tx)
                anchors = await load_zcash_zap1_anchors()
                zap1 = None
                for entry in anchors:
                    if entry.get('txid', '').lower() == q.lower() and entry.get('network') == 'zcash-mainnet':
                        zap1 = dict(entry)
                        zap1['event_label'] = ZAP1_EVENT_LABELS.get(zap1.get('event_type', '').lower(), 'unknown event')
                        break
                result = {
                    'txid': q,
                    'found': True,
                    'block': {
                        'hash': tx.get('blockhash'),
                        'height': tx.get('height'),
                        'time': tx.get('time'),
                        'confirmations': tx.get('confirmations'),
                    },
                    'finality': {'tx': 'pow confirmations', 'block': 'pow confirmations'},
                    'flow': flow,
                    'zap1_anchor': zap1,
                    'vault': None,
                    'zeven_event': None,
                    'size': tx.get('size'),
                    'version': tx.get('version'),
                }
    return templates.TemplateResponse(request, 'zcash_verify.html', {
        'request': request,
        'chain': CHAINS['zcash'],
        'q': q,
        'result': result,
        'error': error,
    })


@app.get('/favicon.ico')
async def favicon():
    return RedirectResponse(url='/static/favicon.svg', status_code=301)


@app.get('/health')
async def health():
    info = await safe_call('getinfo')
    if not info:
        return JSONResponse(status_code=503, content={'ok': False})
    return {'ok': True, 'tip': info.get('blocks'), 'connections': info.get('connections')}


@app.get('/api/tip')
async def api_tip():
    info, final_hh = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('get_tfl_final_block_height_and_hash'),
    )
    if not info:
        return JSONResponse(status_code=503, content={'error': 'node not ready'})
    tip = info.get('blocks')
    finalized = final_hh.get('height') if final_hh and isinstance(final_hh, dict) else None
    return {
        'tip': tip,
        'finalized': finalized,
        'finality_gap': (tip - finalized) if finalized is not None else None,
        'connections': info.get('connections', 0),
    }


@app.get('/api/block/{hash_or_height}')
async def api_block(hash_or_height: str):
    if hash_or_height.isdigit():
        h_hash = await safe_call('getblockhash', [int(hash_or_height)])
    else:
        h_hash = hash_or_height
    if not h_hash:
        return JSONResponse(status_code=404, content={'error': 'not found'})
    block, finality = await asyncio.gather(
        safe_call('getblock', [h_hash, 1]),
        safe_call('get_tfl_block_finality_from_hash', [h_hash]),
    )
    if not block:
        return JSONResponse(status_code=404, content={'error': 'not found'})
    return {'block': block, 'finality': finality or 'Unknown'}


@app.get('/api/tx/{txid}')
async def api_tx(txid: str):
    tx, finality = await asyncio.gather(
        safe_call('getrawtransaction', [txid, 1]),
        safe_call('get_tfl_tx_finality_from_hash', [txid]),
    )
    if not tx:
        return JSONResponse(status_code=404, content={'error': 'not found'})
    return {'tx': tx, 'finality': finality or 'Unknown', 'flow': tx_value_flow(tx)}


@app.get('/api/finalizers')
async def api_finalizers():
    roster = await safe_call('get_tfl_roster_zats') or []
    roster_sorted = sorted(roster, key=lambda m: int(m.get('voting_power', 0)), reverse=True)
    total_vp = sum(int(m.get('voting_power', 0)) for m in roster_sorted)
    return {'count': len(roster_sorted), 'total_voting_power_zat': total_vp, 'roster': roster_sorted}


@app.get('/api/params')
async def api_params():
    info, chaininfo = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('getblockchaininfo'),
    )
    return {
        'chain': (chaininfo or {}).get('chain'),
        'network': 'crosslink s1 feature net',
        'protocol_version': (info or {}).get('protocolversion'),
        'zebra_version': (info or {}).get('subversion'),
        'tip': (info or {}).get('blocks'),
        'difficulty': (info or {}).get('difficulty'),
        'relay_fee': (info or {}).get('relayfee'),
    }


@app.get("/z/address/{addr}")
async def zcash_address(request: Request, addr: str):
    balance = await safe_call_on("zcash", "getaddressbalance", [{"addresses": [addr]}])
    if balance is None:
        raise HTTPException(status_code=400, detail="invalid address on zcash mainnet")
    txids = await safe_call_on("zcash", "getaddresstxids", [{"addresses": [addr]}]) or []
    return templates.TemplateResponse(request, "zcash_address.html", {
        "request": request,
        "chain": CHAINS["zcash"],
        "addr": addr,
        "balance": balance,
        "txids": txids[-50:][::-1],
    })


@app.get("/z/search")
async def zcash_search(request: Request, q: str = ""):
    q = q.strip()
    if not q:
        return RedirectResponse(url="/z")
    if q.isdigit():
        return RedirectResponse(url=f"/z/block/{q}")
    is_hex64 = len(q) == 64 and all(c in "0123456789abcdef" for c in q.lower())
    if is_hex64:
        block = await safe_call_on("zcash", "getblock", [q, 1])
        if block:
            return RedirectResponse(url=f"/z/block/{q}")
        return RedirectResponse(url=f"/z/tx/{q}")
    if q.startswith(("t1", "t3", "zs", "u1")):
        return RedirectResponse(url=f"/z/address/{q}")
    return RedirectResponse(url="/z")


@app.get("/api/z/block/{hash_or_height}")
async def api_zcash_block(hash_or_height: str):
    if hash_or_height.isdigit():
        h_hash = await safe_call_on("zcash", "getblockhash", [int(hash_or_height)])
    else:
        if not is_hex64(hash_or_height):
            return JSONResponse(status_code=400, content={"error": "block hash must be 64 hex characters or a decimal height"})
        h_hash = hash_or_height
    if not h_hash:
        return JSONResponse(status_code=404, content={"error": "not found"})
    block = await safe_call_on("zcash", "getblock", [h_hash, 1])
    if not block:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return {"chain": "zcash-mainnet", "block": block}


@app.get("/api/z/tx/{txid}")
async def api_zcash_tx(txid: str):
    if not is_hex64(txid):
        return JSONResponse(status_code=400, content={"error": "txid must be 64 hex characters"})
    tx = await safe_call_on("zcash", "getrawtransaction", [txid, 1])
    if not tx:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return {"chain": "zcash-mainnet", "tx": tx, "flow": tx_value_flow(tx)}


@app.get("/api/z/pool/{pool_id}")
async def api_zcash_pool(pool_id: str):
    if pool_id not in POOL_META:
        return JSONResponse(status_code=404, content={"error": "unknown pool"})
    info = await safe_call_on("zcash", "getinfo")
    if not info:
        return JSONResponse(status_code=503, content={"error": "node not ready"})
    tip = info.get("blocks", 0)
    series = await cached_pool_history(pool_id, tip, "zcash")
    return {"chain": "zcash-mainnet", "pool": pool_id, "tip": tip, "series": series}


@app.get("/api/z/verify/{txid}")
async def api_zcash_verify(txid: str):
    if not is_hex64(txid):
        return JSONResponse(status_code=400, content={"error": "txid must be 64 hex"})
    tx = await safe_call_on("zcash", "getrawtransaction", [txid, 1])
    if not tx:
        return JSONResponse(status_code=404, content={"txid": txid, "found": False})
    flow = tx_value_flow(tx)
    anchors = load_chain_registry("zap1-anchors.json", "zcash")
    zap1 = None
    for e in anchors:
        if e.get("txid", "").lower() == txid.lower() and e.get("network") == "zcash-mainnet":
            zap1 = dict(e)
            zap1["event_label"] = ZAP1_EVENT_LABELS.get(zap1.get("event_type", "").lower(), "unknown")
            break
    return {"chain": "zcash-mainnet", "txid": txid, "found": True, "flow": flow, "zap1_anchor": zap1}


from fastapi.responses import PlainTextResponse, Response as FastAPIResponse
from email.utils import format_datetime
from datetime import datetime, timezone


@app.get("/tip", response_class=PlainTextResponse)
async def text_tip():
    info = await safe_call("getinfo")
    if not info:
        raise HTTPException(status_code=503, detail="node not ready")
    return str(info.get("blocks", 0))


@app.get("/finalized", response_class=PlainTextResponse)
async def text_finalized():
    f = await safe_call("get_tfl_final_block_height_and_hash")
    if not f:
        return PlainTextResponse("0")
    return str(f.get("height", 0))


@app.get("/gap", response_class=PlainTextResponse)
async def text_gap():
    info, f = await asyncio.gather(
        safe_call("getinfo"),
        safe_call("get_tfl_final_block_height_and_hash"),
    )
    if not info:
        raise HTTPException(status_code=503, detail="node not ready")
    tip = info.get("blocks", 0)
    finalized = f.get("height", 0) if f else 0
    return str(tip - finalized)


@app.get("/tip.z", response_class=PlainTextResponse)
async def text_tip_zcash():
    info = await safe_call_on("zcash", "getinfo")
    if not info:
        raise HTTPException(status_code=503, detail="zcash mainnet node not ready")
    return str(info.get("blocks", 0))


@app.get("/.well-known/explorer")
async def well_known_explorer():
    return {
        "name": "ctaz-explorer",
        "version": "v0.1.0",
        "chains": ["ctaz-s1", "zcash-mainnet"],
        "source": "https://github.com/Frontier-Compute/ctaz-explorer",
        "license": "MIT",
        "operator_pubkey": OPERATOR_FINALIZER_PUBKEY,
        "operator_chain": "ctaz-s1",
        "plain_text_endpoints": ["/tip", "/finalized", "/gap", "/tip.z"],
        "feeds": ["/feed.xml"],
        "api_base": "/api/",
    }


@app.get("/feed.xml")
async def rss_feed():
    info = await safe_call("getinfo")
    if not info:
        raise HTTPException(status_code=503, detail="node not ready")
    tip = info.get("blocks", 0)
    recent_raw = await asyncio.gather(
        *[fetch_recent_block_with_finality(h) for h in range(max(0, tip - 19), tip + 1)]
    )
    recent = [b for b in recent_raw if b is not None]
    items = []
    for b in reversed(recent):
        if b.get("time"):
            pubdate = format_datetime(datetime.fromtimestamp(int(b["time"]), tz=timezone.utc))
        else:
            pubdate = ""
        height = b["height"]
        bhash = b["hash"]
        finality = b["finality"]
        tx_count = b["tx_count"]
        item = (
            "<item>"
            f"<title>block {height} ({finality})</title>"
            f"<link>https://ctaz.frontiercompute.cash/block/{height}</link>"
            f"<guid>https://ctaz.frontiercompute.cash/block/{bhash}</guid>"
            f"<pubDate>{pubdate}</pubDate>"
            f"<description>{tx_count} tx, finality: {finality}</description>"
            "</item>"
        )
        items.append(item)
    body = "".join(items)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        '<title>ctaz-explorer recent blocks</title>'
        '<link>https://ctaz.frontiercompute.cash/</link>'
        '<description>crosslink s1 feature net recent blocks</description>'
        + body +
        '</channel></rss>'
    )
    return FastAPIResponse(content=xml, media_type="application/rss+xml")


@app.get("/z/leaves")
async def zcash_leaves(request: Request):
    leaves, leaf_count = await asyncio.gather(
        load_zcash_zap1_leaves(),
        load_zcash_zap1_leaf_count(),
    )
    by_type = {}
    for lf in leaves:
        t = lf.get("event_type", "??")
        by_type.setdefault(t, []).append(lf)
    return templates.TemplateResponse(request, "zcash_leaves.html", {
        "request": request,
        "chain": CHAINS["zcash"],
        "leaves": leaves,
        "leaf_count": leaf_count,
        "by_type": sorted(by_type.items()),
        "event_labels": ZAP1_EVENT_LABELS,
    })


@app.get("/api/z/leaves")
async def api_zcash_leaves():
    leaves, leaf_count = await asyncio.gather(
        load_zcash_zap1_leaves(),
        load_zcash_zap1_leaf_count(),
    )
    return {
        "protocol": "ZAP1",
        "network": "zcash-mainnet",
        "count": leaf_count,
        "leaves": leaves,
    }


@app.get("/api/z/leaf/{leaf_hash}")
async def api_zcash_leaf(leaf_hash: str):
    leaf = await load_zcash_zap1_leaf(leaf_hash)
    if leaf:
        return leaf
    return JSONResponse(status_code=404, content={"error": "leaf not found", "leaf_hash": leaf_hash})

import hashlib as _hashlib_merkle


def _dsha256(payload: bytes) -> bytes:
    return _hashlib_merkle.sha256(_hashlib_merkle.sha256(payload).digest()).digest()


def _merkle_path(txids: list, txid: str):
    level = [bytes.fromhex(t)[::-1] for t in txids]
    target = bytes.fromhex(txid)[::-1]
    if target not in level:
        return None
    idx = level.index(target)
    path = []
    depth = 0
    while len(level) > 1:
        if len(level) & 1:
            level = level + [level[-1]]
        sibling_idx = idx ^ 1
        sibling = level[sibling_idx]
        side = 'left' if sibling_idx < idx else 'right'
        path.append((depth, side, sibling[::-1].hex()))
        level = [_dsha256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
        idx //= 2
        depth += 1
    return path, level[0][::-1].hex()


async def _merkle_text(chain_key: str, txid: str):
    tx = await safe_call_on(chain_key, 'getrawtransaction', [txid, 1])
    if not tx or not tx.get('blockhash'):
        raise HTTPException(status_code=404, detail='transaction not anchored in a block')
    block = await safe_call_on(chain_key, 'getblock', [tx['blockhash'], 1])
    if not block:
        raise HTTPException(status_code=404, detail='block not found')
    proof = _merkle_path(block.get('tx', []), txid)
    if not proof:
        raise HTTPException(status_code=404, detail='txid missing from block tx list')
    path, computed = proof
    lines = [
        'txid: ' + txid,
        'block: ' + block.get('hash', ''),
        'height: ' + str(block.get('height', '-')),
        'merkleroot: ' + str(block.get('merkleroot', '')),
        'computed: ' + computed,
        'path:',
    ]
    for depth, side, sibling in path:
        lines.append('  L' + str(depth) + ' ' + side + ' ' + sibling)
    return '\n'.join(lines)




@app.get('/robots.txt', response_class=PlainTextResponse)
async def robots_txt_route():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Sitemap: https://ctaz.frontiercompute.cash/sitemap.xml\n"
    )
    return PlainTextResponse(body)


@app.get('/sitemap.xml')
async def sitemap_xml():
    urls = [
        '/', '/z', '/finalizers', '/anchors', '/vaults', '/events',
        '/verify', '/params', '/why', '/stake',
        '/pool/orchard', '/pool/transparent', '/pool/sapling', '/pool/sprout', '/pool/lockbox',
        '/z/anchors', '/z/leaves', '/z/pool/orchard', '/z/verify',
        '/feed.xml', '/.well-known/explorer',
        '/tip', '/finalized', '/gap', '/tip.z',
    ]
    body = ''.join(
        '<url><loc>https://ctaz.frontiercompute.cash' + p + '</loc></url>'
        for p in urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + body +
        '</urlset>'
    )
    return FastAPIResponse(content=xml, media_type='application/xml')


@app.get('/tx/{txid}/merkle.txt', response_class=PlainTextResponse)
async def tx_merkle_text_route(txid: str):
    return PlainTextResponse(await _merkle_text('ctaz', txid))


@app.get('/z/tx/{txid}/merkle.txt', response_class=PlainTextResponse)
async def zcash_merkle_text_route(txid: str):
    return PlainTextResponse(await _merkle_text('zcash', txid))


@app.get('/finalizers/participation')
async def participation_view(request: Request):
    tracker = get_tracker()
    await tracker.start()
    stats = tracker.get_stats()
    roster = await safe_call('get_tfl_roster_zats') or []
    roster_sorted = sorted(roster, key=lambda m: int(m.get('voting_power', 0)), reverse=True)
    total_vp = sum(int(m.get('voting_power', 0)) for m in roster_sorted)
    info = await safe_call('getinfo')
    staking = staking_day_state(info['blocks']) if info else None
    return templates.TemplateResponse(request, 'participation.html', {
        'request': request,
        'roster': roster_sorted,
        'total_vp': total_vp,
        'stats': stats,
        'staking': staking,
        'labels': load_finalizer_labels(),
        'scorecard': get_tracker().get_scorecard(),
    })

@app.get('/stake/plan')
async def stake_plan_view(request: Request, amount: str = '', bond: str = '10'):
    info = await safe_call('getinfo')
    staking = staking_day_state(info['blocks']) if info else None
    plan = None
    if amount:
        try:
            target = float(amount)
            bond_size = float(bond)
            if target > 0 and bond_size > 0:
                n_full = int(target // bond_size)
                remainder = round(target - n_full * bond_size, 4)
                window_blocks = STAKING_WINDOW_BLOCKS
                actions_total = n_full + (1 if remainder > 0.00005 else 0)
                seconds_per_pow_block = 30
                window_seconds = window_blocks * seconds_per_pow_block
                seconds_per_action = window_seconds / actions_total if actions_total else 0
                plan = {
                    'target': target,
                    'bond_size': bond_size,
                    'full_bonds': n_full,
                    'remainder': remainder,
                    'actions_total': actions_total,
                    'window_seconds': int(window_seconds),
                    'seconds_per_action': round(seconds_per_action, 1),
                }
        except (ValueError, TypeError):
            pass
    return templates.TemplateResponse(request, 'stake-plan.html', {
        'request': request,
        'amount': amount,
        'bond': bond,
        'staking': staking,
        'plan': plan,
        'pubkey': OPERATOR_FINALIZER_PUBKEY,
    })

@app.get('/sync-check')
async def sync_check_view(request: Request, height: str = '', hash: str = ''):
    info = await safe_call('getinfo') or {}
    our_tip = info.get('blocks')
    our_peers = info.get('connections')
    result = None
    height_int = None
    your_hash = (hash or '').strip().lower()
    if height:
        try:
            height_int = int(height)
        except ValueError:
            result = {'status': 'invalid', 'note': 'height must be an integer'}
    if height_int is not None:
        canonical_hash = await safe_call('getblockhash', [height_int])
        if canonical_hash is None:
            result = {
                'status': 'ahead',
                'height': height_int,
                'note': f"block {height_int} is not in our chain yet. either your node is ahead of ours (our tip is {our_tip}) or the height does not exist.",
            }
        elif your_hash and len(your_hash) == 64 and all(c in '0123456789abcdef' for c in your_hash):
            if your_hash == canonical_hash.lower():
                result = {
                    'status': 'match',
                    'height': height_int,
                    'canonical_hash': canonical_hash,
                    'your_hash': your_hash,
                }
            else:
                result = {
                    'status': 'mismatch',
                    'height': height_int,
                    'canonical_hash': canonical_hash,
                    'your_hash': your_hash,
                }
        else:
            result = {
                'status': 'canonical_only',
                'height': height_int,
                'canonical_hash': canonical_hash,
            }
    return templates.TemplateResponse(request, 'sync-check.html', {
        'request': request,
        'height': height,
        'hash': hash,
        'result': result,
        'our_tip': our_tip,
        'our_peers': our_peers,
    })


@app.get('/api/sync-check/{height}/{your_hash}')
async def api_sync_check(height: int, your_hash: str):
    info = await safe_call('getinfo') or {}
    canonical = await safe_call('getblockhash', [height])
    if canonical is None:
        return {
            'height': height,
            'canonical_hash': None,
            'your_hash': your_hash,
            'match': None,
            'status': 'ahead_of_our_tip_or_unknown',
            'our_tip': info.get('blocks'),
        }
    return {
        'height': height,
        'canonical_hash': canonical,
        'your_hash': your_hash,
        'match': your_hash.lower() == canonical.lower(),
        'our_tip': info.get('blocks'),
        'our_peers': info.get('connections'),
    }


async def load_snapshot_view_model(base_url: str | None = None):
    our_tip, info, snapshot = await asyncio.gather(
        safe_call('getblockcount'),
        safe_call('getinfo'),
        fetch_cipherscan_snapshot_meta(),
    )
    our_peers = (info or {}).get('connections')
    verification_height = snapshot.get('finalized_height') or snapshot.get('current_cipherscan_tip')
    our_hash_at_snapshot_height = None
    if verification_height is not None and our_tip is not None and our_tip >= verification_height:
        our_hash_at_snapshot_height = await safe_call('getblockhash', [verification_height])

    grade = compute_canonical_grade(
        snapshot.get('current_cipherscan_tip'),
        our_tip,
        cipherscan_hash=snapshot.get('finalized_hash'),
        our_hash=our_hash_at_snapshot_height,
    )

    sync_check_prefill_url = '/sync-check'
    sync_check_api_url = None
    sync_check_api_curl = None
    if verification_height is not None and snapshot.get('finalized_hash'):
        sync_check_prefill_url = f"/sync-check?height={verification_height}&hash={snapshot['finalized_hash']}"
        sync_check_api_url = f"/api/sync-check/{verification_height}/{snapshot['finalized_hash']}"
        if base_url:
            sync_check_api_curl = f"curl -fsSL {base_url.rstrip('/')}{sync_check_api_url}"

    snapshot.update({
        'our_canonical_tip': our_tip,
        'our_peers': our_peers,
        'verification_height': verification_height,
        'our_hash_at_snapshot_height': our_hash_at_snapshot_height,
        'delta': grade.get('delta'),
        'grade': grade,
        'sync_check_prefill_url': sync_check_prefill_url,
        'sync_check_api_url': sync_check_api_url,
        'sync_check_api_curl': sync_check_api_curl,
    })
    return snapshot


@app.get('/snapshots')
async def snapshots_view(request: Request):
    snapshot = await load_snapshot_view_model(str(request.base_url))
    return render_snapshots_page(request, templates, snapshot)


@app.get('/api/snapshots')
async def api_snapshots():
    snapshot = await load_snapshot_view_model()
    return {
        'source': snapshot.get('source_slug'),
        'current_cipherscan_tip': snapshot.get('current_cipherscan_tip'),
        'our_canonical_tip': snapshot.get('our_canonical_tip'),
        'delta': snapshot.get('delta'),
        'grade': snapshot.get('grade', {}).get('grade'),
        'grade_note': snapshot.get('grade', {}).get('note'),
        'hash_match': snapshot.get('grade', {}).get('hash_match'),
        'freshness_age_seconds': snapshot.get('freshness_age_seconds'),
        'freshness_basis': snapshot.get('freshness_basis'),
        'generated_at': snapshot.get('generated_at'),
        'generated_at_display': snapshot.get('generated_at_display'),
        'cipherscan_url': snapshot.get('cipherscan_url'),
        'download_url': snapshot.get('download_url'),
        'sha256_url': snapshot.get('sha256_url'),
        'sha256_if_fetchable': snapshot.get('sha256_if_fetchable'),
        'finalized_height': snapshot.get('finalized_height'),
        'finalized_hash': snapshot.get('finalized_hash'),
        'our_hash_at_snapshot_height': snapshot.get('our_hash_at_snapshot_height'),
        'available': snapshot.get('available'),
        'errors': snapshot.get('errors'),
        'cache_ttl_seconds': snapshot.get('cache_ttl_seconds'),
        'last_checked_at': snapshot.get('last_checked_at'),
    }

@app.get('/chain-health')
async def chain_health_view(request: Request):
    info, final_hh = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('get_tfl_final_block_height_and_hash'),
    )
    info = info or {}
    tip = info.get('blocks')
    peers = info.get('connections')
    finalized_height = final_hh.get('height') if final_hh and isinstance(final_hh, dict) else None
    finality_gap = (tip - finalized_height) if tip is not None and finalized_height is not None else None
    health = get_tracker().get_chain_health()
    chain_info = await safe_call('getblockchaininfo') or {}
    version_info = {
        'version': info.get('version'),
        'build': info.get('build'),
        'subversion': info.get('subversion'),
        'protocolversion': info.get('protocolversion'),
        'chain': chain_info.get('chain'),
    }
    return templates.TemplateResponse(request, 'chain-health.html', {
        'request': request,
        'tip': tip,
        'peers': peers,
        'finalized_height': finalized_height,
        'finality_gap': finality_gap,
        'health': health,
        'reorgs': get_tracker().get_reorg_summary(),
        'silent_finalizers': get_tracker().get_silent_finalizers(),
        'labels': load_finalizer_labels(),
        'version_info': version_info,
        'auto_refresh_s': 20,
    })


@app.get('/api/chain-health')
async def api_chain_health():
    info, final_hh = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('get_tfl_final_block_height_and_hash'),
    )
    info = info or {}
    return {
        'tip': info.get('blocks'),
        'peers': info.get('connections'),
        'finalized_height': (final_hh.get('height') if final_hh and isinstance(final_hh, dict) else None),
        'health': get_tracker().get_chain_health(),
        'reorgs': get_tracker().get_reorg_summary(),
        'silent_finalizers': get_tracker().get_silent_finalizers(),
        'version_info': {
            'version': info.get('version'),
            'build': info.get('build'),
            'subversion': info.get('subversion'),
            'protocolversion': info.get('protocolversion'),
        },
    }

@app.get('/finalizer/{pubkey}')
async def finalizer_detail_view(request: Request, pubkey: str):
    pubkey = pubkey.strip().lower()
    if not pubkey or len(pubkey) < 4 or not all(c in '0123456789abcdef' for c in pubkey):
        raise HTTPException(status_code=400, detail='pubkey must be hex')
    roster = await safe_call('get_tfl_roster_zats') or []
    roster_sorted = sorted(roster, key=lambda m: int(m.get('voting_power', 0)), reverse=True)
    total_vp = sum(int(m.get('voting_power', 0)) for m in roster_sorted)
    match = None
    match_rank = None
    match_node_pk = None
    for i, m in enumerate(roster_sorted):
        raw = m.get('pub_key') or ''
        node_pk = reverse_hex(raw).lower()
        if node_pk.startswith(pubkey) or raw.lower().startswith(pubkey):
            match = m
            match_rank = i + 1
            match_node_pk = node_pk
            break
    if not match:
        raise HTTPException(status_code=404, detail=f'finalizer not found for pubkey prefix {pubkey}')
    tracker = get_tracker()
    scorecard = tracker.get_scorecard()
    sc = scorecard.get(match_node_pk)
    labels = load_finalizer_labels()
    label = labels.get(match_node_pk)
    total_hits = tracker.finalizer_hits.get(match_node_pk, 0)
    total_certs = len(tracker.certs_seen)
    events = sorted(tracker.pos_finalization_events, key=lambda e: e.get('pos_height') or 0)
    recent_window = events[-40:] if len(events) > 40 else events
    signing_history = []
    certs_sorted = sorted(tracker.certs_seen.values(), key=lambda c: c.get('first_seen_at') or 0)
    recent_certs = certs_sorted[-40:] if len(certs_sorted) > 40 else certs_sorted
    for c in recent_certs:
        signers = c.get('signers') or []
        signing_history.append(1 if match_node_pk in signers else 0)
    return templates.TemplateResponse(request, 'finalizer-detail.html', {
        'request': request,
        'pubkey': match_node_pk,
        'pubkey_short': match_node_pk[:20] + '…',
        'label': label,
        'rank': match_rank,
        'stake_zats': int(match.get('voting_power', 0)),
        'share': (int(match.get('voting_power', 0)) / total_vp) if total_vp > 0 else 0,
        'scorecard': sc,
        'total_hits': total_hits,
        'total_certs': total_certs,
        'signing_history': signing_history,
        'auto_refresh_s': 30,
    })

@app.get('/chain-graph')
async def chain_graph_view(request: Request):
    info, final_hh = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('get_tfl_final_block_height_and_hash'),
    )
    info = info or {}
    tip = info.get('blocks')
    peers = info.get('connections')
    finalized_height = final_hh.get('height') if final_hh and isinstance(final_hh, dict) else None
    n_pow = 14
    pow_blocks = []
    if tip is not None:
        for h in range(max(0, tip - n_pow + 1), tip + 1):
            hh = await safe_call('getblockhash', [h])
            if hh:
                blk = await safe_call('getblock', [hh, 1])
                pow_blocks.append({
                    'height': h,
                    'hash': hh,
                    'tx_count': len(blk.get('tx', [])) if blk else 0,
                    'finalized': finalized_height is not None and h <= finalized_height,
                    'tip': h == tip,
                })
    tracker = get_tracker()
    events = sorted(tracker.pos_finalization_events, key=lambda e: e.get('pos_height') or 0)
    pow_heights_in_view = {b['height'] for b in pow_blocks}
    linked_events = []
    for e in events:
        fp = e.get('finalized_pow_height')
        if fp in pow_heights_in_view:
            linked_events.append(e)
    if len(linked_events) > n_pow:
        linked_events = linked_events[-n_pow:]
    return templates.TemplateResponse(request, 'chain-graph.html', {
        'request': request,
        'tip': tip,
        'peers': peers,
        'finalized_height': finalized_height,
        'pow_blocks': pow_blocks,
        'pos_events': linked_events,
        'auto_refresh_s': 30,
    })

@app.get('/guide/staking')
async def staking_guide_view(request: Request):
    info = await safe_call('getinfo')
    staking = staking_day_state(info['blocks']) if info else None
    return templates.TemplateResponse(request, 'staking-guide.html', {
        'request': request,
        'staking': staking,
        'auto_refresh_s': 60,
    })

@app.get('/metrics')
async def prometheus_metrics():
    from fastapi.responses import PlainTextResponse
    info, final_hh = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('get_tfl_final_block_height_and_hash'),
    )
    info = info or {}
    tip = info.get('blocks') or 0
    peers = info.get('connections') or 0
    finalized = final_hh.get('height') if final_hh and isinstance(final_hh, dict) else None
    finality_gap = (tip - finalized) if finalized is not None else 0
    tracker = get_tracker()
    health = tracker.get_chain_health()
    reorgs = tracker.get_reorg_summary()
    silent = tracker.get_silent_finalizers()
    staking = staking_day_state(tip)
    lines = []
    def g(name, value, help_text, metric_type='gauge'):
        lines.append(f'# HELP ctaz_{name} {help_text}')
        lines.append(f'# TYPE ctaz_{name} {metric_type}')
        lines.append(f'ctaz_{name} {value}')
    g('pow_tip', tip, 'latest pow block height seen by this node')
    g('pow_finalized', finalized or 0, 'latest bft-finalized pow block height')
    g('pow_finality_gap_blocks', finality_gap, 'pow blocks between finalized tip and pow tip')
    g('peers', peers, 'number of p2p peers currently connected')
    g('tracker_certs_observed', len(tracker.certs_seen), 'distinct bft certs this tracker has observed since start')
    g('tracker_pos_events', len(tracker.pos_finalization_events), 'pos finalization events recorded')
    g('tracker_heights_tracked', len(tracker.height_hash_history), 'pow heights the reorg detector is tracking')
    g('reorgs_observed_total', reorgs.get('total_observed', 0), 'reorgs observed by this node since tracker start', 'counter')
    g('finalizers_silent', len(silent), 'finalizers who signed at least once but have been absent 50 plus observed certs')
    if health and 'latest_signer_count' in health:
        g('bft_latest_signers', health.get('latest_signer_count', 0), 'signer count on the latest observed bft cert')
        g('bft_median_signers', health.get('median_signers', 0), 'median signer count over recent window')
        g('bft_min_signers', health.get('min_signers', 0), 'min signer count over recent window')
        g('bft_max_signers', health.get('max_signers', 0), 'max signer count over recent window')
        g('bft_latest_degraded', 1 if health.get('degraded') else 0, '1 if the latest cert signer count is below the degraded threshold')
        g('bft_latest_pos_height', health.get('latest_pos_height', 0), 'pos height of the latest observed bft cert')
    if staking:
        g('staking_cycle_pos', staking.get('cycle_pos', 0), 'position inside the 150-block staking cycle')
        g('staking_window_live', 1 if staking.get('live') else 0, '1 if the 70-block staking window is currently open')
        if staking.get('blocks_remaining') is not None:
            g('staking_blocks_remaining', staking['blocks_remaining'], 'pow blocks left in the current staking window')
        if staking.get('next_in') is not None:
            g('staking_next_window_in', staking['next_in'], 'pow blocks until the next staking window opens')
    roster = await safe_call('get_tfl_roster_zats') or []
    g('finalizers_active', len(roster), 'size of the active finalizer roster')
    total_vp = sum(int(m.get('voting_power', 0)) for m in roster)
    g('stake_total_zats', total_vp, 'sum of voting_power across the active roster in zatoshis')
    return PlainTextResponse('\n'.join(lines) + '\n', media_type='text/plain; version=0.0.4; charset=utf-8')

@app.get('/guide/metrics')
async def metrics_guide_view(request: Request):
    return templates.TemplateResponse(request, 'metrics-guide.html', {
        'request': request,
    })

@app.get('/devs')
async def devs_view(request: Request):
    return templates.TemplateResponse(request, 'devs.html', {'request': request})

@app.get('/super')
async def super_view(request: Request):
    supernode = __import__('super')
    payload = await asyncio.to_thread(supernode.build_super_payload)
    response = templates.TemplateResponse(request, 'super.html', {
        'request': request,
        **payload,
        'auto_refresh_s': 30,
    })
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Robots-Tag'] = 'noarchive'
    return response


@app.get('/api/super')
async def api_super():
    supernode = __import__('super')
    return await asyncio.to_thread(supernode.build_super_payload)

