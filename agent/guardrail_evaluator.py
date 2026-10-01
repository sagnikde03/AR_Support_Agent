"""
AR Guardrail Evaluator

Since this is an internal tool used by the CapLinked team (not customers),
guardrails are simpler:

  AUTO_RESPOND  — AR/billing question answerable from ticket history
  ESCALATE      — needs human judgment or outside scope
  HARD_STOP     — sensitive customer PII or legal content
"""

import logging
import os
import re

import anthropic

logger = logging.getLogger(__name__)

HARD_STOP_PATTERNS = [
    r"\b(credit\s+card\s+number|cvv|ssn|social\s+security)\b",
    r"\b(lawsuit|litigation|subpoena|legal\s+action)\b",
    r"\b(data\s+breach|unauthorized\s+access)\b",
]

ESCALATE_PATTERNS = [
    r"\b(refund|chargeback|dispute)\b",
    r"\b(contract\s+(modification|change|amendment)|custom\s+terms)\b",
    r"\b(write\s+off|bad\s+debt|collection\s+agency)\b",
]


def evaluate(query: str, has_context: bool = True) -> dict:
    q = query.lower().strip()

    for pattern in HARD_STOP_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            return {
                "decision":    "HARD_STOP",
                "reason":      f"Sensitive content: {pattern}",
                "escalate_to": ["Alex Pierman <apierman@caplinked.com>"],
                "ack_message": "This requires immediate human review — flagged.",
            }

    for pattern in ESCALATE_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            return {
                "decision":    "ESCALATE",
                "reason":      f"Requires human judgment: {pattern}",
                "escalate_to": ["Brie (Administrative Manager)", "Leila (Director of Operations)"],
                "ack_message": "This falls outside what I can advise on — please check with the team.",
            }

    if not has_context:
        return {
            "decision":    "ESCALATE",
            "reason":      "No relevant AR ticket history found",
            "escalate_to": ["Brie (Administrative Manager)", "Leila (Director of Operations)"],
            "ack_message": "I don't have relevant ticket history for this — no match in the AR knowledge base.",
        }

    return {
        "decision":    "AUTO_RESPOND",
        "reason":      "AR question with matching ticket history",
        "escalate_to": [],
        "ack_message": None,
    }


def check_response(answer: str) -> dict | None:
    """
    Stage 2 guardrail — uses an LLM to check whether the drafted response
    requires sending any attachment, link, document, or form.
    If yes, escalate — the agent cannot send secure files.
    Returns an escalation dict if flagged, None if clean.
    """
    prompt = (
        "Does the following email response imply or require sending an attachment, "
        "document, file, form, or link to the customer? "
        "Answer YES or NO only.\n\n"
        f"Response:\n{answer}"
    )

    if "ANTHROPIC_API_KEY" not in os.environ:
        logger.warning("Stage 2 guardrail inactive — ANTHROPIC_API_KEY not set; escalating to be safe")
        verdict = "YES"
    else:
        try:
            client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            result = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=5,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            verdict = result.content[0].text.strip().upper()
        except Exception:
            verdict = "YES"

    if verdict.startswith("YES"):
        return {
            "decision":    "ESCALATE",
            "reason":      "Stage 2: response requires sending a document/link — needs human action",
            "escalate_to": ["Brie (Administrative Manager)", "Leila (Director of Operations)"],
            "ack_message": (
                "Hi there,\n\nThank you for reaching out. "
                "Someone from our team will follow up with you shortly.\n\n"
                "CapLinked Billing Team"
            ),
        }
    return None
