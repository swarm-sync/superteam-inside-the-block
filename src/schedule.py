"""Leader schedule and leader-window detection.

Solana leaders rotate every 4 slots (1.6s). getLeaderSchedule returns,
per epoch, the slots each validator leads. A "leader window" for a
validator is a run of consecutive slots it leads.

We never assume a probe lands in the window we aimed at: propagation
delay, RPC latency and the leader's own admission mean a probe can land
in a later slot under a different leader. The honest measurement is
therefore: record send time + intended window, then read the ACTUAL
landing slot/leader from getBlock afterwards. The analysis groups by
actual leader, never by intended leader.
"""

from __future__ import annotations

import logging

from .rpc import Rpc

log = logging.getLogger("itb.schedule")

SLOTS_PER_EPOCH_FALLBACK = 432_000


def get_schedule(rpc: Rpc, epoch: int | None = None) -> dict[str, list[int]]:
    # NOTE: public RPCs return null when an explicit epoch is passed;
    # no-arg call returns the CURRENT epoch's schedule. Epoch is kept
    # in the signature for API-parity, but the live call omits it.
    # Callers must record epoch_info alongside the schedule so the
    # epoch the schedule belongs to is unambiguous in the run record.
    # IMPORTANT: the returned slot numbers are RELATIVE to the epoch
    # start (0 .. slots_per_epoch-1), NOT absolute slots. Use
    # to_absolute() before comparing with getSlot / block slots.
    sched = rpc.get_leader_schedule()
    if sched is None:
        raise RuntimeError("getLeaderSchedule returned null for current epoch")
    return {k: sorted(v) for k, v in sched.items()}


def epoch_start_slot(epoch_info: dict) -> int:
    return int(epoch_info["absoluteSlot"]) - int(epoch_info["slotIndex"])


def to_absolute(schedule: dict[str, list[int]],
                start_slot: int) -> dict[str, list[int]]:
    return {k: [s + start_slot for s in v] for k, v in schedule.items()}


def windows_for(pubkey: str, schedule: dict[str, list[int]],
                start_slot: int, end_slot: int) -> list[tuple[int, int]]:
    """Consecutive-slot runs for one validator inside [start_slot, end_slot]."""
    slots = [s for s in schedule.get(pubkey, []) if start_slot <= s <= end_slot]
    windows: list[tuple[int, int]] = []
    for s in slots:
        if windows and s == windows[-1][1] + 1:
            windows[-1] = (windows[-1][0], s)
        else:
            windows.append((s, s))
    return windows


def upcoming_windows(rpc: Rpc, pubkeys: list[str],
                     horizon_slots: int = 1200) -> list[dict]:
    """Next leader windows for the given validators, from the live slot.

    Returns rows: {validator, start_slot, end_slot, slots, version}.
    horizon_slots default 1200 slots ~= 8 minutes.
    """
    epoch_info = rpc.get_epoch_info()
    epoch = int(epoch_info["epoch"])
    now = int(epoch_info["absoluteSlot"])
    schedule = to_absolute(get_schedule(rpc, epoch), epoch_start_slot(epoch_info))
    out = []
    for pk in pubkeys:
        for (a, b) in windows_for(pk, schedule, now + 1, now + horizon_slots):
            out.append({"validator": pk, "start_slot": a, "end_slot": b,
                        "slots": b - a + 1})
    out.sort(key=lambda w: w["start_slot"])
    log.info("epoch %d, current slot %d: %d upcoming windows for %d validators",
             epoch, now, len(out), len(pubkeys))
    return out


def leader_at(slot: int, schedule: dict[str, list[int]]) -> str | None:
    for pk, slots in schedule.items():
        if slot in slots:
            return pk
    return None
