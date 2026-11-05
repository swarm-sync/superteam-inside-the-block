# Experiment Report — Inside the Block

**Bounty:** "Inside the Block: How Solana Validators Order Transactions"
(Superteam Earn, sponsor Superteam Ukraine, verified).
**Prize split:** 1st $800 / 2nd $600 / 3rd $500 / 4th $300 / 5th $300
(total $2,500). **Deadline:** 2026-11-05 14:59 MST.
**Repo status at writing:** pipeline validated by dry-runs on devnet
and mainnet; **no mainnet transaction has been sent yet.**

All dates below are 2026-11-05 (America/Denver, MST) unless noted.
All raw data lives under `data/` in this repo.

## 1. Experiment design

**Question.** When a Solana leader builds a block, in what order does
its scheduler place transactions — and does the rule differ between
client software / forks?

**Method.** Send our own probe transactions into the leader windows
of selected validators, then read the blocks back with `getBlock`
and record where each probe landed.

**Probes.** One memo transaction per probe, marker `ITB-<run>-<seq>`
(e.g. `ITB-202611051827-0000`), one of four priority-fee tiers
(0 / 1,000 / 10,000 / 100,000 micro-lamports per compute unit).
Default batch: 12 probes (3 per tier). Worst-case planned spend
126,600 lamports = 0.0001266 SOL — 0.25% of the 0.05 SOL cap.

**Validator set.** Minimum per the bounty: 6 validators, 3 client
versions. Selected from live `getVoteAccounts` (stake) +
`getClusterNodes` (gossip version), top stake with version diversity.
Mainnet selection on 2026-11-05 (see
`data/mainnet/dry-run-20261105/validators.json`): **12 validators
across 12 versions**:

| validator (prefix) | version | stake (SOL) | commission |
|---|---|---:|---:|
| Fd7btgySsrju… | 4.4.0 | 17,063,613 | 7% |
| JUPiTERkf97e… | 26.10.6 | 9,530,733 | 5% |
| FBbqKvwLfKGZ… | 0.1.1 | 1,920,966 | 1% |
| 8gDfqHXZ5ekt… | 4.3.2 | 1,795,527 | 0% |
| HSDTxfgrqY8Z… | 4.4.0-rc.1 | 1,528,271 | 5% |
| ALp2GdA1eJV8… | 26.10.5 | 572,170 | 0% |
| soLStaCk5TiG… | 4.5.0-alpha.2 | 305,067 | 0% |
| sce2zXNjLpPM… | 4.5.0-alpha.4 | 59,121 | 10% |
| 5ghoFEVrsXeA… | 26.10.4 | 52,227 | 0% |
| 2bpfa8JbFfZU… | 4.3.0-rc.1 | 29,993 | 8% |
| AS4i8EXUZnPb… | 4.3.0 | 9,472 | 0% |
| FsEq1UFSucxy… | 0.1.0 | 1,514 | 10% |

Version strings are gossip-advertised, not attested; the exact
strings above are what was seen at selection time. (Note: versions
like `26.10.x` are Firedancer-family releases; `0.1.x` are
Jito-Solana forks; `4.x` are Agave. The mapping of version string to
client family is by convention, not verified from the binary — the
report records strings, not claimed identities.)

**Leader windows.** Live `getLeaderSchedule` for the current epoch
(mainnet epoch 1078, slot 465,804,877 at selection). 16 upcoming
windows found in the next ~1,200 slots, e.g. `FBbqKvwLfKGZ`
465,806,260–465,806,263 and `Fd7btgySsrju` 465,806,324–465,806,327.
Full list: `data/mainnet/dry-run-20261105/windows.json`.

**Analysis.** Spearman rank correlation fee-tier vs landing index per
block; send-order vs landing-order agreement; median landing index by
fee tier per leader version. See `src/analysis.py`.

## 2. What was actually measured, and when

| Run | Date/time (MST) | Network | What happened | Result |
|---|---|---|---|---|
| Unit tests | 2026-11-05 ~11:21 | — | fee math, spend cap, Spearman, order agreement, block parsing on synthetic fixtures | **All pass** (`tests/test_units.py`) |
| Devnet dry-run | 2026-11-05 ~11:26 | Devnet | select (8 validators, 8 versions) → schedule (61–64 upcoming windows) → build 12 probes → simulate | **Pipeline works.** Simulation fails `AccountNotFound` only — ephemeral payer unfunded (see §4). 0 sent. |
| Mainnet dry-run | 2026-11-05 ~11:27 | Mainnet | select (12 validators, 12 versions) → schedule (16 windows) → build 12 probes, planned 126,600 lamports | **Pipeline works.** 0 sent. |
| Mainnet probe check | 2026-11-05 ~11:35 | Mainnet | `simulateTransaction` on a built probe | Tx well-formed; returns `BlockhashNotFound` only because the blockhash expired between build and simulate (see §4). |
| Mainnet block-parse check | 2026-11-05 ~12:00 | Mainnet | `parse_block` on 3 recent live blocks (slots 465,807,750–752) | **Parser works on real versioned blocks:** 1,391 txs parsed, 68 memo txs recovered. This run also caught and fixed a real bug: some instructions carry `parsed` as a string, not a dict (see §4). |
| Localnet E2E | 2026-11-05 ~11:30–13:15 | — | `solana-test-validator` 1.18.26 + standalone `solana-validator` from a hand-built genesis | **BLOCKED — sandbox, not code. Root cause FOUND (see §4.8).** Four `solana-test-validator` runs: each served RPC healthy (health ok, slots advancing to 194–236, votes committing, snapshots at 100/200) then died after ~1–3 min. Then a standalone `solana-validator` from a hand-built single-node genesis (500,001,000 SOL capitalized) refused to start at all. **Consequence unchanged: the send → land → fetch → analyze loop is validated in pieces (build, simulate, parse on live mainnet blocks) but has never run end to end.** The mainnet live run remains the definitive validation — it uses the same code paths against the real network. |
| Localnet support | 2026-11-05 ~12:50 | — | Added `--network localnet` (127.0.0.1:8899) to `src/run.py` + `DEFAULT_LOCALNET` in `src/rpc.py`; `tools/fund_localnet.py` funds a probe keypair by direct transfer from the genesis faucet keypair (the local `requestAirdrop` fails -32603) | **Code complete, untestable here.** Unit tests 5/5 pass after the change. |
| Mainnet LIVE | — | Mainnet | **Not run.** Needs a funded keypair + `ITB_ALLOW_MAINNET_LIVE=1`. | **Not measured.** This is the one missing piece. |

**Nothing has been measured on mainnet yet.** There are no landing
records, no Spearman values, and no per-version results in this repo
today. Any number in this report that is not in the table above is
clearly marked as planned, not measured.

## 3. Results

**None yet for the ordering question.** The honest result of the
work so far is infrastructure + validation:

- The tool builds well-formed, signed probe transactions that public
  RPCs accept (simulation reaches blockhash validation).
- Validator selection exceeds the bounty minimums (12 validators,
  12 versions vs 6 / 3 required).
- The spend cap math is verified: 12 probes cost 0.0001266 SOL;
  the 0.05 SOL cap would allow ~400+ probes at the cheapest tiers,
  or ~100 probes across all four tiers — plenty of headroom to
  increase statistical power in the live run.
- Two real RPC gotchas are documented and fixed in code (see §4).

## 4. What went wrong

1. **solana-py 0.41 dropped the sync client.** `solana.rpc.api`
   no longer exists; only `AsyncClient` remains. Fixed by using
   raw JSON-RPC (`requests`) + `solders` for signing — fewer
   dependencies, no API churn.
2. **Wrong Memo program ID from memory.** I first wrote
   `MemoSq4gqABAXKb96qnH8TysNcWx1yGcK9gWzUuBEv` (42 chars, decodes
   to 31 bytes). The correct ID, verified against multiple sources,
   is `MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr`. Lesson
   recorded in code: program IDs are verified, never typed from
   memory.
3. **`getLeaderSchedule` slots are epoch-relative.** Comparing them
   to absolute slots returned zero windows. Fixed with
   `epoch_start = absoluteSlot - slotIndex`. Also: public RPCs
   return `null` when an explicit epoch is passed — call with no
   arguments for the current epoch.
4. **`getBlock` needs `maxSupportedTransactionVersion: 1`.**
   Modern blocks contain versioned transactions; `0` is rejected
   with -32015. Fixed.
5. **`simulateTransaction` / `sendTransaction` need
   `encoding: "base64"`.** Default is base58; base64 txs were
   rejected with "invalid base58 encoding". Fixed.
6. **Devnet `requestAirdrop` is dead on the public RPC** (410
   Gone). The web faucet needs a browser. Ankr's public endpoint
   needs an API key. So the devnet live run could not be funded
   from this VM. The devnet dry-run still validates build +
   simulate; the only failure is the unfunded payer
   (`AccountNotFound`).
7. **Blockhash expiry between build and simulate.** Probes built
   at 11:27 simulate as `BlockhashNotFound` at 11:35 — blockhashes
   live ~150 slots (~60 s). Not a bug: the live flow builds and
   sends within seconds. But it means a *dry-run* that simulates
   minutes after building will always show this error, and the
   report must not mistake it for a malformed transaction.
8. **Localnet validator will not stay up in this VM — ROOT CAUSE FOUND.**
   Download was the first hurdle (`release.solana.com` returns
   empty replies; the GitHub tarball stalled at ~95% in `/tmp`
   because `/tmp` is a 512 MB tmpfs at 100% — moved to
   `~/workspace/.cache/` and it completed: v1.18.26, 328 MB).
   Extraction needed `--no-same-owner` (archive uid 999).
   Then the validator itself died four times under
   `solana-test-validator`: (1) SIGPIPE from piping its output to
   `head` (my error — output must go to a file); (2) a restart on
   the existing ledger failed with "Discover failed"; (3) a fresh
   `--reset` ledger started (health ok, slot 84 advancing) and died
   ~1 minute later, log again showing "Discover failed"; (4) a
   fresh ledger on the home disk (after clearing `/tmp` back to
   50% free) served RPC healthy to slot 194–236, votes committing,
   snapshots at 100/200, then died again at ~3 min — and the
   in-between `requestAirdrop` failed -32603. The ledger log
   gives the cause: `Gossip last-error: Some(SendPktsError(IoError
   (Os { code: 1, kind: PermissionDenied, message: "Operation not
   permitted" }, 8)))` — **the VM's sandbox blocks UDP sends at
   the syscall level, so gossip can never discover even the node
   itself** ("discover failed...", Nodes: 0, Spies: 1); the
   test-validator wrapper then exits with `failed to start
   validator: TestValidator startup failed: "Discover failed"`
   and takes the healthy validator down with it. The validator
   was never unhealthy — its wrapper's discovery gate kills it.
   Escape attempt: a standalone `solana-validator` from a
   hand-built single-node genesis (keys + genesis built fine,
   500,001,000 SOL capitalized) refuses to start in this VM too:
   `OS network limit test failed` — it needs sysctl values
   (`net.core.rmem_max` etc.) raised, and sysctl writes are
   blocked even as root (`Operation not permitted`), with
   `net.core.netdev_max_backlog` not present at all; 1.18.26
   offers no flag to skip that test. **Two independent sandbox
   restrictions, both outside the repo's control.** The localnet
   code path (`--network localnet`, `tools/fund_localnet.py`)
   is written and unit-tested but could not be exercised here;
   it will run on any normal machine or Docker host. The devnet
   `requestAirdrop` also fails on the local validator (-32603),
   and the CLI transfer path could never be reached because the
   RPC kept dying. **Consequence: the send → land → fetch →
   analyze loop is validated in pieces (build, simulate, parse)
   but has never run end to end.** The mainnet live run remains
   the definitive validation — it uses the same code paths
   against the real network.
9. **Block parser assumed `parsed` is always a dict.** Real
   mainnet blocks carry some instructions with `parsed` as a plain
   string; the parser crashed on the first live block. Fixed with
   defensive type checks, then validated on 3 live blocks
   (1,391 txs, 68 memos recovered).

## 5. Limitations (honest)

- **No mainnet measurement yet.** The ordering question is
  unanswered until the live run happens.
- **12 probes is low power.** With ~4 probes landing per block,
  the Spearman test can only detect a strong rule. The live run
  should send more probes (the cap allows it) and repeat across
  several leader windows per validator.
- **Arrival order vs scheduler order is not separated.** See
  README §"What this design does NOT separate".
- **Version strings are gossip-advertised**, not attested from
  binaries.
- **Public RPC latency** (2–6 s per call in this VM) limits how
  tightly sends can be timed to leader windows. A dedicated RPC
  endpoint would tighten this.
- **Localnet end-to-end never ran in this VM.** Sandbox blocks
  UDP sends (gossip discovery) and sysctl writes (validator
  network-limits test) — see §4.8. The `--network localnet`
  path and funding helper are written and unit-tested; they
  need a normal machine or Docker host to exercise.
- **Docker could not be tested here** (no Docker in this VM).
  The Dockerfile / compose files are provided and syntactically
  complete but unbuilt.
- **67 submissions already exist** for this bounty; several will
  have live mainnet data. This package competes on design honesty
  and reproducibility, not on having data first.

## 6. What the live mainnet run needs

1. **A funded keypair** — a Solana wallet with ≥0.05 SOL (the
   cap; expected actual spend ~0.001–0.005 SOL for 50–100 probes).
   The keypair file path is passed as `--keypair`; it is never
   copied into the repo.
2. **Approval to spend** — the operator's explicit go-ahead
   (`ITB_ALLOW_MAINNET_LIVE=1` + `--live`).
3. **~30–45 minutes** of wall time: select + schedule (~2 min),
   build + send (~5 min for 50–100 probes paced at 400 ms),
   confirm + fetch blocks (~5 min), analyze + write-up (~15 min).
4. **After the run:** the raw data (`landings.json`,
   `blocks.json`, `analysis.json`) goes in the repo, this report
   gets its results section filled in from real numbers, and the
   submission is a link to the repo.

Command (once the keypair is ready):

```bash
ITB_ALLOW_MAINNET_LIVE=1 python -m src.run measure --network mainnet \
  --live --keypair /path/to/keypair.json --per-tier 12 \
  --out data/mainnet/live-20261105
```

(`--per-tier 12` = 48 probes, planned spend ~0.0005 SOL.)
