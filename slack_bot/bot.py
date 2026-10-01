"""
CapLinked AR Agent — Slack Bot (Socket Mode)

Internal tool for the billing team to query historical AR ticket resolutions.

Start with:
    python slack_bot/bot.py

Requires in .env:
    SLACK_BOT_TOKEN   (xoxb-...)
    SLACK_APP_TOKEN   (xapp-...)
"""

import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from agent.orchestrator import handle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])

DECISION_EMOJI = {
    "AUTO_RESPOND": "✅",
    "ESCALATE":     "⚠️",
    "HARD_STOP":    "🚫",
}


def format_response(result: dict) -> list[dict]:
    decision = result["guardrail"]["decision"]
    emoji    = DECISION_EMOJI.get(decision, "❓")
    blocks   = []

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": result["answer"]},
    })

    blocks.append({"type": "divider"})

    status_text = f"{emoji} *{decision.replace('_', ' ').title()}*"
    if result["guardrail"]["escalate_to"]:
        status_text += "\n>👤 Notify: " + ", ".join(result["guardrail"]["escalate_to"])

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": status_text},
    })

    if result["sources"]:
        source_lines = [
            f"• 🎫 <{s['url']}|Ticket #{s['url'].split('/')[-1]}>"
            if s.get("url") else f"• 🎫 Past resolution {i+1}"
            for i, s in enumerate(result["sources"][:5])
        ]
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "🔒 *Internal reference — do not share with client:*\n" + "\n".join(source_lines),
            }],
        })

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f"ID: `{result['interaction_id']}` · Provider: {result['provider']} · CapLinked AR Agent",
        }],
    })

    return blocks


def _process_query(query: str, say, user_id: str):
    query = query.strip()
    if not query:
        say("Hi! Ask me anything about how AR/billing cases have been handled in the past.")
        return

    say(text="⏳ Searching ticket history...", blocks=[{
        "type": "section",
        "text": {"type": "mrkdwn", "text": "⏳ _Searching ticket history..._"},
    }])

    try:
        result = handle(query=query, channel="slack", user_id=user_id)
        say(text=result["answer"], blocks=format_response(result))
    except Exception:
        logger.exception("Error handling query: %s", query)
        say("Sorry, something went wrong. Check the logs.")


@app.event("message")
def handle_dm(event, say):
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") != "im":
        return
    _process_query(event.get("text", ""), say, event.get("user", "unknown"))


@app.event("app_mention")
def handle_mention(event, say):
    text  = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
    _process_query(text, say, event.get("user", "unknown"))


if __name__ == "__main__":
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        print("ERROR: SLACK_APP_TOKEN not set in .env")
        sys.exit(1)

    print("Starting CapLinked AR Agent Slack bot...")
    SocketModeHandler(app, app_token).start()
