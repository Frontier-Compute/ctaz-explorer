import copy
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx


SNOMP_BASE_URL = os.environ.get('SNOMP_BASE_URL', 'http://127.0.0.1:8888').rstrip('/')
SNOMP_STATS_URL = f'{SNOMP_BASE_URL}/api/stats'
SNOMP_BLOCKS_URL = f'{SNOMP_BASE_URL}/api/blocks'
SNOMP_PAYMENTS_URL = f'{SNOMP_BASE_URL}/api/payments'

ZECMININGPOOL_BASE_URL = os.environ.get('ZECMININGPOOL_BASE_URL', 'https://pool.zecminingpool.com').rstrip('/')
ZECMININGPOOL_STATS_URL = f'{ZECMININGPOOL_BASE_URL}/api/stats'
ZECMININGPOOL_POOL_STATS_URL = f'{ZECMININGPOOL_BASE_URL}/api/pool/stats'
ZECMININGPOOL_BLOCKS_URL = f'{ZECMININGPOOL_BASE_URL}/api/blocks'
ZECMININGPOOL_PAYOUTS_URL = f'{ZECMININGPOOL_BASE_URL}/api/payouts'

HTTP_TIMEOUT_S = 5.0
SNOMP_CACHE_TTL_S = 30
ZECMININGPOOL_CACHE_TTL_S = 60
DAY_SECONDS = 86400

_cache_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}


def iso_utc(ts: float | int | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _coerce_int(value: Any) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch(value: Any) -> int | None:
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000.0
        return int(raw)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _epoch(int(text))
        formats = (
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%dT%H:%M:%S.%f%z',
            '%Y-%m-%dT%H:%M:%S%z',
            '%Y-%m-%dT%H:%M:%S.%f',
            '%Y-%m-%dT%H:%M:%S',
        )
        for fmt in formats:
            try:
                dt = datetime.strptime(text, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except ValueError:
                continue
    return None


def _mh_s(value: Any) -> float | None:
    raw = _coerce_float(value)
    if raw is None:
        return None
    return raw / 1_000_000.0


def _format_mh_s(value: Any) -> str:
    mh = _mh_s(value)
    if mh is None:
        return 'unknown'
    return f'{mh:.2f}'


def _format_mh_value(value: Any) -> str:
    mh = _coerce_float(value)
    if mh is None:
        return 'unknown'
    return f'{mh:.2f}'


def _format_zec(value: Any) -> str:
    amount = _coerce_float(value)
    if amount is None:
        return 'unknown'
    return f'{amount:.8f}'


def _format_count(value: Any) -> str:
    count = _coerce_int(value)
    if count is None:
        return 'unknown'
    return f'{count:,}'


def _status_class(reachable: bool, degraded: bool) -> str:
    if not reachable:
        return 'unknown'
    if degraded:
        return 'notyet'
    return 'finalized'


def _with_cache_meta(value: Any, age: float, ttl: int) -> Any:
    cloned = copy.deepcopy(value)
    if isinstance(cloned, dict):
        cloned['cache_age_s'] = round(age, 1)
        cloned['cache_ttl_s'] = ttl
    return cloned


def _cached(key: str, ttl: int, loader):
    with _cache_lock:
        now = time.monotonic()
        entry = _cache.get(key)
        if entry and (now - entry['cached_at']) < ttl:
            return _with_cache_meta(entry['value'], now - entry['cached_at'], ttl)
        value = loader()
        cached_at = time.monotonic()
        _cache[key] = {
            'cached_at': cached_at,
            'value': copy.deepcopy(value),
        }
        return _with_cache_meta(value, 0.0, ttl)


def _fetch_json(url: str) -> Any:
    response = httpx.get(
        url,
        timeout=HTTP_TIMEOUT_S,
        follow_redirects=True,
        headers={'User-Agent': 'ctaz-explorer/mining'},
    )
    response.raise_for_status()
    return response.json()


def _probe_json(url: str) -> tuple[Any | None, bool, str | None]:
    try:
        return _fetch_json(url), True, None
    except Exception as exc:
        return None, False, str(exc)


def _iter_records(payload: Any, markers: tuple[str, ...]):
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
            continue
        if not isinstance(item, dict):
            continue
        if any(marker in item for marker in markers):
            yield item
            continue
        for value in reversed(list(item.values())):
            if isinstance(value, (dict, list)):
                stack.append(value)


def _record_time(record: dict[str, Any]) -> int | None:
    for key in ('found_at', 'foundAt', 'created_at', 'createdAt', 'paid_at', 'paidAt', 'time', 'timestamp', 'ts'):
        ts = _epoch(record.get(key))
        if ts is not None:
            return ts
    return None


def _record_height(record: dict[str, Any]) -> int | None:
    for key in ('height', 'blockHeight', 'block_height', 'paidBlockHeight', 'lastPaidBlockHeight'):
        height = _coerce_int(record.get(key))
        if height is not None:
            return height
    return None


def _count_recent(records: list[dict[str, Any]], now_ts: int) -> int | None:
    recent = [record for record in records if (_record_time(record) or 0) >= (now_ts - DAY_SECONDS)]
    if recent:
        return len(recent)
    if not records:
        return None
    return 0


def _latest_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    latest_key = -1
    for record in records:
        ts = _record_time(record)
        height = _record_height(record)
        key = ts if ts is not None else (height if height is not None else -1)
        if key > latest_key:
            latest = record
            latest_key = key
    return latest


def _payment_payload(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    amount = None
    for key in ('amount_zec', 'amount', 'amountZec', 'value'):
        amount = _coerce_float(record.get(key))
        if amount is not None:
            break
    return {
        'amount_zec': amount,
        'amount_display': _format_zec(amount) if amount is not None else 'unknown',
        'txid': record.get('txid'),
        'block_height': _record_height(record),
        'at': _record_time(record),
    }


def _base_panel(label: str, slug: str, homepage: str) -> dict[str, Any]:
    return {
        'label': label,
        'slug': slug,
        'homepage': homepage,
        'generated_at': iso_utc(),
        'reachable': False,
        'degraded': True,
        'status_label': 'offline',
        'status_class': 'unknown',
        'error': None,
        'hashrate_mh_s': None,
        'hashrate_mh_display': 'unknown',
        'workers': None,
        'workers_display': 'unknown',
        'blocks_24h': None,
        'blocks_24h_display': 'unknown',
        'blocks_total': None,
        'blocks_total_display': 'unknown',
        'last_block_height': None,
        'last_block_height_display': 'unknown',
        'last_block_found_at': None,
        'last_paid_block_height': None,
        'last_paid_block_height_display': 'unknown',
        'last_payout': None,
        'last_payout_display': 'unknown',
        'total_paid_zec': None,
        'total_paid_display': 'unknown',
        'probes': {},
        'notes': [],
    }


def _finalize_panel(panel: dict[str, Any]) -> dict[str, Any]:
    panel['generated_at'] = iso_utc()
    panel['status_label'] = 'live' if panel['reachable'] and not panel['degraded'] else ('degraded' if panel['reachable'] else 'offline')
    panel['status_class'] = _status_class(panel['reachable'], panel['degraded'])
    panel['hashrate_mh_display'] = _format_mh_value(panel.get('hashrate_mh_s'))
    panel['workers_display'] = _format_count(panel.get('workers'))
    panel['blocks_24h_display'] = _format_count(panel.get('blocks_24h'))
    panel['blocks_total_display'] = _format_count(panel.get('blocks_total'))
    panel['last_block_height_display'] = _format_count(panel.get('last_block_height'))
    if panel.get('last_paid_block_height_display') == 'none yet' and panel.get('last_paid_block_height') is None:
        panel['last_paid_block_height_display'] = 'none yet'
    else:
        panel['last_paid_block_height_display'] = _format_count(panel.get('last_paid_block_height'))
    panel['total_paid_display'] = _format_zec(panel.get('total_paid_zec'))
    payout = panel.get('last_payout')
    if payout:
        amount_display = payout.get('amount_display') or 'unknown'
        when = payout.get('at')
        if when is not None:
            panel['last_payout_display'] = f'{amount_display} ZEC · {iso_utc(when)}'
        else:
            panel['last_payout_display'] = f'{amount_display} ZEC'
    return panel


def fetch_snomp_local_stats() -> dict[str, Any]:
    def load() -> dict[str, Any]:
        panel = _base_panel('London s-nomp', 'snomp', SNOMP_BASE_URL)
        stats_payload, stats_ok, stats_error = _probe_json(SNOMP_STATS_URL)
        panel['probes']['stats'] = {'url': SNOMP_STATS_URL, 'ok': stats_ok, 'error': stats_error}
        if not stats_ok or not isinstance(stats_payload, dict):
            panel['error'] = stats_error or 'stats endpoint unavailable'
            panel['notes'].append('Primary s-nomp stats probe failed.')
            return _finalize_panel(panel)

        blocks_payload, blocks_ok, blocks_error = _probe_json(SNOMP_BLOCKS_URL)
        payments_payload, payments_ok, payments_error = _probe_json(SNOMP_PAYMENTS_URL)
        panel['probes']['blocks'] = {'url': SNOMP_BLOCKS_URL, 'ok': blocks_ok, 'error': blocks_error}
        panel['probes']['payments'] = {'url': SNOMP_PAYMENTS_URL, 'ok': payments_ok, 'error': payments_error}

        pool = ((stats_payload.get('pools') or {}).get('zcash') or {})
        pool_stats = pool.get('poolStats') or {}
        now_ts = int(time.time())

        block_records = list(_iter_records(blocks_payload or {}, ('height', 'blockHeight', 'block_height', 'time', 'foundAt', 'found_at')))
        payment_records = list(_iter_records(payments_payload or [], ('txid', 'amount_zec', 'amount', 'paidAt', 'paid_at', 'created_at')))

        panel['reachable'] = True
        panel['degraded'] = not (blocks_ok and payments_ok)
        panel['hashrate_mh_s'] = _mh_s(pool.get('hashrate') or ((stats_payload.get('algos') or {}).get('equihash') or {}).get('hashrate'))
        panel['workers'] = _coerce_int(pool.get('workerCount') or pool.get('minerCount') or (stats_payload.get('global') or {}).get('workers'))

        known_total_blocks = _coerce_int(pool_stats.get('validBlocks'))
        blocks_24h = _count_recent(block_records, now_ts)
        panel['blocks_total'] = known_total_blocks if known_total_blocks is not None else (len(block_records) if block_records else None)
        if blocks_24h is None and panel['blocks_total'] == 0:
            blocks_24h = 0
        panel['blocks_24h'] = blocks_24h

        last_block = _latest_record(block_records)
        panel['last_block_height'] = _record_height(last_block) if last_block else None
        panel['last_block_found_at'] = _record_time(last_block) if last_block else None

        total_paid = _coerce_float(pool_stats.get('totalPaid'))
        panel['total_paid_zec'] = total_paid
        last_payment = _payment_payload(_latest_record(payment_records))
        panel['last_payout'] = last_payment
        panel['last_paid_block_height'] = last_payment.get('block_height') if last_payment else None

        if last_payment is None:
            if total_paid in (0, 0.0):
                panel['last_payout_display'] = 'none yet'
                panel['last_paid_block_height_display'] = 'none yet'
                panel['notes'].append('s-nomp reports no payouts yet.')
            else:
                panel['last_payout_display'] = 'unknown'
                panel['notes'].append('Payout total is present, but s-nomp did not expose a usable payment record.')
        elif panel['last_paid_block_height'] is None:
            panel['notes'].append('s-nomp payment records did not expose a paid block height.')

        if panel['last_block_height'] is None:
            if panel['blocks_total'] in (0, None):
                panel['notes'].append('No pool-found block record was exposed by s-nomp.')
            else:
                panel['notes'].append('Block total is present, but the detailed block endpoint did not expose a latest height.')

        if not blocks_ok:
            panel['notes'].append('s-nomp block detail endpoint was unavailable; 24h block count may be unknown.')
        if not payments_ok:
            panel['notes'].append('s-nomp payment detail endpoint was unavailable; last payout details may be unknown.')

        return _finalize_panel(panel)

    return _cached('snomp-local', SNOMP_CACHE_TTL_S, load)


def fetch_zecminingpool_stats() -> dict[str, Any]:
    def load() -> dict[str, Any]:
        panel = _base_panel('pool.zecminingpool.com', 'zecminingpool', ZECMININGPOOL_BASE_URL)

        primary_payload = None
        primary_url = ZECMININGPOOL_POOL_STATS_URL
        primary_kind = 'pool_stats'
        stats_error = None

        primary_payload, primary_ok, stats_error = _probe_json(ZECMININGPOOL_POOL_STATS_URL)
        panel['probes']['pool_stats'] = {'url': ZECMININGPOOL_POOL_STATS_URL, 'ok': primary_ok, 'error': stats_error}
        if not primary_ok or not isinstance(primary_payload, dict):
            primary_payload, primary_ok, stats_error = _probe_json(ZECMININGPOOL_STATS_URL)
            primary_url = ZECMININGPOOL_STATS_URL
            primary_kind = 'stats'
            panel['probes']['stats'] = {'url': ZECMININGPOOL_STATS_URL, 'ok': primary_ok, 'error': stats_error}

        if not primary_ok or not isinstance(primary_payload, dict):
            panel['error'] = stats_error or 'public pool stats endpoint unavailable'
            panel['notes'].append('Primary public pool stats probe failed.')
            return _finalize_panel(panel)

        blocks_payload, blocks_ok, blocks_error = _probe_json(ZECMININGPOOL_BLOCKS_URL)
        payouts_payload, payouts_ok, payouts_error = _probe_json(ZECMININGPOOL_PAYOUTS_URL)
        panel['probes']['blocks'] = {'url': ZECMININGPOOL_BLOCKS_URL, 'ok': blocks_ok, 'error': blocks_error}
        panel['probes']['payouts'] = {'url': ZECMININGPOOL_PAYOUTS_URL, 'ok': payouts_ok, 'error': payouts_error}

        now_ts = int(time.time())
        block_records = list(_iter_records(blocks_payload or [], ('height', 'blockHeight', 'block_height', 'found_at', 'foundAt')))
        payment_records = list(_iter_records(payouts_payload or [], ('txid', 'amount_zec', 'amount', 'created_at', 'createdAt')))

        panel['reachable'] = True
        panel['degraded'] = not (blocks_ok and payouts_ok)

        if primary_kind == 'pool_stats':
            panel['hashrate_mh_s'] = _mh_s(primary_payload.get('hashrate_current') or primary_payload.get('hashrate_estimate'))
            panel['workers'] = _coerce_int(primary_payload.get('connected_miners'))
            panel['blocks_total'] = _coerce_int(primary_payload.get('total_blocks'))
        else:
            node = ((primary_payload.get('nodes') or [{}]) or [{}])[0]
            matured = _coerce_int(primary_payload.get('maturedTotal'))
            immature = _coerce_int(primary_payload.get('immatureTotal'))
            candidates = _coerce_int(primary_payload.get('candidatesTotal'))
            panel['hashrate_mh_s'] = _mh_s(primary_payload.get('hashrate'))
            panel['workers'] = _coerce_int(primary_payload.get('workersTotal') or primary_payload.get('minersTotal'))
            if any(value is not None for value in (matured, immature, candidates)):
                panel['blocks_total'] = sum(value or 0 for value in (matured, immature, candidates))
            panel['notes'].append(f'Using fallback stats payload from {primary_url}.')
            if _coerce_int(node.get('height')) is not None and not block_records:
                panel['notes'].append('Network height is available, but no pool-found block list was returned.')
            panel['degraded'] = True

        blocks_24h = _count_recent(block_records, now_ts)
        if blocks_24h is None and panel['blocks_total'] == 0:
            blocks_24h = 0
        panel['blocks_24h'] = blocks_24h

        last_block = _latest_record(block_records)
        panel['last_block_height'] = _record_height(last_block) if last_block else None
        panel['last_block_found_at'] = _record_time(last_block) if last_block else _epoch(((primary_payload.get('stats') or {}).get('lastBlockFound')))

        last_payment = _payment_payload(_latest_record(payment_records))
        panel['last_payout'] = last_payment
        panel['last_paid_block_height'] = last_payment.get('block_height') if last_payment else None

        if last_payment is None:
            if payouts_ok and not payment_records:
                panel['last_payout_display'] = 'none yet'
                panel['last_paid_block_height_display'] = 'none yet'
                panel['notes'].append('Public pool returned no payout records yet.')
            else:
                panel['last_payout_display'] = 'unknown'
                panel['notes'].append('Public pool payout endpoint did not expose a usable payout record.')
        elif panel['last_paid_block_height'] is None:
            panel['notes'].append('Public payout endpoint exposed amount and txid, but not a payout block height.')
        if not blocks_ok:
            panel['notes'].append('Public block list probe failed; 24h block count may be unknown.')
        if not payouts_ok:
            panel['notes'].append('Public payout probe failed; last payout details may be unknown.')

        return _finalize_panel(panel)

    return _cached('zecminingpool', ZECMININGPOOL_CACHE_TTL_S, load)


def build_mining_payload() -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        local_future = executor.submit(fetch_snomp_local_stats)
        remote_future = executor.submit(fetch_zecminingpool_stats)
        local = local_future.result()
        remote = remote_future.result()

    panels = [local, remote]
    return {
        'generated_at': iso_utc(),
        'local': local,
        'remote': remote,
        'panels': panels,
        'reachability': {
            'local': bool(local.get('reachable')),
            'remote': bool(remote.get('reachable')),
            'all': all(panel.get('reachable') for panel in panels),
        },
        'cache': {
            'local_ttl_s': SNOMP_CACHE_TTL_S,
            'remote_ttl_s': ZECMININGPOOL_CACHE_TTL_S,
        },
    }
