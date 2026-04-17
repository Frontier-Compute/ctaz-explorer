import asyncio
import json
import os
import pathlib
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from rpc import ZebradRPC

app = FastAPI(title='ctaz-explorer', docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory='templates')
app.mount('/static', StaticFiles(directory='static'), name='static')
rpc = ZebradRPC()
rpc_zcash = ZebradRPC(url=os.environ.get('ZCASH_MAINNET_RPC_URL', 'http://127.0.0.1:8232'))


NO_STORE_PREFIXES = (
    '/api/', '/block/', '/tx/', '/verify', '/finalizers',
    '/anchors', '/vaults', '/events', '/params', '/tfl',
    '/tip', '/finalized', '/gap', '/feed.xml',
    '/.well-known/explorer', '/z/', '/tip.z',
)


@app.middleware('http')
async def no_store_live_surfaces(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
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

async def cached_pool_history(pool_id: str, tip: int, chain_key: str = 'ctaz'):
    key = f'{chain_key}:{pool_id}:{tip}'
    now = _time.time()
    if key in _pool_cache and (now - _pool_cache[key][0]) < _POOL_CACHE_TTL:
        return _pool_cache[key][1]
    if chain_key == 'ctaz':
        series = await cached_pool_history(pool_id, tip, 'ctaz')
    else:
        series = await fetch_pool_history_chain(pool_id, tip, chain_key)
    _pool_cache[key] = (now, series)
    return series


async def fetch_pool_history_chain(pool_id: str, tip: int, chain_key: str, window: int = POOL_HISTORY_WINDOW):
    start = max(0, tip - window + 1)
    heights = list(range(start, tip + 1))
    sem = asyncio.Semaphore(20)
    async def fetch(h):
        async with sem:
            h_hash = await safe_call_on(chain_key, 'getblockhash', [h])
            if not h_hash:
                return None
            return await safe_call_on(chain_key, 'getblock', [h_hash, 1])
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


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    if request.url.path.startswith('/api/'):
        return JSONResponse(status_code=500, content={'error': 'internal server error'})
    try:
        return templates.TemplateResponse(request, 'error.html', {
            'request': request,
            'status_code': 500,
            'message': 'internal server error',
        }, status_code=500)
    except Exception:
        return JSONResponse(status_code=500, content={'error': 'internal server error'})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith('/api/'):
        return JSONResponse(status_code=exc.status_code, content={'error': exc.detail})
    try:
        return templates.TemplateResponse(request, 'error.html', {
            'request': request,
            'status_code': exc.status_code,
            'message': exc.detail or 'something went wrong',
        }, status_code=exc.status_code)
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
async def home(request: Request):
    info, chaininfo, roster, final_hh = await asyncio.gather(
        safe_call('getinfo'),
        safe_call('getblockchaininfo'),
        safe_call('get_tfl_roster_zats'),
        safe_call('get_tfl_final_block_height_and_hash'),
    )
    if not info or not chaininfo:
        raise HTTPException(status_code=503, detail='node not ready')
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
        'recent': recent,
        'anchor_count': len(anchors),
        'vault_count': len(vaults),
        'event_count': len(events),
    })


@app.get('/block/{hash_or_height}')
async def block_view(request: Request, hash_or_height: str):
    if hash_or_height.isdigit():
        h_hash = await safe_call('getblockhash', [int(hash_or_height)])
    else:
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
    return templates.TemplateResponse(request, 'stake.html', {
        'request': request,
        'pubkey': OPERATOR_FINALIZER_PUBKEY,
        'in_roster': in_roster,
        'our_stake': our_stake,
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
    series = await cached_pool_history(pool_id, tip, 'ctaz')
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
    series = await cached_pool_history(pool_id, tip, 'ctaz')
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
        'zeven_event': zeven,
        'size': tx.get('size'),
        'version': tx.get('version'),
    }


@app.get('/verify')
async def verify_view(request: Request, q: str = ''):
    q = q.strip()
    result = None
    error = None
    if q:
        if len(q) != 64 or not all(c in '0123456789abcdef' for c in q.lower()):
            error = 'txid must be 64 hex characters'
        else:
            result = await build_verification(q)
            if result is None:
                error = 'transaction not found on this chain'
    return templates.TemplateResponse(request, 'verify.html', {
        'request': request,
        'q': q,
        'result': result,
        'error': error,
    })


@app.get('/api/verify/{txid}')
async def api_verify(txid: str):
    if len(txid) != 64 or not all(c in '0123456789abcdef' for c in txid.lower()):
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
    info, chaininfo = await asyncio.gather(
        safe_call_on('zcash', 'getinfo'),
        safe_call_on('zcash', 'getblockchaininfo'),
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
    anchors = [e for e in load_chain_registry('zap1-anchors.json', 'zcash') if e.get('network') == 'zcash-mainnet']
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
        'anchor_count': len(anchors),
        'anchors_preview': anchors[:5],
    })


@app.get('/z/block/{hash_or_height}')
async def zcash_block(request: Request, hash_or_height: str):
    if hash_or_height.isdigit():
        h_hash = await safe_call_on('zcash', 'getblockhash', [int(hash_or_height)])
    else:
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
    tx = await safe_call_on('zcash', 'getrawtransaction', [txid, 1])
    if not tx:
        raise HTTPException(status_code=404, detail='transaction not found on zcash mainnet')
    flow = tx_value_flow(tx)
    anchors = load_chain_registry('zap1-anchors.json', 'zcash')
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
    registry = [e for e in load_chain_registry('zap1-anchors.json', 'zcash') if e.get('network') == 'zcash-mainnet']
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
    registry = [e for e in load_chain_registry('zap1-anchors.json', 'zcash') if e.get('network') == 'zcash-mainnet']
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
        if len(q) != 64 or not all(c in '0123456789abcdef' for c in q.lower()):
            error = 'txid must be 64 hex characters'
        else:
            tx = await safe_call_on('zcash', 'getrawtransaction', [q, 1])
            if not tx:
                error = 'transaction not found on zcash mainnet'
            else:
                flow = tx_value_flow(tx)
                anchors = load_chain_registry('zap1-anchors.json', 'zcash')
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
        h_hash = hash_or_height
    if not h_hash:
        return JSONResponse(status_code=404, content={"error": "not found"})
    block = await safe_call_on("zcash", "getblock", [h_hash, 1])
    if not block:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return {"chain": "zcash-mainnet", "block": block}


@app.get("/api/z/tx/{txid}")
async def api_zcash_tx(txid: str):
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
    if len(txid) != 64 or not all(c in "0123456789abcdef" for c in txid.lower()):
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
    leaves = load_chain_registry("zap1-leaves.json", "zcash")
    by_type = {}
    for lf in leaves:
        t = lf.get("event_type", "??")
        by_type.setdefault(t, []).append(lf)
    return templates.TemplateResponse(request, "zcash_leaves.html", {
        "request": request,
        "chain": CHAINS["zcash"],
        "leaves": leaves,
        "by_type": sorted(by_type.items()),
        "event_labels": ZAP1_EVENT_LABELS,
    })


@app.get("/api/z/leaves")
async def api_zcash_leaves():
    leaves = load_chain_registry("zap1-leaves.json", "zcash")
    return {
        "protocol": "ZAP1",
        "network": "zcash-mainnet",
        "count": len(leaves),
        "leaves": leaves,
    }


@app.get("/api/z/leaf/{leaf_hash}")
async def api_zcash_leaf(leaf_hash: str):
    leaves = load_chain_registry("zap1-leaves.json", "zcash")
    for lf in leaves:
        if lf.get("leaf_hash", "").lower() == leaf_hash.lower():
            return lf
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
    return templates.TemplateResponse(request, 'participation.html', {
        'request': request,
        'roster': roster_sorted,
        'total_vp': total_vp,
        'stats': stats,
    })
