import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from rpc import ZebradRPC

app = FastAPI(title='ctaz-explorer', docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory='templates')
app.mount('/static', StaticFiles(directory='static'), name='static')
rpc = ZebradRPC()

OPERATOR_FINALIZER_PUBKEY = '646ae0e999d5c1d0f69bce3aaf5f5a71537bbc964c270a88f772592d79e14061'


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
templates.env.globals['operator_pubkey'] = OPERATOR_FINALIZER_PUBKEY


async def safe_call(method, params=None):
    try:
        return await rpc.call(method, params)
    except Exception:
        return None


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={'error': 'internal server error'})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
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
        'recent': list(reversed(recent)),
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
    return templates.TemplateResponse(request, 'tx.html', {
        'request': request,
        'tx': tx,
        'txid': txid,
        'finality': finality or 'Unknown',
        'flow': flow,
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
    in_roster = any(m.get('pub_key') == OPERATOR_FINALIZER_PUBKEY for m in roster)
    our_stake = 0
    for m in roster:
        if m.get('pub_key') == OPERATOR_FINALIZER_PUBKEY:
            our_stake = int(m.get('voting_power', 0))
            break
    return templates.TemplateResponse(request, 'stake.html', {
        'request': request,
        'pubkey': OPERATOR_FINALIZER_PUBKEY,
        'in_roster': in_roster,
        'our_stake': our_stake,
    })


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
