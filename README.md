# AR Support Agent 

Internal AI assistant for the company's billing and accounts receivable team.
Answers AR questions by retrieving context from historical Zendesk billing tickets
and past email threads, then generating a grounded response using Claude.
Sensitive or document-dependent queries are escalated to Brie and Leila — never
auto-answered.

This is an **internal-only tool** accessed via Slack. It is not customer-facing.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Repository Structure](#2-repository-structure)
3. [Setup](#3-setup)
4. [Module 1 — Knowledge Pipeline](#4-module-1--knowledge-pipeline)
   - [Ticket Processing](#41-ticket-processing)
   - [AR Tag Filtering — Full Reference](#42-ar-tag-filtering--full-reference)
   - [Email Processing (.mbox)](#43-email-processing-mbox)
   - [Running the Sync](#44-running-the-sync)
5. [Module 2 — Agent Pipeline](#5-module-2--agent-pipeline)
   - [Guardrail Decisions](#51-guardrail-decisions)
   - [Stage 2 — LLM Response Check](#52-stage-2--llm-response-check)
   - [Escalation Routing](#53-escalation-routing)
6. [Running the Slack Bot](#6-running-the-slack-bot)
7. [Logs](#7-logs)
8. [Updating the Knowledge Base](#8-updating-the-knowledge-base)

---

## 1. Architecture Overview

```
                     ┌──────────────────────────────────────┐
                     │     Module 1: Knowledge Pipeline      │  (run as needed)
                     │                                       │
  raw_data/          │  ticket_processor.py                  │
  (NDJSON exports) ──►                                       │──► ChromaDB
                     │  email_processor.py                   │    (data/chroma_db)
  raw_data/          │  (.mbox files)                        │
  (mbox exports)  ───►                                       │
                     │  indexer.py (incremental embed)       │
                     └──────────────────────────────────────┘

                     ┌──────────────────────────────────────┐
                     │     Module 2: Agent Pipeline          │  (always-on)
                     │                                       │
  Slack (internal) ──►  orchestrator.py                     │
                     │    │                                  │
                     │    ├─ knowledge_retrieval.py ─────────►  ChromaDB
                     │    ├─ guardrail_evaluator.py (Stage 1)│
                     │    ├─ response_drafter.py ────────────►  Claude (Anthropic / Bedrock)
                     │    └─ guardrail_evaluator.py (Stage 2)│
                     │                                       │
                     └──────────────────┬────────────────────┘
                                        │
                                  logs/YYYY-MM-DD.jsonl
```

The two modules are independent. The knowledge pipeline can be re-synced while the
agent is live — they share the same ChromaDB directory, and ChromaDB handles
concurrent reads safely.

---

## 2. Repository Structure

```
AR-Agent/
├── agent/                        # Module 2 — always-on agent
│   ├── orchestrator.py           # Entry point: handle(query) → dict
│   ├── guardrail_evaluator.py    # Stage 1 (regex) + Stage 2 (LLM check)
│   ├── knowledge_retrieval.py    # ChromaDB semantic search
│   └── response_drafter.py       # Claude API call + Bedrock fallback
│
├── knowledge_pipeline/           # Module 1 — sync as needed
│   ├── ticket_processor.py       # Filters + extracts AR tickets from NDJSON
│   ├── email_processor.py        # Parses .mbox files, filters noise
│   ├── indexer.py                # Incremental embed + upsert into ChromaDB
│   └── run_sync.py               # Entry point: runs full pipeline
│
├── slack_bot/
│   └── bot.py                    # Slack Socket Mode bot (internal channel)
│
├── data/
│   ├── manifest.json             # Content hash map for incremental sync
│   └── chroma_db/                # ChromaDB vector store
│
├── raw_data/                     # Drop Zendesk NDJSON exports + .mbox files here
├── logs/                         # Daily JSONL interaction logs
├── .env                          # API keys and tokens
└── requirements.txt
```

---

## 3. Setup

### Prerequisites
- Python 3.11+
- Virtual environment at `.venv/`

### Install dependencies
```bash
.venv/Scripts/pip install -r requirements.txt
```

### Configure `.env`
```bash
# Anthropic (primary LLM — required)
ANTHROPIC_API_KEY=sk-ant-...

# Amazon Bedrock (fallback — optional)
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-west-2
BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

---

## 4. Module 1 — Knowledge Pipeline

Builds and maintains the vector knowledge base from two sources:
- **Zendesk NDJSON exports** — AR/billing ticket history
- **.mbox files** — Accounts Receivable, Billing, and Past Due-Collections email threads

Run this before starting the agent for the first time, and whenever new exports are available.

### 4.1 Ticket Processing

The raw Zendesk NDJSON export contains tickets from all departments. The processor
(`knowledge_pipeline/ticket_processor.py`) keeps only AR/billing-relevant tickets
using an inverse filter — it **keeps** tickets that have any AR tag, and drops everything
else.

```
Raw NDJSON export
    │
    ▼
[Filter 1] Tag-based check — keep only AR tags  ──DROP──► support/product tickets
    │
    ▼
[Filter 2] Subject-line check (secondary catch)  ──DROP──► billing noise without tags
    │
    ▼
[Filter 3] Content quality check                 ──DROP──► too short to be useful
    │
    ▼
 Indexed as document
```

### 4.2 AR Tag Filtering — Full Reference

The processor keeps tickets whose tags match any of these (substring or exact):

#### Tags KEPT (substring match on tag prefix)

| Prefix | Example tags caught | Topic |
|---|---|---|
| `billing` | `billing`, `billing_-_enterprise_` | General billing |
| `invoice` | `invoice` | Invoice requests |
| `payment` | `payment_sent_confirmation` | Payment processing |
| `receipt` | `receipt_/_payment` | Payment receipts |
| `remittance` | `remittance` | Remittance advices |
| `accounts_receivable` | `accounts_receivable` | AR department |
| `cancel_subscription` | `cancel_subscription` | Subscription cancellation |
| `reactive_subscription` | `reactive_subscription` | Reactivation requests |
| `upgrade_request` | `upgrade_request` | Plan upgrades |
| `price_increase` | `price_increase_complaint_self-serve` | Price change complaints |
| `trial_extension` | `trial_extension` | Trial extension requests |
| `operations/account_management_` | `operations/account_management_*` | Account management ops |

#### Tags KEPT (exact match only)

These are short enough to cause false matches if used as substrings:

| Tag | Meaning |
|---|---|
| `ar` | Accounts Receivable department |
| `sales` | Sales team tickets |

> **To add a new AR tag:** Add its prefix to `KEEP_TAG_PREFIXES` or add it to
> `KEEP_TAG_EXACT` in `knowledge_pipeline/ticket_processor.py`, then run `--reset`.

### 4.3 Email Processing (.mbox)

Three `.mbox` files are processed from `raw_data/`:

| File | Description | Approx. kept |
|---|---|---|
| `Accounts Receivable.mbox` | Real AR email threads — small, high quality | ~3 |
| `Billing Zendesk Ticket.mbox` | invoices@ mailbox — remittances, payment notices | varies |
| `Past Due-Collections.mbox` | Past-due collection threads — Brie/Leila replies + customer responses | ~12,000 |

The email processor (`knowledge_pipeline/email_processor.py`) filters out noise before indexing:

| Filter | What it catches |
|---|---|
| Subject keywords | Satisfaction surveys, auto-replies, out-of-office, delivery failures |
| Body keywords | Zendesk system notifications, OOO messages, the standard past-due reminder template |
| Zendesk "ticket received" notifications | Short notifications with no real content |
| Minimum body length (80 chars) | Empty or near-empty replies |

Quoted reply chains (`>` lines) and Zendesk markers (`##- Please type your reply above this line -##`)
are stripped before indexing so only the actual message content is stored.

### 4.4 Running the Sync

```bash
# First-time setup or after adding new raw_data files
.venv/Scripts/python knowledge_pipeline/run_sync.py

# Wipe the entire index and rebuild from scratch
# Use this after changing tag filters or email_processor.py logic
.venv/Scripts/python knowledge_pipeline/run_sync.py --reset
```

**Incremental behaviour:** The indexer keeps a `data/manifest.json` that maps each
source document to a SHA-256 hash of its content. On each run, only new or changed
documents are re-embedded — repeated runs are fast (~5s if nothing changed).

> **Important:** After changing any filter logic, always run `--reset`. Without it,
> previously-excluded documents won't be re-evaluated against the new filters.

---

## 5. Module 2 — Agent Pipeline

Every incoming Slack message runs through a four-step pipeline:

```
query
  │
  ▼
knowledge_retrieval.py   → semantic search → top 5 AR ticket/email matches
  │
  ▼
guardrail_evaluator.py   → Stage 1 regex triage → AUTO_RESPOND / ESCALATE / HARD_STOP
  │
  ├── HARD_STOP   → return ack, notify Alex immediately
  ├── ESCALATE    → return ack, notify Brie + Leila
  └── AUTO_RESPOND
        │
        ▼
      response_drafter.py  → Claude Haiku (temp=0) → email draft grounded in context
        │
        ├── INSUFFICIENT_CONTEXT → escalate, notify Brie + Leila
        └── draft answer
              │
              ▼
            guardrail_evaluator.py  → Stage 2 LLM check (does response need a doc/link?)
              │
              ├── YES → ESCALATE, notify Brie + Leila
              └── NO  → send response to user
  │
  ▼
orchestrator.py logs interaction to logs/YYYY-MM-DD.jsonl
```

### 5.1 Guardrail Decisions

Stage 1 uses regex patterns — no LLM call, zero latency cost.

| Decision | Trigger | Action |
|---|---|---|
| `HARD_STOP` | Credit card/SSN/PII, lawsuit, litigation, subpoena, data breach | Never auto-respond. Return generic ack. Notify Alex immediately. |
| `ESCALATE` | Refund, chargeback, dispute, contract modification, write-off, bad debt, collection agency | Return ack. Notify Brie + Leila. |
| `ESCALATE` | No matching AR ticket/email context found | Return ack. Notify Brie + Leila. |
| `AUTO_RESPOND` | AR/billing question with matching history, no sensitive patterns | Generate response via Claude. |

### 5.2 Stage 2 — LLM Response Check

After drafting a response, a second LLM call (`claude-haiku-4-5`, `max_tokens=5`,
`temperature=0`) checks whether the drafted response implies sending an attachment,
document, form, or link to the customer.

- **YES** → Escalate to Brie + Leila. The agent cannot send files or payment links.
- **NO** → Send the response.

If the Stage 2 check fails for any reason (API error, timeout), it defaults to **YES**
and escalates — fail safe.

### 5.3 Escalation Routing

| Situation | Notified |
|---|---|
| HARD_STOP (PII, legal content) | Alex Pierman |
| ESCALATE (any reason) | Brie (Administrative Manager) + Leila (Director of Operations) |

Internal Slack source references (past ticket/email links) are shown only to the
internal user in a separate context block — never included in the response text
sent to the customer.

---

## 6. Running the Slack Bot

### One-time Slack App Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** →
   **From scratch** → name it `CapLinked AR Agent`

2. **Socket Mode** (left sidebar) → Enable → Generate App-Level Token with scope
   `connections:write` → copy the `xapp-...` token → add to `.env` as `SLACK_APP_TOKEN`

3. **OAuth & Permissions** → Bot Token Scopes → add:
   - `app_mentions:read`
   - `chat:write`
   - `im:history`
   - `im:read`
   - `im:write`
   - `authorizations:read`

4. **Event Subscriptions** → Enable → Subscribe to Bot Events:
   - `app_mention`
   - `message.im`

5. **Install to Workspace** → copy `xoxb-...` Bot Token → add to `.env` as `SLACK_BOT_TOKEN`

### Starting the bot

```bash
.venv/Scripts/python slack_bot/bot.py
```

### What the response looks like

**Auto-responded query:**
```
Hi there,

Thank you for reaching out. Our standard billing cycle runs on a monthly
basis. If your invoice date falls on the 15th, your next invoice will
be issued on the 15th of the following month.

CapLinked Billing Team

────────────────────────────────────────
✅ Auto-responded

🔒 Internal reference — do not share with client:
• 🎫 Ticket #52801
• 🎫 Ticket #51934

ID: `a1b2c3d4` · Provider: anthropic · CapLinked AR Agent
```

**Escalated query:**
```
Hi there,

Thank you for reaching out. I don't have enough information in our records
to answer this confidently — someone from our team will follow up shortly.

CapLinked Billing Team

────────────────────────────────────────
⚠️ Escalated for human review
👤 Notify: Brie (Administrative Manager), Leila (Director of Operations)

ID: `b2c3d4e5` · Provider: anthropic · CapLinked AR Agent
```

---

## 7. Logs

Every interaction is written to `logs/YYYY-MM-DD.jsonl` (one JSON object per line).

Sample log entry:
```json
{
  "interaction_id": "a1b2c3d4",
  "timestamp": "2026-04-17T10:23:45+00:00",
  "channel": "slack",
  "user_id": "U04CQHLB54J",
  "query": "Our automatic payment keeps failing. Do you accept ACH bank transfer?",
  "tickets_found": 5,
  "guardrail_decision": "AUTO_RESPOND",
  "guardrail_reason": "AR question with matching ticket history",
  "guardrail_final": "AUTO_RESPOND",
  "provider": "anthropic",
  "answer_chars": 380,
  "escalate_to": []
}
```

Log fields:

| Field | Description |
|---|---|
| `interaction_id` | Short UUID for cross-referencing with Slack messages |
| `tickets_found` | Number of relevant documents retrieved from ChromaDB |
| `guardrail_decision` | Stage 1 guardrail outcome |
| `guardrail_final` | Final outcome after Stage 2 check |
| `stage2_flag` | Present only when Stage 2 triggers an escalation |
| `provider` | `anthropic`, `bedrock`, or `none` (escalated before LLM call) |
| `escalate_to` | List of humans notified |

---

## 8. Updating the Knowledge Base

### New Zendesk ticket export available
1. Drop the new NDJSON file into `raw_data/`
2. Run:
   ```bash
   .venv/Scripts/python knowledge_pipeline/run_sync.py
   ```

### New .mbox file added
1. Drop the `.mbox` file into `raw_data/`
2. Run a full reset (new mbox files are not incrementally detectable):
   ```bash
   .venv/Scripts/python knowledge_pipeline/run_sync.py --reset
   ```

### Changing tag or email filter rules
1. Edit `KEEP_TAG_PREFIXES` / `KEEP_TAG_EXACT` in `ticket_processor.py`, or
   edit `SKIP_BODY_KEYWORDS` / `SKIP_SUBJECT_KEYWORDS` in `email_processor.py`
2. Run:
   ```bash
   .venv/Scripts/python knowledge_pipeline/run_sync.py --reset
   ```

> Always use `--reset` after changing filter logic. The incremental manifest tracks
> content hashes, not filter outcomes — without `--reset`, previously-excluded
> documents will not be re-evaluated.
