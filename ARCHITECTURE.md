# Frontier Compute — Full Infra Architecture

Last updated: 2026-04-18 05:00 UTC

## Vision

**Three physical anchors, one converged supernode:** London (Zcash ecosystem + protocol), Frankfurt (trading + HyperDeck growth), Norway (mining). All tied together by mempalace (memory), ZAP1 (attestation), and LiquidLv DAO LLC (commercial wrapper).

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ LONDON VPS       │     │ FRANKFURT VPS    │     │ NORDIC SHIELD    │
│ 78.141.247.162   │     │ 136.244.95.226   │     │ Norway (Hamus)   │
│                  │     │                  │     │                  │
│ protocol +       │◄───►│ trading +        │◄───►│ 6× Z15 Pro       │
│ ecosystem +      │ (c) │ HyperDeck +      │ (5) │ mining →         │
│ explorer         │     │ paper-takers     │     │ early May 2026   │
└─────────▲────────┘     └─────────▲────────┘     └─────────▲────────┘
          │                        │                        │
          └────────────────────────┼────────────────────────┘
                                   │
                       ┌───────────┴────────────┐
                       │ LAPTOP (mempalace      │
                       │ primary + seed hex +   │
                       │ control-plane)         │
                       └────────────────────────┘
```

(c) cross-VPS rsync every 3 min (handbacks, missions, actualize files)
(5) mining telemetry + BTC→ZEC swap hooks (planned May)

## Stack inventory (what we have, as of 2026-04-18 05:00 UTC)

### London VPS (`london`, 78.141.247.162, 169G disk 62%)

| Component | Port | State | Purpose |
|---|---|---|---|
| zebra-crosslink | 58000 (RPC) / 38233 (p2p) / 12301 (BFT) | tmux `zebrad-manual`, sidechain-stuck 2402 | operator node (Pub{93bb}, 146.3 cTAZ staked) |
| ctaz-explorer | 8088 | systemd `ctaz-explorer.service`, Restart=always | block explorer frontend (ctaz.frontiercompute.cash) |
| zat-explorer-signature | 8090 | systemd | zat.frontiercompute.cash |
| zat-explorer-institutional | 8089 | systemd | zat.frontiercompute.cash/institutional |
| ctaz-zainod | - | systemd | gRPC Zaino bridge |
| Docker containers | - | - | zap1 (ZAP1 API), falkordb (KG), s-nomp (mining pool), snomp-redis, zebra-mainnet (mainnet Zcash ref), signal-cli (bot), n8n (automation) |
| Supernode cron (3min) | - | crontab | `/root/bin-supernode-handback-sync.sh` — bidirectional rsync with frankfurt |
| Healthcheck cron (1min) | - | crontab | `/root/bin-supernode-healthcheck.sh` — JSON state dump |

### Frankfurt VPS (`frankfurt`, 136.244.95.226, 244G disk 21%)

| Component | Port | State | Purpose |
|---|---|---|---|
| zebra-crosslink | 58000 / 38233 / 12301 | tmux `zebrad-frankfurt`, fresh-sync sidechain 2402 | observer node (Pub{0b55} auto-generated, 0 stake) |
| ctaz-explorer mirror | 8088 | systemd | redundancy for london |
| HyperDeck core | - | `/root/hyperdeck_core_new/`, 203MB | trading engine + strategies |
| paper_taker_cl.py × 2 | - | active pids 2712227, 2712230 | CL + SILVER paper trading on enforce gate mode |
| WAL recorders | - | active | SOL / ETH / BTC / ZEC / HYPE market data |
| codex-12c-build | - | tmux, build complete | Lane 12c opus+codex-trader persona files ready |
| codex (meta-cog) | - | tmux, build complete | /super dashboard module built |
| Nginx site files | - | - | (ready to serve ctaz.frontiercompute.cash if DNS switches) |

### Seed / identity

Operator pubkey: `bb93fde13cfc03f430af8d03f9114f711897170c18192a3524e48251d8f77e64`
Seed SHA-256: `ea5a31f67b0067737ec79d54a7e3b17c14f0b9892147cdc06f79df2ec79a5378`
Backed up 6 places:
1. London live cache dir
2. `/root/.zeven/ctaz-identity/secret.seed.pub93bb.bak` (london)
3. `/root/.zeven/ctaz-identity/secret.seed.pub93bb.bak` (frankfurt, new tonight)
4. `/tmp/SEED-LOCKBOX-*.bak` (london, multiple timestamps)
5. `/root/.zeven/ctaz-identity/secret.seed.snap-recovery-*.bak` (london)
6. `C:/Users/Skander/.seed-backups/secret.seed.london.*.hex` (laptop, hex-encoded)

### Repositories / code

| Repo | Visibility | Purpose |
|---|---|---|
| Frontier-Compute/ctaz-explorer | public GitHub | FastAPI block explorer. Tonight: 6eb2333 /snapshots, 72af9a7 RUNBOOK, 3bbf904 finalizers banner, ab553b4 /super dashboard |
| Frontier-Compute/zap1 | public | ZAP1 protocol + npm `@frontiercompute/zap1` + `zap1-attest` |
| Frontier-Compute/openclaw-zap1 | public | openclaw x ZAP1 bridge npm |
| Frontier-Compute/zcash-mcp | public | Zcash MCP server npm |
| Frontier-Compute/hyperdeck-core | private | trading stack source |
| ShieldedLabs/crosslink_monolith | upstream | PRs: #17 (mempool backoff), #18 (stake helper), #19 (docs), #20 (staking-cli) on s1_dev branch; Issue #16 (RFC) |
| ZcashFoundation/zebra | upstream | CVE-2026-40881 reporter (Zk-nd3r); merged GHSA-xr93-pcq3-pxf8 addr/addrv2 DoS fix |
| zcash/librustzcash | upstream | PR #2278 merged Apr 14 (ZIP-244 sighash breaking change) |

### Public surfaces

| URL | Backend | State |
|---|---|---|
| frontiercompute.cash | nginx static | main site, pricing, deck |
| ctaz.frontiercompute.cash | london :8088 | block explorer (honest grade, wall-of-death banner on /finalizers) |
| ctaz.zat-explorer.cash | london :8088 | alias for same backend |
| zat.frontiercompute.cash | london :8090/:8089 | zat-explorer signature + institutional |
| api.frontiercompute.cash | london docker:zap1 | ZAP1 API |
| pay.frontiercompute.io | - | billing/pay (existing) |

### Attestation (ZAP1)

7 chain deployments: Ethereum L1/Arb/Base/HL/Sepolia + NEAR + Sui. 3+ live ctaz anchors. New `hyperdeck_core_new/integrations/zap1_trade_receipts.py` module (tonight) canonicalizes trade receipts deterministically for per-trade attestation.

### Trading (HyperDeck on Frankfurt)

Live: `paper_taker_cl.py` CL + SILVER on enforce gate mode.

Built tonight (Lane 12c, not yet launched):
- `deploy/experiments/opus_trader_btc_paper.json` — aggressive persona (80 bps hard stop, 50 bps profit target, shadow→enforce)
- `deploy/experiments/codex_trader_btc_paper.json` — conservative persona (40 bps hard stop, 25 bps profit target, enforce immediately)
- `scripts/paper_taker_hl.py` — Hyperliquid-adapted launcher
- Both use existing `scripts/hl_pacman.py` decision engine via `exchange/live_maker_runner.py`

### Nordic Shield (Norway mining)

- 6× Z15 Pro antminers (Leedminer, fully paid)
- Hamus Hosting AS, Norway ($2500 USDC deposit sent)
- Ships late April, mining starts early May 2026
- Baseline: $253 ZEC/unit, 2.66 ZEC/mo, $11K 3-year profit

### Mempalace (memory graph)

- Primary: `C:/Users/Skander/.mempalace/palace/` on laptop (SQLite + markdown drawers)
- 2,773 drawers, 2,243 entities, 13,637+ triples
- Snapshot tarball `/root/.zeven/mempalace-backup-latest.tar.gz` on both VPSs (11.4MB)
- Doctrine files in `C:/Users/Skander/.claude/projects/C--Users-Skander/memory/*.md`

### Commercial wrapper

- LiquidLv DAO LLC, Wyoming
- Brand: Frontier Compute
- Pricing: Operator $299/mo, Custody $999/mo (5 surfaces synced on 2fbb0a0)
- Customer ceiling: current tier targets individuals; UAE/institutional expansion parked

### Identity / public record

- @Zk_nd3r (Twitter) — Zooko follows + RT'd s1w1 infra-diagnostic post 2026-04-17 23:45 UTC
- github.com/Zk-nd3r + Frontier-Compute org
- CVE-2026-40881 credited reporter (GHSA-xr93-pcq3-pxf8)
- Named in Zooko+SL "AIpocalypse" advisory cohort (Scalar + Zk-nd3r + Kenbak + sangsoo-osec)
- Daira 👍 on ZODL 15-finding dossier (2026-04-16)
- Sam Smith engagement on s1_dev PR stack
- shieldedmark community endorsement ❤️4💯👹

### Doctrines (hard rules enforced)

1. No wipe under disk/sync pressure without fresh user gate
2. Ask user for local resources before multi-step recovery missions
3. Never assume, ask (discernment + boldness)
4. Boost peers (Kenbak), don't compete — become undeniable plumbing
5. Bicephalic hunk-filter discipline on git commits
6. Internal codewords never on public surfaces (commit messages included)
7. Check mempalace BEFORE acting, not just after
8. Replicate = resilience 101

## Full infra design (HyperDeck-from-Frankfurt growth)

### 30-day ship plan

**Week 1 (now → 2026-04-25)**

- [ ] Launch Lane 12c opus-trader vs codex-trader Hyperliquid BTC paper competition (Frankfurt codex-12c-build handback is ready, awaiting gate)
- [ ] Gate at 48h: declare winner persona
- [ ] ZAP1 × HyperDeck trade-receipt hook wired into paper_taker loops (module is built, needs one-line integration per taker)
- [ ] /super dashboard main.py route registration committed, live on both VPSs
- [ ] V3 release from SL expected this week → zebrad upgrade, chain-resume, bb93 re-registered in roster
- [ ] CVE-2026-40881 surfacing: tweet + /why page line + ZCG embed (tomorrow AM gate)
- [ ] Phillip sidechain-tips RFC reply post-sync

**Week 2 (2026-04-26 → 2026-05-03)**

- [ ] Nordic Shield rigs arrive in Norway, online + hashing
- [ ] Mining pool config (s-nomp or solo to shielded address)
- [ ] Per-mined-block ZAP1 attestation pipeline
- [ ] Kris Nuttycombe advisor-framing DM (after ZODL FT decision revisit)

**Week 3-4 (2026-05-04 → 2026-05-17)**

- [ ] HyperDeck symbol expansion: ETH, SOL, HYPE paper takers (if Lane 12c persona wins)
- [ ] Commercialize: attested-trade-receipt product (Frontier Compute Custody tier upgrade path)
- [ ] Q2 Retroactive Grants application (ZAP1 ecosystem work, thread opens Apr 16)
- [ ] Nordic Shield → HyperDeck treasury: mining ZEC rewards feed paper-taker capital

### HyperDeck Frankfurt-grown architecture

```
┌─────────────────────────────────────────────────────────────┐
│ FRANKFURT HYPERDECK STACK (growing out)                     │
│                                                              │
│  Market data ingestion                                       │
│  ├── WAL recorders (live): SOL ETH BTC ZEC HYPE             │
│  └── → write to /root/hyperdeck_data/*.tmp                  │
│                                                              │
│  Decision engines                                            │
│  ├── scripts/hl_pacman.py (primary, Pacman strategist)     │
│  ├── strategies/pacman_contracts.py (role contracts)        │
│  ├── strategies/execution_frameworks.py (timing/holding)    │
│  └── paper_engine/maker_sim.py (fill model)                 │
│                                                              │
│  Execution surface                                           │
│  ├── exchange/live_maker_runner.py (canonical live path)    │
│  └── scripts/paper_taker_*.py (wrappers per symbol)        │
│                                                              │
│  Active paper takers                                         │
│  ├── paper_taker_cl.py --symbol xyz:CL                     │
│  ├── paper_taker_cl.py --symbol xyz:SILVER                 │
│  ├── [PLANNED] paper_taker_hl.py --persona opus --symbol BTC│
│  └── [PLANNED] paper_taker_hl.py --persona codex --symbol BTC│
│                                                              │
│  Attestation layer (NEW, built tonight)                      │
│  ├── integrations/zap1_trade_receipts.py                    │
│  │   └── build_trade_receipt() canonicalize → hash → commit │
│  └── integrations/trade_attest_hook.py                      │
│      └── fire-and-forget at trade exit, async daemon        │
│                                                              │
│  ZAP1 anchoring (london docker, remote callable)             │
│  └── api.frontiercompute.cash/event → Merkle leaf           │
│      → periodic anchor → Zcash mainnet tx                   │
│                                                              │
│  Outputs                                                     │
│  ├── desk-briefings/YYYY-MM-DD.md (daily)                   │
│  ├── paper-taker-* logs                                     │
│  └── /api/super dashboard (meta-cognition)                  │
└─────────────────────────────────────────────────────────────┘
```

### Nordic Shield tie-in (May)

```
Norway rigs (Hamus Hosting)
  → mining to stratum pool (s-nomp on london OR direct to Zcash pool)
  → shielded payout address controlled by operator
  → each mined block → ZAP1 attestation leaf
  → ZEC rewards flow to LiquidLv DAO LLC treasury
  → optional swap: ZEC → USDC via NEAR Intents → feeds HyperDeck paper/live capital
  → monitoring: rig hashrate + uptime on /super dashboard (frankfurt)
```

### Memory / control plane

- Mempalace primary: laptop (file-based SQLite + markdown drawers)
- Daily tarball export → `/root/.zeven/mempalace-backup-latest.tar.gz` on both VPSs
- Handback files flow bi-directionally via 3-min rsync cron
- All doctrines reachable from either VPS via doctrine-check.sh script (built tonight)

### Public surfaces (unified brand)

All under frontiercompute.cash / .io / zat-explorer.cash umbrella. DNS should round-robin between london + frankfurt once user configures registrar. Custody product at frontiercompute.cash/#pricing, Custody $999/mo includes ZAP1 attestation + dWallet + multi-chain signing.

### Known gaps / next-phase investments

1. **DNS failover** — requires registrar access (user)
2. **Real-time mempalace replication** — currently manual snapshot, should be hourly cron
3. **Rig monitoring (Nordic Shield)** — not yet connected; install telemetry agent when rigs online
4. **v3 upgrade path** — when SL ships, both VPSs upgrade zebrad, re-register bb93
5. **Hyperliquid production trade signing** — currently all paper; live trading requires wallet+risk-gate+Kelly-sizing discipline
6. **Advisor-framing path to Kris** — parked 2-3 days post-Zooko-RT-moment

## Bottom line

We have a converged supernode running now: 2 VPS + 1 mining op + 1 laptop + 1 memory graph + 5 commercial surfaces + 7 attestation chains + 4 npm packages + 1 operator identity + 4 active doctrines. Frankfurt is the trading/HyperDeck growth anchor, London is the ecosystem/protocol anchor, Norway is the mining-economic engine. All tied by ZAP1 attestation + mempalace memory + cross-VPS rsync. Ship cadence converging into a single brand (Frontier Compute) under one wrapper (LiquidLv DAO LLC).
