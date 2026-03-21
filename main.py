#!/usr/bin/env python3
"""
main.py — CLI entry point for btc-file-market.

Commands:
  encrypt-file       Encrypt a file and create a listing
  generate-payment   Create a BTC payment request for a listing
  monitor-payment    Poll the blockchain until payment confirmed
  release-key        Release decryption key after payment confirmed
  decrypt-file       Decrypt a purchased file (buyer side)
  list               Show all listings
  web-ui             Start the local Flask dashboard
  info               Print environment / config summary

Usage examples:
  python main.py encrypt-file --file ./secret.pdf --price 0.001 --method torrent
  python main.py generate-payment --file-id <file_id>
  python main.py monitor-payment --tx-id <tx_id>
  python main.py release-key --tx-id <tx_id>
  python main.py web-ui
"""

import sys
import os
import time
import threading
import json
from pathlib import Path

import click

# ── Bootstrap ─────────────────────────────────────────────────
# Ensure .env is loaded before any config import
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

from logger import get_logger

log = get_logger("btc_market.cli")


# ── Lazy config loader (fails gracefully if .env is missing) ──

def _load_config():
    try:
        import config
        return config
    except EnvironmentError as exc:
        click.echo(click.style(f"\n⚠ Configuration error:\n  {exc}\n", fg="red"), err=True)
        click.echo("  Run:  cp .env.example .env  and fill in your settings.", err=True)
        sys.exit(1)


def _get_db():
    cfg = _load_config()
    from database import Database
    return Database(cfg.DATABASE_PATH), cfg


def _get_monitor(db, cfg):
    from bitcoin_monitor import BitcoinPaymentMonitor
    return BitcoinPaymentMonitor(db, cfg)


# ──────────────────────────────────────────────────────────────
# CLI group
# ──────────────────────────────────────────────────────────────

@click.group()
@click.version_option("1.0.0", prog_name="btc-file-market")
def cli():
    """₿ btc-file-market — sell files for Bitcoin via BitTorrent/IPFS."""
    pass


# ──────────────────────────────────────────────────────────────
# encrypt-file
# ──────────────────────────────────────────────────────────────

@cli.command("encrypt-file")
@click.option("--file",      "-f", required=True,  help="Path to the plaintext file to sell.")
@click.option("--price",     "-p", required=True,  type=float, help="Sale price in BTC (e.g. 0.001).")
@click.option("--method",    "-m", default="torrent",
              type=click.Choice(["torrent", "ipfs", "both", "none"]),
              help="Distribution method.", show_default=True)
@click.option("--output-dir","-o", default=None,   help="Override storage directory.")
def encrypt_file(file, price, method, output_dir):
    """
    Encrypt a file with AES-256-GCM, create a listing, and distribute via
    BitTorrent or IPFS.

    \b
    The decryption key is saved to <output_dir>/<file>.enc.key
    Metadata (no key) is saved to <output_dir>/<file_id>.meta.json
    """
    cfg = _load_config()
    db, _ = _get_db()

    from encryption import encrypt_file as _encrypt, write_metadata_file
    from torrent_ipfs import distribute_file

    storage = Path(output_dir or cfg.STORAGE_DIR)
    price_satoshis = int(round(price * 1e8))

    click.echo(click.style(f"\n🔐 Encrypting '{file}'…", fg="cyan", bold=True))

    # 1. Encrypt
    result = _encrypt(file, storage)

    # 2. Save decryption key to a .key sidecar file (server-side secret)
    key_path = Path(result.encrypted_path).with_suffix(".key")
    key_path.write_text(result.key_b64)
    key_path.chmod(0o600)

    # 3. Write public metadata file
    meta_path = write_metadata_file(result, storage)

    click.echo(click.style("  ✓ Encrypted: ", fg="green") + result.encrypted_path)
    click.echo(click.style("  ✓ Key file:  ", fg="yellow") + str(key_path) + " (KEEP SECRET)")
    click.echo(click.style("  ✓ Metadata:  ", fg="blue") + str(meta_path))

    # 4. Distribute (torrent / IPFS)
    magnet_uri = None
    ipfs_cid = None

    if method != "none":
        click.echo(f"\n🌐 Distributing via {method}…")
        try:
            dist = distribute_file(
                encrypted_file_path=result.encrypted_path,
                output_dir=storage,
                method=method,
                trackers=cfg.TORRENT_TRACKERS,
                pinata_jwt=cfg.PINATA_JWT,
                ipfs_api_url=cfg.IPFS_API_URL,
            )
            magnet_uri = dist.get("magnet_uri")
            ipfs_cid   = dist.get("ipfs_cid")

            if magnet_uri:
                click.echo(click.style("  🧲 Magnet URI:\n  ", fg="green") + magnet_uri)
            if ipfs_cid:
                click.echo(click.style("  🌐 IPFS CID: ", fg="blue") + ipfs_cid)
                click.echo(f"     Gateway:   https://ipfs.io/ipfs/{ipfs_cid}")
        except Exception as exc:
            click.echo(click.style(f"  ⚠ Distribution warning: {exc}", fg="yellow"), err=True)

    # 5. Save listing to DB
    db.add_listing(
        file_id=result.file_id,
        original_filename=result.original_filename,
        encrypted_path=result.encrypted_path,
        metadata_path=str(meta_path),
        price_satoshis=price_satoshis,
        original_sha256=result.original_sha256,
        encrypted_sha256=result.encrypted_sha256,
        file_size_bytes=result.file_size_bytes,
        torrent_magnet=magnet_uri,
        ipfs_cid=ipfs_cid,
    )

    click.echo(f"\n{'─'*60}")
    click.echo(click.style("✅ Listing created successfully!", fg="green", bold=True))
    click.echo(f"   File ID:  {result.file_id}")
    click.echo(f"   Price:    {price:.8f} BTC ({price_satoshis:,} satoshis)")
    click.echo(f"\nNext: python main.py generate-payment --file-id {result.file_id}")


# ──────────────────────────────────────────────────────────────
# generate-payment
# ──────────────────────────────────────────────────────────────

@cli.command("generate-payment")
@click.option("--file-id", "-f", required=True, help="Listing file_id (from encrypt-file).")
@click.option("--qr",           is_flag=True, default=True, help="Generate QR code image.", show_default=True)
def generate_payment(file_id, qr):
    """
    Create a unique Bitcoin payment address for a listing.
    Outputs address, amount, QR code, and payment URI.
    """
    cfg = _load_config()
    db, _ = _get_db()

    listing = db.get_listing(file_id)
    if not listing:
        click.echo(click.style(f"Error: Listing '{file_id[:16]}…' not found.", fg="red"), err=True)
        sys.exit(1)

    monitor = _get_monitor(db, cfg)
    payment = monitor.create_payment_request(file_id, listing["price_satoshis"])

    price_btc = payment["price_btc"]
    address   = payment["btc_address"]
    tx_id     = payment["tx_id"]
    uri       = payment["payment_uri"]

    click.echo(f"\n{'─'*60}")
    click.echo(click.style("💳 Payment Request Created", fg="cyan", bold=True))
    click.echo(f"{'─'*60}")
    click.echo(f"  File:      {listing['original_filename']}")
    click.echo(f"  Amount:    {price_btc:.8f} BTC ({listing['price_satoshis']:,} sat)")
    click.echo(f"  Address:   {address}")
    click.echo(f"  TX ID:     {tx_id}")
    if payment.get("expires_at"):
        exp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(payment["expires_at"]))
        click.echo(f"  Expires:   {exp}")
    click.echo(f"\n  URI:  {uri}")

    if qr:
        from qr_utils import generate_qr_file
        qr_path = Path(cfg.STORAGE_DIR) / f"payment_{tx_id[:12]}.qr.png"
        generate_qr_file(uri, qr_path)
        click.echo(click.style(f"\n  📱 QR Code:  {qr_path}", fg="yellow"))

    click.echo(f"\n{'─'*60}")
    click.echo(f"Next: python main.py monitor-payment --tx-id {tx_id}")


# ──────────────────────────────────────────────────────────────
# monitor-payment
# ──────────────────────────────────────────────────────────────

@cli.command("monitor-payment")
@click.option("--tx-id",  "-t", required=True,  help="Transaction ID from generate-payment.")
@click.option("--once",         is_flag=True,   help="Check once and exit (no loop).")
@click.option("--auto-release", is_flag=True, default=True,
              help="Automatically release key on confirmation.", show_default=True)
def monitor_payment(tx_id, once, auto_release):
    """
    Poll the blockchain and wait for a payment to confirm.
    Optionally auto-releases the decryption key on confirmation.
    """
    cfg = _load_config()
    db, _ = _get_db()

    tx = db.get_transaction(tx_id)
    if not tx:
        click.echo(click.style(f"Error: Transaction '{tx_id}' not found.", fg="red"), err=True)
        sys.exit(1)

    monitor = _get_monitor(db, cfg)

    if once:
        status = monitor.check_single(tx_id)
        _print_tx_status(status)
        if auto_release and status.get("status") == "confirmed":
            _do_release_key(db, tx_id)
        return

    click.echo(click.style(
        f"\n📡 Monitoring payment for tx_id={tx_id[:16]}…\n"
        f"   Address: {tx['btc_address']}\n"
        f"   Waiting for {cfg.BTC_MIN_CONFIRMATIONS} confirmation(s)…\n"
        f"   (Ctrl+C to stop)\n",
        fg="cyan"
    ))

    stop = threading.Event()

    def on_confirmed(confirmed_tx: dict):
        click.echo(click.style(
            f"\n✅ PAYMENT CONFIRMED!\n"
            f"   btxid: {confirmed_tx.get('btxid')}\n"
            f"   Confirmations: {confirmed_tx.get('confirmations')}\n"
            f"   Received: {confirmed_tx.get('paid_satoshis', 0):,} sat",
            fg="green", bold=True,
        ))
        if auto_release:
            _do_release_key(db, confirmed_tx["tx_id"])
        stop.set()

    try:
        monitor.monitor_loop(on_confirmed=on_confirmed, stop_event=stop)
    except KeyboardInterrupt:
        stop.set()
        click.echo("\n⛔ Monitoring stopped by user.")


def _print_tx_status(tx: dict):
    status = tx.get("status", "unknown")
    colors = {"pending": "yellow", "confirmed": "green",
              "key_released": "blue", "expired": "red"}
    color = colors.get(status, "white")
    click.echo(f"\n  Status:        {click.style(status.upper(), fg=color, bold=True)}")
    click.echo(f"  Address:       {tx.get('btc_address', 'N/A')}")
    click.echo(f"  Expected:      {tx.get('price_satoshis', 0):,} sat")
    if tx.get("paid_satoshis"):
        click.echo(f"  Received:      {tx['paid_satoshis']:,} sat")
    if tx.get("confirmations"):
        click.echo(f"  Confirmations: {tx['confirmations']}")
    if tx.get("btxid"):
        click.echo(f"  Block TX:      {tx['btxid']}")


# ──────────────────────────────────────────────────────────────
# release-key
# ──────────────────────────────────────────────────────────────

@cli.command("release-key")
@click.option("--tx-id", "-t", required=True, help="Transaction ID to release key for.")
def release_key_cmd(tx_id):
    """
    Release the decryption key for a confirmed payment.
    Enforces replay protection — each tx_id can only release a key once.
    """
    db, _ = _get_db()
    _do_release_key(db, tx_id)


def _do_release_key(db, tx_id: str):
    """Internal helper: validates payment status, records release, prints key."""
    import sqlite3 as _sqlite3

    tx = db.get_transaction(tx_id)
    if not tx:
        click.echo(click.style(f"Error: Transaction '{tx_id}' not found.", fg="red"), err=True)
        return

    if tx["status"] not in ("confirmed",):
        # Check if already released
        if tx["status"] == "key_released":
            click.echo(click.style("ℹ Key was already released for this transaction.", fg="blue"))
            return
        click.echo(click.style(
            f"Cannot release key: transaction status is '{tx['status']}' (need 'confirmed').",
            fg="yellow"
        ), err=True)
        return

    # Replay protection check
    if db.key_already_released(tx_id):
        click.echo(click.style(
            "🛡  Replay protection: key already released for this tx_id.", fg="blue"
        ))
        return

    # Retrieve the key from sidecar .key file
    listing = db.get_listing(tx["file_id"])
    if not listing:
        click.echo(click.style("Error: Listing not found.", fg="red"), err=True)
        return

    key_file = Path(listing["encrypted_path"]).with_suffix(".key")
    if not key_file.exists():
        click.echo(click.style(f"Error: Key file not found: {key_file}", fg="red"), err=True)
        return

    key_b64 = key_file.read_text().strip()

    # Record release (DB-level replay prevention)
    try:
        db.record_key_release(tx_id, tx["file_id"], tx["btc_address"], key_b64)
    except _sqlite3.IntegrityError:
        click.echo(click.style(
            "🛡  Replay protection triggered: duplicate release attempt blocked.", fg="red"
        ), err=True)
        return

    db.update_transaction_released(tx_id)

    click.echo(f"\n{'═'*60}")
    click.echo(click.style("🔓 DECRYPTION KEY RELEASED", fg="green", bold=True))
    click.echo(f"{'═'*60}")
    click.echo(f"  TX ID:       {tx_id}")
    click.echo(f"  File:        {listing['original_filename']}")
    click.echo(f"\n  AES-256-GCM Key (base64):\n")
    click.echo(click.style(f"  {key_b64}", fg="bright_yellow", bold=True))
    click.echo(f"\n  Decrypt with:")
    click.echo(f"  python main.py decrypt-file --enc-file <path.enc> --key {key_b64}")
    click.echo(f"{'═'*60}\n")


# ──────────────────────────────────────────────────────────────
# decrypt-file (buyer side)
# ──────────────────────────────────────────────────────────────

@cli.command("decrypt-file")
@click.option("--enc-file", "-e", required=True,  help="Path to the .enc encrypted file.")
@click.option("--key",      "-k", required=True,  help="Base64 AES-256 decryption key.")
@click.option("--output",   "-o", default=None,   help="Output directory (default: same as input).")
def decrypt_file_cmd(enc_file, key, output):
    """
    Decrypt a purchased file. Buyer-side command.

    \b
    Example:
      python main.py decrypt-file --enc-file ./secret.pdf.enc --key <key_b64>
    """
    from encryption import decrypt_file as _decrypt

    enc_path = Path(enc_file).resolve()
    out_dir  = Path(output or enc_path.parent).resolve()

    click.echo(f"\n🔓 Decrypting '{enc_path.name}'…")

    try:
        out_path = _decrypt(enc_path, key, out_dir)
        click.echo(click.style(f"  ✅ Decrypted → {out_path}", fg="green", bold=True))
    except Exception as exc:
        click.echo(click.style(f"  ❌ Decryption failed: {exc}", fg="red"), err=True)
        click.echo("     Possible causes: wrong key, tampered file, or corrupted download.")
        sys.exit(1)


# ──────────────────────────────────────────────────────────────
# list
# ──────────────────────────────────────────────────────────────

@cli.command("list")
@click.option("--json-output", is_flag=True, help="Output raw JSON.")
def list_listings(json_output):
    """List all file listings in the market."""
    db, _ = _get_db()
    listings = db.list_listings()

    if json_output:
        click.echo(json.dumps(listings, indent=2))
        return

    if not listings:
        click.echo("No listings found.")
        return

    click.echo(f"\n{'─'*80}")
    click.echo(f"  {'FILE':<30} {'PRICE (BTC)':<14} {'SIZE':<12} {'DISTRIBUTION'}")
    click.echo(f"{'─'*80}")
    for l in listings:
        price = l["price_satoshis"] / 1e8
        size  = f"{l['file_size_bytes'] / 1024:.1f} KB"
        dist  = []
        if l.get("torrent_magnet"): dist.append("🧲 torrent")
        if l.get("ipfs_cid"):       dist.append("🌐 IPFS")
        dist_str = ", ".join(dist) or "—"
        click.echo(f"  {l['original_filename'][:29]:<30} {price:<14.8f} {size:<12} {dist_str}")
        click.echo(f"  {click.style('ID: ' + l['file_id'][:32] + '…', fg='bright_black')}")
        click.echo()


# ──────────────────────────────────────────────────────────────
# web-ui
# ──────────────────────────────────────────────────────────────

@cli.command("web-ui")
def web_ui_cmd():
    """Start the local Flask web dashboard."""
    cfg = _load_config()
    db, _ = _get_db()
    monitor = _get_monitor(db, cfg)

    from web_ui import run_web_ui
    run_web_ui(db, monitor, cfg)


# ──────────────────────────────────────────────────────────────
# info
# ──────────────────────────────────────────────────────────────

@cli.command("info")
def info_cmd():
    """Print current configuration summary (no secrets)."""
    cfg = _load_config()
    db, _ = _get_db()

    listings  = db.list_listings()
    with db._conn() as conn:
        tx_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    click.echo(f"\n{'─'*50}")
    click.echo(click.style("  btc-file-market — Config Summary", bold=True))
    click.echo(f"{'─'*50}")
    click.echo(f"  Network:         {cfg.BTC_NETWORK}")
    click.echo(f"  API URL:         {cfg.BLOCKSTREAM_API_URL}")
    click.echo(f"  Min confirms:    {cfg.BTC_MIN_CONFIRMATIONS}")
    click.echo(f"  Poll interval:   {cfg.BTC_POLL_INTERVAL}s")
    click.echo(f"  Payment expiry:  {cfg.PAYMENT_EXPIRY_SECONDS}s")
    click.echo(f"  Storage dir:     {cfg.STORAGE_DIR}")
    click.echo(f"  Database:        {cfg.DATABASE_PATH}")
    click.echo(f"  Listings:        {len(listings)}")
    click.echo(f"  Transactions:    {tx_count}")
    click.echo(f"  Mnemonic set:    {'✓' if cfg.BTC_MASTER_MNEMONIC else '✗'}")
    click.echo(f"  Pinata JWT set:  {'✓' if cfg.PINATA_JWT else '—'}")
    click.echo(f"  IPFS daemon:     {cfg.IPFS_API_URL}")
    click.echo(f"{'─'*50}\n")


# ──────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
