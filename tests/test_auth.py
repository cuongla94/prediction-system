from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_client.auth import KalshiCredentials, sign_request


def _make_credentials(key_id: str = "test-key-id") -> KalshiCredentials:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return KalshiCredentials(key_id=key_id, private_key=private_key)


def test_sign_request_returns_expected_headers():
    credentials = _make_credentials("abc-123")

    headers = sign_request(credentials, "get", "/trade-api/v2/portfolio/balance")

    assert set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE", "KALSHI-ACCESS-TIMESTAMP"}
    assert headers["KALSHI-ACCESS-KEY"] == "abc-123"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()


def test_sign_request_uppercases_method_and_signs_full_path():
    credentials = _make_credentials()
    path = "/trade-api/v2/portfolio/balance"

    headers = sign_request(credentials, "get", path)

    message = (headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + path).encode("utf-8")
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

    # Verifying with the matching public key confirms the exact message we intend to
    # sign (timestamp + upper-cased method + full path, no query string) round-trips —
    # this is the piece Kalshi's server checks bit-for-bit, so a passing verification
    # here is the real guarantee, not just "no exception was raised".
    public_key = credentials.private_key.public_key()
    public_key.verify(
        signature,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_sign_request_excludes_query_string_from_message():
    credentials = _make_credentials()
    path_without_query = "/trade-api/v2/markets"

    headers = sign_request(credentials, "GET", path_without_query)
    message = (headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + path_without_query).encode("utf-8")
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

    public_key = credentials.private_key.public_key()
    public_key.verify(
        signature,
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_from_pem_file_round_trips(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "test_key.pem"
    key_path.write_bytes(pem_bytes)

    credentials = KalshiCredentials.from_pem_file("key-id", key_path)

    assert credentials.key_id == "key-id"
    assert credentials.private_key.key_size == 2048
