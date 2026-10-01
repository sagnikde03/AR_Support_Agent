"""
AR Response Drafter
Generates answers from historical AR/billing ticket resolutions.
Uses claude-haiku-4-5 (Anthropic) with Bedrock fallback.
"""

import logging
import os
import re

import anthropic

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL      = "claude-haiku-4-5"
DEFAULT_BEDROCK_MODEL = "anthropic.claude-3-haiku-20240307-v1:0"

SYSTEM_PROMPT = """You are an AR assistant for the CapLinked billing team. The team asks you a question and you draft a professional email reply they can send to the customer.

Use the provided historical ticket resolutions as context to understand how similar situations were handled — but write a fresh response directly addressing what was asked. Do not copy paste from the tickets.

RULES:
1. Use ONLY the context from historical resolutions to inform your answer. Never invent facts.
2. If the context does not contain enough information to answer, respond with exactly: INSUFFICIENT_CONTEXT
3. Format as a short professional email: start with "Hi there," — never use names from the tickets. End with "CapLinked Billing Team". No subject line.
4. Answer only what was asked. Do not add information the customer did not ask for.
5. Keep it concise — 2 to 4 sentences in the body maximum.
6. NEVER mention customer names, company names, or project names from the tickets."""

_anthropic_client: anthropic.Anthropic | None = None
_bedrock_client: anthropic.AnthropicBedrock | None = None


def _get_anthropic() -> anthropic.Anthropic | None:
    global _anthropic_client
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=api_key, max_retries=4)
    return _anthropic_client


def _get_bedrock() -> anthropic.AnthropicBedrock | None:
    global _bedrock_client
    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        return None
    if _bedrock_client is None:
        _bedrock_client = anthropic.AnthropicBedrock(
            aws_access_key=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        )
    return _bedrock_client


_PREAMBLE_RE = re.compile(
    r"^(based on (the (historical|ticket|past)|our)|according to (the|our)|from the (context|tickets?))[^.]*[,:\s]+",
    re.IGNORECASE,
)


def _strip_preamble(text: str) -> str:
    text = _PREAMBLE_RE.sub("", text).lstrip()
    return text[0].upper() + text[1:] if text else text


def draft(query: str, context_text: str) -> dict:
    user_message = (
        f"Historical AR ticket resolutions:\n\n{context_text}\n\n"
        f"---\n\nQuestion: {query}"
    )

    client = _get_anthropic()
    if client:
        try:
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1024,
                temperature=0,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_message}],
            )
            text = _strip_preamble(next((b.text for b in response.content if b.type == "text"), ""))
            return {
                "answer": text,
                "insufficient_context": text.strip().startswith("INSUFFICIENT_CONTEXT"),
                "provider": "anthropic",
            }
        except anthropic.APIError as e:
            logger.warning("Anthropic failed (%s) — falling back to Bedrock", e)

    bedrock = _get_bedrock()
    if bedrock:
        response = bedrock.messages.create(
            model=os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL),
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = _strip_preamble(next((b.text for b in response.content if b.type == "text"), ""))
        return {
            "answer": text,
            "insufficient_context": text.strip().startswith("INSUFFICIENT_CONTEXT"),
            "provider": "bedrock",
        }

    raise RuntimeError("No LLM provider available. Set ANTHROPIC_API_KEY in .env")
