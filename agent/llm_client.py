"""
Shared LLM client factory for the AR Agent.

Provides lazily-initialised singleton clients for:
  - Anthropic direct API  (primary)
  - Anthropic Bedrock     (fallback)

Two model tiers:
  - Orchestrator : claude-opus-4-7         (routing, classification, intent)
  - Executor     : claude-sonnet-4-6        (response drafting, classification)

Mirrors the pattern in Support-Agent/agent/llm_client.py.
"""

import logging
import os

import anthropic

logger = logging.getLogger(__name__)

_DEFAULT_ANTHROPIC_ORCHESTRATOR = "claude-opus-4-7"
_DEFAULT_ANTHROPIC_EXECUTOR     = "claude-sonnet-4-6"

_DEFAULT_BEDROCK_ORCHESTRATOR   = "us.anthropic.claude-opus-4-7-20250514-v1:0"
_DEFAULT_BEDROCK_EXECUTOR       = "us.anthropic.claude-sonnet-4-6-20250514-v1:0"


def _anthropic_orchestrator_model() -> str:
    return os.environ.get("ANTHROPIC_ORCHESTRATOR_MODEL", _DEFAULT_ANTHROPIC_ORCHESTRATOR)


def _anthropic_executor_model() -> str:
    return os.environ.get("ANTHROPIC_EXECUTOR_MODEL", _DEFAULT_ANTHROPIC_EXECUTOR)


def _bedrock_orchestrator_model() -> str:
    return os.environ.get("BEDROCK_ORCHESTRATOR_MODEL_ID", _DEFAULT_BEDROCK_ORCHESTRATOR)


def _bedrock_executor_model() -> str:
    return os.environ.get("BEDROCK_EXECUTOR_MODEL_ID", _DEFAULT_BEDROCK_EXECUTOR)


_anthropic_client: anthropic.Anthropic | None = None
_bedrock_client: anthropic.AnthropicBedrock | None = None


def get_anthropic() -> anthropic.Anthropic | None:
    global _anthropic_client
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=api_key, max_retries=4)
    return _anthropic_client


def get_bedrock() -> anthropic.AnthropicBedrock | None:
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


def orchestrator_model(provider: str) -> str:
    return _anthropic_orchestrator_model() if provider == "anthropic" else _bedrock_orchestrator_model()


def executor_model(provider: str) -> str:
    return _anthropic_executor_model() if provider == "anthropic" else _bedrock_executor_model()


def active_provider() -> str:
    if get_anthropic() is not None:
        return "anthropic"
    if get_bedrock() is not None:
        return "bedrock"
    raise RuntimeError(
        "No LLM provider available. Set ANTHROPIC_API_KEY or AWS credentials in .env"
    )
