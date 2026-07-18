from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


@dataclass(frozen=True)
class KalshiCredentials:
    """API key ID + RSA private key used to sign authenticated Kalshi requests."""

    key_id: str
    private_key: rsa.RSAPrivateKey

    @classmethod
    def from_pem_file(cls, key_id: str, private_key_path: str | Path) -> "KalshiCredentials":
        pem_bytes = Path(private_key_path).expanduser().read_bytes()
        private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise TypeError(f"Expected an RSA private key, got {type(private_key).__name__}")
        return cls(key_id=key_id, private_key=private_key)


def sign_request(credentials: KalshiCredentials, method: str, full_path: str) -> dict[str, str]:
    """Build the KALSHI-ACCESS-* headers for one request.

    `full_path` must be the complete request path Kalshi receives — including the
    `/trade-api/v2` prefix — with the query string stripped. Kalshi signs that exact
    string on their end; any deviation (missing prefix, included query params, wrong
    case on the method) fails signature verification with a bare 401.
    """
    timestamp_ms = str(int(time.time() * 1000))
    message = (timestamp_ms + method.upper() + full_path).encode("utf-8")
    signature = credentials.private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": credentials.key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }
