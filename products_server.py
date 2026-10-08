"""
products_server — MCP server for NeoBank loan products and applications.

Tools    (5): list_loan_products, get_loan_product_details,
              check_eligibility_criteria, submit_loan_application,
              get_loan_application_status
Resources (1): product://{product_id}/details

Data access: loan_products, loan_applications tables only (via ProductsDB).

KYC gate note:
  submit_loan_application enforces a KYC gate at the Tool level by calling
  ComplianceDB.get_kyc_status() before writing. ProductsDB itself never
  queries customer data — the tool orchestrates across the two accessors.
  In a real microservices deployment this would be an HTTP call to
  compliance_comms_server; here it's a direct function call on the shared DB.

Run: python products_server.py
"""

import os
import uuid
from mcp.server.mcpserver import MCPServer
from typing import Optional

from shared.database import ProductsDB, ComplianceDB
from shared.logging_config import get_logger, trace
from shared.guardrails import (
    validate_product_id,
    validate_customer_id,
    validate_or_error,
    can_submit_loan,
    redact_for_logging,
)

mcp = MCPServer("products_server")
logger = get_logger("products_server")
db = ProductsDB()
compliance_db = ComplianceDB()   # used ONLY for the KYC gate in submit_loan_application


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
@trace(logger)
def list_loan_products(category: Optional[str] = None) -> dict:
    """
    Return the loan product catalogue, optionally filtered by category.

    category: PERSONAL | HOME | AUTO | BUSINESS  (optional)

    Returns product summaries — no customer data involved.
    """
    valid_categories = {"PERSONAL", "HOME", "AUTO", "BUSINESS"}
    if category and category.upper() not in valid_categories:
        return {
            "error": f"Unknown category '{category}'. "
                     f"Valid categories: {sorted(valid_categories)}",
            "code": "INVALID_INPUT",
        }

    products = db.list_loan_products(category)
    return {
        "count": len(products),
        "category_filter": category.upper() if category else "ALL",
        "products": products,
    }


@mcp.tool()
@trace(logger)
def get_loan_product_details(product_id: str) -> dict:
    """
    Return full details of a single loan product.

    product_id: PROD-XX-XX format (e.g. PROD-PL-01)
    """
    if err := validate_or_error(validate_product_id, product_id): return err

    product = db.get_loan_product(product_id)
    if not product:
        return {"error": f"Product '{product_id}' not found", "code": "NOT_FOUND"}

    return product


@mcp.tool()
@trace(logger)
def check_eligibility_criteria(product_id: str, applicant_risk_rating: str) -> dict:
    """
    Check whether a risk rating is eligible for a loan product.

    product_id:            PROD-XX-XX format
    applicant_risk_rating: LOW | MEDIUM | HIGH

    Read-only — no customer data is queried; only the product's criteria.
    """
    if err := validate_or_error(validate_product_id, product_id): return err

    valid_ratings = {"LOW", "MEDIUM", "HIGH"}
    if applicant_risk_rating.upper() not in valid_ratings:
        return {
            "error": f"Unknown risk rating '{applicant_risk_rating}'. "
                     f"Valid values: {sorted(valid_ratings)}",
            "code": "INVALID_INPUT",
        }

    return db.check_eligibility(product_id, applicant_risk_rating)


@mcp.tool()
@trace(logger, redact=redact_for_logging)
def submit_loan_application(
    customer_id: str,
    product_id: str,
    amount: float,
    tenure_months: int,
) -> dict:
    """
    Submit a loan application.  KYC gate enforced at the Tool level.

    customer_id:    CUS-XXXXX format
    product_id:     PROD-XX-XX format
    amount:         requested loan amount in INR
    tenure_months:  repayment tenure in months

    Gate logic (in order):
      1. Validate ID formats.
      2. Check customer KYC — blocked if not VERIFIED.
      3. Check product eligibility for customer's risk rating.
      4. Validate amount and tenure are within product limits.
      5. Write the application and return its ID.
      6. Audit log entry written on every outcome.
    """
    if err := validate_or_error(validate_customer_id, customer_id): return err
    if err := validate_or_error(validate_product_id, product_id): return err

    # ── Gate 1: KYC check (via ComplianceDB — ProductsDB stays clean) ────────
    try:
        kyc_record = compliance_db.get_kyc_status(customer_id)
    except Exception as exc:
        return {
            "error": f"Compliance service unavailable: {exc}",
            "code": "SERVICE_UNAVAILABLE",
        }
    if not kyc_record:
        compliance_db.write_audit_log(
            action_type="LOAN_APPLICATION_ATTEMPT",
            performed_by="products_server",
            customer_id=customer_id,
            outcome="FAILURE",
            metadata={"reason": "customer_not_found", "product_id": product_id},
        )
        return {"error": f"Customer '{customer_id}' not found", "code": "NOT_FOUND"}

    allowed, reason = can_submit_loan(kyc_record["kyc_status"])
    if not allowed:
        compliance_db.write_audit_log(
            action_type="LOAN_APPLICATION_ATTEMPT",
            performed_by="products_server",
            customer_id=customer_id,
            outcome="BLOCKED",
            metadata={"reason": "kyc_gate", "kyc_status": kyc_record["kyc_status"]},
        )
        return {"error": reason, "code": "KYC_GATE_BLOCKED"}

    # ── Gate 2: Product existence and eligibility ─────────────────────────────
    product = db.get_loan_product(product_id)
    if not product:
        return {"error": f"Product '{product_id}' not found", "code": "NOT_FOUND"}

    import json
    eligible_ratings = json.loads(product["eligible_risk_ratings"])
    if kyc_record["risk_rating"].upper() not in eligible_ratings:
        compliance_db.write_audit_log(
            action_type="LOAN_APPLICATION_ATTEMPT",
            performed_by="products_server",
            customer_id=customer_id,
            outcome="REJECTED",
            metadata={
                "reason": "ineligible_risk_rating",
                "risk_rating": kyc_record["risk_rating"],
                "product_id": product_id,
            },
        )
        return {
            "error": (
                f"Customer risk rating '{kyc_record['risk_rating']}' is not eligible "
                f"for product '{product_id}'. Eligible ratings: {eligible_ratings}"
            ),
            "code": "ELIGIBILITY_FAILED",
        }

    # ── Gate 3: Amount and tenure within product limits ───────────────────────
    if not (product["min_amount"] <= amount <= product["max_amount"]):
        return {
            "error": (
                f"Amount ₹{amount:,.2f} is outside product limits "
                f"(₹{product['min_amount']:,.2f} – ₹{product['max_amount']:,.2f})"
            ),
            "code": "AMOUNT_OUT_OF_RANGE",
        }

    if not (product["min_tenure_months"] <= tenure_months <= product["max_tenure_months"]):
        return {
            "error": (
                f"Tenure {tenure_months} months is outside product limits "
                f"({product['min_tenure_months']}–{product['max_tenure_months']} months)"
            ),
            "code": "TENURE_OUT_OF_RANGE",
        }

    # ── Submit ────────────────────────────────────────────────────────────────
    application_id = "APP-" + uuid.uuid4().hex[:8].upper()
    result = db.submit_loan_application(
        application_id=application_id,
        customer_id=customer_id,
        product_id=product_id,
        amount=amount,
        tenure_months=tenure_months,
        risk_rating=kyc_record["risk_rating"],
    )

    # ── Audit log on success ──────────────────────────────────────────────────
    compliance_db.write_audit_log(
        action_type="LOAN_APPLICATION_SUBMITTED",
        performed_by="products_server",
        customer_id=customer_id,
        outcome="SUCCESS",
        metadata={
            "application_id": application_id,
            "product_id": product_id,
            "amount": amount,
            "tenure_months": tenure_months,
        },
    )

    return result


@mcp.tool()
@trace(logger, redact=redact_for_logging)
def get_loan_application_status(application_id: str) -> dict:
    """
    Return the current status of a loan application.

    application_id: APP-XXXXXXXX format (returned by submit_loan_application)
    """
    if not application_id.startswith("APP-"):
        return {
            "error": f"Invalid application_id '{application_id}'. Expected APP-XXXXXXXX format.",
            "code": "INVALID_INPUT",
        }

    record = db.get_loan_application_status(application_id)
    if not record:
        return {"error": f"Application '{application_id}' not found", "code": "NOT_FOUND"}

    return record


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("product://{product_id}/details")
@trace(logger)
def product_details_resource(product_id: str) -> str:
    """
    Loan product details as readable text for context injection.
    No customer data — product catalogue is public information.
    """
    if err := validate_or_error(validate_product_id, product_id): return str(err)

    product = db.get_loan_product(product_id)
    if not product:
        return f"Product '{product_id}' not found."

    import json
    eligible = json.loads(product["eligible_risk_ratings"])
    return "\n".join([
        f"Product: {product['name']} ({product_id})",
        f"Category      : {product['category']}",
        f"Interest Rate : {product['interest_rate']}% per annum",
        f"Loan Amount   : ₹{product['min_amount']:,.0f} – ₹{product['max_amount']:,.0f}",
        f"Tenure        : {product['min_tenure_months']}–{product['max_tenure_months']} months",
        f"Eligible For  : {', '.join(eligible)} risk ratings",
        f"Description   : {product.get('description', '')}",
    ])


if __name__ == "__main__":
    mcp.run()
