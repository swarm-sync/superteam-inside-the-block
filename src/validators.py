"""Validator selection for the measurement.

Goal (per the bounty): at least 6 validators spanning at least 3 distinct
client software / fork versions, identifiable by version string at
measurement time.

Data sources, all public and unauthenticated:
- getVoteAccounts: the live validator set with activated stake.
- getClusterNodes: gossip-advertised version string per node pubkey.

We join the two, rank by activated stake, and pick the highest-stake
validators while maximising version diversity. Jito validators are in
scope (their bundles/tips are NOT measured -- only the leader's own
scheduler, and our probes never use Jito bundles).

Honesty note: the version string is gossip-advertised, not attested.
We record the exact strings seen at selection time, and re-check them
at measurement time. If a validator's version changes between selection
and measurement, the run record keeps both.
"""

from __future__ import annotations

import json
import logging
from collections import Counter

from .rpc import Rpc

log = logging.getLogger("itb.validators")


def fetch_validator_table(rpc: Rpc) -> list[dict]:
    """Join vote accounts with gossip version strings.

    Returns rows: {pubkey, version, activated_stake, commission,
    delinquent, vote_pubkey}.
    """
    vote = rpc.get_vote_accounts()
    nodes = rpc.get_cluster_nodes()
    versions = {n["pubkey"]: n.get("version") for n in nodes}

    rows: list[dict] = []
    for group, delinquent in (("current", False), ("delinquent", True)):
        for v in vote.get(group, []):
            rows.append({
                "pubkey": v["nodePubkey"],
                "vote_pubkey": v["votePubkey"],
                "version": versions.get(v["nodePubkey"]),
                "activated_stake": int(v["activatedStake"]),
                "commission": int(v["commission"]),
                "delinquent": delinquent,
            })
    return rows


def select_validators(rpc: Rpc, min_validators: int = 6,
                      min_versions: int = 3) -> list[dict]:
    """Pick the measurement set: top stake, maximising version diversity.

    Strategy: sort current (non-delinquent) validators by stake, then
    greedily take the highest-stake validator of each distinct version
    first, then fill up to min_validators by stake. Raises if the live
    set cannot meet the minimums.
    """
    rows = fetch_validator_table(rpc)
    live = [r for r in rows if not r["delinquent"] and r["version"]]
    live.sort(key=lambda r: r["activated_stake"], reverse=True)

    versions = Counter(r["version"] for r in live)
    log.info("live validators with version: %d across %d versions",
             len(live), len(versions))
    log.info("top versions by validator count: %s", versions.most_common(8))
    if len(versions) < min_versions:
        raise RuntimeError(
            f"only {len(versions)} distinct versions visible, need {min_versions}")

    picked: list[dict] = []
    seen_versions: set[str] = set()
    # Round 1: one top-stake validator per version (stake order).
    for r in live:
        if r["version"] not in seen_versions:
            picked.append(r)
            seen_versions.add(r["version"])
        if len(seen_versions) >= len(versions):
            break
    # Round 2: fill by stake until we have min_validators.
    picked_keys = {r["pubkey"] for r in picked}
    for r in live:
        if len(picked) >= min_validators:
            break
        if r["pubkey"] not in picked_keys:
            picked.append(r)
            picked_keys.add(r["pubkey"])
    picked.sort(key=lambda r: r["activated_stake"], reverse=True)
    got_versions = {r["version"] for r in picked}
    if len(picked) < min_validators or len(got_versions) < min_versions:
        raise RuntimeError(
            f"selection short: {len(picked)} validators, "
            f"{len(got_versions)} versions")
    for r in picked:
        log.info("selected %s version=%s stake=%.1f SOL commission=%d%%",
                 r["pubkey"][:8], r["version"],
                 r["activated_stake"] / 1e9, r["commission"])
    return picked


def save_selection(rows: list[dict], path: str) -> None:
    with open(path, "w") as f:
        json.dump({"validators": rows}, f, indent=2)
    log.info("wrote %s (%d validators)", path, len(rows))
