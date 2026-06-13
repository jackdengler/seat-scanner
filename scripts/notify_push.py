"""Send Web Push notifications to all registered subscriptions.

Uses pywebpush (installed in the workflow) with the VAPID private key
from the VAPID_PRIVATE_KEY Actions secret. Returns endpoints that the
push service reports as gone so the caller can flag them.
"""

import json

from pywebpush import webpush, WebPushException


def send_all(subscriptions, payload, vapid_private_key,
             contact="mailto:seat-scanner@users.noreply.github.com"):
    dead = []
    for sub in subscriptions:
        endpoint = sub.get("endpoint", "")
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload),
                vapid_private_key=vapid_private_key,
                vapid_claims={"sub": contact},
            )
            print(f"push delivered to ...{endpoint[-24:]}")
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                print(f"push subscription gone (HTTP {status}): ...{endpoint[-24:]}")
                dead.append(endpoint)
            else:
                print(f"push failed (HTTP {status}): {e}")
    return dead
