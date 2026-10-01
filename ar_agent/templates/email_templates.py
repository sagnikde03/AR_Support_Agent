"""
Email Template Library (F-05).

Templates for all AR outbound email scenarios. Hard rules applied to every
template via the render() helper:
  - Subject format: "{account_name} — Invoice #{invoice_number}"
  - CC Leila (enforced in postmark_client.py, not here)
  - FreshBooks payment link in body
  - Invoice PDF attached (handled by reminder_actions.py via postmark_client)
  - Sign-off: "CapLinked Billing Team"

NOTE: Body copy is placeholder text matching the tone and structure of
Caplinked's existing AR emails. Brie to review and update wording before
shadow mode goes live. Mark each template with # BRIE_REVIEW to track.
"""

from dataclasses import dataclass


@dataclass
class EmailTemplate:
    subject: str
    html_body: str
    text_body: str
    template_name: str


# ── Shared elements ───────────────────────────────────────────────────────────

_SIGN_OFF_HTML = """
<br><br>
<p>Best regards,<br>
<strong>CapLinked Billing Team</strong><br>
<a href="https://www.caplinked.com">caplinked.com</a></p>
"""

_SIGN_OFF_TEXT = "\n\nBest regards,\nCapLinked Billing Team\ncaplinked.com"


def _payment_link_html(payment_link: str) -> str:
    if not payment_link:
        return ""
    return f'<p><a href="{payment_link}" style="background:#1E5C97;color:#fff;padding:10px 20px;border-radius:4px;text-decoration:none;display:inline-block;">Pay Invoice Online</a></p>'


def _payment_link_text(payment_link: str) -> str:
    if not payment_link:
        return ""
    return f"\nPay online: {payment_link}\n"


_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "CAD": "CA$",
    "AUD": "A$",
}


def _amount_display(amount: float, currency: str = "USD") -> str:
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")
    return f"{symbol}{amount:,.2f}"


# ── Stage 1: 7 days before due ────────────────────────────────────────────────

def reminder_7_days_before_due(
    account_name: str,
    invoice_number: str,
    amount: float,
    due_date: str,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>This is a friendly reminder that invoice <strong>#{invoice_number}</strong> for <strong>{amount_str}</strong> is due on <strong>{due_date}</strong>.</p>
<p>Please find the invoice attached to this email for your records. You can also pay online using the link below:</p>
{_payment_link_html(payment_link)}
<p>If you have any questions about this invoice or need to set up autopay, please reply to this email and we'll be happy to help.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

This is a friendly reminder that invoice #{invoice_number} for {amount_str} is due on {due_date}.

Please find the invoice attached to this email for your records.
{_payment_link_text(payment_link)}
If you have any questions or need to set up autopay, please reply to this email.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="reminder_7_days_before_due")


# ── Stage 2: Due today ────────────────────────────────────────────────────────

def reminder_due_today(
    account_name: str,
    invoice_number: str,
    amount: float,
    due_date: str,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>Invoice <strong>#{invoice_number}</strong> for <strong>{amount_str}</strong> is due <strong>today, {due_date}</strong>.</p>
<p>Please find the invoice attached. You can pay online here:</p>
{_payment_link_html(payment_link)}
<p>If payment has already been sent, please ignore this message. If you have any questions, reply to this email.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Invoice #{invoice_number} for {amount_str} is due today, {due_date}.

Please find the invoice attached.
{_payment_link_text(payment_link)}
If payment has already been sent, please ignore this message.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="reminder_due_today")


# ── Stage 3: 7 days overdue ───────────────────────────────────────────────────

def overdue_7(
    account_name: str,
    invoice_number: str,
    amount: float,
    days_overdue: int,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>We wanted to follow up regarding invoice <strong>#{invoice_number}</strong> for <strong>{amount_str}</strong>, which is now <strong>{days_overdue} days past due</strong>.</p>
<p>Please find the invoice attached. If you have already arranged payment, please let us know and we can note it on your account.</p>
{_payment_link_html(payment_link)}
<p>If there is an issue with the invoice or you need to discuss payment timing, please reply to this email.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Invoice #{invoice_number} for {amount_str} is now {days_overdue} days past due.

Please find the invoice attached. If you have already arranged payment, let us know.
{_payment_link_text(payment_link)}
If there is an issue or you need to discuss timing, please reply to this email.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="overdue_7")


# ── Stage 4: 14 days overdue ──────────────────────────────────────────────────

def overdue_14(
    account_name: str,
    invoice_number: str,
    amount: float,
    days_overdue: int,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>This is our second follow-up regarding invoice <strong>#{invoice_number}</strong> for <strong>{amount_str}</strong>, which is now <strong>{days_overdue} days past due</strong>.</p>
<p>We have attached the invoice again for your reference. Please arrange payment at your earliest convenience:</p>
{_payment_link_html(payment_link)}
<p>If you are waiting on internal approvals or have a specific payment date in mind, please let us know by replying to this email so we can update our records.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Second follow-up: invoice #{invoice_number} for {amount_str} is now {days_overdue} days past due.

Invoice attached. Please arrange payment at your earliest convenience.
{_payment_link_text(payment_link)}
If you have a payment date in mind, please reply so we can update our records.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="overdue_14")


# ── Stage 5: 21 days overdue ──────────────────────────────────────────────────

def overdue_21(
    account_name: str,
    invoice_number: str,
    amount: float,
    days_overdue: int,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>We are writing again regarding invoice <strong>#{invoice_number}</strong> for <strong>{amount_str}</strong>, now <strong>{days_overdue} days past due</strong>.</p>
<p>We understand that payments can sometimes be delayed, and we are happy to work with you on timing. Please reply to this email with an expected payment date, or make payment using the link below:</p>
{_payment_link_html(payment_link)}
<p>If this invoice has already been paid, please disregard this message and forward the payment confirmation so we can reconcile our records.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Invoice #{invoice_number} for {amount_str} is now {days_overdue} days past due.

We are happy to work with you on timing. Please reply with an expected payment date or pay online:
{_payment_link_text(payment_link)}
If already paid, please forward your confirmation so we can reconcile.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="overdue_21")


# ── Stage 6: 28 days overdue ──────────────────────────────────────────────────

def overdue_28(
    account_name: str,
    invoice_number: str,
    amount: float,
    days_overdue: int,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>Invoice <strong>#{invoice_number}</strong> for <strong>{amount_str}</strong> is now <strong>{days_overdue} days past due</strong>. We have reached out several times and have not yet received a response or payment.</p>
<p>We ask that you please arrange payment or contact us immediately to discuss your account status:</p>
{_payment_link_html(payment_link)}
<p>If we do not hear from you, we may need to review your account access. We would prefer to resolve this directly — please reply to this email or contact us.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Invoice #{invoice_number} for {amount_str} is now {days_overdue} days past due.

We have reached out several times without response. Please arrange payment or contact us immediately.
{_payment_link_text(payment_link)}
If we do not hear from you, we may need to review your account access.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="overdue_28")


# ── Stage 7: Suspension risk (45+ days) ──────────────────────────────────────

def suspension_risk(
    account_name: str,
    invoice_number: str,
    amount: float,
    days_overdue: int,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW — Leila approves before sending
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>Invoice <strong>#{invoice_number}</strong> for <strong>{amount_str}</strong> is now <strong>{days_overdue} days past due</strong>. Despite multiple attempts to contact you, we have not received payment or a response.</p>
<p>Your CapLinked account is at risk of suspension. To prevent an interruption in service, please make payment immediately:</p>
{_payment_link_html(payment_link)}
<p>If you believe there is an error or would like to speak with our team, please reply to this email immediately or contact us directly.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Invoice #{invoice_number} for {amount_str} is now {days_overdue} days past due.

Your CapLinked account is at risk of suspension. Please make payment immediately:
{_payment_link_text(payment_link)}
If you believe there is an error or need to speak with our team, please reply immediately.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="suspension_risk")


# ── Overage invoice (sent on 7th of month) ───────────────────────────────────

def overage_invoice(
    account_name: str,
    invoice_number: str,
    amount: float,
    due_date: str,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>Please find attached your overage invoice <strong>#{invoice_number}</strong> for <strong>{amount_str}</strong>. Payment is due by <strong>{due_date}</strong>.</p>
<p>This invoice covers usage above your plan limits for the prior period. If you have questions about the usage breakdown, please reply to this email.</p>
{_payment_link_html(payment_link)}
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Please find attached your overage invoice #{invoice_number} for {amount_str}. Payment is due by {due_date}.

This invoice covers usage above your plan limits for the prior period.
{_payment_link_text(payment_link)}
If you have questions, please reply to this email.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="overage_invoice")


# ── Overage collections follow-up ────────────────────────────────────────────

def overage_followup(
    account_name: str,
    invoice_number: str,
    amount: float,
    days_overdue: int,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>Following up on overage invoice <strong>#{invoice_number}</strong> for <strong>{amount_str}</strong>, which is now <strong>{days_overdue} days past due</strong>.</p>
{_payment_link_html(payment_link)}
<p>Please arrange payment at your earliest convenience or reply if you have any questions.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Following up on overage invoice #{invoice_number} for {amount_str} — {days_overdue} days past due.
{_payment_link_text(payment_link)}
Please arrange payment or reply with any questions.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="overage_followup")


# ── Failed payment notice (F-26) ──────────────────────────────────────────────

def failed_payment(
    account_name: str,
    invoice_number: str,
    amount: float,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>We wanted to let you know that your automatic payment for invoice <strong>#{invoice_number}</strong> ({amount_str}) did not process successfully.</p>
<p>Please update your payment method or make a manual payment using the link below to ensure uninterrupted access to your CapLinked account:</p>
{_payment_link_html(payment_link)}
<p>If you have questions or believe this is in error, please reply to this email.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Your automatic payment for invoice #{invoice_number} ({amount_str}) did not process successfully.

Please update your payment method or make a manual payment:
{_payment_link_text(payment_link)}
If you have questions or believe this is in error, please reply to this email.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="failed_payment")


# ── P2P acknowledgment ────────────────────────────────────────────────────────

def p2p_acknowledgment(
    account_name: str,
    invoice_number: str,
    amount: float,
    promise_date: str,
    payment_method: str = "",
    currency: str = "USD",
) -> EmailTemplate:
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)
    method_note = f" via {payment_method}" if payment_method else ""

    html = f"""<p>Hi {account_name} team,</p>
<p>Thank you for letting us know. We have noted that payment for invoice <strong>#{invoice_number}</strong> ({amount_str}) is expected by <strong>{promise_date}</strong>{method_note}.</p>
<p>We will update our records accordingly. If there are any changes to the payment timeline, please let us know.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Thank you — we have noted that payment for invoice #{invoice_number} ({amount_str}) is expected by {promise_date}{method_note}.

If there are any changes to the timeline, please let us know.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="p2p_acknowledgment")


# ── P2P reminder (for known non-compliers, sent on promised date) ─────────────

def p2p_reminder(
    account_name: str,
    invoice_number: str,
    amount: float,
    promise_date: str,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:  # BRIE_REVIEW
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>As a reminder, you mentioned that payment for invoice <strong>#{invoice_number}</strong> ({amount_str}) would be made by <strong>{promise_date}</strong>.</p>
<p>If you have already sent payment, please let us know and we will update your account. If not, please arrange payment today:</p>
{_payment_link_html(payment_link)}
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Reminder: you mentioned payment for invoice #{invoice_number} ({amount_str}) would be made by {promise_date}.

If already paid, please let us know. If not, please arrange payment today:
{_payment_link_text(payment_link)}
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="p2p_reminder")


# ── Routine billing FAQ responses ─────────────────────────────────────────────

def autopay_setup_faq(
    account_name: str,
    invoice_number: str,
) -> EmailTemplate:
    subject = f"{account_name} — Invoice #{invoice_number}"

    html = f"""<p>Hi {account_name} team,</p>
<p>Thank you for your question about autopay. CapLinked supports automatic payments on a per-invoice basis.</p>
<p>To set up autopay for your account, please log in to your CapLinked workspace and navigate to <strong>Account Settings &gt; Billing &gt; Payment Methods</strong>. From there you can add a payment method and enable autopay for future invoices.</p>
<p>If you need assistance with the setup, our support team is happy to help — please visit <a href="https://support.caplinked.com">support.caplinked.com</a> or reply to this email.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

To set up autopay, log in to your CapLinked workspace and go to Account Settings > Billing > Payment Methods.

For assistance, visit support.caplinked.com or reply to this email.
{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="autopay_setup_faq")


def payment_instructions_faq(
    account_name: str,
    invoice_number: str,
    amount: float,
    payment_link: str = "",
    currency: str = "USD",
) -> EmailTemplate:
    subject = f"{account_name} — Invoice #{invoice_number}"
    amount_str = _amount_display(amount, currency)

    html = f"""<p>Hi {account_name} team,</p>
<p>Thank you for your message. Here are the payment options for invoice <strong>#{invoice_number}</strong> ({amount_str}):</p>
<ul>
<li><strong>Online payment</strong> — use the secure link below (credit card, ACH, or bank transfer)</li>
<li><strong>Check</strong> — made payable to CapLinked Inc., mailed to our billing address on file</li>
<li><strong>Wire / ACH</strong> — please reply to this email to request our banking details</li>
<li><strong>Bill.com</strong> — reply to this email and we will send a Bill.com payment request</li>
</ul>
{_payment_link_html(payment_link)}
<p>If you have questions about a specific payment method, please reply to this email.</p>
{_SIGN_OFF_HTML}"""

    text = f"""Hi {account_name} team,

Payment options for invoice #{invoice_number} ({amount_str}):

- Online: {payment_link}
- Check: made payable to CapLinked Inc.
- Wire/ACH or Bill.com: reply to this email and we will provide details.

{_SIGN_OFF_TEXT}"""

    return EmailTemplate(subject=subject, html_body=html, text_body=text, template_name="payment_instructions_faq")


# ── Template dispatch ─────────────────────────────────────────────────────────

_STAGE_TEMPLATES = {
    1: reminder_7_days_before_due,
    2: reminder_due_today,
    3: overdue_7,
    4: overdue_14,
    5: overdue_21,
    6: overdue_28,
    7: suspension_risk,
}


def get_cadence_template(stage: int, **kwargs) -> EmailTemplate:
    """Return the email template for a given cadence stage (1-7)."""
    fn = _STAGE_TEMPLATES.get(stage)
    if not fn:
        raise ValueError(f"No template for cadence stage {stage}")
    return fn(**kwargs)
