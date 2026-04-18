import copy
import json
import os
import pathlib
import subprocess
import time
from datetime import datetime, timezone


SUPER_LOCAL_STATE_SCRIPT = pathlib.Path("/root/bin-super-local-state.sh")
PEER_CACHE_TTL_S = 30
DEFAULT_LOCAL_LABEL = os.environ.get("SUPER_LOCAL_LABEL", "frankfurt")
DEFAULT_PEER_LABEL = os.environ.get("SUPER_PEER_LABEL", "london")
DEFAULT_PEER_HOST = os.environ.get("SUPER_PEER_HOST", "london")

_peer_state_cache = {}


def iso_utc(ts=None):
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def recent_handbacks(limit=10):
    files = sorted(
        pathlib.Path("/root/.zeven").glob("HANDBACK-*.md"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    items = []
    for path in files[:limit]:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        items.append(
            {
                "file": path.name,
                "path": str(path),
                "mtime": iso_utc(stat.st_mtime),
                "mtime_epoch": int(stat.st_mtime),
                "bytes": stat.st_size,
                "title": _handback_title(path),
            }
        )
    return items


def fetch_local_vps_state():
    return _load_state_from_command([str(SUPER_LOCAL_STATE_SCRIPT)], fallback_host=DEFAULT_LOCAL_LABEL)


def fetch_peer_vps_state(peer_host):
    peer_host = (peer_host or DEFAULT_PEER_HOST or "").strip()
    if not peer_host:
        return _error_state(DEFAULT_PEER_LABEL, "peer host not configured")

    now = time.time()
    cached = _peer_state_cache.get(peer_host)
    if cached and (now - cached["cached_at"]) < PEER_CACHE_TTL_S:
        state = copy.deepcopy(cached["state"])
        state["cache_age_s"] = round(now - cached["cached_at"], 1)
        return state

    state = _load_state_from_command(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=1",
            peer_host,
            "/root/bin-super-local-state.sh",
        ],
        fallback_host=DEFAULT_PEER_LABEL or peer_host,
    )
    state["peer_host"] = peer_host
    _peer_state_cache[peer_host] = {"cached_at": now, "state": copy.deepcopy(state)}
    state["cache_age_s"] = 0.0
    return state


def compute_alignment_flags(local, peer):
    local = local or {}
    peer = peer or {}
    local_services = local.get("services") or {}
    peer_services = peer.get("services") or {}
    service_mismatch = []
    if peer.get("reachable", False):
        for service in sorted(set(local_services) | set(peer_services)):
            local_state = local_services.get(service, "missing")
            peer_state = peer_services.get(service, "missing")
            if local_state != peer_state:
                service_mismatch.append(
                    {
                        "service": service,
                        "local": local_state,
                        "peer": peer_state,
                    }
                )

    local_height = _safe_int((local.get("zebrad") or {}).get("h"))
    peer_height = _safe_int((peer.get("zebrad") or {}).get("h"))
    height_delta = abs(local_height - peer_height) if local_height is not None and peer_height is not None else None

    local_hash = ((local.get("zebrad") or {}).get("besthash") or "").strip()
    peer_hash = ((peer.get("zebrad") or {}).get("besthash") or "").strip()
    besthash_mismatch = bool(
        local_height is not None
        and peer_height is not None
        and local_height == peer_height
        and local_hash
        and peer_hash
        and local_hash != peer_hash
    )

    local_mem = _safe_float(local.get("mem_pct"))
    peer_mem = _safe_float(peer.get("mem_pct"))
    memory_delta_pct = round(abs(local_mem - peer_mem), 1) if local_mem is not None and peer_mem is not None else None
    memory_drift = memory_delta_pct is not None and memory_delta_pct >= 20.0

    local_disk = _safe_float(local.get("disk_pct"))
    peer_disk = _safe_float(peer.get("disk_pct"))
    disk_delta_pct = round(abs(local_disk - peer_disk), 1) if local_disk is not None and peer_disk is not None else None
    disk_drift = disk_delta_pct is not None and disk_delta_pct >= 15.0

    drifts = []
    if not peer.get("reachable", False):
        drifts.append(peer.get("error") or "peer unreachable")
    if height_delta not in (None, 0):
        drifts.append(f"zebrad height delta {height_delta}")
    if besthash_mismatch:
        drifts.append("besthash mismatch at matched height")
    if service_mismatch:
        drifts.append(f"{len(service_mismatch)} service mismatch")
    if memory_drift and memory_delta_pct is not None:
        drifts.append(f"memory drift {memory_delta_pct:.1f}pp")
    if disk_drift and disk_delta_pct is not None:
        drifts.append(f"disk drift {disk_delta_pct:.1f}pp")

    return {
        "zebrad_height_delta": height_delta,
        "services_mismatch": service_mismatch,
        "memory_drift": memory_drift,
        "memory_delta_pct": memory_delta_pct,
        "disk_drift": disk_drift,
        "disk_delta_pct": disk_delta_pct,
        "besthash_mismatch": besthash_mismatch,
        "peer_unreachable": not peer.get("reachable", False),
        "in_tune": not drifts,
        "drifts": drifts,
    }


def build_super_payload(peer_host=None, local_label=DEFAULT_LOCAL_LABEL, peer_label=DEFAULT_PEER_LABEL):
    local = fetch_local_vps_state()
    local["recent_handbacks"] = recent_handbacks(limit=10)
    local["display_host"] = local_label or local.get("host") or DEFAULT_LOCAL_LABEL

    peer = fetch_peer_vps_state(peer_host or DEFAULT_PEER_HOST)
    peer["display_host"] = peer_label or peer.get("host") or DEFAULT_PEER_LABEL

    alignment = compute_alignment_flags(local, peer)
    merged = _merge_handbacks(local, peer, limit=5)
    return {
        "generated_at": iso_utc(),
        "local": local,
        "peer": peer,
        "panels": [peer, local],
        "alignment": alignment,
        "merged_handbacks": merged,
        "peer_host": peer.get("peer_host") or peer_host or DEFAULT_PEER_HOST,
        "peer_cache_ttl_s": PEER_CACHE_TTL_S,
    }


def _handback_title(path):
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return path.stem
    for line in lines[:20]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    for line in lines[:20]:
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return path.stem


def _normalize_state(payload, fallback_host):
    payload = payload if isinstance(payload, dict) else {}
    payload.setdefault("host", fallback_host)
    payload.setdefault("ts", iso_utc())
    payload.setdefault("reachable", True)
    payload.setdefault("zebrad", {})
    payload.setdefault("services", {})
    payload.setdefault("tmux_sessions", [])
    payload.setdefault("recent_handbacks", [])
    payload.setdefault("disk_pct", None)
    payload.setdefault("mem_used_mb", None)
    payload.setdefault("mem_total_mb", None)
    payload.setdefault("mem_pct", None)
    return payload


def _error_state(host, message):
    return {
        "host": host,
        "ts": iso_utc(),
        "reachable": False,
        "error": message,
        "zebrad": {},
        "services": {},
        "tmux_sessions": [],
        "recent_handbacks": [],
        "disk_pct": None,
        "mem_used_mb": None,
        "mem_total_mb": None,
        "mem_pct": None,
    }


def _load_state_from_command(cmd, fallback_host):
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except Exception as exc:
        return _error_state(fallback_host, str(exc))

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return _error_state(fallback_host, detail)

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return _error_state(fallback_host, f"invalid peer payload: {exc}")

    return _normalize_state(payload, fallback_host=fallback_host)


def _merge_handbacks(local, peer, limit=5):
    timeline = []
    for state in (peer, local):
        label = state.get("display_host") or state.get("host") or "node"
        for item in state.get("recent_handbacks") or []:
            entry = dict(item)
            entry["node"] = label
            timeline.append(entry)
    timeline.sort(key=lambda item: item.get("mtime_epoch") or 0, reverse=True)
    return timeline[:limit]


def _safe_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _safe_float(value):
    try:
        return float(value)
    except Exception:
        return None
