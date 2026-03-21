"""
config.py — Centralised configuration loader.
All settings come from environment variables (via .env).
Nothing sensitive is ever hard-coded here.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (or parent dirs)
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)


def _require(key: str) -> str:
    """Raise a clear error if a required env var is missing."""
    val = os.getenv(key, "").strip()
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example → .env and fill it in."
        )
    return val


def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


# ── Bitcoin ──────────────────────────────────────────────────
BTC_MASTER_MNEMONIC: str = _require("BTC_MASTER_MNEMONIC")
BTC_DERIVATION_PATH: str = _optional("BTC_DERIVATION_PATH", "m/84'/0'/0'/0")
BTC_NETWORK: str = _optional("BTC_NETWORK", "testnet")  # "mainnet" | "testnet"
BTC_MIN_CONFIRMATIONS: int = _int("BTC_MIN_CONFIRMATIONS", 1)
BTC_POLL_INTERVAL: int = _int("BTC_POLL_INTERVAL", 30)
PAYMENT_EXPIRY_SECONDS: int = _int("PAYMENT_EXPIRY_SECONDS", 86400)

BLOCKSTREAM_API_URL: str = _optional(
    "BLOCKSTREAM_API_URL",
    "https://blockstream.info/testnet/api" if BTC_NETWORK == "testnet"
    else "https://blockstream.info/api",
)

# ── IPFS ─────────────────────────────────────────────────────
PINATA_JWT: str = _optional("PINATA_JWT", "")
IPFS_API_URL: str = _optional("IPFS_API_URL", "http://127.0.0.1:5001")

# ── Torrent ──────────────────────────────────────────────────
TORRENT_TRACKERS: list[str] = [
    t.strip()
    for t in _optional(
        "TORRENT_TRACKERS",
        "udp://tracker.opentrackr.org:1337/announce,"
        "udp://open.tracker.cl:1337/announce",
    ).split(",")
    if t.strip()
]

# ── Storage ──────────────────────────────────────────────────
STORAGE_DIR: Path = Path(_optional("STORAGE_DIR", "./storage")).resolve()
DATABASE_PATH: Path = Path(_optional("DATABASE_PATH", "./storage/market.db")).resolve()

# Ensure storage directory exists
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# ── Flask ─────────────────────────────────────────────────────
FLASK_SECRET_KEY: str = _optional("FLASK_SECRET_KEY", os.urandom(32).hex())
FLASK_HOST: str = _optional("FLASK_HOST", "127.0.0.1")
FLASK_PORT: int = _int("FLASK_PORT", 5000)

# ── Logging ──────────────────────────────────────────────────
LOG_LEVEL: str = _optional("LOG_LEVEL", "INFO").upper()
LOG_FILE: str = _optional("LOG_FILE", str(STORAGE_DIR / "market.log"))
