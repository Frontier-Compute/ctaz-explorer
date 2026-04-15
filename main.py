from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from rpc import ZebradRPC

app = FastAPI(title="ctaz-explorer", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
rpc = ZebradRPC()

def zats_to_ctaz(zats):
    try:
        return f"{int(zats) / 1e8:.4f}"
    except Exception:
        return "0"

templates.env.filters["ctaz"] = zats_to_ctaz


@app.get("/")
async def home(request: Request):
    info = await rpc.call("getinfo")
    chaininfo = await rpc.call("getblockchaininfo")
    try:
        roster = await rpc.call("get_tfl_roster_zats") or []
    except Exception:
        roster = []
    try:
        final_hh = await rpc.call("get_tfl_final_block_height_and_hash")
    except Exception:
        final_hh = None
    tip = info["blocks"]
    recent = []
    for h in range(max(0, tip - 9), tip + 1):
        try:
            h_hash = await rpc.call("getblockhash", [h])
            block = await rpc.call("getblock", [h_hash, 1])
            recent.append({
                "height": h,
                "hash": h_hash,
                "tx_count": len(block.get("tx", [])),
                "time": block.get("time"),
            })
        except Exception:
            continue
    total_vp = sum(int(m.get("voting_power", 0)) for m in roster)
    return templates.TemplateResponse(request, "home.html", {
        "request": request,
        "tip": tip,
        "connections": info.get("connections", 0),
        "chaininfo": chaininfo,
        "roster": roster,
        "total_vp": total_vp,
        "finalized": final_hh,
        "recent": list(reversed(recent)),
    })


@app.get("/block/{hash_or_height}")
async def block_view(request: Request, hash_or_height: str):
    try:
        if hash_or_height.isdigit():
            h_hash = await rpc.call("getblockhash", [int(hash_or_height)])
        else:
            h_hash = hash_or_height
        block = await rpc.call("getblock", [h_hash, 1])
    except Exception as e:
        raise HTTPException(status_code=404, detail="block not found")
    try:
        finality = await rpc.call("get_tfl_block_finality_from_hash", [h_hash])
    except Exception:
        finality = None
    return templates.TemplateResponse(request, "block.html", {
        "request": request,
        "block": block,
        "finality": finality,
    })


@app.get("/tx/{txid}")
async def tx_view(request: Request, txid: str):
    try:
        tx = await rpc.call("getrawtransaction", [txid, 1])
    except Exception as e:
        raise HTTPException(status_code=404, detail="transaction not found")
    try:
        finality = await rpc.call("get_tfl_tx_finality_from_hash", [txid])
    except Exception:
        finality = None
    return templates.TemplateResponse(request, "tx.html", {
        "request": request,
        "tx": tx,
        "txid": txid,
        "finality": finality,
    })


@app.get("/address/{addr}")
async def address_view(request: Request, addr: str):
    try:
        balance = await rpc.call("getaddressbalance", [{"addresses": [addr]}])
    except Exception as e:
        raise HTTPException(status_code=400, detail="invalid address")
    try:
        txids = await rpc.call("getaddresstxids", [{"addresses": [addr]}]) or []
    except Exception:
        txids = []
    return templates.TemplateResponse(request, "address.html", {
        "request": request,
        "addr": addr,
        "balance": balance,
        "txids": txids[-50:][::-1],
    })


@app.get("/finalizers")
async def finalizers_view(request: Request):
    try:
        roster = await rpc.call("get_tfl_roster_zats") or []
    except Exception:
        roster = []
    try:
        finalized = await rpc.call("get_tfl_final_block_height_and_hash")
    except Exception:
        finalized = None
    total_vp = sum(int(m.get("voting_power", 0)) for m in roster)
    roster_sorted = sorted(roster, key=lambda m: int(m.get("voting_power", 0)), reverse=True)
    return templates.TemplateResponse(request, "finalizers.html", {
        "request": request,
        "roster": roster_sorted,
        "total_vp": total_vp,
        "finalized": finalized,
    })


@app.get("/search")
async def search(request: Request, q: str = ""):
    q = q.strip()
    if not q:
        return RedirectResponse(url="/")
    if q.isdigit() or (len(q) == 64 and all(c in "0123456789abcdef" for c in q.lower())):
        try:
            return RedirectResponse(url=f"/block/{q}")
        except Exception:
            pass
    if q.startswith(("t1", "t2", "t3", "tm", "u1", "utest", "zs", "ztestsapling")):
        return RedirectResponse(url=f"/address/{q}")
    return RedirectResponse(url=f"/tx/{q}")


@app.get("/health")
async def health():
    try:
        info = await rpc.call("getinfo")
        return {"ok": True, "tip": info.get("blocks"), "connections": info.get("connections")}
    except Exception as e:
        return {"ok": False}
