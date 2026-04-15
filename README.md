# ctaz-explorer

minimal open source block explorer for the crosslink season 1 feature net. rendered live from a local zebrad node, quick and dirty, runs on a single python process.



## what it shows

* chain tip, finalized height, cTAZ supply, peer count, total staked
* recent blocks with tx count
* block detail view with transactions and finality status
* transaction detail view with inputs, outputs, finality status
* address view with balance and recent txids
* active finalizer roster with voting power and stake share

## how to run it

you need a running zebra-crosslink node with rpc enabled at 127.0.0.1:8232 (the feature net default after the application.rs config manipulation). see https://github.com/ShieldedLabs/crosslink_monolith for the node itself.

```
pip install fastapi uvicorn jinja2 httpx
git clone https://github.com/Frontier-Compute/ctaz-explorer
cd ctaz-explorer
python3 -m uvicorn main:app --host 0.0.0.0 --port 8088
```

open http://localhost:8088

## rpc methods it uses

getinfo, getblockchaininfo, getblockhash, getblock, getrawtransaction, getaddressbalance, getaddresstxids, get_tfl_roster_zats, get_tfl_final_block_height_and_hash, get_tfl_block_finality_from_hash, get_tfl_tx_finality_from_hash

## notes

* this is a complement to cipherscan, not a replacement. the more explorers the better for a feature net
* built because zooko said someone should, and suggested explorer operators run a finalizer and ask protocol guardians for delegated stake as a funding model. that is the plan
* not production hardened, expect rough edges

## license

MIT
