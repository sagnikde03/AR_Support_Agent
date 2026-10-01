"""
Postmark Email Integration — Outbound (F-04).

Single provider for all outbound AR emails. Hard rules enforced here:
  - CC Leila on every send
  - Invoice PDF attached when bytes are provided
  - Subject line format: "{account_name} — Invoice #{invoice_number}"

Postmark open/click tracking serves as the operational replacement for
Gmail-native read receipts, which are not available via transactional APIs.

Configured via environment variables:
  POSTMARK_API_KEY      — required
  AR_FROM_EMAIL         — e.g. billing@caplinked.com
  AR_FROM_NAME          — e.g. CapLinked Billing Team
  AR_CC_EMAIL           — Leila's email (hard CC on every send)
  AR_REPLY_TO           — e.g. ar-reply@billing.caplinked.com
"""

import base64
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

_POSTMARK_API = "https://api.postmarkapp.com/email"
_POSTMARK_BATCH_API = "https://api.postmarkapp.com/email/batch"

_SEND_TIMEOUT_SECONDS = 15


def _api_key() -> str:
    key = os.environ.get("POSTMARK_API_KEY", "")
    if not key:
        raise RuntimeError("POSTMARK_API_KEY not set")
    return key


def _from_address() -> str:
    name = os.environ.get("AR_FROM_NAME", "CapLinked Billing Team")
    email = os.environ.get("AR_FROM_EMAIL", "")
    if not email:
        raise RuntimeError("AR_FROM_EMAIL not set")
    return f"{name} <{email}>"


def _cc_leila() -> str:
    cc = os.environ.get("AR_CC_EMAIL", "").strip()
    if not cc:
        raise RuntimeError("AR_CC_EMAIL not set — mandatory CC on every AR email")
    return cc


def _reply_to() -> str:
    return os.environ.get("AR_REPLY_TO", "")


def _headers() -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Postmark-Server-Token": _api_key(),
    }


def send_email(
    to: str | list[str],
    subject: str,
    html_body: str,
    text_body: str,
    cc: str | list[str] | None = None,
    reply_to: str | None = None,
    tag: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """
    Send a plain email. Leila is always CC'd — callers should not need to add her.

    Args:
        to         : recipient address or list of addresses
        subject    : email subject
        html_body  : HTML version of the body
        text_body  : plain text version
        cc         : additional CC addresses beyond Leila
        reply_to   : override default reply-to
        tag        : Postmark message stream tag
        metadata   : arbitrary k/v metadata stored in Postmark

    Returns:
        Postmark API response dict
    """
    to_str = ", ".join(to) if isinstance(to, list) else to

    cc_parts = []
    leila = _cc_leila()
    if leila:
        cc_parts.append(leila)
    if cc:
        extra = ", ".join(cc) if isinstance(cc, list) else cc
        cc_parts.append(extra)
    cc_str = ", ".join(cc_parts) if cc_parts else None

    payload: dict[str, Any] = {
        "From":     _from_address(),
        "To":       to_str,
        "Subject":  subject,
        "HtmlBody": html_body,
        "TextBody": text_body,
        "TrackOpens":  True,
        "TrackLinks": "HtmlAndText",
        "MessageStream": "outbound",
    }
    if cc_str:
        payload["Cc"] = cc_str
    rt = reply_to or _reply_to()
    if rt:
        payload["ReplyTo"] = rt
    if tag:
        payload["Tag"] = tag
    if metadata:
        payload["Metadata"] = metadata

    return _post(_POSTMARK_API, payload)


def send_with_invoice_pdf(
    to: str | list[str],
    subject: str,
    html_body: str,
    text_body: str,
    invoice_pdf_bytes: bytes,
    invoice_filename: str,
    cc: str | list[str] | None = None,
    reply_to: str | None = None,
    tag: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """
    Send an email with the invoice PDF attached.

    Every outbound AR reminder must include the invoice PDF — this is the
    standard send path. Raises if pdf_bytes is absent; callers must fetch
    the PDF before calling this function.
    """
    if not invoice_pdf_bytes:
        raise RuntimeError(
            f"Invoice PDF required for '{invoice_filename}' but no bytes were provided — "
            "aborting send to enforce the attachment contract"
        )

    to_str = ", ".join(to) if isinstance(to, list) else to

    cc_parts = []
    leila = _cc_leila()
    if leila:
        cc_parts.append(leila)
    if cc:
        extra = ", ".join(cc) if isinstance(cc, list) else cc
        cc_parts.append(extra)
    cc_str = ", ".join(cc_parts) if cc_parts else None

    attachment = {
        "Name":        invoice_filename,
        "Content":     base64.b64encode(invoice_pdf_bytes).decode("ascii"),
        "ContentType": "application/pdf",
    }

    payload: dict[str, Any] = {
        "From":        _from_address(),
        "To":          to_str,
        "Subject":     subject,
        "HtmlBody":    html_body,
        "TextBody":    text_body,
        "Attachments": [attachment],
        "TrackOpens":  True,
        "TrackLinks":  "HtmlAndText",
        "MessageStream": "outbound",
    }
    if cc_str:
        payload["Cc"] = cc_str
    rt = reply_to or _reply_to()
    if rt:
        payload["ReplyTo"] = rt
    if tag:
        payload["Tag"] = tag
    if metadata:
        payload["Metadata"] = metadata

    return _post(_POSTMARK_API, payload)


def _post(url: str, payload: dict) -> dict:
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=_SEND_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ErrorCode", 0) != 0:
            raise RuntimeError(f"Postmark error {data['ErrorCode']}: {data.get('Message')}")
        logger.info(
            "Postmark send OK — MessageID=%s To=%s",
            data.get("MessageID"),
            payload.get("To"),
        )
        return data
    except requests.RequestException as exc:
        logger.error("Postmark send failed: %s", exc)
        raise


def build_subject(account_name: str, invoice_number: str) -> str:
    """Standard subject line format required by F-05 spec."""
    return f"{account_name} — Invoice #{invoice_number}"
