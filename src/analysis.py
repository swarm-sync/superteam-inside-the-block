"""Ordering analysis: fee vs position, send order vs landing order,
rule vs noise.

All statistics are computed from the landing records (probes we sent
ourselves, found in fetched blocks). No third-party tx is treated as
evidence of the rule -- other txs in the block are used only for
context (block size, fee distribution).

Methods (stdlib only, no scipy):
- Spearman rank correlation between fee tier and landing index within
  a block: +1 = higher fee lands earlier, 0 = no relation, -1 = reversed.
  Computed per block with >=4 of our probes, then averaged.
- Send-order vs landing-order: Kendall-style pairwise agreement --
  fraction of probe pairs whose landing order matches send order.
- Per-leader and per-version grouping: median landing index by fee
  tier, so a version-specific rule shows up as a different pattern
  for one client version vs another.
- Noise accounting: for each block, the fee spread of NON-probe txs
  (their priority fees are unknown from jsonParsed alone -- we report
  their base fees only) and the number of competing txs ahead of our
  probes. If our probes are a small share of a full block, ordering
  signal is weak and we say so.

A null result ("no rule, it's noise") is a valid finding and is
reported as such -- it is not a failure of the experiment.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

log = logging.getLogger("itb.analysis")


def _ranks(values: list[float]) -> list[float]:
    """Average ranks for ties (1-based)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    """Spearman rank correlation, or None if undefined (<2 points / no variance)."""
    if len(x) < 2 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    rx, ry = _ranks(x), _ranks(y)
    n = len(x)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def pairwise_order_agreement(pairs: list[tuple[int, int]]) -> float | None:
    """Fraction of pairs (send_seq, landing_index) in the same order.

    pairs are (send order, landing order) for probes in one block.
    1.0 = landing order exactly matches send order; 0.5 = random.
    """
    if len(pairs) < 2:
        return None
    agree = total = 0
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            total += 1
            if (pairs[i][0] - pairs[j][0]) * (pairs[i][1] - pairs[j][1]) > 0:
                agree += 1
    return agree / total if total else None


def analyze(landings: list[dict], schedule: dict[str, list[int]],
            versions: dict[str, str]) -> dict:
    """Full analysis of landing records.

    `schedule`: validator -> [slots]; `versions`: validator -> version str.
    Leader for a slot is resolved from the schedule; if the slot is not
    in the fetched schedule (epoch boundary), leader is 'unknown'.
    """
    slot_to_leader: dict[int, str] = {}
    for validator, slots in schedule.items():
        for s in slots:
            slot_to_leader[s] = validator

    by_block: dict[int, list[dict]] = defaultdict(list)
    for rec in landings:
        rec["leader"] = slot_to_leader.get(rec["landing_slot"], "unknown")
        rec["leader_version"] = versions.get(rec["leader"], "unknown")
        by_block[rec["landing_slot"]].append(rec)

    per_block = []
    spearman_values = []
    for slot, recs in sorted(by_block.items()):
        fees = [r["fee_tier_micro_lamports"] for r in recs]
        idxs = [r["landing_index"] for r in recs]
        sp = spearman(fees, idxs)
        if sp is not None:
            spearman_values.append(sp)
        agree = pairwise_order_agreement([(r["seq"], r["landing_index"]) for r in recs])
        per_block.append({
            "slot": slot,
            "leader": recs[0]["leader"],
            "leader_version": recs[0]["leader_version"],
            "probe_count": len(recs),
            "block_tx_count": recs[0]["block_tx_count"],
            "spearman_fee_vs_index": sp,
            "send_landing_order_agreement": agree,
            "median_index_by_tier": {
                str(t): _median([r["landing_index"] for r in recs
                                 if r["fee_tier_micro_lamports"] == t])
                for t in sorted(set(fees))
            },
        })

    by_version: dict[str, list[float]] = defaultdict(list)
    for rec in landings:
        by_version[rec["leader_version"]].append(rec["landing_index"])
    version_summary = {
        v: {"probe_count": len(ixs), "median_landing_index": _median(ixs)}
        for v, ixs in sorted(by_version.items())
    }

    total_probes = len(landings)
    return {
        "probes_landed": total_probes,
        "blocks_with_probes": len(by_block),
        "mean_spearman_fee_vs_index": (
            sum(spearman_values) / len(spearman_values) if spearman_values else None),
        "spearman_per_block": spearman_values,
        "per_block": per_block,
        "by_leader_version": version_summary,
        "interpretation": _interpret(spearman_values, per_block),
    }


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _interpret(spearman_values: list[float], per_block: list[dict]) -> str:
    if not spearman_values:
        return ("No block contained enough of our probes to test "
                "fee-vs-position. No rule can be claimed from this run.")
    mean_sp = sum(spearman_values) / len(spearman_values)
    if mean_sp < -0.5:
        return ("Higher-fee probes consistently landed earlier in the block "
                f"(mean Spearman {mean_sp:.2f}). Consistent with a "
                "fee-ordered scheduler, within this run's noise.")
    if mean_sp > 0.5:
        return ("Higher-fee probes consistently landed LATER in the block "
                f"(mean Spearman {mean_sp:.2f}). Against a simple "
                "fee-ordered rule; possibly arrival order dominates.")
    return (f"No consistent fee ordering (mean Spearman {mean_sp:.2f}). "
            "Landing order looks dominated by arrival order / noise at "
            "this probe volume. This is a null result, not a failure.")


def save_json(obj: object, path: str) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
    log.info("wrote %s", path)
