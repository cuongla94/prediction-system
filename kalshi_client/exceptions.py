from __future__ import annotations

import httpx


class KalshiError(Exception):
    """Base exception for all Kalshi client errors."""


class KalshiAuthError(KalshiError):
    """Raised when an authenticated endpoint is called without valid credentials."""


class KalshiAPIError(KalshiError):
    """Raised when the Kalshi API returns a non-2xx response."""

    def __init__(self, status_code: int, message: str, body: str | None = None):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Kalshi API error {status_code}: {message}")

    @classmethod
    def from_response(cls, response: httpx.Response) -> "KalshiAPIError":
        message = response.text
        try:
            payload = response.json()
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message", message)
            elif isinstance(error, str):
                message = error
            elif "message" in payload:
                message = payload["message"]
        except ValueError:
            pass
        return cls(response.status_code, message, body=response.text)
