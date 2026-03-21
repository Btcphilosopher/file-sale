"""
database.py — SQLite persistence layer.

Tables:
  listings     — files offered for sale
  transactions — payment requests (one BTC address per sale)
  key_releases — audit log of every key delivery (replay protection)

Replay protection:
  A (file_id, btc_address) pair can only ever release a key ONCE.
  The key_releases table uses a UNIQUE constraint on tx_id to enforce this
  at the database level, independent of application logic.
"""

import sqlite3
import json
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from logger import get_logger

log = get_logger("btc_market.database")


# ── Schema ────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS listings (
    file_id             TEXT PRIMARY KEY,
    original_filename   TEXT NOT NULL,
    encrypted_path      TEXT NOT NULL,
    metadata_path       TEXT,
    price_satoshis      INTEGER NOT NULL,        -- asking price in satoshis
    torrent_magnet      TEXT,
    ipfs_cid            TEXT,
    original_sha256     TEXT NOT NULL,
    encrypted_sha256    TEXT NOT NULL,
    file_size_bytes     INTEGER NOT NULL,
    created_at          REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_id           TEXT PRIMARY KEY,            -- UUID we generate
    file_id         TEXT NOT NULL REFERENCES listings(file_id),
    btc_address     TEXT NOT NULL UNIQUE,        -- one address per sale
    price_satoshis  INTEGER NOT NULL,
    hd_index        INTEGER NOT NULL,            -- BIP-32 child key index
    status          TEXT NOT NULL DEFAULT 'pending',
        -- pending | confirmed | expired | key_released
    btxid           TEXT,                        -- blockchain txid once seen
    paid_satoshis   INTEGER,                     -- actual amount received
    confirmations   INTEGER DEFAULT 0,
    created_at      REAL NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    expires_at      REAL,                        -- NULL = never
    confirmed_at    REAL,
    released_at     REAL
);

CREATE TABLE IF NOT EXISTS key_releases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id       TEXT NOT NULL UNIQUE REFERENCES transactions(tx_id),
    file_id     TEXT NOT NULL,
    btc_address TEXT NOT NULL,
    key_b64     TEXT NOT NULL,                   -- the decryption key delivered
    released_at REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);

-- fast lookups
CREATE INDEX IF NOT EXISTS idx_tx_address  ON transactions(btc_address);
CREATE INDEX IF NOT EXISTS idx_tx_file     ON transactions(file_id);
CREATE INDEX IF NOT EXISTS idx_tx_status   ON transactions(status);
"""


# ── Connection helper ─────────────────────────────────────────

class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        log.info("Database ready at %s", self.db_path)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ── Listings ──────────────────────────────────────────────

    def add_listing(
        self,
        file_id: str,
        original_filename: str,
        encrypted_path: str,
        metadata_path: str | None,
        price_satoshis: int,
        original_sha256: str,
        encrypted_sha256: str,
        file_size_bytes: int,
        torrent_magnet: str | None = None,
        ipfs_cid: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO listings
                  (file_id, original_filename, encrypted_path, metadata_path,
                   price_satoshis, torrent_magnet, ipfs_cid,
                   original_sha256, encrypted_sha256, file_size_bytes)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    file_id, original_filename, encrypted_path, metadata_path,
                    price_satoshis, torrent_magnet, ipfs_cid,
                    original_sha256, encrypted_sha256, file_size_bytes,
                ),
            )
        log.debug("Listing added: %s", file_id[:16])

    def get_listing(self, file_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM listings WHERE file_id = ?", (file_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_listing_distribution(
        self, file_id: str, torrent_magnet: str | None, ipfs_cid: str | None
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE listings SET torrent_magnet=?, ipfs_cid=? WHERE file_id=?",
                (torrent_magnet, ipfs_cid, file_id),
            )

    def list_listings(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM listings ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Transactions ──────────────────────────────────────────

    def add_transaction(
        self,
        tx_id: str,
        file_id: str,
        btc_address: str,
        price_satoshis: int,
        hd_index: int,
        expires_at: float | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO transactions
                  (tx_id, file_id, btc_address, price_satoshis, hd_index, expires_at)
                VALUES (?,?,?,?,?,?)
                """,
                (tx_id, file_id, btc_address, price_satoshis, hd_index, expires_at),
            )
        log.debug("Transaction created: tx_id=%s addr=%s", tx_id, btc_address)

    def get_transaction(self, tx_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE tx_id = ?", (tx_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_transaction_by_address(self, btc_address: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE btc_address = ?", (btc_address,)
            ).fetchone()
        return dict(row) if row else None

    def get_pending_transactions(self) -> list[dict]:
        """Return all pending (non-expired) transactions."""
        now = time.time()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM transactions
                WHERE status = 'pending'
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at ASC
                """,
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_transaction_confirmed(
        self,
        tx_id: str,
        btxid: str,
        paid_satoshis: int,
        confirmations: int,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE transactions
                SET status='confirmed', btxid=?, paid_satoshis=?,
                    confirmations=?, confirmed_at=unixepoch('now','subsec')
                WHERE tx_id=?
                """,
                (btxid, paid_satoshis, confirmations, tx_id),
            )
        log.info(
            "Transaction confirmed: tx_id=%s btxid=%s confs=%d paid=%d sat",
            tx_id, btxid, confirmations, paid_satoshis,
        )

    def update_transaction_released(self, tx_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE transactions
                SET status='key_released', released_at=unixepoch('now','subsec')
                WHERE tx_id=?
                """,
                (tx_id,),
            )

    def expire_old_transactions(self) -> int:
        """Mark expired pending transactions. Returns count updated."""
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE transactions
                SET status='expired'
                WHERE status='pending'
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                """,
                (now,),
            )
        count = cur.rowcount
        if count:
            log.info("Expired %d stale transaction(s)", count)
        return count

    def get_next_hd_index(self) -> int:
        """Return the next unused BIP-32 child index."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(hd_index) AS m FROM transactions"
            ).fetchone()
        return (row["m"] + 1) if row["m"] is not None else 0

    # ── Key releases (replay protection) ─────────────────────

    def key_already_released(self, tx_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM key_releases WHERE tx_id=?", (tx_id,)
            ).fetchone()
        return row is not None

    def record_key_release(
        self, tx_id: str, file_id: str, btc_address: str, key_b64: str
    ) -> None:
        """
        Insert into key_releases with UNIQUE constraint on tx_id.
        Raises sqlite3.IntegrityError if tx_id was already used (replay attack).
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO key_releases (tx_id, file_id, btc_address, key_b64)
                VALUES (?,?,?,?)
                """,
                (tx_id, file_id, btc_address, key_b64),
            )
        log.info("Key release recorded for tx_id=%s", tx_id)
