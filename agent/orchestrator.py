"""
AR Agent Orchestrator — Claude Managed Agents pattern (F-01).

Handles inbound customer replies to AR/billing emails. Uses claude-opus-4-7
as the parent orchestrator with three tools:

  get_invoice_context   → reads state engine + outreach history  (no LLM)
  classify_reply        → Sonnet-4.6 structured classifier        (LLM)
  execute_ar_action     → routes decision to action handlers      (no LLM)

The orchestrator reasons dynamically about each inbound reply:
  - Payment confirmation  → stop dunning, update state, acknowledge
  - P2P commitment        → record promise, acknowledge
  - Routine billing FAQ   → draft response via Postmark
  - Escalation triggers   → notify correct human via Slack
  - Sales/activation      → forward to Greg
  - Unclassifiable        → escalate to Leila/Brie

Called by api/main.py when a Postmark inbound webhook fires.
"""

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

from agent.llm_client import (
    active_provider,
    executor_model,
    get_anthropic,
    get_bedrock,
    orchestrator_model,
)
from ar_agent.agent import decision_engine, escalation_engine, response_classifier
from ar_agent.state import invoice_db

LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger(__name__)
_log_lock = threading.Lock()

# ── Orchestrator system prompt ────────────────────────────────────────────────

_ORCHESTRATOR_SYSTEM = """You are the CapLinked AR Agent Orchestrator. You process inbound customer replies to accounts-receivable and billing emails. Your job is to coordinate three tools to understand each reply and take the right action.

YOUR DECISION PROCESS

For every inbound customer email you receive, work through the following:

1. First, call get_invoice_context with the sender's email address to retrieve their invoice status, outstanding balance, outreach history, and any promise-to-pay records.

2. Then call classify_reply with the email body and the invoice context to determine what the customer is communicating (payment confirmation, P2P commitment, dispute, escalation request, routine billing question, etc).

3. Finally, call execute_ar_action with the classification result and invoice ID to take the appropriate action (update state, send acknowledgment, escalate to human, route to Greg for sales).

IMPORTANT RULES
- Always get invoice context first — never classify without it.
- If get_invoice_context returns no matching invoice, still call classify_reply, then escalate.
- Never attempt to draft a payment, issue a refund, modify invoice amounts, or take any action involving money movement. Those are always human.
- W9 requests always escalate to Leila/Brie — never handle autonomously.
- If classification confidence is below 0.6, escalate rather than act.

After all tool calls, output a brief one-sentence summary of the action taken. This is logged internally — it is not customer-facing."""

# ── Tool definitions ──────────────────────────────────────────────────────────

_TOOLS: list[dict] = [
    {
        "name": "get_invoice_context",
        "description": (
            "Retrieve the invoice and account context for a customer by their email address. "
            "Returns invoice status, amount, days overdue, cadence stage, outreach history, "
            "promise-to-pay records, and contact tier. Always call this first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "The sender's email address from the inbound reply.",
                }
            },
            "required": ["email"],
        },
    },
    {
        "name": "classify_reply",
        "description": (
            "Classify an inbound customer reply into one of nine AR categories: "
            "(1) in-conversation payment confirmation, (2) promise-to-pay commitment, "
            "(3) dispute or line-item query, (4) escalation/cancellation request, "
            "(5) partial payment acknowledgement, (6) payment portal or PO# request, "
            "(7) routine billing FAQ (autopay setup, payment instructions, past invoice), "
            "(8) out-of-office / no meaningful response, (9) sales or activation inquiry. "
            "Returns category number, confidence score, and any extracted details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_body": {
                    "type": "string",
                    "description": "The full text of the customer's reply email.",
                },
                "invoice_context": {
                    "type": "string",
                    "description": "JSON string of invoice context from get_invoice_context.",
                },
            },
            "required": ["email_body", "invoice_context"],
        },
    },
    {
        "name": "execute_ar_action",
        "description": (
            "Execute the appropriate AR action based on the classification result. "
            "Updates invoice state, queues or sends a reply email, or routes an escalation "
            "to the correct human via Slack. Returns a summary of the action taken."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "string",
                    "description": "The invoice ID from the context (or 'unknown' if not found).",
                },
                "classification": {
                    "type": "string",
                    "description": "JSON string of the classification result from classify_reply.",
                },
            },
            "required": ["invoice_id", "classification"],
        },
    },
]


# ── Tool execution ─────────────────────────────────────────────────────────────

def _run_get_invoice_context(inputs: dict) -> dict:
    email = inputs["email"]
    invoice = invoice_db.get_invoice_by_contact_email(email)
    if not invoice:
        return {"found": False, "email": email}
    contacts = invoice_db.get_contacts(invoice["account_id"])
    history = invoice_db.get_outreach_history(invoice["invoice_id"])
    p2p = invoice_db.get_p2p_record(invoice["invoice_id"])
    return {
        "found": True,
        "invoice_id": invoice["invoice_id"],
        "account_id": invoice["account_id"],
        "account_name": invoice["account_name"],
        "amount": invoice["amount_cents"] / 100,
        "currency": invoice["currency"],
        "due_date": invoice["due_date"],
        "status": invoice["status"],
        "cadence_stage": invoice["cadence_stage"],
        "attempt_count": invoice["attempt_count"],
        "last_outreach_at": invoice["last_outreach_at"],
        "days_overdue": invoice.get("days_overdue", 0),
        "contacts": contacts,
        "outreach_count": history["total_sent"],
        "last_sent_stage": history["last_stage"],
        "current_p2p_date": invoice.get("current_p2p_date"),
        "known_non_complier": invoice.get("known_non_complier", False),
        "notes": invoice.get("notes", "[]"),
    }


def _run_classify_reply(inputs: dict) -> dict:
    result = response_classifier.classify(
        email_text=inputs["email_body"],
        invoice_context=inputs["invoice_context"],
    )
    return {
        "category": result.category,
        "category_name": result.category_name,
        "confidence": result.confidence,
        "payment_signal": result.payment_signal,
        "p2p_details": result.p2p_details,
        "requires_escalation": result.requires_escalation,
        "is_w9_request": result.is_w9_request,
        "notes": result.notes,
    }


def _run_execute_ar_action(inputs: dict) -> dict:
    invoice_id = inputs["invoice_id"]
    classification_data = json.loads(inputs["classification"]) if isinstance(inputs["classification"], str) else inputs["classification"]

    if invoice_id == "unknown":
        return escalation_engine.escalate_unknown(classification_data)

    invoice = invoice_db.get_invoice(invoice_id)
    if not invoice:
        return {"action": "escalation", "reason": "Invoice not found in state engine"}

    action = decision_engine.decide(classification_data, invoice)

    # Escalation decisions must actually reach Slack — decide() only computes
    # the routing; the escalation engine delivers it.
    if action.get("action") in ("escalate", "route_greg"):
        escalation_result = escalation_engine.escalate(
            invoice=invoice,
            classification=classification_data,
            action=action,
            reason=action.get("escalation_reason", "Escalation required"),
        )
        action["escalation"] = escalation_result

    return action


_TOOL_HANDLERS = {
    "get_invoice_context": _run_get_invoice_context,
    "classify_reply":      _run_classify_reply,
    "execute_ar_action":   _run_execute_ar_action,
}


def _execute_tool(name: str, inputs: dict) -> dict:
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}
    try:
        return handler(inputs)
    except Exception as exc:
        logger.error("Tool %s failed: %s", name, exc, exc_info=True)
        return {"error": str(exc)}


# ── Agentic loop ───────────────────────────────────────────────────────────────

def _build_system_blocks() -> list[dict]:
    return [
        {
            "type": "text",
            "text": _ORCHESTRATOR_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _serialise_tool_result(result: dict) -> str:
    cleaned = {k: v for k, v in result.items() if not k.startswith("_")}
    return json.dumps(cleaned)


def _run_agentic_loop(email_body: str, sender_email: str) -> tuple[str, dict, list[str], str]:
    query = f"From: {sender_email}\n\n{email_body}"
    messages: list[dict] = [{"role": "user", "content": query}]
    tool_results: dict[str, dict] = {}
    tools_called: list[str] = []
    final_answer: str = ""

    for provider_name in ("anthropic", "bedrock"):
        client = get_anthropic() if provider_name == "anthropic" else get_bedrock()
        if client is None:
            continue

        model = orchestrator_model(provider_name)
        messages = [{"role": "user", "content": query}]
        tool_results = {}
        tools_called = []

        try:
            for _ in range(10):
                response = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=_build_system_blocks(),
                    tools=_TOOLS,
                    messages=messages,
                )

                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    for block in response.content:
                        if hasattr(block, "text"):
                            final_answer = block.text.strip()
                            break
                    return final_answer, tool_results, tools_called, provider_name

                if response.stop_reason == "tool_use":
                    tool_result_blocks = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        tools_called.append(block.name)
                        result = _execute_tool(block.name, block.input)
                        tool_results[block.name] = result
                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _serialise_tool_result(result),
                        })
                    messages.append({"role": "user", "content": tool_result_blocks})
                    continue

                logger.warning("Unexpected stop_reason: %s", response.stop_reason)
                break

            raise RuntimeError(
                f"Orchestrator loop exhausted without final answer "
                f"(provider={provider_name}, tools_called={tools_called})"
            )

        except anthropic.APIError as exc:
            if provider_name == "anthropic":
                logger.warning("Anthropic orchestrator failed (%s) — falling back to Bedrock", exc)
                continue
            raise

    raise RuntimeError("No LLM provider available. Set ANTHROPIC_API_KEY or AWS credentials.")


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(record: dict) -> None:
    log_file = LOG_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    with _log_lock:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


# ── Public interface ──────────────────────────────────────────────────────────

def handle_inbound(email_body: str, sender_email: str, subject: str = "") -> dict:
    """
    Process an inbound customer reply end-to-end.

    Args:
        email_body   : full text of the customer's reply
        sender_email : Reply-To or From address
        subject      : email subject (for logging)

    Returns:
        interaction_id, action_taken, invoice_id, classification, provider
    """
    interaction_id = str(uuid.uuid4())[:8]
    # Logs carry a deterministic sender hash for correlation — never the raw
    # email address, subject, or account name (customer-identifying data).
    sender_hash = hashlib.sha256(sender_email.lower().strip().encode()).hexdigest()[:16]
    log: dict = {
        "interaction_id": interaction_id,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "channel":        "inbound_email",
        "sender_hash":    sender_hash,
        "body_chars":     len(email_body),
    }

    # ── Hard rule: W9 / vendor tax form requests never take the LLM path ──────
    # Deterministic guard on the raw request, before any classification.
    if decision_engine.is_w9_request(email_body) or decision_engine.is_w9_request(subject):
        w9_classification = {"category": 6, "category_name": "w9_request", "confidence": 1.0}
        w9_action = {
            "action": "escalate",
            "escalation_reason": "W9 / vendor tax form request — always handled by a human",
            "escalate_to": ["Leila", "Brie"],
            "notes": "Deterministic W9 guard (pre-classification)",
        }
        invoice = invoice_db.get_invoice_by_contact_email(sender_email)
        if invoice:
            escalation_result = escalation_engine.escalate(
                invoice=invoice, classification=w9_classification,
                action=w9_action, reason=w9_action["escalation_reason"],
            )
        else:
            escalation_result = escalation_engine.escalate_unknown(w9_classification)
        log.update({
            "invoice_id":   invoice["invoice_id"] if invoice else "unknown",
            "action_taken": "escalate",
            "w9_guard":     True,
            "provider":     "none",
            "escalation_posted": escalation_result.get("posted"),
        })
        _log(log)
        return {
            "interaction_id": interaction_id,
            "action_taken":   "escalate",
            "invoice_id":     invoice["invoice_id"] if invoice else "unknown",
            "classification": {"category": 6, "name": "w9_request", "confidence": 1.0},
            "provider":       "none",
        }

    try:
        summary, tool_results, tools_called, provider = _run_agentic_loop(email_body, sender_email)
    except Exception as exc:
        # Every failure gets an audit record — not only RuntimeError
        logger.error("AR orchestrator loop failed: %s", exc, exc_info=True)
        log.update({
            "provider": "none",
            "tools_called": [],
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        _log(log)
        raise

    context_result = tool_results.get("get_invoice_context", {})
    classify_result = tool_results.get("classify_reply", {})
    action_result = tool_results.get("execute_ar_action", {})

    log.update({
        "invoice_id":     context_result.get("invoice_id", "unknown"),
        "classification": classify_result.get("category"),
        "category_name":  classify_result.get("category_name"),
        "confidence":     classify_result.get("confidence"),
        "action_taken":   action_result.get("action"),
        "tools_called":   tools_called,
        "provider":       provider,
        "summary":        summary,
        "orchestrator_model": orchestrator_model(provider),
        "executor_model":     executor_model(provider),
    })
    _log(log)

    return {
        "interaction_id": interaction_id,
        "action_taken":   action_result.get("action", "unknown"),
        "invoice_id":     context_result.get("invoice_id", "unknown"),
        "classification": {
            "category":     classify_result.get("category"),
            "name":         classify_result.get("category_name"),
            "confidence":   classify_result.get("confidence"),
        },
        "provider": provider,
    }
