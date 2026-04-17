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

    def get_recent_leet_capture(self, within_seconds: int = 3600):
        """Return the most recent observed cert whose pos_height ends in 337
        (1337, 2337, 3337, ...) if observed within the time window."""
        now = time.time()
        leet = [
            e for e in self.pos_finalization_events
            if e.get('pos_height') is not None
            and e['pos_height'] % 1000 == 337
            and (now - (e.get('observed_at') or 0)) < within_seconds
        ]
        if not leet:
            return None
        latest = max(leet, key=lambda e: e.get('observed_at') or 0)
        return {
            'pos_height': latest['pos_height'],
            'signer_count': latest.get('signer_count') or 0,
            'observed_at': latest.get('observed_at'),
            'age_seconds': int(now - (latest.get('observed_at') or now)),
            'finalized_pow_height': latest.get('finalized_pow_height'),
        }

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

    def get_scorecard(self, recent_n: int = 20) -> dict[str, dict[str, Any]]:
        """For each finalizer pubkey (node-log order), compute participation
        rate overall, recent-window rate, trend (recent_rate vs overall),
        last-seen cert index, and letter grade."""
        total_certs = len(self.certs_seen)
        certs_sorted = sorted(self.certs_seen.items(), key=lambda kv: kv[1].get('first_seen_at') or 0)
        recent_slice = certs_sorted[-recent_n:] if len(certs_sorted) > recent_n else certs_sorted
        recent_total = len(recent_slice)
        out = {}
        for pk, hits in self.finalizer_hits.items():
            overall_rate = (hits / total_certs) if total_certs > 0 else 0.0
            recent_hits = sum(1 for _, c in recent_slice if pk in (c.get('signers') or []))
            recent_rate = (recent_hits / recent_total) if recent_total > 0 else 0.0
            last_seen_idx = None
            for i in range(len(certs_sorted) - 1, -1, -1):
                if pk in (certs_sorted[i][1].get('signers') or []):
                    last_seen_idx = len(certs_sorted) - 1 - i
                    break
            trend = 'flat'
            if total_certs >= recent_n and recent_rate > overall_rate + 0.05:
                trend = 'up'
            elif total_certs >= recent_n and recent_rate < overall_rate - 0.05:
                trend = 'down'
            if overall_rate >= 0.95:
                grade = 'A'
            elif overall_rate >= 0.8:
                grade = 'B'
            elif overall_rate >= 0.5:
                grade = 'C'
            elif overall_rate > 0:
                grade = 'D'
            else:
                grade = 'F'
            out[pk] = {
                'hits': hits,
                'overall_rate': overall_rate,
                'recent_rate': recent_rate,
                'trend': trend,
                'last_seen_ago': last_seen_idx,
                'grade': grade,
            }
        return out

    def get_chain_health(self, recent_n: int = 40, degraded_delta: int = 2) -> dict[str, Any]:
        """Compute chain-health indicators from observed certs.
        Returns recent-window stats, median signer count, latest cert status,
        and whether the latest is "degraded" (signers >= median-delta below).
        """
        events = sorted(
            [e for e in self.pos_finalization_events if e.get('pos_height') is not None and e.get('signer_count') is not None],
            key=lambda e: e['pos_height'],
        )
        if not events:
            return {
                'total_events': 0,
                'sparkline': [],
            }
        window = events[-recent_n:] if len(events) > recent_n else events
        signer_counts = [e['signer_count'] for e in window]
        sorted_counts = sorted(signer_counts)
        mid = len(sorted_counts) // 2
        if len(sorted_counts) % 2 == 0 and len(sorted_counts) > 0:
            median = (sorted_counts[mid - 1] + sorted_counts[mid]) / 2
        else:
            median = sorted_counts[mid] if sorted_counts else 0
        latest = events[-1]
        latest_count = latest['signer_count']
        degraded = latest_count is not None and latest_count < median - degraded_delta
        finalized = latest.get('finalized_pow_height')
        sparkline = [e['signer_count'] for e in window]
        min_sig = min(signer_counts) if signer_counts else 0
        max_sig = max(signer_counts) if signer_counts else 0
        avg_sig = sum(signer_counts) / len(signer_counts) if signer_counts else 0
        return {
            'total_events': len(events),
            'window_size': len(window),
            'median_signers': median,
            'avg_signers': round(avg_sig, 2),
            'min_signers': min_sig,
            'max_signers': max_sig,
            'latest_pos_height': latest['pos_height'],
            'latest_signer_count': latest_count,
            'latest_finalized_pow': finalized,
            'degraded': degraded,
            'degraded_threshold': median - degraded_delta,
            'sparkline': sparkline,
            'events': window,
        }

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
