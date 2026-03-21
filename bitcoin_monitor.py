"""
bitcoin_monitor.py — Bitcoin address generation and payment monitoring.

Architecture:
  • HD wallet (BIP-84 native SegWit / bech32) derived from the master mnemonic.
  • Each transaction gets a unique child address at index N — zero address reuse.
  • Blockstream Esplora REST API used for monitoring (no local node needed).
  • Polling loop checks all pending addresses every BTC_POLL_INTERVAL seconds.
  • Payment is accepted once >= BTC_MIN_CONFIRMATIONS confirmations are seen AND
    received_satoshis >= expected_satoshis (prevents underpayment attacks).

Security notes:
  • The mnemonic lives only in the environment; never printed or logged.
  • Only the *receiving* (public) address is stored in the DB — no private keys.
  • Amount validation prevents "send 1 sat and claim the key" attacks.
"""

import time
import uuid
import hashlib
import hmac
import struct
from typing import Callable

import requests

from logger import get_logger

log = get_logger("btc_market.bitcoin")

# ──────────────────────────────────────────────────────────────
# Lightweight BIP-32/BIP-84 address derivation
# (avoids heavy hdwallet dependency; pure Python + hashlib)
# ──────────────────────────────────────────────────────────────

try:
    from hdwallet import HDWallet
    
    _USE_HDWALLET = True
    log.debug("Using hdwallet library for key derivation")
except ImportError:
    _USE_HDWALLET = False
    log.debug("hdwallet not available — falling back to bit library")

try:
    from bit import PrivateKeyTestnet, PrivateKey
    _USE_BIT = True
except ImportError:
    _USE_BIT = False


class AddressDerivationError(Exception):
    pass


def _derive_address_hdwallet(mnemonic: str, index: int, testnet: bool) -> tuple[str, str]:
    """
    Derive a BIP-84 (native SegWit / bech32) address using hdwallet v3.
    Returns (address, wif_private_key).
    """
    from hdwallet.cryptocurrencies import Bitcoin
    from hdwallet.hds import BIP84HD
    from hdwallet.mnemonics import BIP39Mnemonic
    from hdwallet.derivations import BIP84Derivation

    network = "testnet" if testnet else "mainnet"
    wallet = HDWallet(cryptocurrency=Bitcoin, hd=BIP84HD, network=network)
    wallet.from_mnemonic(mnemonic=BIP39Mnemonic(mnemonic=mnemonic))

    deriv = BIP84Derivation(account=0, change="external-chain", address=index)
    wallet.from_derivation(derivation=deriv)

    addr = wallet.address()
    wif  = wallet.wif()
    if not addr:
        raise AddressDerivationError(f"hdwallet returned no address at index {index}")
    return addr, wif


def _derive_address_bit(mnemonic: str, index: int, testnet: bool) -> tuple[str, str]:
    """
    Simplified fallback using the `bit` library's PrivateKey from WIF.
    NOTE: This is a placeholder — in production, integrate proper BIP-32.
    """
    # Deterministic seed from mnemonic + index (not BIP-32 compliant but
    # deterministic and safe for demonstration purposes)
    seed = hmac.new(
        mnemonic.encode(),
        f"btc-file-market:{index}".encode(),
        hashlib.sha256,
    ).digest()
    if testnet:
        key = PrivateKeyTestnet(seed)
    else:
        key = PrivateKey(seed)
    return key.address, key.to_wif()


def derive_receiving_address(
    mnemonic: str, index: int, testnet: bool = True
) -> tuple[str, str]:
    """
    Derive a unique receiving address at HD child index.

    Returns:
        (bitcoin_address, wif_private_key)
        — WIF key stored only in memory; caller must keep it secret.
    """
    if _USE_HDWALLET:
        return _derive_address_hdwallet(mnemonic, index, testnet)
    elif _USE_BIT:
        return _derive_address_bit(mnemonic, index, testnet)
    else:
        raise AddressDerivationError(
            "Neither 'hdwallet' nor 'bit' library is available. "
            "Run: pip install hdwallet bit"
        )


# ──────────────────────────────────────────────────────────────
# Blockstream Esplora API helpers
# ──────────────────────────────────────────────────────────────

class BlockstreamAPI:
    """Thin wrapper around the Blockstream Esplora REST API."""

    def __init__(self, base_url: str, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "btc-file-market/1.0"})

    def _get(self, path: str) -> dict | list | str | None:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                return resp.text
        except requests.RequestException as exc:
            log.warning("Blockstream API error [%s]: %s", path, exc)
            return None

    def get_address_utxos(self, address: str) -> list[dict]:
        """Return list of UTXOs for an address."""
        result = self._get(f"/address/{address}/utxo")
        return result if isinstance(result, list) else []

    def get_address_txs(self, address: str) -> list[dict]:
        """Return confirmed + mempool transactions touching this address."""
        result = self._get(f"/address/{address}/txs")
        return result if isinstance(result, list) else []

    def get_tx(self, txid: str) -> dict | None:
        """Fetch a transaction by its txid."""
        result = self._get(f"/tx/{txid}")
        return result if isinstance(result, dict) else None

    def get_current_block_height(self) -> int | None:
        result = self._get("/blocks/tip/height")
        if isinstance(result, (int, str)):
            try:
                return int(result)
            except (ValueError, TypeError):
                pass
        return None

    def check_address_payment(
        self, address: str, expected_satoshis: int, min_confirmations: int
    ) -> tuple[bool, str | None, int, int]:
        """
        Check if the address has received at least *expected_satoshis* with
        at least *min_confirmations* confirmations.

        Returns:
            (is_paid, txid | None, received_satoshis, confirmations)
        """
        tip_height = self.get_current_block_height()
        if tip_height is None:
            log.warning("Could not fetch block tip height — skipping check")
            return False, None, 0, 0

        txs = self.get_address_txs(address)
        if not txs:
            return False, None, 0, 0

        best_txid = None
        best_received = 0
        best_confs = 0

        for tx in txs:
            # Sum all outputs that pay to our address
            received = sum(
                vout["value"]
                for vout in tx.get("vout", [])
                if vout.get("scriptpubkey_address") == address
            )
            if received == 0:
                continue

            tx_block = tx.get("status", {}).get("block_height")
            if tx_block:
                confs = tip_height - tx_block + 1
            else:
                confs = 0  # unconfirmed / mempool

            log.debug(
                "addr=%s txid=%s received=%d sat confs=%d",
                address, tx["txid"], received, confs,
            )

            if received > best_received:
                best_received = received
                best_txid = tx["txid"]
                best_confs = confs

        if best_received >= expected_satoshis and best_confs >= min_confirmations:
            return True, best_txid, best_received, best_confs

        return False, best_txid, best_received, best_confs


# ──────────────────────────────────────────────────────────────
# Payment request creation & monitoring
# ──────────────────────────────────────────────────────────────

class BitcoinPaymentMonitor:
    """
    High-level façade used by main.py and web_ui.py.

    Responsibilities:
      1. Create payment requests (derive fresh address, persist to DB).
      2. Poll pending requests and fire a callback when confirmed.
    """

    def __init__(self, db, config_module):
        self.db = db
        self.cfg = config_module
        self.api = BlockstreamAPI(config_module.BLOCKSTREAM_API_URL)

    # ── Address generation ────────────────────────────────────

    def create_payment_request(
        self, file_id: str, price_satoshis: int
    ) -> dict:
        """
        Create a new payment request for a file listing.

        Returns a dict with:
          tx_id, btc_address, price_satoshis, expires_at, payment_uri
        """
        import time as _time

        # Derive the next fresh address
        index = self.db.get_next_hd_index()
        testnet = self.cfg.BTC_NETWORK == "testnet"
        address, _wif = derive_receiving_address(
            self.cfg.BTC_MASTER_MNEMONIC, index, testnet
        )
        # _wif is deliberately discarded — we only need the address for monitoring.
        # (In a real sweep scenario you'd store the encrypted WIF to later sweep funds.)

        tx_id = str(uuid.uuid4())
        expiry = self.cfg.PAYMENT_EXPIRY_SECONDS
        expires_at = _time.time() + expiry if expiry > 0 else None

        self.db.add_transaction(
            tx_id=tx_id,
            file_id=file_id,
            btc_address=address,
            price_satoshis=price_satoshis,
            hd_index=index,
            expires_at=expires_at,
        )

        price_btc = price_satoshis / 1e8
        payment_uri = f"bitcoin:{address}?amount={price_btc:.8f}&label=btc-file-market"

        log.info(
            "Payment request created: tx_id=%s addr=%s amount=%.8f BTC",
            tx_id, address, price_btc,
        )

        return {
            "tx_id": tx_id,
            "btc_address": address,
            "price_satoshis": price_satoshis,
            "price_btc": price_btc,
            "expires_at": expires_at,
            "payment_uri": payment_uri,
            "hd_index": index,
        }

    # ── Monitoring ────────────────────────────────────────────

    def check_single(self, tx_id: str) -> dict:
        """
        Manually check one transaction by tx_id.
        Returns current status dict.
        """
        tx = self.db.get_transaction(tx_id)
        if not tx:
            return {"error": f"Transaction {tx_id} not found"}

        paid, btxid, received, confs = self.api.check_address_payment(
            tx["btc_address"],
            tx["price_satoshis"],
            self.cfg.BTC_MIN_CONFIRMATIONS,
        )

        if paid and tx["status"] == "pending":
            self.db.update_transaction_confirmed(tx_id, btxid, received, confs)
            tx = self.db.get_transaction(tx_id)

        return dict(tx)

    def monitor_loop(
        self,
        on_confirmed: Callable[[dict], None],
        stop_event=None,
    ) -> None:
        """
        Blocking polling loop.

        Args:
            on_confirmed: Called with the transaction dict when payment confirmed.
            stop_event:   threading.Event — set it to stop the loop gracefully.
        """
        import threading
        if stop_event is None:
            stop_event = threading.Event()

        log.info(
            "Starting payment monitor (poll every %ds, min %d confs)",
            self.cfg.BTC_POLL_INTERVAL,
            self.cfg.BTC_MIN_CONFIRMATIONS,
        )

        while not stop_event.is_set():
            try:
                self.db.expire_old_transactions()
                pending = self.db.get_pending_transactions()

                if pending:
                    log.debug("Checking %d pending transaction(s)…", len(pending))

                for tx in pending:
                    paid, btxid, received, confs = self.api.check_address_payment(
                        tx["btc_address"],
                        tx["price_satoshis"],
                        self.cfg.BTC_MIN_CONFIRMATIONS,
                    )
                    if paid:
                        self.db.update_transaction_confirmed(
                            tx["tx_id"], btxid, received, confs
                        )
                        updated_tx = self.db.get_transaction(tx["tx_id"])
                        log.info(
                            "✓ Payment confirmed: tx_id=%s addr=%s",
                            tx["tx_id"], tx["btc_address"],
                        )
                        on_confirmed(updated_tx)
                    else:
                        log.debug(
                            "Waiting: addr=%s received=%d/%d sat confs=%d/%d",
                            tx["btc_address"],
                            received, tx["price_satoshis"],
                            confs, self.cfg.BTC_MIN_CONFIRMATIONS,
                        )

            except Exception as exc:
                log.error("Monitor loop error: %s", exc, exc_info=True)

            stop_event.wait(timeout=self.cfg.BTC_POLL_INTERVAL)

        log.info("Payment monitor stopped.")
