"""
Shared Slack notification helper.

Single implementation of the webhook/bot-token posting logic used by the
reporting, poller, and failed-payment modules. Returns delivery status so
callers can surface undelivered notifications instead of failing silently.
"""

import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# One retry with a short backoff covers transient network blips and 429s.
# Deliberately not more: escalations must surface quickly, and a caller
# already gets an explicit False when delivery fails.
_RETRY_BACKOFF_SECONDS = 2.0


def slack_webhook() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "")


def slack_bot_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "")


def ops_channel() -> str:
    return os.environ.get("AR_OPS_SLACK_CHANNEL", "#ar-operations")


def user_mention(env_var: str, fallback_name: str) -> str:
    """Return a Slack <@id> mention if the user ID env var is set, else the plain name."""
    uid = os.environ.get(env_var, "")
    return f"<@{uid}>" if uid else fallback_name


def post(message: str, blocks: list | None = None) -> bool:
    """
    Post to Slack. Returns True only if the message was actually delivered.
    Retries once on a transient failure (network error or HTTP 429).
    """
    webhook = slack_webhook()
    token = slack_bot_token()
    if not webhook and not token:
        logger.error(
            "No Slack configuration (SLACK_WEBHOOK_URL / SLACK_BOT_TOKEN) — message NOT delivered: %s",
            message,
        )
        return False

    for attempt in (1, 2):
        try:
            if webhook:
                payload: dict[str, Any] = {"text": message}
                if blocks:
                    payload["blocks"] = blocks
                resp = requests.post(webhook, json=payload, timeout=10)
                delivered = resp.ok
            else:
                payload = {"channel": ops_channel(), "text": message}
                if blocks:
                    payload["blocks"] = blocks
                resp = requests.post(
                    "https://slack.com/api/chat.postMessage",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                delivered = resp.ok and resp.json().get("ok", False)

            if delivered:
                return True
            # Only rate limiting is worth retrying; other failures are permanent
            if resp.status_code != 429 or attempt == 2:
                logger.error("Slack post failed (HTTP %s) — message: %s", resp.status_code, message)
                return False
            logger.warning("Slack rate-limited (429) — retrying once")
        except requests.RequestException as exc:
            if attempt == 2:
                logger.error("Slack post failed: %s — message: %s", exc, message)
                return False
            logger.warning("Slack post transient error (%s) — retrying once", exc)
        except Exception as exc:
            logger.error("Slack post failed: %s — message: %s", exc, message)
            return False
        time.sleep(_RETRY_BACKOFF_SECONDS)

    return False
