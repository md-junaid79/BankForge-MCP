"""
accounts_server — MCP server for NeoBank account and transaction data.

Tools     (3): get_account_summary, get_accounts_for_customer, get_transaction_history
Resources (2): customer://{customer_id}/profile, account://{account_id}/summary
Prompts   (1): transaction_analysis_prompt

Data access: accounts, customers, transactions tables only (via AccountsDB).
Run: python accounts_server.py
"""

import os
from mcp.server.mcpserver import MCPServer

from shared.database import AccountsDB
from shared.logging_config import get_logger, trace
from shared.guardrails import (
    validate_account_id,
    validate_customer_id,
    validate_or_error,
    minimize_account_fields,
    mask_account_number,
    redact_for_logging,
)

mcp = MCPServer("accounts_server")
logger = get_logger("accounts_server")
db = AccountsDB()


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
@trace(logger, redact=redact_for_logging)
def get_account_summary(account_id: str, caller_scope: str) -> dict:
    """
    Return account details filtered by the caller's role scope.

    account_id:   ACC-XXXXX format (e.g. ACC-10042)
    caller_scope: teller | loan_officer | compliance_officer | admin

    Tellers see the least data; admins see everything.
    Account numbers are always masked to last 4 digits.
    """
    if err := validate_or_error(validate_account_id, account_id): return err

    account = db.get_account(account_id)
    if not account:
        return {"error": f"Account '{account_id}' not found", "code": "NOT_FOUND"}

    return minimize_account_fields(account, caller_scope)


@mcp.tool()
@trace(logger, redact=redact_for_logging)
def get_accounts_for_customer(customer_id: str, caller_scope: str) -> dict:
    """
    Return all accounts belonging to a customer, fields filtered by scope.

    customer_id:  CUS-XXXXX format (e.g. CUS-10042)
    caller_scope: teller | loan_officer | compliance_officer | admin
    """
    if err := validate_or_error(validate_customer_id, customer_id): return err

    accounts = db.get_accounts_for_customer(customer_id)
    if not accounts:
        if not db.customer_exists(customer_id):
            return {"error": f"Customer '{customer_id}' not found", "code": "NOT_FOUND"}
        return {"customer_id": customer_id, "accounts": [], "count": 0}

    return {
        "customer_id": customer_id,
        "count": len(accounts),
        "accounts": [minimize_account_fields(a, caller_scope) for a in accounts],
    }


@mcp.tool()
@trace(logger, redact=redact_for_logging)
def get_transaction_history(account_id: str, limit: int = 10) -> dict:
    """
    Return recent transactions for an account, most recent first.

    account_id: ACC-XXXXX format
    limit:      number of transactions to return (1–100, default 10)

    Account number in the response is masked.
    """
    if err := validate_or_error(validate_account_id, account_id): return err

    if not db.account_exists(account_id):
        return {"error": f"Account '{account_id}' not found", "code": "NOT_FOUND"}

    limit = max(1, min(limit, 100))
    transactions = db.get_transaction_history(account_id, limit)

    return {
        "account_id": mask_account_number(account_id),
        "transaction_count": len(transactions),
        "transactions": transactions,
    }


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("customer://{customer_id}/profile")
@trace(logger, redact=redact_for_logging)
def customer_profile(customer_id: str) -> str:
    """
    Customer profile summary for context injection.
    No raw account numbers or balance data — masked references only.
    """
    if err := validate_or_error(validate_customer_id, customer_id): return str(err)

    accounts = db.get_accounts_for_customer(customer_id)
    if not accounts:
        return f"Customer '{customer_id}' not found."

    first = accounts[0]
    lines = [
        f"Customer Profile: {customer_id}",
        f"KYC Status     : {first.get('kyc_status', 'UNKNOWN')}",
        f"Risk Rating    : {first.get('risk_rating', 'UNKNOWN')}",
        f"Accounts ({len(accounts)}):",
    ]
    for a in accounts:
        lines.append(
            f"  {mask_account_number(a['account_id'])}  "
            f"[{a['account_type']}]  status={a['status']}"
        )
    return "\n".join(lines)


@mcp.resource("account://{account_id}/summary")
@trace(logger, redact=redact_for_logging)
def account_summary_resource(account_id: str) -> str:
    """
    Masked account summary for context injection.
    Reuses guardrail masking — account number is always last-4-digits only.
    """
    if err := validate_or_error(validate_account_id, account_id): return str(err)

    account = db.get_account(account_id)
    if not account:
        return f"Account '{account_id}' not found."

    return "\n".join([
        "Account Summary",
        f"Account : {mask_account_number(account_id)}",
        f"Type    : {account['account_type']}",
        f"Status  : {account['status']}",
        f"KYC     : {account.get('kyc_status', 'UNKNOWN')}",
    ])


# ── Prompts ───────────────────────────────────────────────────────────────────

@mcp.prompt()
@trace(logger)
def transaction_analysis_prompt(
    customer_name: str,
    account_type: str,
    analysis_period: str,
) -> str:
    """
    Structured prompt for analysing a customer's transaction patterns.

    Directs the AI to retrieve data via the right tools rather than
    embedding sensitive data directly in the prompt.
    """
    return (
        f"You are a NeoBank India compliance analyst.\n\n"
        f"Task: Analyse transaction patterns for customer '{customer_name}'.\n"
        f"Account type  : {account_type}\n"
        f"Analysis period: {analysis_period}\n\n"
        "Step-by-step instructions:\n"
        "1. Call get_accounts_for_customer (caller_scope='compliance_officer') "
        "to retrieve the customer's account list.\n"
        "2. Call get_transaction_history for each account to retrieve recent transactions.\n"
        "3. Identify:\n"
        "   a. Any single transaction above ₹10,00,000 (report flag threshold).\n"
        "   b. Unusual debit/credit patterns or frequency spikes.\n"
        "   c. Transactions with vague or missing descriptions.\n"
        "4. Call run_compliance_check for any transaction that requires a "
        "compliance decision.\n\n"
        "Output format: structured report with sections — Summary, Flagged Transactions, "
        "Recommendations.\n"
        "Constraint: Never include raw account numbers — use masked references only."
    )


if __name__ == "__main__":
    mcp.run()
