"""Sending probes: dry-run by default, live only behind --live.

Rules enforced here, not by convention:
- Default mode is DRY-RUN: txs are built and simulated (a free read),
  never sent. Nothing in this module can send without live=True.
- Live mode requires ALL of: --live on the CLI, a keypair file path,
  and the spend cap not being exceeded.
- Sends are paced (default 400 ms apart) so the probes do not arrive
  as a single burst -- a burst would measure our own batching, not the
  leader's scheduler.
- Every send records wall-clock time, signature, fee tier and intended
  window, so send order can be compared against landing order later.
"""

from __future__ import annotations

import logging
import time

from .rpc import Rpc, RpcError
from .txbuild import Probe, SpendCap

log = logging.getLogger("itb.sender")


def simulate_probes(rpc: Rpc, probes: list[Probe]) -> None:
    """Dry-run: simulate each probe (free, no state change)."""
    for p in probes:
        try:
            res = rpc.simulate_transaction(p.tx_b64)
            err = res.get("value", {}).get("err")
            p.status = "simulated" if err is None else "failed"
            if err is not None:
                log.warning("probe %s simulate err: %s", p.marker, err)
        except RpcError as exc:
            p.status = "failed"
            log.warning("probe %s simulate failed: %s", p.marker, exc)


def send_probes(rpc: Rpc, probes: list[Probe], cap: SpendCap,
                live: bool, pace_s: float = 0.4) -> None:
    """Send probes in seq order, paced. Dry-run unless live=True."""
    if not live:
        simulate_probes(rpc, probes)
        log.info("dry-run: %d probes simulated, 0 sent", len(probes))
        return
    for p in probes:
        if cap.spent_lamports + p.cost_lamports > cap.limit_lamports:
            raise RuntimeError(
                f"spend cap would be exceeded sending {p.marker}; aborting")
        p.sent_at_unix = time.time()
        try:
            sig = rpc.send_transaction(p.tx_b64)
            assert sig == p.signature, f"sent sig {sig} != built {p.signature}"
            p.status = "sent"
            log.info("sent %s tier=%d sig=%s", p.marker, p.fee_tier, sig[:12])
        except (RpcError, AssertionError) as exc:
            p.status = "failed"
            log.warning("probe %s send failed: %s", p.marker, exc)
        time.sleep(pace_s)


def wait_for_landings(rpc: Rpc, probes: list[Probe],
                      timeout_s: int = 90) -> dict[str, dict | None]:
    """Poll signature status for sent probes; returns sig -> status."""
    sent = [p for p in probes if p.status == "sent"]
    results: dict[str, dict | None] = {}
    deadline = time.time() + timeout_s
    pending = {p.signature for p in sent}
    while pending and time.time() < deadline:
        statuses = rpc.get_signature_statuses(sorted(pending))
        for sig, st in zip(sorted(pending), statuses):
            if st is None:
                continue
            if st.get("err"):
                results[sig] = st
                pending.discard(sig)
            elif st.get("confirmationStatus") in ("confirmed", "finalized"):
                results[sig] = st
                pending.discard(sig)
        if pending:
            time.sleep(2)
    for sig in pending:
        results[sig] = None
    for p in sent:
        st = results.get(p.signature)
        if st and not st.get("err"):
            p.status = "confirmed"
        elif st and st.get("err"):
            p.status = "failed"
        else:
            p.status = "unconfirmed"
    log.info("landings: %d confirmed, %d failed/unconfirmed of %d sent",
             sum(1 for p in sent if p.status == "confirmed"),
             sum(1 for p in sent if p.status != "confirmed"), len(sent))
    return results
