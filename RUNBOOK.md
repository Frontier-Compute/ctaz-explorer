# Frontier Compute Supernode — Disaster Recovery Runbook

Last updated: 2026-04-18 04:35 UTC

## Architecture

Two-VPS supernode, designed for bulletproof operation:
- **London** (78.141.247.162): primary operator identity (Pub{93bb}), ctaz-explorer, zat-explorer, ZAP1 API, ctaz-zainod, zebra-crosslink active validator
- **Frankfurt** (136.244.95.226): secondary observer (Pub{0b55} generated-local, 0 stake), ctaz-explorer mirror, HyperDeck trading, zebra-crosslink fresh-sync node, Hyperliquid paper takers CL+SILVER

## Operator identity

Pub{93bb} SHA-256 seed: `ea5a31f67b0067737ec79d54a7e3b17c14f0b9892147cdc06f79df2ec79a5378`

Seed backups (6-deep as of 2026-04-18):
1. Live: `/var/lib/zebra-crosslink/zebra_crosslink_workshop_season_one_v2_37482_cache_delete_me/secret.seed` (london)
2. London permanent: `/root/.zeven/ctaz-identity/secret.seed.pub93bb.bak`
3. Frankfurt permanent: `/root/.zeven/ctaz-identity/secret.seed.pub93bb.bak`
4. Laptop hex: `C:/Users/Skander/.seed-backups/secret.seed.london.*.hex`
5. London /tmp lockboxes: `/tmp/SEED-LOCKBOX-*.bak` (multiple timestamped)
6. London snap-recovery backups: `/root/.zeven/ctaz-identity/secret.seed.snap-recovery-*.bak`

**Seed integrity check (run on either VPS):**
```bash
sha256sum /root/.zeven/ctaz-identity/secret.seed.pub93bb.bak
# expected: ea5a31f67b0067737ec79d54a7e3b17c14f0b9892147cdc06f79df2ec79a5378
```

## Critical services (all systemd, Restart=always)

| Service | Port | Hosts | Purpose |
|---|---|---|---|
| ctaz-explorer | 8088 | both | block explorer frontend |
| zat-explorer-signature | 8090 | london | zat.frontiercompute.cash |
| zat-explorer-institutional | 8089 | london | zat.frontiercompute.cash/institutional |
| ctaz-zainod | various | london | gRPC Zaino bridge |
| zebrad-tmux | 58000 | london | zebra-crosslink active validator (tmux wrapped) |
| zebrad-frankfurt-tmux | 58000 | frankfurt | zebra-crosslink observer (tmux wrapped) |

**Self-healing:** zebrad tmux services run a retry loop (`/root/run-zebrad-london-loop.sh`, `/root/run-zebrad-frankfurt-loop.sh`) that re-launches zebrad every 10s if it exits. Systemd also auto-starts on reboot.

## Background crons

| Cron | Cadence | Purpose |
|---|---|---|
| `/root/bin-supernode-handback-sync.sh` | every 3 min | bidirectional rsync of `/root/.zeven/HANDBACK-*`, `ACTUALIZE-*`, `MISSION-*` between VPSs |
| `/root/bin-supernode-healthcheck.sh` | every 1 min | writes `/var/run/supernode-health.json` with service statuses |

## Full disaster recovery (both VPSs gone)

1. Spin up two fresh Ubuntu 22.04+ VPSs (>= 4GB RAM, 100GB disk)
2. Restore seed hex backup from laptop:
   ```bash
   xxd -r C:/Users/Skander/.seed-backups/secret.seed.london.*.hex > /tmp/seed
   # verify sha256 matches ea5a31f6...
   scp /tmp/seed newhost:/root/.zeven/ctaz-identity/secret.seed.pub93bb.bak
   ```
3. Restore mempalace from latest backup on whichever VPS is reachable:
   ```bash
   cp /root/.zeven/mempalace-backup-latest.tar.gz /tmp/
   # or from laptop: scp C:/Users/Skander/.mempalace/palace/ newhost:...
   ```
4. Clone `Frontier-Compute/ctaz-explorer` repo, install deps (`pip3 install --break-system-packages fastapi uvicorn jinja2 httpx`), create systemd units from templates in this runbook, enable+start.
5. Deploy zebra-crosslink binary (from london backup or rebuild from `crosslink-v2` source), create config based on `/root/zebrad-v2-config.toml` template, place seed into cache dir per codex's HANDBACK-WS-CTAZ-NODE-RECOVERY protocol.
6. Re-peer: add known canonical peer `138.68.99.64` (Cipherscan) if sidechain problem present.

## Single-VPS failure

If **London dies:**
- Frankfurt continues serving explorers (already active on :8088)
- Update DNS A records for ctaz.frontiercompute.cash, zat.frontiercompute.cash, api.frontiercompute.cash to point to frankfurt IP
- Operator identity Pub{93bb} stops participating in BFT until restored on Frankfurt (requires seed insertion protocol)

If **Frankfurt dies:**
- London continues full operation (operator identity unaffected)
- Lose Hyperliquid trading + HyperDeck WAL recorders (data loss: current in-flight paper trades)
- Respawn zebra-crosslink + HyperDeck on replacement VPS per deploy/experiments

## Non-destructive doctrine

**Do NOT wipe node cache / stateful dirs under disk or sync pressure.** See `feedback_no_wipe_under_disk_pressure.md`. Pattern observed destructive multiple times in April 2026 — prefer non-destructive reclaim (docker prune, build artifact removal, log vacuum) first.

## Community snapshot sources (for bootstrap recovery)

- Cipherscan: https://api.crosslink.cipherscan.app/bootstrap/bootstrap.tar.gz (verifies against our explorer)
- Shielded Only's zebra.zip: archived on laptop at `Desktop/bountyzcash/zebra.zip`

## Contacts / escalation

- Shielded Labs technical: Phillip Trudeau-Tavara (Signal Crosslink Updates channel)
- Cipherscan peer hint: Kenbak (@k6nb4k on X, forum.zcashcommunity.com/u/Kenbak)
- Zaino/block-explorer RPC roadmap: pacu (ZODL Developer Relations Engineer)

## Mempalace state

- Primary: `C:/Users/Skander/.mempalace/palace/` on laptop
- Snapshot backups: `/root/.zeven/mempalace-backup-latest.tar.gz` on both VPSs (update cadence: manual / on-demand currently)

## Post-recovery sanity checklist

1. `curl https://ctaz.frontiercompute.cash/api/chain-health` returns valid JSON
2. Zebrad seed sha matches `ea5a31f6...`
3. `curl https://ctaz.frontiercompute.cash/api/snapshots` returns snapshot metadata
4. Both VPSs in `/api/super` (once meta-cog dashboard ships) show aligned state
5. Operator bb93 visible in `get_tfl_roster_zats` RPC when post-sync
