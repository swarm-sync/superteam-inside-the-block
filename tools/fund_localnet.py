"""Fund a probe keypair on a local test validator.

The local validator's `requestAirdrop` RPC fails with -32603 in this
VM (faucet service never becomes usable), so this bypasses the faucet
service entirely: it signs a plain SystemProgram transfer from the
validator's own genesis-funded faucet keypair (test-ledger*/faucet-keypair.json)
to the probe keypair. Localnet SOL only; worthless outside the test
validator. The probe keypair file lives OUTSIDE the repo
(~/.cache/itb-localnet-keypair.json) and is never committed.
"""

from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/superteam-inside-the-block")

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from src.rpc import Rpc

LEDGER = "/home/hatch/workspace/.cache/genesis-ledger"
FAUCET_KEYPAIR = "/home/hatch/workspace/.cache/keys/faucet.json"
PROBE_PATH = "/home/hatch/workspace/.cache/itb-localnet-keypair.json"
LAMPORTS = 1_000_000_000  # 1 SOL


def main() -> int:
    rpc = Rpc("http://127.0.0.1:8899")
    with open(FAUCET_KEYPAIR) as f:
        faucet = Keypair.from_bytes(bytes(json.load(f)))
    try:
        with open(PROBE_PATH) as f:
            probe = Keypair.from_bytes(bytes(json.load(f)))
    except FileNotFoundError:
        probe = Keypair()
        with open(PROBE_PATH, "w") as f:
            json.dump(list(bytes(probe)), f)
    print("faucet", str(faucet.pubkey()), "balance",
          rpc.get_balance(str(faucet.pubkey())))
    print("probe ", str(probe.pubkey()), "balance",
          rpc.get_balance(str(probe.pubkey())))
    if rpc.get_balance(str(probe.pubkey())) >= LAMPORTS:
        print("probe already funded")
        return 0
    bh = rpc.get_latest_blockhash()["blockhash"]
    ix = transfer(TransferParams(
        from_pubkey=faucet.pubkey(), to_pubkey=probe.pubkey(),
        lamports=LAMPORTS))
    msg = MessageV0.try_compile(
        faucet.pubkey(), [ix], [], Hash.from_string(bh))
    tx = VersionedTransaction(msg, [faucet])
    import base64
    sig = rpc.send_transaction(base64.b64encode(bytes(tx)).decode())
    print("transfer sig", sig)
    for _ in range(30):
        time.sleep(2)
        st = rpc.confirm(sig, timeout_s=5)
        if st:
            print("confirmed", st)
            break
    print("probe balance now", rpc.get_balance(str(probe.pubkey())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
