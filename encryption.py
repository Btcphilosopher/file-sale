"""
encryption.py — AES-256-GCM authenticated file encryption.

Design choices:
  • AES-256-GCM: authenticated encryption (confidentiality + integrity).
  • Random 256-bit key per file — never reused.
  • Random 96-bit IV (nonce) per encryption — stored prepended to ciphertext.
  • 128-bit GCM authentication tag appended by the library — verifies
    the ciphertext has not been tampered with on decryption.
  • Keys are returned as URL-safe base64 strings for easy sharing/storage.
  • The key is NEVER written to disk by this module — the caller decides.

File format on disk:
  [ 12 bytes IV ][ N bytes ciphertext+tag ]
  (tag is the last 16 bytes, managed transparently by cryptography lib)
"""

import os
import base64
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass, asdict

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from logger import get_logger

log = get_logger("btc_market.encryption")

IV_SIZE = 12        # bytes — 96-bit nonce recommended for GCM
KEY_SIZE = 32       # bytes — AES-256
CHUNK_SIZE = 64 * 1024  # 64 KB streaming chunks


# ── Data structures ───────────────────────────────────────────

@dataclass
class EncryptionResult:
    """Everything produced by a single encrypt_file() call."""
    file_id: str            # hex SHA-256 of the *encrypted* file
    original_filename: str
    encrypted_path: str     # absolute path to .enc file
    key_b64: str            # URL-safe base64 AES-256 key (SECRET)
    iv_b64: str             # base64 IV (not secret, embedded in file)
    original_sha256: str    # hex SHA-256 of plaintext (for buyer verification)
    encrypted_sha256: str   # hex SHA-256 of ciphertext
    file_size_bytes: int    # original file size

    def to_metadata_dict(self) -> dict:
        """
        Return a dict safe to write to a public metadata file.
        The decryption key is intentionally EXCLUDED.
        """
        d = asdict(self)
        del d["key_b64"]   # never expose the key in metadata
        return d


# ── Core functions ────────────────────────────────────────────

def generate_key() -> bytes:
    """Generate a cryptographically secure 256-bit AES key."""
    return os.urandom(KEY_SIZE)


def key_to_b64(key: bytes) -> str:
    """Encode raw key bytes as URL-safe base64."""
    return base64.urlsafe_b64encode(key).decode()


def b64_to_key(key_b64: str) -> bytes:
    """Decode URL-safe base64 key string to raw bytes."""
    raw = base64.urlsafe_b64decode(key_b64.encode())
    if len(raw) != KEY_SIZE:
        raise ValueError(f"Invalid key length: expected {KEY_SIZE}, got {len(raw)}")
    return raw


def _sha256_file(path: Path) -> str:
    """Return hex SHA-256 digest of a file, reading in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def encrypt_file(
    input_path: str | Path,
    output_dir: str | Path,
    key: bytes | None = None,
) -> EncryptionResult:
    """
    Encrypt *input_path* with AES-256-GCM and save to *output_dir*.

    Args:
        input_path:  Path to the plaintext file.
        output_dir:  Directory where the .enc file is written.
        key:         Optional raw 32-byte key; generated if None.

    Returns:
        EncryptionResult with all metadata and the plaintext key.

    Raises:
        FileNotFoundError: input_path does not exist.
        ValueError:        key is the wrong length.
    """
    input_path = Path(input_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if key is None:
        key = generate_key()
    elif len(key) != KEY_SIZE:
        raise ValueError(f"Key must be {KEY_SIZE} bytes, got {len(key)}")

    iv = os.urandom(IV_SIZE)
    aesgcm = AESGCM(key)

    # Derive output filename from original name
    enc_filename = input_path.name + ".enc"
    enc_path = output_dir / enc_filename

    log.info("Encrypting '%s' → '%s'", input_path.name, enc_path)

    # Hash the original file before encryption
    original_sha256 = _sha256_file(input_path)
    original_size = input_path.stat().st_size

    # Read full plaintext (for large files, chunked streaming with GCM
    # requires careful nonce management — for simplicity we buffer here;
    # swap to streaming ChaCha20-Poly1305 for multi-GB files).
    plaintext = input_path.read_bytes()

    # Encrypt (ciphertext includes 16-byte GCM tag at the end)
    ciphertext = aesgcm.encrypt(iv, plaintext, None)

    # Write: IV || ciphertext+tag
    with open(enc_path, "wb") as f:
        f.write(iv)
        f.write(ciphertext)

    encrypted_sha256 = _sha256_file(enc_path)
    file_id = encrypted_sha256  # use enc-file hash as stable ID

    log.info(
        "Encryption complete. file_id=%s size=%d bytes",
        file_id[:16] + "…",
        original_size,
    )

    return EncryptionResult(
        file_id=file_id,
        original_filename=input_path.name,
        encrypted_path=str(enc_path),
        key_b64=key_to_b64(key),
        iv_b64=base64.urlsafe_b64encode(iv).decode(),
        original_sha256=original_sha256,
        encrypted_sha256=encrypted_sha256,
        file_size_bytes=original_size,
    )


def decrypt_file(
    enc_path: str | Path,
    key_b64: str,
    output_dir: str | Path,
    output_filename: str | None = None,
) -> Path:
    """
    Decrypt a file produced by encrypt_file().

    Args:
        enc_path:        Path to the .enc file.
        key_b64:         URL-safe base64 AES-256 key.
        output_dir:      Directory for the decrypted output.
        output_filename: Override the output filename (optional).

    Returns:
        Path to the decrypted file.

    Raises:
        cryptography.exceptions.InvalidTag: if key is wrong or file is tampered.
    """
    enc_path = Path(enc_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    key = b64_to_key(key_b64)
    aesgcm = AESGCM(key)

    with open(enc_path, "rb") as f:
        iv = f.read(IV_SIZE)
        ciphertext_with_tag = f.read()

    if len(iv) != IV_SIZE:
        raise ValueError("Encrypted file is malformed (IV too short)")

    log.info("Decrypting '%s'", enc_path.name)
    plaintext = aesgcm.decrypt(iv, ciphertext_with_tag, None)

    # Derive output filename: strip .enc suffix if present
    if output_filename is None:
        stem = enc_path.name
        output_filename = stem[:-4] if stem.endswith(".enc") else stem + ".dec"

    out_path = output_dir / output_filename
    out_path.write_bytes(plaintext)
    log.info("Decryption complete → '%s'", out_path)
    return out_path


def write_metadata_file(result: EncryptionResult, output_dir: str | Path) -> Path:
    """
    Write a JSON metadata file (no secret key) alongside the encrypted file.
    """
    output_dir = Path(output_dir).resolve()
    meta_path = output_dir / f"{result.file_id[:16]}.meta.json"
    meta = result.to_metadata_dict()
    meta_path.write_text(json.dumps(meta, indent=2))
    log.info("Metadata written → '%s'", meta_path)
    return meta_path
