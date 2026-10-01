"""
Response Classifier (F-13).

Sonnet-4.6 structured-output classifier that reads an inbound customer reply
and categorises it into one of nine AR classification types.

Nine categories:
  1 — In-conversation payment confirmation (check #, wire ref, send date stated) — highest priority
  2 — Promise-to-pay commitment (specific future payment date stated)
  3 — Dispute or line-item query
  4 — Escalation request, cancellation claim, or explicit churn signal
  5 — Partial payment acknowledgement
  6 — Payment portal or PO# request
  7 — Routine billing FAQ (autopay setup, payment instructions, past invoice request)
  8 — Out-of-office or no meaningful response
  9 — Sales or account activation inquiry (route to Greg)

Fail-safe: confidence below 0.60 → treat as unclassifiable and escalate.
"""

import json
import logging
import os
from dataclasses import dataclass

import anthropic
from dotenv import load_dotenv

load_dotenv()

from agent.llm_client import executor_model, get_anthropic, get_bedrock

logger = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = float(os.environ.get("CLASSIFIER_CONFIDENCE_THRESHOLD", "0.60"))

_CLASSIFICATION_TOOL = {
    "name": "classify_ar_reply",
    "description": "Classify an inbound customer reply into the correct AR category.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "integer",
                "description": "Classification category number (1-9).",
                "minimum": 1,
                "maximum": 9,
            },
            "category_name": {
                "type": "string",
                "description": "Short name of the category.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence score 0.0-1.0. Use 0.0 if truly uncertain.",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "payment_signal": {
                "type": "object",
                "description": "Populated only for category 1. Contains check_number, wire_reference, send_date, payment_method as extracted from the text.",
                "properties": {
                    "check_number": {"type": "string"},
                    "wire_reference": {"type": "string"},
                    "send_date": {"type": "string"},
                    "payment_method": {"type": "string"},
                    "raw_quote": {"type": "string", "description": "Verbatim quote from the email confirming payment"},
                },
            },
            "p2p_details": {
                "type": "object",
                "description": "Populated only for category 2. Contains promise_date and payment_method_stated.",
                "properties": {
                    "promise_date": {"type": "string", "description": "ISO date (YYYY-MM-DD) of the promised payment"},
                    "payment_method_stated": {"type": "string"},
                    "raw_quote": {"type": "string"},
                },
            },
            "requires_escalation": {
                "type": "boolean",
                "description": "True if confidence < 0.60 or category is 4 (escalation request).",
            },
            "is_w9_request": {
                "type": "boolean",
                "description": "True if the email requests a W9/W-9 form or any vendor tax form, regardless of category. These always route to a human.",
            },
            "notes": {
                "type": "string",
                "description": "Brief reasoning note — what in the email drove this classification.",
            },
        },
        "required": ["category", "category_name", "confidence", "requires_escalation", "notes"],
    },
}

_SYSTEM_PROMPT = """You are an AR (Accounts Receivable) email classification agent for CapLinked, an enterprise SaaS company.

Your task is to read an inbound customer reply to a billing/AR email and classify it into exactly one of nine categories:

1. IN-CONVERSATION PAYMENT CONFIRMATION
   The customer explicitly states they have already paid — e.g. "I sent a check for $1,200 on Tuesday", "Wire went out this morning ref #AB1234", "Check #4021 was mailed on the 3rd." Extract: check number, wire reference, send date, payment method.

2. PROMISE-TO-PAY COMMITMENT
   The customer commits to a specific future payment date — e.g. "I'll pay by Friday", "Payment will go out next Tuesday", "Our AP will process this on the 15th." Extract the promised date and payment method if stated.

3. DISPUTE OR LINE-ITEM QUERY
   The customer disputes the invoice amount, questions line items, or claims the invoice is incorrect.

4. ESCALATION REQUEST / CANCELLATION CLAIM
   The customer asks to speak with a manager, escalates, threatens cancellation, mentions closing their account, or mentions legal action.

5. PARTIAL PAYMENT ACKNOWLEDGEMENT
   The customer acknowledges making a partial payment and explains the rest is coming.

6. PAYMENT PORTAL OR PO# REQUEST
   The customer asks how to submit payment through their company's AP portal, requests a PO number, or needs a vendor setup form.

7. ROUTINE BILLING FAQ
   The customer asks a routine billing question: how to set up autopay, how to pay (payment methods available), or requests a copy of a past invoice.

8. OUT-OF-OFFICE / NO MEANINGFUL RESPONSE
   Auto-reply, out-of-office, unsubscribe request, or a reply that contains no actionable information.

9. SALES OR ACCOUNT ACTIVATION INQUIRY
   The customer asks about upgrading, new features, pricing, or activating additional users or workspaces.

IMPORTANT RULES:
- Category 1 (payment confirmation) takes priority over all others — if the email contains both a payment confirmation AND a dispute, classify as 1.
- If confidence is below 0.60, still pick the best category but set requires_escalation = true.
- If the email requests a W9, W-9, or any vendor tax form, set is_w9_request = true no matter which category you choose — these always route to a human.
- Always call the classify_ar_reply tool — do not answer in prose."""


@dataclass
class Classification:
    category: int
    category_name: str
    confidence: float
    requires_escalation: bool
    notes: str
    payment_signal: dict | None = None
    p2p_details: dict | None = None
    is_w9_request: bool = False


_CATEGORY_NAMES = {
    1: "payment_confirmation",
    2: "p2p_commitment",
    3: "dispute",
    4: "escalation_request",
    5: "partial_payment",
    6: "portal_po_request",
    7: "billing_faq",
    8: "no_response",
    9: "sales_inquiry",
}


def classify(email_text: str, invoice_context: str | dict | None = None) -> Classification:
    """
    Classify an inbound customer reply.

    Args:
        email_text       : full text of the customer's email
        invoice_context  : invoice context dict or JSON string (optional, improves accuracy)

    Returns:
        Classification dataclass
    """
    context_note = ""
    if invoice_context:
        ctx = invoice_context if isinstance(invoice_context, str) else json.dumps(invoice_context)
        context_note = f"\n\nAccount context:\n{ctx}"

    user_message = f"Classify this inbound customer email:{context_note}\n\n---\n{email_text}\n---"

    for provider_name in ("anthropic", "bedrock"):
        client = get_anthropic() if provider_name == "anthropic" else get_bedrock()
        if client is None:
            continue

        model = executor_model(provider_name)

        try:
            response = client.messages.create(
                model=model,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                tools=[_CLASSIFICATION_TOOL],
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": user_message}],
            )

            for block in response.content:
                if block.type == "tool_use" and block.name == "classify_ar_reply":
                    # Guard against malformed tool input — any parsing failure
                    # escalates rather than escaping unhandled.
                    try:
                        data = block.input
                        category = int(data["category"])
                        confidence = float(data.get("confidence", 0.0))
                    except (KeyError, TypeError, ValueError) as exc:
                        logger.warning("Malformed classifier tool input (%s) — escalating", exc)
                        return _fallback_escalate(email_text)

                    payment_signal = data.get("payment_signal")
                    if not isinstance(payment_signal, dict):
                        payment_signal = None
                    p2p_details = data.get("p2p_details")
                    if not isinstance(p2p_details, dict):
                        p2p_details = None

                    # Apply fail-safe threshold
                    requires_escalation = bool(data.get("requires_escalation", False))
                    if confidence < _CONFIDENCE_THRESHOLD:
                        requires_escalation = True

                    return Classification(
                        category=category,
                        category_name=data.get("category_name", _CATEGORY_NAMES.get(category, "unknown")),
                        confidence=confidence,
                        requires_escalation=requires_escalation,
                        notes=data.get("notes", ""),
                        payment_signal=payment_signal,
                        p2p_details=p2p_details,
                        is_w9_request=bool(data.get("is_w9_request", False)),
                    )

            logger.warning("Classifier did not return a tool call — escalating")
            return _fallback_escalate(email_text)

        except anthropic.APIError as exc:
            if provider_name == "anthropic":
                logger.warning("Anthropic classifier failed (%s) — falling back to Bedrock", exc)
                continue
            raise

    return _fallback_escalate(email_text)


def _fallback_escalate(email_text: str) -> Classification:
    """Used when no LLM provider is available — always escalates to be safe."""
    return Classification(
        category=4,
        category_name="escalation_request",
        confidence=0.0,
        requires_escalation=True,
        notes="Fallback: no LLM provider available — escalating to human.",
    )
