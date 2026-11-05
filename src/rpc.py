"""Raw JSON-RPC client for the Solana RPC API.

Deliberately dependency-light (requests only) and version-pinned: no
solana-py, whose client API has moved between releases. Every call is a
plain HTTP POST, retried on transient failure, and every response is
logged with its slot so runs are reproducible from the raw data files.

Public endpoints are used by default; override with --rpc-url.
No keys are needed for any read call. Sending needs a keypair file,
passed explicitly -- never committed to the repo.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

log = logging.getLogger("itb.rpc")

DEFAULT_DEVNET = "https://api.devnet.solana.com"
DEFAULT_MAINNET = "https://api.mainnet-beta.solana.com"
DEFAULT_LOCALNET = "http://127.0.0.1:8899"


class RpcError(Exception):
    """RPC returned an error object, or the call failed after retries."""


class Rpc:
    def __init__(self, url: str, timeout: int = 30, retries: int = 3):
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self._id = 0
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def call(self, method: str, params: list | None = None) -> Any:
        """One JSON-RPC call with exponential-backoff retries."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method,
                   "params": params or []}
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self.session.post(self.url, json=payload,
                                         timeout=self.timeout)
                resp.raise_for_status()
                body = resp.json()
                if "error" in body:
                    raise RpcError(f"{method}: {body['error']}")
                return body["result"]
            except (requests.RequestException, RpcError) as exc:
                last_exc = exc
                wait = 2 ** attempt
                log.warning("rpc %s attempt %d failed (%s); retry in %ds",
                            method, attempt + 1, exc, wait)
                time.sleep(wait)
        raise RpcError(f"{method} failed after {self.retries} attempts: {last_exc}")

    # ---- convenience wrappers (all read-only unless noted) ----
    def get_slot(self) -> int:
        return int(self.call("getSlot"))

    def get_epoch_info(self) -> dict:
        return self.call("getEpochInfo")

    def get_leader_schedule(self, epoch: int | None = None) -> dict:
        # Returns {validator_pubkey: [slot, ...]} for the epoch.
        params = [epoch] if epoch is not None else []
        return self.call("getLeaderSchedule", params)

    def get_cluster_nodes(self) -> list:
        # Each node: {pubkey, gossip, tpu, rpc, version, ...}
        return self.call("getClusterNodes")

    def get_vote_accounts(self) -> dict:
        return self.call("getVoteAccounts")

    def get_latest_blockhash(self) -> dict:
        return self.call("getLatestBlockhash")["value"]

    def get_block(self, slot: int) -> dict | None:
        """Full block, jsonParsed. Returns None for skipped/missing slots."""
        try:
            return self.call("getBlock", [slot, {
                "encoding": "jsonParsed",
                "transactionDetails": "full",
                "rewards": False,
                "maxSupportedTransactionVersion": 1,
            }])
        except RpcError as exc:
            log.warning("getBlock slot %d: %s", slot, exc)
            return None

    def get_fee_for_message(self, message_b64: str) -> int | None:
        res = self.call("getFeeForMessage", [message_b64])
        return res["value"] if res else None

    def simulate_transaction(self, tx_b64: str) -> dict:
        return self.call("simulateTransaction", [tx_b64, {
            "encoding": "base64",
            "sigVerify": True, "commitment": "processed",
        }])

    def get_signature_statuses(self, sigs: list[str]) -> list:
        res = self.call("getSignatureStatuses", [sigs, {"searchTransactionHistory": True}])
        return res["value"]

    def get_balance(self, pubkey: str) -> int:
        return int(self.call("getBalance", [pubkey])["value"])

    # ---- sending / funding (write calls; only used with --live) ----
    def request_airdrop(self, pubkey: str, lamports: int) -> str:
        return self.call("requestAirdrop", [pubkey, lamports])

    def send_transaction(self, tx_b64: str) -> str:
        return self.call("sendTransaction", [tx_b64, {
            "encoding": "base64",
            "skipPreflight": False,
            "preflightCommitment": "processed",
            "maxRetries": 3,
        }])

    def confirm(self, sig: str, timeout_s: int = 60) -> dict | None:
        """Poll signature status until confirmed/finalized or timeout."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            st = self.get_signature_statuses([sig])[0]
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return st
            if st and st.get("err"):
                return st
            time.sleep(2)
        return None
