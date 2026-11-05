"""Inside the Block -- CLI.

One command reproduces a full measurement (or a dry run of one):

    python -m src.run measure --network devnet --out data/devnet/run1
    python -m src.run measure --network mainnet --dry-run --out data/mainnet/dry1
    python -m src.run measure --network mainnet --live --keypair /path/to/key.json --out data/mainnet/live1

Sub-commands (for judges / debugging) do each stage separately:
    select    pick 6+ validators across 3+ client versions
    schedule  upcoming leader windows for the selected validators
    build     build probe txs (never sends)
    send      send probes (--live required to actually send)
    fetch     fetch blocks for landed slots and find our probes
    analyze   fee-vs-position / send-vs-landing analysis

Safety:
- Default is DRY-RUN everywhere. Nothing sends without --live.
- Live sends need --keypair pointing at a file OUTSIDE the repo
  (keys are never committed; .gitignore blocks *.json key files and
  the keys/ directory).
- Spend cap 0.05 SOL is enforced before building and before sending.
- No mainnet send is possible in this checkout's default config:
  --network mainnet --live additionally requires the env var
  ITB_ALLOW_MAINNET_LIVE=1, so a stray --live cannot spend real SOL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from solders.keypair import Keypair

from .analysis import analyze, save_json as save_analysis
from .blocks import fetch_blocks, find_probes, save_json
from .rpc import DEFAULT_DEVNET, DEFAULT_LOCALNET, DEFAULT_MAINNET, Rpc
from .schedule import epoch_start_slot, get_schedule, to_absolute, upcoming_windows
from .sender import send_probes, wait_for_landings
from .txbuild import (CAP_LAMPORTS_DEFAULT, FEE_TIERS, SpendCap,
                      build_probes, probes_to_json, save_probes)
from .validators import fetch_validator_table, select_validators

log = logging.getLogger("itb")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S")


def rpc_for(network: str, rpc_url: str | None) -> Rpc:
    if rpc_url:
        return Rpc(rpc_url)
    if network == "devnet":
        return Rpc(DEFAULT_DEVNET)
    if network == "localnet":
        return Rpc(DEFAULT_LOCALNET)
    return Rpc(DEFAULT_MAINNET)


def load_keypair(path: str) -> Keypair:
    with open(path) as f:
        secret = json.load(f)
    if isinstance(secret, dict):
        secret = secret.get("secretKey") or secret.get("keypair") or []
    return Keypair.from_bytes(bytes(secret))


def run_id_default() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M")


def cmd_select(args) -> int:
    rpc = rpc_for(args.network, args.rpc_url)
    rows = select_validators(rpc, min_validators=args.min_validators,
                             min_versions=args.min_versions)
    os.makedirs(args.out, exist_ok=True)
    save_json({"validators": rows}, os.path.join(args.out, "validators.json"))
    versions = {r["pubkey"]: r["version"] for r in rows}
    print(json.dumps({"validators": len(rows),
                      "versions": sorted(set(versions.values()))}, indent=2))
    return 0


def cmd_schedule(args) -> int:
    rpc = rpc_for(args.network, args.rpc_url)
    with open(os.path.join(args.out, "validators.json")) as f:
        validators = json.load(f)["validators"]
    wins = upcoming_windows(rpc, [v["pubkey"] for v in validators])
    save_json({"windows": wins}, os.path.join(args.out, "windows.json"))
    print(f"{len(wins)} upcoming windows")
    return 0


def cmd_build(args) -> int:
    rpc = rpc_for(args.network, args.rpc_url)
    keypair = load_keypair(args.keypair) if args.keypair else Keypair()
    if not args.keypair:
        log.warning("no keypair given: ephemeral keypair generated in memory "
                    "only (fine for dry-run; live sends need --keypair)")
    cap = SpendCap(limit_lamports=args.cap_lamports)
    intended = None
    wins_path = os.path.join(args.out, "windows.json")
    if os.path.exists(wins_path):
        with open(wins_path) as f:
            wins = json.load(f)["windows"]
        intended = [{"validator": w["validator"],
                     "window": [w["start_slot"], w["end_slot"]]} for w in wins]
    probes = build_probes(rpc, keypair, args.run_id, FEE_TIERS,
                          args.per_tier, cap, intended=intended)
    os.makedirs(args.out, exist_ok=True)
    save_probes(probes, os.path.join(args.out, "probes.json"))
    save_json({"cap_lamports": cap.limit_lamports,
               "planned_lamports": cap.planned_lamports,
               "probe_count": len(probes)},
              os.path.join(args.out, "cap.json"))
    print(f"built {len(probes)} probes, planned {cap.planned_lamports} lamports "
          f"(cap {cap.limit_lamports})")
    return 0


def _load_probes(out: str) -> list[dict]:
    with open(os.path.join(out, "probes.json")) as f:
        return json.load(f)["probes"]


def cmd_send(args) -> int:
    from .txbuild import Probe
    rpc = rpc_for(args.network, args.rpc_url)
    if args.live and args.network == "mainnet":
        if os.environ.get("ITB_ALLOW_MAINNET_LIVE") != "1":
            print("ERROR: mainnet live sends need ITB_ALLOW_MAINNET_LIVE=1 "
                  "in the environment. Refusing.", file=sys.stderr)
            return 2
    keypair = load_keypair(args.keypair) if args.keypair else None
    if args.live and keypair is None:
        print("ERROR: --live needs --keypair <file>. Refusing.", file=sys.stderr)
        return 2
    probes = []
    for p in _load_probes(args.out):
        probes.append(Probe(
            run_id=p["run_id"], seq=p["seq"],
            fee_tier=p["fee_tier_micro_lamports"],
            marker=p["marker"], signature=p.get("signature", ""),
            tx_b64=p.get("tx_b64", ""), cost_lamports=p["cost_lamports"],
            intended_validator=p.get("intended_validator", ""),
            intended_window=p.get("intended_window", []),
            sent_at_unix=p.get("sent_at_unix", 0.0),
            status=p.get("status", "built")))
    cap = SpendCap(limit_lamports=args.cap_lamports)
    cap.planned_lamports = sum(p.cost_lamports for p in probes)
    send_probes(rpc, probes, cap, live=args.live)
    if args.live:
        wait_for_landings(rpc, probes)
    save_probes(probes, os.path.join(args.out, "probes.json"))
    save_json({"cap_lamports": cap.limit_lamports,
               "planned_lamports": cap.planned_lamports,
               "spent_lamports": cap.spent_lamports},
              os.path.join(args.out, "cap.json"))
    return 0


def cmd_fetch(args) -> int:
    rpc = rpc_for(args.network, args.rpc_url)
    probes = _load_probes(args.out)
    by_sig = {p["signature"]: p for p in probes if p.get("signature")}
    # Discover landing slots from signature statuses.
    sigs = sorted(by_sig)
    slots: list[int] = []
    for i in range(0, len(sigs), 100):
        for st in rpc.get_signature_statuses(sigs[i:i + 100]):
            if st and st.get("slot") is not None:
                slots.append(int(st["slot"]))
    log.info("signature statuses point at %d distinct slots", len(set(slots)))
    blocks = fetch_blocks(rpc, slots)
    landings = find_probes(blocks, by_sig)
    save_json({"landings": landings}, os.path.join(args.out, "landings.json"))
    save_json({"blocks": {str(k): v for k, v in blocks.items()}},
              os.path.join(args.out, "blocks.json"))
    print(f"{len(landings)} of {len(probes)} probes found in fetched blocks")
    return 0


def cmd_analyze(args) -> int:
    rpc = rpc_for(args.network, args.rpc_url)
    with open(os.path.join(args.out, "landings.json")) as f:
        landings = json.load(f)["landings"]
    epoch_info = rpc.get_epoch_info()
    schedule = to_absolute(get_schedule(rpc, int(epoch_info["epoch"])),
                           epoch_start_slot(epoch_info))
    versions: dict[str, str] = {}
    try:
        with open(os.path.join(args.out, "validators.json")) as f:
            for v in json.load(f)["validators"]:
                versions[v["pubkey"]] = v["version"]
    except FileNotFoundError:
        log.warning("no validators.json; leader versions will be 'unknown'")
    result = analyze(landings, schedule, versions)
    save_analysis(result, os.path.join(args.out, "analysis.json"))
    print(json.dumps(result, indent=2))
    return 0


def cmd_measure(args) -> int:
    """Full pipeline: select -> schedule -> build -> send -> fetch -> analyze."""
    os.makedirs(args.out, exist_ok=True)
    args.out = args.out
    rc = cmd_select(args)
    if rc:
        return rc
    rc = cmd_schedule(args)
    if rc:
        return rc
    # Devnet funding: ephemeral keypair + airdrop, only for live devnet.
    keypair_path = args.keypair
    if args.live and args.network == "devnet" and not keypair_path:
        rpc = rpc_for(args.network, args.rpc_url)
        kp = Keypair()
        keypair_path = os.path.join(args.out, "devnet-ephemeral-keypair.json")
        with open(keypair_path, "w") as f:
            json.dump(list(bytes(kp)), f)
        log.info("generated ephemeral devnet keypair %s (NOT committed; "
                 "delete after the run)", keypair_path)
        sig = rpc.request_airdrop(str(kp.pubkey()), 1_000_000_000)
        log.info("airdrop requested: %s", sig[:16])
        time.sleep(15)  # let the airdrop confirm
        bal = rpc.get_balance(str(kp.pubkey()))
        log.info("ephemeral keypair balance: %d lamports", bal)
        if bal < 100_000:
            print("ERROR: airdrop did not land; cannot fund devnet probes.",
                  file=sys.stderr)
            return 2
        args.keypair = keypair_path
    rc = cmd_build(args)
    if rc:
        return rc
    rc = cmd_send(args)
    if rc:
        return rc
    if args.live:
        rc = cmd_fetch(args)
        if rc:
            return rc
        rc = cmd_analyze(args)
        if rc:
            return rc
    # Ephemeral devnet keypair: delete it now that the run is done.
    # It was never committed (.gitignore), but it must not linger.
    eph = os.path.join(args.out, "devnet-ephemeral-keypair.json")
    if os.path.exists(eph):
        os.remove(eph)
        log.info("deleted ephemeral devnet keypair %s", eph)
    print(f"run complete. Outputs in {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="inside-the-block",
                                description="Measure how Solana leaders order txs.")
    p.add_argument("--network", choices=["devnet", "mainnet", "localnet"],
                   default="devnet")
    p.add_argument("--rpc-url", default=None)
    p.add_argument("--out", default="data/run")
    p.add_argument("--run-id", default=None)
    p.add_argument("--keypair", default=None,
                   help="path to a keypair JSON file (outside the repo)")
    p.add_argument("--cap-lamports", type=int, default=CAP_LAMPORTS_DEFAULT)
    p.add_argument("--per-tier", type=int, default=3,
                   help="probes per fee tier")
    p.add_argument("--min-validators", type=int, default=6)
    p.add_argument("--min-versions", type=int, default=3)
    p.add_argument("--live", action="store_true",
                   help="actually send txs (default: dry-run only)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("command", choices=["select", "schedule", "build", "send",
                                       "fetch", "analyze", "measure"])
    args = p.parse_args(argv)
    args.run_id = args.run_id or run_id_default()
    setup_logging(args.verbose)
    log.info("run_id=%s network=%s live=%s out=%s",
             args.run_id, args.network, args.live, args.out)
    cmds = {"select": cmd_select, "schedule": cmd_schedule,
            "build": cmd_build, "send": cmd_send, "fetch": cmd_fetch,
            "analyze": cmd_analyze, "measure": cmd_measure}
    return cmds[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
