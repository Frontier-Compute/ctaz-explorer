# ctaz-explorer

minimal open source block explorer for the crosslink season 1 feature net. renders live from a local zebrad node via json rpc. single python process, ~200 loc.

## what it shows

* tip, finalized height, supply, peer count, total stake
* recent 10 blocks with tx count
* block detail with transactions and finality status
* tx detail with inputs, outputs, finality status
* address balance and recent txids
* active finalizer roster sorted by voting power
* /stake page with operator finalizer pub key and delegation instructions, per zooko's funding model

## run

needs a running zebra-crosslink node with rpc at 127.0.0.1:8232 (default for upstream zebra-crosslink).

    pip install fastapi uvicorn jinja2 httpx
    git clone https://github.com/Frontier-Compute/ctaz-explorer
    cd ctaz-explorer
    python3 -m uvicorn main:app --host 0.0.0.0 --port 8088

open http://localhost:8088

## rpc methods used

getinfo, getblockchaininfo, getblockhash, getblock, getrawtransaction, getaddressbalance, getaddresstxids, get_tfl_roster_zats, get_tfl_final_block_height_and_hash, get_tfl_block_finality_from_hash, get_tfl_tx_finality_from_hash

## notes

* complement to cipherscan, not a replacement
* built because zooko said someone should
* not production hardened, expect rough edges
* report issues at https://github.com/Frontier-Compute/ctaz-explorer/issues

## license

mit
