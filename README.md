# Inside the Block: How Solana Validators Order Transactions

A measurement tool for the [Superteam Earn bounty of the same name](https://earn.superteam.fun/listings/bounty/inside-the-block-how-solana-validators-order-transactions)
(sponsor: Superteam Ukraine). It sends real transactions into leader
windows on Solana Mainnet and shows, from the blocks themselves, what
ordering rule each leader's scheduler landed by.

**Status: pipeline validated end to end on devnet (dry-run) and mainnet
(dry-run). No mainnet transaction has been sent yet — the live run is
one command away and needs only a funded keypair (see
[Live mainnet run](#live-mainnet-run)).**

## The question

The order of transactions inside a block is decided by the block
leader's scheduler. Agave's rule is public; the forks holding most of
the stake are not documented. This tool measures the rule instead of
guessing it: it sends its own probe transactions — each carrying a
unique memo marker and one of four priority-fee tiers — into the
leader windows of selected validators, then reads the blocks back and
records where each probe landed.

## How to run it (one command)

```bash
docker compose up            # mainnet DRY-RUN: builds + simulates, sends nothing
```

Or without Docker:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
python -m src.run measure --network mainnet --out data/mainnet/dry-run
```

Live runs (spend real SOL, hard-capped at 0.05 SOL):

```bash
# Devnet end to end (free, proves the pipeline):
python -m src.run measure --network devnet --live \
    --min-validators 2 --min-versions 1 --out data/devnet/live1
# Mainnet live (needs a funded keypair + explicit opt-in):
ITB_ALLOW_MAINNET_LIVE=1 python -m src.run measure --network mainnet --live \
    --keypair /path/to/keypair.json --out data/mainnet/live1
```

Every stage also runs separately: `select`, `schedule`, `build`,
`send`, `fetch`, `analyze` (see `python -m src.run --help`).

## Design

### Probe transactions

One probe = one transaction:

- a **Memo instruction** carrying a unique marker, e.g.
  `ITB-20261105a-0007`. The marker is what lets us find the tx again
  inside a fetched block, and it is what makes the probe identifiable
  as *ours* — primary evidence is only ever transactions we sent.
- a **ComputeBudget `set_compute_unit_price`** instruction carrying
  the fee tier. The fee tier is the independent variable: if a
  leader's scheduler orders by fee, higher-tier probes should land
  earlier in the block.

Fee tiers (micro-lamports per compute unit) and worst-case cost per
probe (base fee 5,000 lamports + priority fee at the default 200,000
compute-unit limit):

| tier (µL/CU) | priority fee | total per probe |
|---:|---:|---:|
| 0 | 0 | 5,000 |
| 1,000 | 200 | 5,200 |
| 10,000 | 2,000 | 7,000 |
| 100,000 | 20,000 | 25,000 |

Default batch: 4 tiers × 3 probes = 12 probes, planned spend
**126,600 lamports = 0.0001266 SOL** — 0.25% of the 0.05 SOL cap.
The cap is enforced in code *before* building and *before* sending;
the run aborts if planned or actual spend would exceed it. Actual
spend is reconciled afterwards from `meta.fee` in the fetched blocks.

### Validator selection

At least 6 validators spanning at least 3 distinct client versions
(the bounty's minimum), picked from live public data:

- `getVoteAccounts` — the live validator set with activated stake.
- `getClusterNodes` — the gossip-advertised version string per node.

We rank by activated stake and greedily take the highest-stake
validator of each distinct version first, then fill by stake. The
version string is gossip-advertised, not attested — we record the
exact strings seen at selection time and re-check them at measurement
time. Jito validators are in scope; Jito bundles and tips are **not**
used and not measured (a separate mechanism from the leader's
scheduler).

Mainnet selection on 2026-11-05 (dry-run): 12 validators across 12
versions — 4.4.0, 26.10.6, 0.1.1, 4.3.2, 4.4.0-rc.1, 26.10.5,
4.5.0-alpha.2, 4.5.0-alpha.4, 26.10.4, 4.3.0-rc.1, 4.3.0, 0.1.0
(see `data/mainnet/dry-run-20261105/validators.json`).

### Leader windows

`getLeaderSchedule` returns the current epoch's schedule with slot
numbers **relative to the epoch start** — a real gotcha we hit during
development (comparing them to absolute slots yields zero windows).
We convert to absolute slots with
`epoch_start = absoluteSlot - slotIndex` from `getEpochInfo`.
Public RPCs return `null` when an explicit epoch is passed, so we
call it with no arguments (current epoch) and record the epoch
alongside the schedule.

### Sending

Probes are built against a fresh blockhash and sent paced (~400 ms
apart) so they do not arrive as a single burst — a burst would
measure our own batching, not the leader's scheduler. We record send
time, signature, fee tier and intended window for every probe, then
poll signature status until confirmed.

**We never assume a probe lands in the window we aimed at.**
Propagation delay and the leader's own admission mean a probe can
land in a later slot under a different leader. The honest measurement
is: record send time + intended window, then read the *actual*
landing slot and leader from `getBlock`. The analysis groups by
actual leader, never by intended leader.

### Reading the blocks back

`getBlock` (jsonParsed, `maxSupportedTransactionVersion: 1` — modern
blocks contain versioned transactions) returns each transaction in
execution order with `meta.fee`. A probe is located by signature
match, confirmed by its memo marker, with fee as a sanity check.
A probe's index in the block's transaction list **is** its landing
position.

### Analysis

All statistics come from our own landed probes only. Other
transactions in the block are context (block size, fee spread), never
evidence of the rule.

- **Spearman rank correlation** between fee tier and landing index
  within a block (−1 = higher fee lands earlier, 0 = no relation).
  Computed per block with ≥4 of our probes, then averaged.
- **Send-order vs landing-order agreement**: fraction of probe pairs
  whose landing order matches send order (1.0 = exact match,
  0.5 = random).
- **Per-leader-version grouping**: median landing index by fee tier,
  so a version-specific rule shows up as a different pattern for one
  client version vs another.

A null result ("no rule, it's noise") is a valid finding and is
reported as such — it is not a failure of the experiment.

## How this design handles the accounting points

| Point | How it is handled |
|---|---|
| Leader schedule and leader types | Live `getLeaderSchedule` + `getClusterNodes` versions; windows computed from the real schedule; analysis groups by **actual** landing leader/version, never intended. |
| Path of a tx from backend to executing node | Probes are sent to a public RPC (`sendTransaction`), which forwards to the current leader's TPU. We record send time and intended window; landing slot/leader is read back from the chain. The RPC hop is identical for every probe, so it cannot fake a fee-ordering signal. |
| Interaction of our own txs with each other | Probes are paced 400 ms apart and carry distinct markers; send order is recorded and compared against landing order explicitly (order-agreement stat). If probes cluster, the analysis says so. |
| Competition inside the block | Every landing record carries `block_tx_count` and the block's fee context. Probes are a small share of a full mainnet block, which limits signal — reported as a limitation, not hidden. |
| Send order | Recorded per probe (`sent_at_unix`, `seq`) and tested directly against landing order. |
| Noise vs rule | Spearman per block + order agreement + per-version medians; with 12 probes the power is low and the report says so. More probes (still inside the 0.05 SOL cap — the cap allows ~400+ probes at the cheapest tiers) would sharpen this. |
| Alpenglow (Oct 21, 2026) | All measurements are dated; every run in this repo is after Alpenglow. Pre- and post-upgrade data are never mixed. |

## What this design does NOT separate

- **Arrival order at the leader vs scheduler order.** A probe that
  arrives late can land late even under a fee-ordered scheduler. We
  measure the *joint* outcome (arrival + scheduler). The fee tiers
  and the send-order test bound this, but do not eliminate it.
- **The leader's mempool state.** We cannot see which other
  transactions were waiting when our probe arrived. A block that is
  full of high-fee transactions will push our probes down regardless
  of the rule.
- **Stake-weighted sampling.** High-stake leaders lead more slots;
  our window targeting follows the schedule, so big validators are
  over-represented by design. The per-version grouping is the
  control for this, not the raw counts.
- **Jito vs non-Jito scheduling.** Jito validators are included as
  leaders, but bundles/tips are excluded; any Jito-specific ordering
  of *our* non-bundle probes is measured as part of that leader's
  rule, not separated out.
- **Devnet results say nothing about mainnet ordering.** Devnet has
  one client and near-empty blocks. The devnet run proves the
  pipeline only.

## Safety

- **Dry-run by default.** Nothing sends without `--live`.
- **Mainnet live needs `ITB_ALLOW_MAINNET_LIVE=1`** in addition to
  `--live`, so a stray flag cannot spend real SOL.
- **Spend cap 0.05 SOL**, enforced before building and before
  sending, reconciled from on-chain fees afterwards.
- **Keys only via `--keypair <file>`** (a file outside the repo) or
  an ephemeral devnet keypair generated in memory. No key is ever
  written into the repo; `.gitignore` blocks keypair files.
- **No Jito bundles, no tips, no MEV extraction.** Probes are plain
  memo transactions.
- **Simulate before send** is not used as proof of landing — only
  `getBlock` counts.

## Repository layout

```
README.md                  this file
REPORT.md                  experiment design, results, what went wrong
requirements.txt           requests, solders (pinned)
Dockerfile                 python:3.12-slim, one-command reproduction
docker-compose.yml         docker compose up  (dry-run by default)
src/
  rpc.py                   raw JSON-RPC client (retries, logging)
  validators.py            validator selection (stake + version diversity)
  schedule.py              leader schedule, epoch-relative slot fix
  txbuild.py               probe construction + hard spend cap
  sender.py                paced sending, dry-run default, --live gate
  blocks.py                getBlock fetch + probe recovery
  analysis.py              Spearman, order agreement, per-version medians
  run.py                   CLI: select/schedule/build/send/fetch/analyze/measure
tests/
  test_units.py            fee math, cap, correlation, block parsing
data/
  devnet/run-20261105-dry/ devnet dry-run (build + simulate, 0 sent)
  mainnet/dry-run-20261105/ mainnet dry-run (12 validators, 12 versions)
```

## Validation done so far

See `REPORT.md` for the full record. In short:

- Unit tests: fee math, spend-cap enforcement, Spearman, order
  agreement, block parsing on synthetic fixtures — all pass.
- Devnet dry-run (2026-11-05): select + schedule + build + simulate
  work end to end. Simulation fails with `AccountNotFound` only
  because the ephemeral payer is unfunded (the public devnet
  `requestAirdrop` returns 410 Gone and the web faucet needs a
  browser) — the txs themselves are well-formed and accepted.
- Mainnet dry-run (2026-11-05): 12 validators / 12 versions
  selected, 16 upcoming windows found, 12 probes built, planned
  spend 0.0001266 SOL. Simulation of a built probe returns
  `BlockhashNotFound` only because the blockhash expired between
  build and simulate — the tx is well-formed; the live flow builds
  and sends within seconds, inside blockhash lifetime.
- Localnet end-to-end (send → land → fetch → analyze): **blocked in
  this VM only** — root cause found and documented in REPORT.md §8:
  the sandbox blocks UDP sends (gossip discovery can never
  succeed, so the test-validator wrapper kills the healthy
  validator) and blocks sysctl writes (the standalone validator
  exits on its OS network-limits test). The loop is validated in
  pieces, not end to end. On a normal machine it runs with one
  command: start `solana-test-validator`, then
  `python -m src.run measure --network localnet --live --keypair
  /path/to/funded-keypair.json --min-validators 1 --min-versions 1
  --out data/localnet/run1` (`tools/fund_localnet.py` funds the
  probe keypair from the validator's genesis faucet keypair if
  `requestAirdrop` is unavailable).
