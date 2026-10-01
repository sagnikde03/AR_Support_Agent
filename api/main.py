"""
AR Agent FastAPI Application.

Two main entry points:
  POST /webhook/inbound   — Postmark inbound email webhook (F-12)
  GET  /health            — health check

Dashboard is mounted at /dashboard (F-23).
Slack interactive actions (shadow queue approve/skip) at /slack/actions.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response

from ar_agent.state import invoice_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    invoice_db.init_db()
    logger.info("AR Agent API started — DB initialised")
    yield


app = FastAPI(title="CapLinked AR Agent", docs_url=None, redoc_url=None, lifespan=lifespan)

# No CORS middleware: the dashboard is served same-origin, and Postmark/Slack
# webhooks are server-to-server calls that do not use CORS.

# Mount dashboard
from ar_agent.dashboard.routes import router as dashboard_router
app.include_router(dashboard_router)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ar-agent"}


# ── Postmark inbound webhook (F-12) ───────────────────────────────────────────

@app.post("/webhook/inbound")
async def inbound_email(request: Request):
    """
    Receives Postmark inbound email events and routes to the AR orchestrator.

    Postmark sends a JSON payload with fields including:
      FromFull.Email, Subject, TextBody, HtmlBody, MessageID, Date

    Requires HTTP Basic Auth matching POSTMARK_WEBHOOK_USER/PASSWORD —
    configure the same credentials in the Postmark inbound webhook URL.
    The endpoint is public, so this runs before anything is read or stored.
    """
    if not _verify_inbound_auth(request):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="ar-inbound"'},
        )

    body = await request.body()

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    sender_email: str = (data.get("FromFull") or {}).get("Email") or data.get("From", "")
    subject: str = data.get("Subject", "")
    text_body: str = data.get("TextBody") or data.get("HtmlBody", "")

    if not sender_email or not text_body:
        logger.warning("Inbound webhook missing sender or body — ignoring")
        return {"status": "ignored", "reason": "missing_sender_or_body"}

    # Strip quoted reply history (everything after first '---' or 'On ... wrote:')
    clean_body = _strip_quoted_reply(text_body)

    # Log the inbound reply (invoice_id resolved by orchestrator)
    reply_id = invoice_db.record_inbound_reply(
        invoice_id=None,  # resolved by orchestrator
        from_email=sender_email,
        body_text=clean_body,
        subject=subject,
    )

    # Process via AR orchestrator (F-01)
    try:
        from agent.orchestrator import handle_inbound
        result = handle_inbound(
            email_body=clean_body,
            sender_email=sender_email,
            subject=subject,
        )
        # Update reply record with classification
        classification = result.get("classification", {})
        if classification.get("category"):
            invoice_db.update_reply_classification(
                reply_id=reply_id,
                classification=classification["category"],
                category_name=classification.get("name", ""),
                confidence=classification.get("confidence") or 0.0,
                action_taken=result.get("action_taken", ""),
            )
        return {"status": "processed", "interaction_id": result.get("interaction_id")}
    except Exception as exc:
        logger.error("Inbound processing failed for %s: %s", sender_email, exc, exc_info=True)
        # Don't expose internal errors to Postmark — return 200 so Postmark doesn't retry
        return {"status": "error_logged"}


# ── Slack interactive actions (F-20/F-21) ────────────────────────────────────

@app.post("/slack/actions")
async def slack_actions(request: Request):
    """
    Handle Slack Block Kit interactive payloads (Approve/Skip button clicks
    from shadow queue messages).
    """
    if not _verify_slack_signature(request, await request.body()):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    payload_str = form.get("payload", "")
    if not payload_str:
        return Response(status_code=200)

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        return Response(status_code=200)

    if payload.get("type") == "block_actions":
        user = payload.get("user", {})
        user_id = user.get("id", "")
        user_name = user.get("name", user_id)

        for action in payload.get("actions", []):
            action_id = action.get("action_id", "")
            value = action.get("value", "")
            from ar_agent.slack_bot.command_handler import handle_block_action
            response_text = handle_block_action(action_id, value, user_id, user_name)
            logger.info("Block action %s by %s: %s", action_id, user_name, response_text)

    return Response(status_code=200)


# ── Slack slash command (F-20) ────────────────────────────────────────────────

@app.post("/slack/commands")
async def slack_commands(request: Request):
    """Handle /ar slash commands from Slack."""
    if not _verify_slack_signature(request, await request.body()):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    command = form.get("command", "").lstrip("/")
    text = form.get("text", "")
    user_id = form.get("user_id", "")
    user_name = form.get("user_name", user_id)

    from ar_agent.slack_bot.command_handler import handle_command
    response_text = handle_command(command, text, user_id, user_name)

    return {"response_type": "ephemeral", "text": response_text}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_inbound_auth(request: Request) -> bool:
    """
    HTTP Basic Auth for the Postmark inbound webhook.

    Postmark supports credentials embedded in the webhook URL
    (https://user:pass@host/webhook/inbound) and sends them as an
    Authorization header. Fails closed unless ENVIRONMENT=dev, matching
    the Slack signature policy.
    """
    user = os.environ.get("POSTMARK_WEBHOOK_USER", "")
    password = os.environ.get("POSTMARK_WEBHOOK_PASSWORD", "")
    if not user or not password:
        if os.environ.get("ENVIRONMENT", "") == "dev":
            logger.warning("POSTMARK_WEBHOOK_USER/PASSWORD not set — accepting inbound (ENVIRONMENT=dev)")
            return True
        logger.error("POSTMARK_WEBHOOK_USER/PASSWORD not set — rejecting inbound webhook")
        return False

    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        supplied_user, _, supplied_password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False

    # Constant-time comparison on both halves
    return (
        hmac.compare_digest(supplied_user, user)
        and hmac.compare_digest(supplied_password, password)
    )


def _verify_slack_signature(request: Request, body: bytes) -> bool:
    """Verify the X-Slack-Signature header."""
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        # Fail closed unless the environment is EXPLICITLY marked dev —
        # a prod deployment missing the secret must not accept requests.
        if os.environ.get("ENVIRONMENT", "") == "dev":
            logger.warning("SLACK_SIGNING_SECRET not set — accepting request (ENVIRONMENT=dev)")
            return True
        logger.error("SLACK_SIGNING_SECRET not set — rejecting Slack request")
        return False

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    sig_header = request.headers.get("X-Slack-Signature", "")

    # Guard against replay attacks (5-minute window)
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    base_string = f"v0:{timestamp}:{body.decode()}"
    computed = "v0=" + hmac.new(
        signing_secret.encode(),
        base_string.encode(),
        hashlib.sha256,
    ).hexdigest()  # type: ignore[attr-defined]
    return hmac.compare_digest(computed, sig_header)


def _strip_quoted_reply(text: str) -> str:
    """Remove quoted email history to get only the customer's new content."""
    lines = text.splitlines()
    clean = []
    for line in lines:
        stripped = line.strip()
        # Common quoted-reply indicators
        if stripped.startswith(">") or stripped.startswith("On ") and "wrote:" in stripped:
            break
        if "-----Original Message-----" in stripped:
            break
        clean.append(line)
    return "\n".join(clean).strip()
