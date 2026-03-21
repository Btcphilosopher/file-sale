# ₿ btc-file-market

**A self-hosted, decentralised file market on Bitcoin.**  
Encrypt any file locally → distribute via BitTorrent or IPFS → accept Bitcoin payment → automatically release the decryption key. Zero third-party custody. Zero trust required.

---

## How it works

```
Seller                          Bitcoin network              Buyer
──────                          ───────────────              ─────
encrypt-file                                              download .enc via
  │                                                       magnet / IPFS CID
  ├─ AES-256-GCM encrypt                                       │
  ├─ save .enc + .key                                           │
  └─ create .torrent / upload IPFS                             │
                                                               │
generate-payment                                               │
  │                                                    scan QR code
  └─ derive fresh BIP-84 address ──────────────────►  send BTC
       (HD wallet, index N)
                │
                ▼
monitor-payment (polls Blockstream API)
  │
  └─ 1+ confirmation + amount ≥ price?
        │
        ▼
release-key ──────────────────────────────────────────► AES key revealed
  │                                                     decrypt-file
  └─ DB replay guard (UNIQUE constraint)
       prevents key being issued twice
```

---

## Security model

| Threat | Mitigation |
|---|---|
| Key exposure | Never logged/stored in plaintext; sidecar `.key` file is `chmod 600` |
| Underpayment | Amount validation: `received_sat ≥ expected_sat` before key release |
| Replay attack | `key_releases` table enforces `UNIQUE(tx_id)` at DB level |
| Payment expiry | Configurable TTL; expired requests never release a key |
| File tampering | AES-256-GCM authentication tag detects any modification |
| Wrong key | GCM `InvalidTag` exception — decryption fails safely |
| Private key leak | HD wallet WIF discarded after address derivation; only address stored |
| Exposed web UI | Flask binds `127.0.0.1` only; never exposed externally |
| Secrets in env | All sensitive values via `.env`; `.env` is `.gitignore`d |

---

## Quickstart

### 1. Install

```bash
git clone https://github.com/youruser/btc-file-market
cd btc-file-market
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` — the only required field is `BTC_MASTER_MNEMONIC`:

```bash
# Generate a fresh 24-word BIP-39 mnemonic:
python -c "
from hdwallet.mnemonics import BIP39Mnemonic
print(BIP39Mnemonic.generate(language='english', strength=256))
"
```

Paste the output into `.env`:

```env
BTC_MASTER_MNEMONIC="word1 word2 ... word24"
BTC_NETWORK=testnet          # change to mainnet when ready
```

> ⚠️ **Back up your mnemonic offline.** It is the only way to recover funds sent to derived addresses.

### 3. Sell a file

```bash
# Step 1 — encrypt and list
python main.py encrypt-file \
  --file ./report.pdf \
  --price 0.001 \
  --method torrent

# Output:
#   ✓ Encrypted:  ./storage/report.pdf.enc
#   ✓ Key file:   ./storage/report.pdf.key  ← KEEP SECRET
#   ✓ Metadata:   ./storage/<hash>.meta.json
#   🧲 Magnet URI: magnet:?xt=urn:btih:...
#   ✅ Listing created  File ID: abc123...

# Step 2 — create a payment request
python main.py generate-payment --file-id abc123...

# Output:
#   Address:  tb1q...
#   Amount:   0.00100000 BTC
#   URI:      bitcoin:tb1q...?amount=0.001&label=btc-file-market
#   📱 QR Code: ./storage/payment_<txid>.qr.png
#   TX ID:    d284855c-...

# Step 3 — monitor (blocks until paid, then auto-releases key)
python main.py monitor-payment --tx-id d284855c-...
```

### 4. Buy a file (buyer side)

```bash
# 1. Get the magnet URI or IPFS CID from the seller's metadata file
# 2. Download with any BitTorrent client (qBittorrent, Transmission)
#    or: ipfs get <CID>
# 3. Once you've paid and received the key, decrypt:

python main.py decrypt-file \
  --enc-file ./report.pdf.enc \
  --key <base64-key-from-seller>
```

---

## CLI reference

```
python main.py [COMMAND] [OPTIONS]
```

| Command | Description |
|---|---|
| `encrypt-file` | Encrypt a file, create listing, distribute via torrent/IPFS |
| `generate-payment` | Create a unique BTC address for a listing |
| `monitor-payment` | Poll blockchain; auto-release key on confirmation |
| `release-key` | Manually release key for a confirmed transaction |
| `decrypt-file` | Buyer: decrypt a purchased file with the released key |
| `list` | Show all listings |
| `web-ui` | Start the local Flask dashboard |
| `info` | Show current config summary |

### encrypt-file options

```
--file, -f       Path to the plaintext file (required)
--price, -p      Sale price in BTC, e.g. 0.001 (required)
--method, -m     Distribution: torrent | ipfs | both | none  [default: torrent]
--output-dir,-o  Override storage directory
```

### monitor-payment options

```
--tx-id, -t      Transaction ID from generate-payment (required)
--once           Check once and exit (no loop)
--auto-release   Automatically release key on confirmation  [default: true]
```

---

## Web UI

```bash
python main.py web-ui
# → http://127.0.0.1:5000
```

Features:
- Dashboard: all listings and recent transactions
- Per-listing buy page with live QR code
- Transaction status page (auto-refreshes every 15 s while pending)
- One-click key release after confirmation
- JSON API at `/api/listings`

---

## Project structure

```
btc-file-market/
├── main.py              CLI entry point — all 8 commands
├── config.py            Environment variable loader
├── logger.py            Coloured + rotating file logging
├── encryption.py        AES-256-GCM encrypt/decrypt
├── bitcoin_monitor.py   HD wallet + Blockstream API polling
├── torrent_ipfs.py      torf torrent creation + Pinata/kubo IPFS
├── qr_utils.py          BIP-21 QR code generation (PNG + base64)
├── database.py          SQLite schema + replay protection
├── web_ui.py            Flask local dashboard
├── .env.example         Configuration template
└── requirements.txt
```

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `BTC_MASTER_MNEMONIC` | *required* | BIP-39 24-word mnemonic |
| `BTC_NETWORK` | `testnet` | `mainnet` or `testnet` |
| `BTC_MIN_CONFIRMATIONS` | `1` | Confirmations before key release |
| `BTC_POLL_INTERVAL` | `30` | Blockchain polling interval (seconds) |
| `PAYMENT_EXPIRY_SECONDS` | `86400` | Payment TTL; `0` = never expire |
| `BLOCKSTREAM_API_URL` | auto | Esplora API endpoint |
| `PINATA_JWT` | *(empty)* | Pinata JWT for IPFS pinning |
| `IPFS_API_URL` | `http://127.0.0.1:5001` | Local IPFS daemon |
| `TORRENT_TRACKERS` | opentrackr, open.tracker.cl | Comma-separated tracker URLs |
| `STORAGE_DIR` | `./storage` | Encrypted files + keys + torrents |
| `DATABASE_PATH` | `./storage/market.db` | SQLite database |
| `FLASK_SECRET_KEY` | random | Flask session secret |
| `FLASK_HOST` | `127.0.0.1` | Web UI bind address |
| `FLASK_PORT` | `5000` | Web UI port |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |
| `LOG_FILE` | `./storage/market.log` | Rotating log (5 MB × 3) |

---

## On-disk file format

### Encrypted file (`.enc`)

```
┌─────────────────────────────────────────────────────┐
│  12 bytes  │  N bytes ciphertext + 16 bytes GCM tag │
│  IV/nonce  │                                         │
└─────────────────────────────────────────────────────┘
```

- IV: random 96-bit nonce, unique per encryption
- GCM tag: last 16 bytes, verifies integrity on decryption
- Key: random 256-bit, **never embedded in the file**

### Metadata file (`.meta.json`) — public, no key

```json
{
  "file_id": "15c4152776b94050...",
  "original_filename": "report.pdf",
  "encrypted_path": "/path/to/report.pdf.enc",
  "original_sha256": "abc...",
  "encrypted_sha256": "def...",
  "file_size_bytes": 1048576
}
```

### Key sidecar (`.key`) — `chmod 600`, server-side secret

```
CEb07neQJ0E2VxnCjpFZ...   (URL-safe base64 AES-256 key)
```

---

## Database schema

```sql
listings       — files for sale
transactions   — one BTC address per sale (HD index tracked)
key_releases   — audit log; UNIQUE(tx_id) blocks replay attacks
```

Transaction lifecycle: `pending → confirmed → key_released | expired`

---

## Dependencies

| Package | Purpose |
|---|---|
| `cryptography` | AES-256-GCM via `AESGCM` |
| `hdwallet` | BIP-84 HD wallet address derivation |
| `requests` | Blockstream Esplora API calls |
| `torf` | `.torrent` creation and magnet URI |
| `qrcode[pil]` | QR code PNG generation |
| `flask` | Local web dashboard |
| `python-dotenv` | `.env` loading |
| `click` | CLI argument parsing |
| `colorlog` | Coloured terminal logging |

---

## Production checklist

- [ ] Switch `BTC_NETWORK=mainnet` and update `BLOCKSTREAM_API_URL`
- [ ] Generate a fresh mnemonic (not the test `abandon...` mnemonic)
- [ ] Set `FLASK_SECRET_KEY` to a random 64-char hex string
- [ ] Run Flask behind nginx with TLS if exposing beyond localhost
- [ ] Store `.key` sidecar files on an encrypted volume
- [ ] Back up `market.db` regularly (it holds transaction state)
- [ ] Consider encrypting the `.key` files at rest with a passphrase
- [ ] Set `PAYMENT_EXPIRY_SECONDS` to a reasonable value (e.g. 86400 = 24h)
- [ ] Increase `BTC_MIN_CONFIRMATIONS` to 3–6 for high-value files

---

## Limitations and known gaps

- **Key delivery is manual** for the web UI path (seller clicks "Release key"). Automated delivery via email or an API endpoint would require additional infrastructure.
- **Large files**: AES-GCM buffers the entire plaintext in RAM. For files > 500 MB, switch to ChaCha20-Poly1305 with streaming chunks.
- **Wallet sweep**: derived private keys (WIF) are currently discarded. Add encrypted WIF storage + a `sweep-funds` command to consolidate received payments.
- **No HTTPS on web UI by default**: the Flask dev server is HTTP. Put nginx + certbot in front if exposing to a LAN.
- **Single-seller model**: the DB schema and key storage assumes one operator. Multi-seller support would require per-seller key isolation.

---

## License

MIT — do whatever you want, but don't blame the author if you accept mainnet Bitcoin for cat pictures.
