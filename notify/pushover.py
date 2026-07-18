"""Push notifications via Pushover — chosen over a free alternative like
ntfy.sh specifically because it has a genuine native Mac app alongside
iOS/Android, not just a mobile story; the user asked for both.

Setup (one-time, on pushover.net):
1. Create a free account and install the app on your phone/Mac.
2. Create an "Application" in the dashboard to get an API token
   (PUSHOVER_APP_TOKEN).
3. Your account page shows a "User Key" (PUSHOVER_USER_KEY).
Both go in .env. There's a small one-time cost per platform app (not a
subscription) after a trial period — this wasn't free-first, it was
picked for the specific "reliable native Mac notifications" requirement.

**Partially live-verified 2026-07-18**: a real request with an invalid token
correctly hit /1/messages.json and got back exactly the documented error
shape (`{"token":"invalid","errors":[...],"status":0}`), confirming the
endpoint, request fields, and response shape are all right — the
`raise_for_status`/`status != 1` handling in `send_notification` below is
exercised and correct. What's NOT yet verified is the success path (no real
account/token exists to test an actual send with) — send yourself one real
test notification once you have an account, to confirm delivery end to end,
not just that the API accepts the request.
"""

from __future__ import annotations

import httpx

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"


class PushoverError(Exception):
    pass


def send_notification(token: str, user_key: str, title: str, message: str, url: str | None = None) -> None:
    """Sends one push notification. Raises PushoverError on any non-success
    response rather than failing silently — a notification pipeline that
    quietly stops working is worse than one that's loud about breaking.
    """
    payload = {
        "token": token,
        "user": user_key,
        "title": title,
        "message": message,
    }
    if url:
        payload["url"] = url

    response = httpx.post(PUSHOVER_API_URL, data=payload, timeout=15.0)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise PushoverError(f"Pushover API returned {response.status_code}: {response.text}") from exc

    body = response.json()
    if body.get("status") != 1:
        raise PushoverError(f"Pushover reported failure: {body}")
