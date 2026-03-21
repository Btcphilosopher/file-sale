"""
qr_utils.py — QR code generation for Bitcoin payment URIs.

Generates:
  1. A PNG QR code image file.
  2. An inline base64-encoded PNG string (for embedding in HTML).

The QR encodes the standard BIP-21 payment URI:
  bitcoin:<address>?amount=<btc>&label=<label>
"""

import base64
import io
from pathlib import Path

from logger import get_logger

log = get_logger("btc_market.qr")


def _make_qr(data: str, error_correction="M", box_size: int = 10, border: int = 4):
    """
    Create a qrcode.QRCode object.
    error_correction: L=7%, M=15%, Q=25%, H=30% data recovery.
    """
    try:
        import qrcode
        from qrcode.constants import (
            ERROR_CORRECT_L, ERROR_CORRECT_M,
            ERROR_CORRECT_Q, ERROR_CORRECT_H,
        )
    except ImportError as exc:
        raise ImportError("qrcode library required: pip install 'qrcode[pil]'") from exc

    ec_map = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M,
               "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}

    qr = qrcode.QRCode(
        version=None,        # auto-size
        error_correction=ec_map.get(error_correction.upper(), ERROR_CORRECT_M),
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr


def generate_qr_file(
    payment_uri: str,
    output_path: str | Path,
    fill_color: str = "black",
    back_color: str = "white",
) -> Path:
    """
    Write a QR code PNG to *output_path*.

    Args:
        payment_uri: The BIP-21 URI or any string to encode.
        output_path: Destination file path (will be created/overwritten).
        fill_color:  QR module colour (default black).
        back_color:  Background colour (default white).

    Returns:
        Path to the written PNG file.
    """
    qr = _make_qr(payment_uri)
    img = qr.make_image(fill_color=fill_color, back_color=back_color)

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path))

    log.info("QR code written → %s", output_path)
    return output_path


def generate_qr_base64(
    payment_uri: str,
    fill_color: str = "black",
    back_color: str = "white",
) -> str:
    """
    Return a base64-encoded PNG string suitable for embedding in HTML:
      <img src="data:image/png;base64,<RESULT>" />

    Args:
        payment_uri: The BIP-21 URI or any string to encode.
        fill_color:  QR module colour.
        back_color:  Background colour.

    Returns:
        Base64-encoded PNG string (no data-URI prefix).
    """
    qr = _make_qr(payment_uri)
    img = qr.make_image(fill_color=fill_color, back_color=back_color)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    log.debug("QR base64 generated (%d chars)", len(b64))
    return b64


def build_payment_uri(
    address: str,
    amount_btc: float,
    label: str = "btc-file-market",
    message: str = "",
) -> str:
    """
    Build a BIP-21 Bitcoin payment URI.

    Example:
        bitcoin:bc1qxxx?amount=0.001&label=btc-file-market
    """
    uri = f"bitcoin:{address}?amount={amount_btc:.8f}&label={label}"
    if message:
        uri += f"&message={message}"
    return uri
