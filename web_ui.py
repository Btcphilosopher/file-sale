"""
web_ui.py — Optional Flask local dashboard for btc-file-market.

Routes:
  GET  /                    — Dashboard: all listings + transactions
  GET  /listing/<file_id>   — Listing detail + buy page
  POST /listing/<file_id>/buy — Create payment request, show QR
  GET  /tx/<tx_id>          — Transaction status (JSON + UI)
  GET  /tx/<tx_id>/status   — JSON status endpoint (for polling)
  POST /tx/<tx_id>/release  — Manually trigger key release
  GET  /api/listings        — JSON list of all listings

Security:
  • Runs on 127.0.0.1 only by default (never exposed externally).
  • Flask secret key comes from environment — not hard-coded.
  • All key release actions go through the replay-protected DB layer.
"""

import threading
import time
import json
from pathlib import Path

from flask import (
    Flask, render_template_string, jsonify, redirect,
    url_for, request, abort, flash
)

from logger import get_logger

log = get_logger("btc_market.web")


# ──────────────────────────────────────────────────────────────
# HTML Templates (inline — no template files needed)
# ──────────────────────────────────────────────────────────────

_BASE_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d0d0d; --surface: #1a1a1a; --border: #2a2a2a;
    --text: #e8e8e8; --muted: #888; --accent: #f7931a;
    --green: #27ae60; --red: #c0392b; --blue: #2980b9;
    --font: 'JetBrains Mono', 'Courier New', monospace;
  }
  body { background: var(--bg); color: var(--text); font-family: var(--font);
         font-size: 14px; line-height: 1.6; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .nav { background: var(--surface); border-bottom: 1px solid var(--border);
         padding: 12px 24px; display: flex; align-items: center; gap: 20px; }
  .nav .logo { color: var(--accent); font-size: 18px; font-weight: bold; }
  .container { max-width: 1000px; margin: 32px auto; padding: 0 24px; }
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: 8px; padding: 20px; margin-bottom: 20px; }
  .card h2 { color: var(--accent); margin-bottom: 16px; font-size: 16px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 12px; font-weight: bold; text-transform: uppercase; }
  .badge-pending  { background: #333; color: #f7931a; }
  .badge-confirmed { background: #1a3a1a; color: #27ae60; }
  .badge-released { background: #1a2a3a; color: #2980b9; }
  .badge-expired  { background: #2a1a1a; color: #c0392b; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-size: 12px; text-transform: uppercase; }
  .btn { display: inline-block; padding: 8px 18px; border-radius: 5px; border: none;
         cursor: pointer; font-family: var(--font); font-size: 13px; font-weight: bold; }
  .btn-orange { background: var(--accent); color: #000; }
  .btn-green  { background: var(--green); color: #fff; }
  .btn-blue   { background: var(--blue); color: #fff; }
  .mono { font-family: var(--font); word-break: break-all; font-size: 12px; }
  .qr-wrapper { text-align: center; padding: 20px; }
  .qr-wrapper img { border: 6px solid white; border-radius: 4px; max-width: 220px; }
  .status-box { border: 1px solid var(--border); border-radius: 6px;
                padding: 14px; margin-top: 14px; }
  .flash { padding: 12px 18px; margin-bottom: 16px; border-radius: 6px;
           background: #1a3a1a; border: 1px solid #27ae60; color: #27ae60; }
"""

DASHBOARD_TMPL = """
<!DOCTYPE html><html><head><meta charset="utf-8">
<title>BTC File Market</title>
<style>""" + _BASE_CSS + """</style></head><body>
<nav class="nav">
  <span class="logo">₿ BTC File Market</span>
  <a href="/">Dashboard</a>
</nav>
<div class="container">
  <div class="card">
    <h2>📦 Listings</h2>
    {% if listings %}
    <table>
      <tr><th>File</th><th>Price (BTC)</th><th>Size</th><th>Distribution</th><th></th></tr>
      {% for l in listings %}
      <tr>
        <td class="mono">{{ l.original_filename }}</td>
        <td>{{ "%.8f"|format(l.price_satoshis / 1e8) }}</td>
        <td>{{ "%.1f"|format(l.file_size_bytes / 1024) }} KB</td>
        <td>
          {% if l.torrent_magnet %}<span class="badge badge-confirmed">🧲 Torrent</span>{% endif %}
          {% if l.ipfs_cid %}<span class="badge badge-released">🌐 IPFS</span>{% endif %}
        </td>
        <td><a href="/listing/{{ l.file_id }}" class="btn btn-orange">Buy</a></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}<p style="color:var(--muted)">No listings yet. Use the CLI to add files.</p>{% endif %}
  </div>

  <div class="card">
    <h2>💳 Recent Transactions</h2>
    {% if transactions %}
    <table>
      <tr><th>Tx ID</th><th>Address</th><th>Amount</th><th>Status</th><th></th></tr>
      {% for tx in transactions %}
      <tr>
        <td class="mono">{{ tx.tx_id[:12] }}…</td>
        <td class="mono">{{ tx.btc_address[:18] }}…</td>
        <td>{{ "%.8f"|format(tx.price_satoshis / 1e8) }} BTC</td>
        <td><span class="badge badge-{{ tx.status }}">{{ tx.status }}</span></td>
        <td><a href="/tx/{{ tx.tx_id }}">Details</a></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}<p style="color:var(--muted)">No transactions yet.</p>{% endif %}
  </div>
</div></body></html>
"""

LISTING_TMPL = """
<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{{ listing.original_filename }} — BTC File Market</title>
<style>""" + _BASE_CSS + """</style></head><body>
<nav class="nav">
  <span class="logo">₿ BTC File Market</span>
  <a href="/">Dashboard</a>
</nav>
<div class="container">
  {% for msg in get_flashed_messages() %}
  <div class="flash">{{ msg }}</div>
  {% endfor %}

  <div class="card">
    <h2>📄 {{ listing.original_filename }}</h2>
    <table>
      <tr><td style="color:var(--muted)">Price</td>
          <td><strong style="color:var(--accent)">{{ "%.8f"|format(listing.price_satoshis / 1e8) }} BTC</strong></td></tr>
      <tr><td style="color:var(--muted)">File Size</td>
          <td>{{ "%.2f"|format(listing.file_size_bytes / 1024) }} KB</td></tr>
      <tr><td style="color:var(--muted)">SHA-256 (plaintext)</td>
          <td class="mono" style="font-size:11px">{{ listing.original_sha256 }}</td></tr>
      {% if listing.torrent_magnet %}
      <tr><td style="color:var(--muted)">Magnet</td>
          <td class="mono" style="font-size:11px">{{ listing.torrent_magnet[:80] }}…</td></tr>
      {% endif %}
      {% if listing.ipfs_cid %}
      <tr><td style="color:var(--muted)">IPFS CID</td>
          <td class="mono">{{ listing.ipfs_cid }}</td></tr>
      {% endif %}
    </table>
    <br>
    <form method="POST" action="/listing/{{ listing.file_id }}/buy">
      <button type="submit" class="btn btn-orange">🛒 Generate Payment Request</button>
    </form>
  </div>

  {% if payment %}
  <div class="card">
    <h2>💳 Payment Request</h2>
    <div style="display:flex; gap:32px; align-items:flex-start;">
      <div class="qr-wrapper">
        <img src="data:image/png;base64,{{ qr_b64 }}" alt="Payment QR" />
        <p style="margin-top:8px; color:var(--muted); font-size:11px">Scan with any Bitcoin wallet</p>
      </div>
      <div style="flex:1">
        <p>Send exactly:</p>
        <p style="font-size:24px; color:var(--accent); margin:8px 0">
          {{ "%.8f"|format(payment.price_btc) }} BTC
        </p>
        <p style="color:var(--muted); margin-bottom:12px">to address:</p>
        <p class="mono" style="background:#111; padding:10px; border-radius:4px; word-break:break-all">
          {{ payment.btc_address }}
        </p>
        {% if payment.expires_at %}
        <p style="color:var(--muted); margin-top:10px; font-size:12px">
          ⏰ Expires: {{ payment.expires_at | int }}
        </p>
        {% endif %}
        <br>
        <a href="/tx/{{ payment.tx_id }}" class="btn btn-blue">📡 Monitor Payment Status</a>
      </div>
    </div>
  </div>
  {% endif %}
</div></body></html>
"""

TX_TMPL = """
<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Transaction {{ tx.tx_id[:12] }}… — BTC File Market</title>
<style>""" + _BASE_CSS + """</style>
<script>
  // Auto-refresh status every 15 seconds if pending
  var status = "{{ tx.status }}";
  if (status === "pending" || status === "confirmed") {
    setTimeout(function(){ window.location.reload(); }, 15000);
  }
</script>
</head><body>
<nav class="nav">
  <span class="logo">₿ BTC File Market</span>
  <a href="/">Dashboard</a>
</nav>
<div class="container">
  <div class="card">
    <h2>💳 Transaction Detail</h2>
    <span class="badge badge-{{ tx.status }}" style="font-size:14px; margin-bottom:16px; display:inline-block">
      {{ tx.status }}
    </span>

    <table style="margin-top:12px">
      <tr><td style="color:var(--muted)">Tx ID</td><td class="mono">{{ tx.tx_id }}</td></tr>
      <tr><td style="color:var(--muted)">BTC Address</td><td class="mono">{{ tx.btc_address }}</td></tr>
      <tr><td style="color:var(--muted)">Expected</td>
          <td><strong style="color:var(--accent)">{{ "%.8f"|format(tx.price_satoshis / 1e8) }} BTC</strong></td></tr>
      {% if tx.paid_satoshis %}
      <tr><td style="color:var(--muted)">Received</td>
          <td style="color:var(--green)">{{ "%.8f"|format(tx.paid_satoshis / 1e8) }} BTC</td></tr>
      <tr><td style="color:var(--muted)">Confirmations</td>
          <td>{{ tx.confirmations }}</td></tr>
      {% endif %}
      {% if tx.btxid %}
      <tr><td style="color:var(--muted)">Blockchain TX</td>
          <td class="mono"><a href="https://blockstream.info/testnet/tx/{{ tx.btxid }}" target="_blank">{{ tx.btxid }}</a></td></tr>
      {% endif %}
    </table>

    {% if tx.status == "confirmed" %}
    <div class="status-box" style="border-color: var(--accent); margin-top:20px">
      <p style="color:var(--accent); margin-bottom:12px">⚡ Payment confirmed! Click below to release the decryption key.</p>
      <form method="POST" action="/tx/{{ tx.tx_id }}/release">
        <button type="submit" class="btn btn-green">🔓 Release Decryption Key</button>
      </form>
    </div>
    {% endif %}

    {% if tx.status == "key_released" and key %}
    <div class="status-box" style="border-color: var(--green); margin-top:20px">
      <p style="color:var(--green); margin-bottom:8px">✅ Key Released — save this immediately!</p>
      <p class="mono" style="background:#0a1a0a; padding:14px; border-radius:6px; word-break:break-all; font-size:13px; color:#7fff7f">
        {{ key }}
      </p>
    </div>
    {% elif tx.status == "pending" %}
    <div class="status-box" style="margin-top:20px">
      <p style="color:var(--muted)">⏳ Waiting for payment… (auto-refreshes every 15s)</p>
    </div>
    {% endif %}
  </div>
</div></body></html>
"""


# ──────────────────────────────────────────────────────────────
# Flask application factory
# ──────────────────────────────────────────────────────────────

def create_app(db, bitcoin_monitor, config_module, key_store: dict) -> Flask:
    """
    Create and configure the Flask application.

    Args:
        db:              Database instance.
        bitcoin_monitor: BitcoinPaymentMonitor instance.
        config_module:   Loaded config module.
        key_store:       In-memory dict mapping tx_id → key_b64.
                         Populated by the key release logic in main.py.
                         Keys are NOT persisted in the web process.
    """
    app = Flask(__name__)
    app.secret_key = config_module.FLASK_SECRET_KEY

    # ── Routes ─────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        listings = db.list_listings()
        with db._conn() as conn:
            txs = conn.execute(
                "SELECT * FROM transactions ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        transactions = [dict(r) for r in txs]
        return render_template_string(DASHBOARD_TMPL,
                                      listings=listings,
                                      transactions=transactions)

    @app.route("/listing/<file_id>")
    def listing_detail(file_id):
        listing = db.get_listing(file_id)
        if not listing:
            abort(404)
        return render_template_string(LISTING_TMPL, listing=listing, payment=None, qr_b64=None)

    @app.route("/listing/<file_id>/buy", methods=["POST"])
    def create_payment(file_id):
        listing = db.get_listing(file_id)
        if not listing:
            abort(404)

        payment = bitcoin_monitor.create_payment_request(
            file_id, listing["price_satoshis"]
        )

        # Generate QR code
        from qr_utils import generate_qr_base64
        qr_b64 = generate_qr_base64(payment["payment_uri"])

        flash("Payment request created!")
        return render_template_string(
            LISTING_TMPL, listing=listing, payment=payment, qr_b64=qr_b64
        )

    @app.route("/tx/<tx_id>")
    def tx_detail(tx_id):
        tx = db.get_transaction(tx_id)
        if not tx:
            abort(404)
        key = key_store.get(tx_id)
        return render_template_string(TX_TMPL, tx=tx, key=key)

    @app.route("/tx/<tx_id>/status")
    def tx_status(tx_id):
        """JSON status endpoint for programmatic polling."""
        tx = db.get_transaction(tx_id)
        if not tx:
            return jsonify({"error": "not found"}), 404

        # Trigger a live check
        updated = bitcoin_monitor.check_single(tx_id)
        return jsonify(updated)

    @app.route("/tx/<tx_id>/release", methods=["POST"])
    def release_key(tx_id):
        import sqlite3 as _sqlite3
        tx = db.get_transaction(tx_id)
        if not tx:
            abort(404)

        if tx["status"] not in ("confirmed",):
            flash("Cannot release key: payment not confirmed.")
            return redirect(url_for("tx_detail", tx_id=tx_id))

        if db.key_already_released(tx_id):
            # Key was already released — show it from the release log
            with db._conn() as conn:
                row = conn.execute(
                    "SELECT key_b64 FROM key_releases WHERE tx_id=?", (tx_id,)
                ).fetchone()
            if row:
                key_store[tx_id] = row["key_b64"]
            return redirect(url_for("tx_detail", tx_id=tx_id))

        # Retrieve key_b64 — in a real deployment this comes from
        # secure encrypted storage; here we look it up from listings metadata
        listing = db.get_listing(tx["file_id"])
        if not listing:
            flash("Listing not found for this transaction.")
            return redirect(url_for("tx_detail", tx_id=tx_id))

        # The key must be stored securely server-side.
        # For this demo we load it from a sidecar .key file created during encryption.
        key_file = Path(listing["encrypted_path"]).with_suffix(".key")
        if not key_file.exists():
            flash("Key file not found on server.")
            return redirect(url_for("tx_detail", tx_id=tx_id))

        key_b64 = key_file.read_text().strip()

        try:
            db.record_key_release(tx_id, tx["file_id"], tx["btc_address"], key_b64)
        except _sqlite3.IntegrityError:
            flash("Key already released (replay protection triggered).")
            return redirect(url_for("tx_detail", tx_id=tx_id))

        db.update_transaction_released(tx_id)
        key_store[tx_id] = key_b64

        log.info("Key released via web UI: tx_id=%s", tx_id)
        return redirect(url_for("tx_detail", tx_id=tx_id))

    @app.route("/api/listings")
    def api_listings():
        return jsonify(db.list_listings())

    return app


def run_web_ui(db, bitcoin_monitor, config_module) -> None:
    """
    Entry point called from main.py `web-ui` command.
    Starts the Flask dev server in the foreground.
    """
    key_store: dict = {}
    app = create_app(db, bitcoin_monitor, config_module, key_store)

    log.info(
        "Starting web UI at http://%s:%d",
        config_module.FLASK_HOST,
        config_module.FLASK_PORT,
    )
    app.run(
        host=config_module.FLASK_HOST,
        port=config_module.FLASK_PORT,
        debug=False,
        use_reloader=False,
    )
