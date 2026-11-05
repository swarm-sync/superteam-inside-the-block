"""Unit tests -- run with: python -m pytest tests/ -v
(or: python -m unittest discover tests)

These test the pure logic (fee math, cap enforcement, block parsing,
rank correlation) on synthetic fixtures. They never touch the network.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.analysis import pairwise_order_agreement, spearman
from src.blocks import memo_text_of, parse_block
from src.txbuild import (CAP_LAMPORTS_DEFAULT, SpendCap, probe_cost_lamports,
                         priority_fee_lamports)


def test_fee_math():
    assert priority_fee_lamports(0) == 0
    assert priority_fee_lamports(1_000) == 200
    assert priority_fee_lamports(10_000) == 2_000
    assert priority_fee_lamports(100_000) == 20_000
    assert probe_cost_lamports(100_000) == 25_000
    assert probe_cost_lamports(0) == 5_000


def test_spend_cap_blocks_overrun():
    cap = SpendCap(limit_lamports=10_000)
    cap.plan(5_000)
    try:
        cap.plan(5_001)
        raise AssertionError("cap should have refused")
    except RuntimeError as e:
        assert "spend cap exceeded" in str(e)
    # Whole default measurement (4 tiers x 3) fits the 0.05 SOL cap easily.
    cap2 = SpendCap(limit_lamports=CAP_LAMPORTS_DEFAULT)
    for tier in (0, 1_000, 10_000, 100_000):
        for _ in range(3):
            cap2.plan(probe_cost_lamports(tier))
    assert cap2.planned_lamports == 3 * (5_000 + 5_200 + 7_000 + 25_000)
    assert cap2.planned_lamports < CAP_LAMPORTS_DEFAULT


def test_spearman_perfect_negative():
    # Higher fee -> earlier index: perfect negative correlation.
    assert spearman([0, 1, 2, 3], [3, 2, 1, 0]) == -1.0
    assert spearman([0, 1, 2, 3], [0, 1, 2, 3]) == 1.0
    assert spearman([5, 5, 5], [0, 1, 2]) is None  # no fee variance


def test_order_agreement():
    assert pairwise_order_agreement([(0, 0), (1, 1), (2, 2)]) == 1.0
    assert pairwise_order_agreement([(0, 2), (1, 1), (2, 0)]) == 0.0
    assert pairwise_order_agreement([(0, 0)]) is None


def _fake_block():
    # Minimal jsonParsed block: 3 txs, middle one carries our memo.
    def tx(sig, fee, memo=None):
        ix = {"program": "spl-memo", "parsed": {"info": {"memo": memo},
              "type": "memo"}, "programId": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"} if memo else \
             {"program": "system", "parsed": {"info": {}, "type": "transfer"},
              "programId": "11111111111111111111111111111111"}
        return {"transaction": {"signatures": [sig],
                                "message": {"instructions": [ix]}},
                "meta": {"fee": fee, "err": None, "logMessages": []}}
    return {"transactions": [tx("sigA", 5000),
                             tx("sigB", 7000, "ITB-20261105a-0001"),
                             tx("sigC", 5000)]}


def test_parse_block_finds_memo_and_order():
    rows = parse_block(_fake_block(), slot=12345)
    assert len(rows) == 3
    assert rows[1]["signature"] == "sigB"
    assert rows[1]["index"] == 1
    assert rows[1]["memo"] == "ITB-20261105a-0001"
    assert rows[1]["fee_lamports"] == 7000
    assert rows[0]["memo"] is None


if __name__ == "__main__":
    test_fee_math()
    test_spend_cap_blocks_overrun()
    test_spearman_perfect_negative()
    test_order_agreement()
    test_parse_block_finds_memo_and_order()
    print("all unit tests passed")
