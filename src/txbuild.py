"""Probe transaction construction + hard spend cap.

Probe design (one tx = one measurement point):
- One Memo instruction carrying a unique marker, e.g. ``ITB-20261105a-0007``.
  The marker is what lets us find the tx again inside a fetched block,
  and it is also what makes the probe identifiable as OURS (primary
  evidence must be txs we send ourselves).
- One ComputeBudget ``set_compute_unit_price`` instruction carrying the
  fee tier. The fee tier is the independent variable: if a leader's
  scheduler orders by fee, higher-tier probes should land earlier in
  the block. Tiers are micro-lamports per compute unit, CU limit left
  at the default 200,000, so priority fee = price * 200_000 / 1e6.

Fee tiers (micro-lamports / CU) and worst-case cost per probe
(base fee 5,000 lamports + priority fee):
    0       ->  5,000 lamports
    1,000   ->  5,200
    10,000  ->  7,000
    100,000 -> 25,000

Spend cap: 0.05 SOL = 50,000,000 lamports for the WHOLE measurement.
The cap is enforced BEFORE any send: the planned worst-case total is
computed from the fee tiers, and the run aborts if it exceeds the cap.
Actual spend is reconciled afterwards from meta.fee in fetched blocks.

No private key is ever written to disk by this module. Keypairs are
loaded from a file path supplied at runtime (env/CLI) or generated
ephemerally in memory for a dry run.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field

from solders.compute_budget import set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from .rpc import Rpc

log = logging.getLogger("itb.txbuild")

MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
BASE_FEE_LAMPORTS = 5_000
CU_LIMIT = 200_000  # default compute-unit limit per tx
CAP_LAMPORTS_DEFAULT = 50_000_000  # 0.05 SOL
FEE_TIERS = [0, 1_000, 10_000, 100_000]  # micro-lamports per CU


def priority_fee_lamports(price_micro_lamports: int) -> int:
    return price_micro_lamports * CU_LIMIT // 1_000_000


def probe_cost_lamports(price_micro_lamports: int) -> int:
    return BASE_FEE_LAMPORTS + priority_fee_lamports(price_micro_lamports)


@dataclass
class SpendCap:
    """Hard cap on lamports spent by a measurement run."""
    limit_lamports: int = CAP_LAMPORTS_DEFAULT
    planned_lamports: int = 0
    spent_lamports: int = 0

    def plan(self, cost: int) -> None:
        if self.planned_lamports + cost > self.limit_lamports:
            raise RuntimeError(
                f"spend cap exceeded: planned "
                f"{self.planned_lamports + cost} > limit {self.limit_lamports}")
        self.planned_lamports += cost

    def record_spent(self, lamports: int) -> None:
        self.spent_lamports += lamports
        if self.spent_lamports > self.limit_lamports:
            raise RuntimeError(
                f"spend cap exceeded in reality: spent {self.spent_lamports} "
                f"> limit {self.limit_lamports}")


@dataclass
class Probe:
    run_id: str
    seq: int
    fee_tier: int  # micro-lamports per CU
    marker: str = ""
    signature: str = ""
    tx_b64: str = ""
    cost_lamports: int = 0
    intended_validator: str = ""
    intended_window: list = field(default_factory=list)
    sent_at_unix: float = 0.0
    status: str = "built"  # built | simulated | sent | confirmed | failed

    def __post_init__(self):
        if not self.marker:
            self.marker = f"ITB-{self.run_id}-{self.seq:04d}"
        if not self.cost_lamports:
            self.cost_lamports = probe_cost_lamports(self.fee_tier)


def build_probe_tx(payer: Keypair, probe: Probe, blockhash: str) -> str:
    """Build + sign one probe tx. Returns base64 wire tx. Sends nothing."""
    memo_ix = Instruction(
        program_id=MEMO_PROGRAM_ID, data=probe.marker.encode(), accounts=[])
    ixs = [memo_ix]
    if probe.fee_tier > 0:
        ixs.append(set_compute_unit_price(probe.fee_tier))
    msg = MessageV0.try_compile(
        payer.pubkey(), ixs, [], Hash.from_string(blockhash))
    tx = VersionedTransaction(msg, [payer])
    return base64.b64encode(bytes(tx)).decode()


def build_probes(rpc: Rpc, payer: Keypair, run_id: str,
                 fee_tiers: list[int], count_per_tier: int,
                 cap: SpendCap,
                 intended: list[dict] | None = None) -> list[Probe]:
    """Build a probe batch against a fresh blockhash.

    `intended` (optional) maps seq -> {validator, window} for the
    design record. Building never sends anything.
    """
    blockhash = rpc.get_latest_blockhash()["blockhash"]
    probes: list[Probe] = []
    seq = 0
    for tier in fee_tiers:
        for _ in range(count_per_tier):
            probe = Probe(run_id=run_id, seq=seq, fee_tier=tier)
            cap.plan(probe.cost_lamports)
            if intended and seq < len(intended):
                probe.intended_validator = intended[seq].get("validator", "")
                probe.intended_window = intended[seq].get("window", [])
            probe.tx_b64 = build_probe_tx(payer, probe, blockhash)
            # Signature of a VersionedTransaction is its first signature.
            tx = VersionedTransaction.from_bytes(base64.b64decode(probe.tx_b64))
            probe.signature = str(tx.signatures[0])
            probes.append(probe)
            seq += 1
    log.info("built %d probes, planned spend %d lamports (cap %d)",
             len(probes), cap.planned_lamports, cap.limit_lamports)
    return probes


def probes_to_json(probes: list[Probe]) -> list[dict]:
    out = []
    for p in probes:
        out.append({
            "run_id": p.run_id, "seq": p.seq, "marker": p.marker,
            "fee_tier_micro_lamports": p.fee_tier,
            "cost_lamports": p.cost_lamports,
            "signature": p.signature,
            "tx_b64": p.tx_b64,
            "intended_validator": p.intended_validator,
            "intended_window": p.intended_window,
            "sent_at_unix": p.sent_at_unix, "status": p.status,
        })
    return out


def save_probes(probes: list[Probe], path: str) -> None:
    with open(path, "w") as f:
        json.dump({"probes": probes_to_json(probes)}, f, indent=2)
    log.info("wrote %s (%d probes)", path, len(probes))
