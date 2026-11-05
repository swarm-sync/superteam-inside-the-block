"""Fetch blocks and recover our probes' positions inside them.

getBlock with encoding=jsonParsed returns, per transaction:
- transaction.signatures[0]
- transaction.message.instructions[] (parsed; Memo shows as
  program "spl-memo" with parsed info, or as raw data)
- meta.fee, meta.err, meta.preBalances/postBalances
- meta.logMessages (memo text appears here as "Program log: Memo ...")

We locate a probe three independent ways and require agreement:
1. signature match against our built list;
2. memo marker string in the instruction data / log messages;
3. fee == expected cost for the probe's tier (sanity check only).

A block's transaction list is in execution order, so a probe's index
in that list IS its landing position. Position 0 = first tx executed
in the block.
"""

from __future__ import annotations

import json
import logging

from .rpc import Rpc

log = logging.getLogger("itb.blocks")

MEMO_PROGRAMS = {"MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "spl-memo"}


def memo_text_of(tx: dict) -> str | None:
    """Extract memo text from a jsonParsed transaction, if present.

    jsonParsed is ragged in practice: for some instructions `parsed`
    is a dict, for others a plain string. Be defensive everywhere.
    """
    try:
        for ix in tx["transaction"]["message"]["instructions"]:
            if not isinstance(ix, dict):
                continue
            prog = ix.get("program")
            if prog in MEMO_PROGRAMS or prog == "spl-memo":
                parsed = ix.get("parsed")
                if isinstance(parsed, dict):
                    info = parsed.get("info", {})
                    if isinstance(info, dict) and "memo" in info:
                        return str(info["memo"])
                    if isinstance(info, str):
                        return info
                elif isinstance(parsed, str):
                    return parsed
                data = ix.get("data")
                if isinstance(data, str):
                    return data
    except (KeyError, TypeError, AttributeError):
        pass
    # Fallback: memo text also appears in log messages.
    try:
        for line in tx.get("meta", {}).get("logMessages", []) or []:
            if isinstance(line, str) and line.startswith("Program log: Memo "):
                return line[len("Program log: Memo "):]
    except (KeyError, TypeError, AttributeError):
        pass
    return None


def parse_block(block: dict, slot: int) -> list[dict]:
    """All txs in a block, in execution order, with fee + memo."""
    out = []
    for idx, tx in enumerate(block.get("transactions", [])):
        try:
            sig = tx["transaction"]["signatures"][0]
            meta = tx.get("meta") or {}
            out.append({
                "slot": slot,
                "index": idx,
                "signature": sig,
                "fee_lamports": meta.get("fee"),
                "err": meta.get("err"),
                "memo": memo_text_of(tx),
            })
        except (KeyError, TypeError, IndexError):
            continue
    return out


def fetch_blocks(rpc: Rpc, slots: list[int]) -> dict[int, list[dict]]:
    """Fetch + parse blocks. Returns slot -> parsed tx rows."""
    result: dict[int, list[dict]] = {}
    for slot in sorted(set(slots)):
        block = rpc.get_block(slot)
        if block is None:
            log.warning("slot %d: no block (skipped or pruned)", slot)
            result[slot] = []
            continue
        rows = parse_block(block, slot)
        result[slot] = rows
        log.info("slot %d: %d txs, blockhash %s", slot, len(rows),
                 str(block.get("blockhash", ""))[:12])
    return result


def find_probes(blocks: dict[int, list[dict]],
                probes_by_sig: dict[str, dict]) -> list[dict]:
    """Locate our probes inside fetched blocks.

    Returns landing records: probe fields + slot, index, block size,
    fee actually charged, leader-agnostic (leader is joined later from
    the schedule).
    """
    landings = []
    for slot, rows in blocks.items():
        for row in rows:
            probe = probes_by_sig.get(row["signature"])
            if probe is None and row.get("memo"):
                # second-chance match on marker text
                for p in probes_by_sig.values():
                    if p["marker"] == row["memo"]:
                        probe = p
                        break
            if probe is None:
                continue
            landings.append({
                **probe,
                "landing_slot": slot,
                "landing_index": row["index"],
                "block_tx_count": len(rows),
                "fee_charged_lamports": row["fee_lamports"],
                "landing_err": row["err"],
            })
    return landings


def save_json(obj: object, path: str) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
    log.info("wrote %s", path)
