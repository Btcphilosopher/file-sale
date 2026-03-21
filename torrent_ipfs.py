"""
torrent_ipfs.py — Encrypted file distribution via BitTorrent or IPFS.

BitTorrent:
  • Uses the `torf` library to create a .torrent file.
  • Returns a magnet URI (includes info-hash, name, trackers).
  • The .torrent file can be seeded with any BitTorrent client.

IPFS:
  • Supports two backends:
    1. Pinata cloud pinning via their REST API (requires PINATA_JWT).
    2. Local IPFS daemon via the HTTP API (kubo/go-ipfs).
  • Returns a CIDv1 content identifier.
  • The IPFS gateway URL for browsers is also returned.
"""

import os
import hashlib
import json
from pathlib import Path
from typing import Optional

import requests

from logger import get_logger

log = get_logger("btc_market.distribution")


# ──────────────────────────────────────────────────────────────
# BitTorrent
# ──────────────────────────────────────────────────────────────

def create_torrent(
    encrypted_file_path: str | Path,
    output_dir: str | Path,
    trackers: list[str],
    comment: str = "",
) -> tuple[str, str]:
    """
    Create a .torrent file for the encrypted file.

    Args:
        encrypted_file_path: Path to the .enc file to distribute.
        output_dir:          Where to save the .torrent file.
        trackers:            List of announce URLs.
        comment:             Optional torrent comment (metadata).

    Returns:
        (torrent_path, magnet_uri)

    Raises:
        ImportError: if `torf` is not installed.
        FileNotFoundError: if the encrypted file does not exist.
    """
    try:
        import torf
    except ImportError as exc:
        raise ImportError("torf library required: pip install torf") from exc

    enc_path = Path(encrypted_file_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not enc_path.is_file():
        raise FileNotFoundError(f"Encrypted file not found: {enc_path}")

    torrent_name = enc_path.stem  # filename without .enc → used as torrent name
    torrent_path = output_dir / f"{torrent_name}.torrent"

    log.info("Creating torrent for '%s'…", enc_path.name)

    t = torf.Torrent(
        path=str(enc_path),
        trackers=[[url] for url in trackers],  # each tracker in its own tier
        comment=comment or f"btc-file-market: {enc_path.name}",
        private=False,
        source="btc-file-market",
    )

    # Generate pieces (this may take a moment for large files)
    def _progress(torrent, filepath, pieces_done, pieces_total):
        if pieces_total and pieces_done % 20 == 0:
            pct = 100 * pieces_done / pieces_total
            log.debug("Hashing pieces: %.0f%%", pct)

    t.generate(callback=_progress, interval=0.5)
    t.write(str(torrent_path), overwrite=True)

    magnet = str(t.magnet())
    log.info("Torrent created: %s", torrent_path)
    log.info("Magnet URI: %s", magnet)

    return str(torrent_path), magnet


# ──────────────────────────────────────────────────────────────
# IPFS
# ──────────────────────────────────────────────────────────────

class IPFSError(Exception):
    pass


def _upload_to_pinata(file_path: Path, pinata_jwt: str) -> str:
    """
    Pin a file to IPFS via Pinata's REST API.
    Returns the IPFS CID string.
    """
    url = "https://api.pinata.cloud/pinning/pinFileToIPFS"
    headers = {
        "Authorization": f"Bearer {pinata_jwt}",
    }

    log.info("Uploading '%s' to Pinata…", file_path.name)

    with open(file_path, "rb") as f:
        response = requests.post(
            url,
            headers=headers,
            files={"file": (file_path.name, f, "application/octet-stream")},
            timeout=120,
        )

    if response.status_code not in (200, 201):
        raise IPFSError(
            f"Pinata upload failed [{response.status_code}]: {response.text[:300]}"
        )

    data = response.json()
    cid = data.get("IpfsHash")
    if not cid:
        raise IPFSError(f"Pinata response missing IpfsHash: {data}")

    log.info("Pinata upload successful. CID: %s", cid)
    return cid


def _upload_to_local_ipfs(file_path: Path, api_url: str) -> str:
    """
    Add a file to a local IPFS daemon via the HTTP API.
    Returns the IPFS CID string.
    """
    url = f"{api_url.rstrip('/')}/api/v0/add"
    log.info("Adding '%s' to local IPFS daemon…", file_path.name)

    with open(file_path, "rb") as f:
        response = requests.post(
            url,
            files={"file": (file_path.name, f)},
            params={"pin": "true", "cid-version": "1"},
            timeout=120,
        )

    if response.status_code != 200:
        raise IPFSError(
            f"Local IPFS daemon error [{response.status_code}]: {response.text[:300]}"
        )

    data = response.json()
    cid = data.get("Hash")
    if not cid:
        raise IPFSError(f"IPFS add response missing Hash: {data}")

    log.info("Local IPFS upload successful. CID: %s", cid)
    return cid


def upload_to_ipfs(
    file_path: str | Path,
    pinata_jwt: str = "",
    ipfs_api_url: str = "http://127.0.0.1:5001",
) -> tuple[str, str]:
    """
    Upload a file to IPFS using Pinata (preferred) or local daemon (fallback).

    Args:
        file_path:    Path to the file to pin.
        pinata_jwt:   Pinata JWT token. If empty, local daemon is used.
        ipfs_api_url: Local IPFS daemon API base URL.

    Returns:
        (cid, gateway_url)
        — gateway_url is a public IPFS gateway URL for browser access.
    """
    file_path = Path(file_path).resolve()

    if not file_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    if pinata_jwt:
        cid = _upload_to_pinata(file_path, pinata_jwt)
    else:
        cid = _upload_to_local_ipfs(file_path, ipfs_api_url)

    gateway_url = f"https://ipfs.io/ipfs/{cid}"
    return cid, gateway_url


# ──────────────────────────────────────────────────────────────
# Convenience wrapper — try torrent first, fall back to IPFS
# ──────────────────────────────────────────────────────────────

def distribute_file(
    encrypted_file_path: str | Path,
    output_dir: str | Path,
    method: str,  # "torrent" | "ipfs" | "both"
    trackers: list[str] | None = None,
    pinata_jwt: str = "",
    ipfs_api_url: str = "http://127.0.0.1:5001",
) -> dict:
    """
    Distribute an encrypted file via torrent, IPFS, or both.

    Returns a dict with keys:
      method, torrent_path, magnet_uri, ipfs_cid, ipfs_gateway_url
    """
    result: dict = {
        "method": method,
        "torrent_path": None,
        "magnet_uri": None,
        "ipfs_cid": None,
        "ipfs_gateway_url": None,
    }

    if method in ("torrent", "both"):
        try:
            torrent_path, magnet = create_torrent(
                encrypted_file_path,
                output_dir,
                trackers or [],
            )
            result["torrent_path"] = torrent_path
            result["magnet_uri"] = magnet
        except Exception as exc:
            log.error("Torrent creation failed: %s", exc)
            if method == "torrent":
                raise

    if method in ("ipfs", "both"):
        try:
            cid, gw = upload_to_ipfs(
                encrypted_file_path,
                pinata_jwt=pinata_jwt,
                ipfs_api_url=ipfs_api_url,
            )
            result["ipfs_cid"] = cid
            result["ipfs_gateway_url"] = gw
        except Exception as exc:
            log.error("IPFS upload failed: %s", exc)
            if method == "ipfs":
                raise

    return result
