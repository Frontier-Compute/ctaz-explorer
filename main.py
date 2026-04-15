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


def zats_to_ctaz(zats):
    try:
        return f'{int(zats) / 1e8:.4f}'
    except Exception:
        return '0'


def short_hash(h, n=16):
    if not h:
        return ''
    return str(h)[:n] + '…'


templates.env.filters['ctaz'] = zats_to_ctaz
templates.env.filters['short'] = short_hash
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


async def fetch_recent_block(h):
    h_hash = await safe_call('getblockhash', [h])
    if not h_hash:
        return None
    block = await safe_call('getblock', [h_hash, 1])
    if not block:
        return None
    return {
        'height': h,
        'hash': h_hash,
        'tx_count': len(block.get('tx', [])),
        'time': block.get('time'),
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
    recent_raw = await asyncio.gather(*[fetch_recent_block(h) for h in range(max(0, tip - 9), tip + 1)])
    recent = [b for b in recent_raw if b is not None]
    total_vp = sum(int(m.get('voting_power', 0)) for m in roster)
    return templates.TemplateResponse(request, 'home.html', {
        'request': request,
        'tip': tip,
        'connections': info.get('connections', 0),
        'chaininfo': chaininfo,
        'roster': roster,
        'total_vp': total_vp,
        'finalized': final_hh,
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
    return templates.TemplateResponse(request, 'block.html', {
        'request': request,
        'block': block,
        'finality': finality,
    })


@app.get('/tx/{txid}')
async def tx_view(request: Request, txid: str):
    tx = await safe_call('getrawtransaction', [txid, 1])
    if not tx:
        raise HTTPException(status_code=404, detail='transaction not found')
    finality = await safe_call('get_tfl_tx_finality_from_hash', [txid])
    return templates.TemplateResponse(request, 'tx.html', {
        'request': request,
        'tx': tx,
        'txid': txid,
        'finality': finality,
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
