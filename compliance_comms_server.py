"""
compliance_comms_server — MCP server for KYC, compliance, fraud, and communications.

Tools     (6): get_kyc_status, run_compliance_check, send_customer_communication,
               get_fraud_flags, write_audit_log, generate_customer_communication
Resources (1): template://{template_name}
Prompts   (1): customer_communication_prompt

Data access: customers, communications_log, fraud_flags, audit_log tables (via ComplianceDB).

Run: python compliance_comms_server.py
"""

import os
import re
from pathlib import Path
from typing import Optional
from mcp.server.mcpserver import MCPServer

from shared.database import ComplianceDB
from shared.logging_config import get_logger, trace
from shared.guardrails import (
    validate_customer_id,
    validate_or_error,
    can_send_communication,
    sanitize_free_text,
    redact_for_logging,
)

mcp = MCPServer("compliance_comms_server")
logger = get_logger("compliance_comms_server")
db = ComplianceDB()

# Templates directory — relative to project root
TEMPLATES_DIR = Path(__file__).parent / "communication_templates"

# Transactions above this amount trigger a large-transaction reporting flag.
# ₹10,00,000 = 10 lakh — standard Indian banking STR threshold.
LARGE_TXN_THRESHOLD = 1_000_000.0


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
@trace(logger, redact=redact_for_logging)
def get_kyc_status(customer_id: str) -> dict:
    """
    Return raw KYC data for a customer.

    This is the ONLY tool in the ecosystem that returns raw KYC data.
    customer_id: CUS-XXXXX format

    Returns: kyc_status (PENDING | VERIFIED | REJECTED), risk_rating, account_tier.
    """
    if err := validate_or_error(validate_customer_id, customer_id): return err

    record = db.get_kyc_status(customer_id)
    if not record:
        return {"error": f"Customer '{customer_id}' not found", "code": "NOT_FOUND"}

    return dict(record)


@mcp.tool()
@trace(logger, redact=redact_for_logging)
def run_compliance_check(customer_id: str, transaction_amount: float) -> dict:
    """
    Run a deterministic compliance check for a transaction.

    customer_id:          CUS-XXXXX format
    transaction_amount:   amount in INR (positive number)

    Returns:
      decision:           PASS | BLOCK
      large_txn_flag:     true if amount >= ₹10,00,000 (STR threshold)
      fraud_alert:        true if customer has HIGH/CRITICAL open fraud flags
      reason:             human-readable explanation
    """
    if err := validate_or_error(validate_customer_id, customer_id): return err

    if transaction_amount <= 0:
        return {"error": "transaction_amount must be a positive number", "code": "INVALID_INPUT"}

    customer = db.get_customer(customer_id)
    if not customer:
        return {"error": f"Customer '{customer_id}' not found", "code": "NOT_FOUND"}

    # Rule 1: active HIGH/CRITICAL fraud flag → BLOCK
    fraud_flags = db.get_fraud_flags(customer_id)
    active_high_flags = [
        f for f in fraud_flags
        if f["status"] in ("OPEN", "INVESTIGATING")
        and f["severity"] in ("HIGH", "CRITICAL")
    ]
    if active_high_flags:
        return {
            "decision": "BLOCK",
            "large_txn_flag": transaction_amount >= LARGE_TXN_THRESHOLD,
            "fraud_alert": True,
            "reason": (
                f"Transaction blocked: {len(active_high_flags)} active "
                f"HIGH/CRITICAL fraud flag(s) on this customer."
            ),
            "flags_count": len(active_high_flags),
        }

    # Rule 2: large-transaction reporting flag (does not block, but must be reported)
    large_txn = transaction_amount >= LARGE_TXN_THRESHOLD

    return {
        "decision": "PASS",
        "large_txn_flag": large_txn,
        "fraud_alert": False,
        "reason": (
            f"Transaction approved. "
            + (
                f"Note: amount ₹{transaction_amount:,.2f} exceeds ₹10,00,000 — "
                "file an STR (Suspicious Transaction Report) per RBI guidelines."
                if large_txn else
                "No compliance issues detected."
            )
        ),
        "customer_kyc": customer["kyc_status"],
    }


@mcp.tool()
@trace(logger, redact=redact_for_logging)
def send_customer_communication(
    customer_id: str,
    channel: str,
    message: str,
) -> dict:
    """
    Send a communication to a customer.

    customer_id: CUS-XXXXX format
    channel:     sms | email | sms_marketing | email_marketing | push_notification
    message:     text content (sanitised before sending)

    KYC gate: marketing channels are blocked unless KYC is VERIFIED.
    Every send is logged to communications_log and audit_log.
    """
    if err := validate_or_error(validate_customer_id, customer_id): return err

    customer = db.get_customer(customer_id)
    if not customer:
        return {"error": f"Customer '{customer_id}' not found", "code": "NOT_FOUND"}

    # ── KYC gate ──────────────────────────────────────────────────────────────
    allowed, reason = can_send_communication(customer["kyc_status"], channel)
    if not allowed:
        db.write_audit_log(
            action_type="COMMUNICATION_BLOCKED",
            performed_by="compliance_comms_server",
            customer_id=customer_id,
            outcome="BLOCKED",
            metadata={"channel": channel, "reason": reason},
        )
        return {"error": reason, "code": "KYC_GATE_BLOCKED"}

    # ── Sanitise message ──────────────────────────────────────────────────────
    try:
        clean_message = sanitize_free_text(message)
    except ValueError as e:
        db.write_audit_log(
            action_type="COMMUNICATION_BLOCKED",
            performed_by="compliance_comms_server",
            customer_id=customer_id,
            outcome="BLOCKED",
            metadata={"channel": channel, "reason": str(e)},
        )
        return {"error": str(e), "code": "SANITIZATION_FAILED"}

    # ── Send (log to communications_log) ──────────────────────────────────────
    log_id = db.log_communication(customer_id, channel, clean_message)

    db.write_audit_log(
        action_type="COMMUNICATION_SENT",
        performed_by="compliance_comms_server",
        customer_id=customer_id,
        outcome="SUCCESS",
        metadata={"channel": channel, "log_id": log_id},
    )

    return {
        "status": "sent",
        "customer_id": customer_id,
        "channel": channel,
        "log_id": log_id,
    }


@mcp.tool()
@trace(logger, redact=redact_for_logging)
def get_fraud_flags(customer_id: str) -> dict:
    """
    Return all fraud flags for a customer.

    customer_id: CUS-XXXXX format

    Returns a list of flag records with severity and status.
    """
    if err := validate_or_error(validate_customer_id, customer_id): return err

    if not db.customer_exists(customer_id):
        return {"error": f"Customer '{customer_id}' not found", "code": "NOT_FOUND"}

    flags = db.get_fraud_flags(customer_id)
    return {
        "customer_id": customer_id,
        "flag_count": len(flags),
        "flags": flags,
    }


@mcp.tool()
@trace(logger, redact=redact_for_logging)
def write_audit_log(
    action_type: str,
    performed_by: str,
    customer_id: Optional[str],
    outcome: str,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Write an entry to the audit log.

    action_type:  e.g. LOAN_SUBMITTED, COMMUNICATION_SENT, KYC_UPDATED
    performed_by: identifier of the actor (server name, staff ID, or SYSTEM)
    customer_id:  CUS-XXXXX format (optional — system actions may not have one)
    outcome:      SUCCESS | FAILURE | BLOCKED
    metadata:     additional context as a dict (optional)

    Every write operation in the system should produce an audit log entry.
    The audit_log table is the compliance trail retrievable for regulatory review.
    """
    if customer_id:
        if err := validate_or_error(validate_customer_id, customer_id): return err

    valid_outcomes = {"SUCCESS", "FAILURE", "BLOCKED"}
    if outcome not in valid_outcomes:
        return {
            "error": f"Invalid outcome '{outcome}'. Must be one of: {sorted(valid_outcomes)}",
            "code": "INVALID_INPUT",
        }

    try:
        clean_action = sanitize_free_text(action_type)
    except ValueError as e:
        return {"error": f"action_type validation failed: {e}", "code": "SANITIZATION_FAILED"}

    entry_id = db.write_audit_log(
        action_type=clean_action,
        performed_by=performed_by,
        customer_id=customer_id,
        outcome=outcome,
        metadata=metadata,
    )

    return {
        "status": "logged",
        "entry_id": entry_id,
        "action_type": action_type,
        "outcome": outcome,
    }


@mcp.tool()
@trace(logger, redact=redact_for_logging)
def generate_customer_communication(
    template_name: str,
    customer_name: str,
    account_tier: str,
    specific_detail: str,
) -> dict:
    """
    Render a communication from a Markdown template file.

    template_name:   name of the template (without .md extension)
                     Available: welcome | loan_approval | loan_rejection |
                                kyc_reminder | statement
    customer_name:   recipient's name
    account_tier:    STANDARD | PREMIUM | ELITE
    specific_detail: context-specific content to embed in the template

    Renders from communication_templates/{template_name}.md — never a plain string.
    The specific_detail field is sanitised before rendering.
    """
    # Sanitise inputs
    try:
        safe_detail = sanitize_free_text(specific_detail)
        safe_name = sanitize_free_text(customer_name)
    except ValueError as e:
        return {"error": str(e), "code": "SANITIZATION_FAILED"}

    template_path = TEMPLATES_DIR / f"{template_name}.md"
    if not template_path.exists():
        available = [p.stem for p in TEMPLATES_DIR.glob("*.md")]
        return {
            "error": f"Template '{template_name}' not found.",
            "available_templates": sorted(available),
            "code": "NOT_FOUND",
        }

    raw = template_path.read_text(encoding="utf-8")

    # Render — replace {{variable}} placeholders
    rendered = raw
    for key, value in {
        "customer_name":  safe_name,
        "account_tier":   account_tier,
        "specific_detail": safe_detail,
    }.items():
        rendered = rendered.replace("{{" + key + "}}", value)

    return {
        "template_name": template_name,
        "rendered_content": rendered,
        "character_count": len(rendered),
    }


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("template://{template_name}")
@trace(logger)
def template_resource(template_name: str) -> str:
    """
    Return the raw content of a communication template for inspection.
    """
    template_path = TEMPLATES_DIR / f"{template_name}.md"
    if not template_path.exists():
        available = [p.stem for p in TEMPLATES_DIR.glob("*.md")]
        return (
            f"Template '{template_name}' not found. "
            f"Available: {', '.join(sorted(available))}"
        )
    return template_path.read_text(encoding="utf-8")


# ── Prompts ───────────────────────────────────────────────────────────────────

@mcp.prompt()
@trace(logger)
def customer_communication_prompt(
    template_name: str,
    customer_name: str,
    account_tier: str,
    specific_detail: str,
) -> str:
    """
    Prompt for drafting and sending a customer communication.

    Directs the AI to verify KYC, render the template via the tool,
    and only then send — never to embed sensitive data directly.
    """
    return (
        f"You are a NeoBank India communications officer.\n\n"
        f"Task: Send a '{template_name}' communication to {customer_name}.\n"
        f"Account tier  : {account_tier}\n"
        f"Context detail: {specific_detail}\n\n"
        "Step-by-step instructions:\n"
        "1. Use get_kyc_status to confirm the customer's KYC before proceeding.\n"
        "2. Use generate_customer_communication with template_name='"
        + template_name
        + "' to render the message — do NOT compose a message from scratch.\n"
        "3. Review the rendered content for accuracy.\n"
        "4. Use send_customer_communication to deliver the message on the "
        "appropriate channel (sms for transactional, email for formal notices).\n"
        "5. Confirm the log_id returned from the send call.\n\n"
        "Constraint: Never include raw account numbers, balances, or PAN numbers "
        "in any outbound message."
    )


if __name__ == "__main__":
    mcp.run()
