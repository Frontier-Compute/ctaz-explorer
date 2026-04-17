"""
finalizer participation crawler for ctaz-explorer.

polls get_tfl_fat_pointer_to_bft_chain_tip periodically, dedupes by cert
content hash (vote_for_block_without_finalizer_public_key), records which
finalizers signed each observed cert.

signals per andrew reece in the crosslink updates signal group:
- fat pointer signers represent at least 67 percent of stake, so a given
  cert carries a lower-bound signer set, not the full set of voters.
- signers in a pow block's fat pointer voted on an earlier pow block's
  finalization via the pos block between them, not on the block that
  carries the pointer. rolling window over many observations smooths this.

state is persisted to data/participation-state.json so restarts do not
discard observed history. this module is intentionally self-contained:
it manages its own rpc client and runs a single background poll task.
"""

import asyncio
import hashlib
import json
import os
import pathlib
import time
from typing import Any

import httpx


STATE_PATH = pathlib.Path(os.environ.get(
    'PARTICIPATION_STATE_PATH',
    '/root/ctaz-explorer/data/participation-state.json',
))
RPC_URL = os.environ.get('ZEBRAD_RPC_URL', 'http://127.0.0.1:58000')
POLL_INTERVAL_S = int(os.environ.get('PARTICIPATION_POLL_INTERVAL_S', '15'))
PERSIST_EVERY_N_POLLS = 4


def _cert_id(vote_bytes: list[int]) -> str:
    return hashlib.sha256(bytes(vote_bytes)).hexdigest()


def _bytes_to_hex(byte_list: list[int]) -> str:
    return bytes(byte_list).hex()


class ParticipationTracker:
    def __init__(self) -> None:
        self.certs_seen: dict[str, dict[str, Any]] = {}
        self.finalizer_hits: dict[str, int] = {}
        self.pos_finalization_events: list[dict[str, Any]] = []
        self.started_at: float = time.time()
        self.last_poll_at: float | None = None
        self.last_poll_ok: bool | None = None
        self.last_cert_id: str | None = None
        self.poll_count: int = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=8.0)
        self._load_state()

    def _load_state(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            raw = json.loads(STATE_PATH.read_text())
            self.certs_seen = raw.get('certs_seen', {})
            self.finalizer_hits = raw.get('finalizer_hits', {})
            self.pos_finalization_events = raw.get('pos_finalization_events', [])
            self.started_at = raw.get('started_at', time.time())
        except Exception:
            pass

    def _save_state(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix('.json.tmp')
            tmp.write_text(json.dumps({
                'certs_seen': self.certs_seen,
                'finalizer_hits': self.finalizer_hits,
                'pos_finalization_events': self.pos_finalization_events,
                'started_at': self.started_at,
            }))
            tmp.replace(STATE_PATH)
        except Exception:
            pass

    async def _rpc_fat_pointer(self) -> dict | None:
        try:
            r = await self._client.post(RPC_URL, json={
                'jsonrpc': '2.0',
                'method': 'get_tfl_fat_pointer_to_bft_chain_tip',
                'params': [],
                'id': 1,
            })
            r.raise_for_status()
            data = r.json()
            if data.get('error'):
                return None
            return data.get('result')
        except Exception:
            return None

    async def _poll_once(self) -> None:
        self.poll_count += 1
        self.last_poll_at = time.time()
        fp = await self._rpc_fat_pointer()
        if fp is None:
            self.last_poll_ok = False
            return
        self.last_poll_ok = True
        vote_bytes = fp.get('vote_for_block_without_finalizer_public_key') or []
        sigs = fp.get('signatures') or []
        if not vote_bytes or not sigs:
            return
        cid = _cert_id(vote_bytes)
        self.last_cert_id = cid
        async with self._lock:
            if cid in self.certs_seen:
                return
            signers = []
            for s in sigs:
                pk = s.get('pub_key')
                if pk:
                    signers.append(bytes(pk)[::-1].hex())
            import struct
            try:
                pos_height = struct.unpack('<Q', bytes(vote_bytes[32:40]))[0]
            except Exception:
                pos_height = None
            finalized_pow_height = None
            try:
                r = await self._client.post(RPC_URL, json={
                    'jsonrpc': '2.0', 'method': 'get_tfl_final_block_height_and_hash',
                    'params': [], 'id': 1,
                })
                r.raise_for_status()
                data = r.json()
                res = data.get('result')
                if isinstance(res, dict):
                    finalized_pow_height = res.get('height')
            except Exception:
                pass
            self.certs_seen[cid] = {
                'first_seen_at': self.last_poll_at,
                'signer_count': len(signers),
                'signers': signers,
                'pos_height': pos_height,
                'finalized_pow_height': finalized_pow_height,
            }
            if pos_height is not None:
                self.pos_finalization_events.append({
                    'pos_height': pos_height,
                    'finalized_pow_height': finalized_pow_height,
                    'signer_count': len(signers),
                    'observed_at': self.last_poll_at,
                })
                if len(self.pos_finalization_events) > 500:
                    self.pos_finalization_events = self.pos_finalization_events[-500:]
            for pk in signers:
                self.finalizer_hits[pk] = self.finalizer_hits.get(pk, 0) + 1
            self._save_state()
        

    async def _loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except Exception:
                pass
            await asyncio.sleep(POLL_INTERVAL_S)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def get_pow_to_pos_map(self, pow_heights: list[int]) -> dict[int, dict[str, Any]]:
        """For each requested PoW height, find the smallest PoS cert that had
        finalized_pow_height >= that PoW height at observation time.
        Returns dict mapping pow_height -> {pos_height, signer_count, observed_at}."""
        events = sorted(
            [e for e in self.pos_finalization_events if e.get('pos_height') is not None and e.get('finalized_pow_height') is not None],
            key=lambda e: e['pos_height'],
        )
        out = {}
        for h in pow_heights:
            match = next((e for e in events if e['finalized_pow_height'] >= h), None)
            if match is not None:
                out[h] = {
                    'pos_height': match['pos_height'],
                    'signer_count': match['signer_count'],
                    'observed_at': match['observed_at'],
                }
        return out

    def get_stats(self) -> dict[str, Any]:
        total_certs = len(self.certs_seen)
        per_finalizer = {}
        for pk, hits in self.finalizer_hits.items():
            rate = (hits / total_certs) if total_certs > 0 else 0.0
            per_finalizer[pk] = {'hits': hits, 'rate': rate}
        return {
            'started_at': self.started_at,
            'last_poll_at': self.last_poll_at,
            'last_poll_ok': self.last_poll_ok,
            'poll_count': self.poll_count,
            'total_certs': total_certs,
            'last_cert_id': self.last_cert_id,
            'per_finalizer': per_finalizer,
            'poll_interval_s': POLL_INTERVAL_S,
        }


_tracker: ParticipationTracker | None = None


def get_tracker() -> ParticipationTracker:
    global _tracker
    if _tracker is None:
        _tracker = ParticipationTracker()
    return _tracker
