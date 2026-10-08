"""
Security guardrails — the single source of truth for all security controls.

Why one module?
  If masking logic lives in 3 different servers, one server's bug leaks PII.
  Centralising here means a single fix applies everywhere, and auditors can
  review one file to understand every security control.

Controls implemented:
  1. validate_*_id()         — typed format errors on malformed IDs
  2. mask_account_number()   — last-4-digits masking (hard requirement)
  3. minimize_account_fields() — role-based field visibility
  4. can_send_communication() — KYC gate for marketing comms
  5. can_submit_loan()        — KYC gate for loan applications
  6. sanitize_free_text()     — HTML stripping + injection detection
  7. redact_for_logging()     — PII masking before log lines are written
"""

import re
import html
from typing import Optional

# ── ID format patterns ────────────────────────────────────────────────────────
# Spec §4: CUS-XXXXX / ACC-XXXXX / PROD-XX-XX  (X = digit, XX = alpha/digit)
_CUS_RE  = re.compile(r'^CUS-\d{5}$')
_ACC_RE  = re.compile(r'^ACC-\d{5}$')
_PROD_RE = re.compile(r'^PROD-[A-Z]{2}-\d{2}$')

# ── Injection-detection patterns ──────────────────────────────────────────────
# These catch the most common prompt-injection and XSS patterns.
# Regex is fast and deterministic — unlike an LLM judge — which is what
# the spec means by "deterministic compliance rules".
_INJECTION_PATTERNS = [
    re.compile(r'<\s*script', re.IGNORECASE),
    re.compile(r'javascript\s*:', re.IGNORECASE),
    re.compile(r'ignore\s+(previous|all)\s+instructions', re.IGNORECASE),
    re.compile(r'\bsystem\s*prompt\b', re.IGNORECASE),
    re.compile(r'\{\{.*?\}\}'),          # template/Jinja injection
    re.compile(r'<!--.*?-->', re.DOTALL), # HTML comment hiding
    re.compile(r'<\?php', re.IGNORECASE),
    re.compile(r'eval\s*\(', re.IGNORECASE),
]

_MAX_FREE_TEXT_LEN = 2000

# ── PII field names redacted from log lines ───────────────────────────────────
# Why redact at the logging layer?
#   India's DPDP Act (Digital Personal Data Protection) requires data
#   minimisation — logs are the most common accidental PII leak vector.
#   Masking here means the mask applies even if a developer forgets to
#   strip PII before calling logger.debug().
_PII_FIELDS = frozenset({
    "account_id", "customer_id", "phone", "email",
    "pan_number", "aadhaar", "mobile",
})

# ── Role-based field visibility ───────────────────────────────────────────────
# Tellers see the least; admins see everything (None = unrestricted).
# Why explicit allowlist rather than denylist?
#   New fields added to the accounts table are invisible until explicitly
#   granted — fail-safe by default.
_SCOPE_ALLOWED_FIELDS: dict[str, Optional[frozenset]] = {
    "teller": frozenset({
        "masked_account_number", "account_type", "status",
    }),
    "loan_officer": frozenset({
        "masked_account_number", "account_type", "status",
        "balance", "credit_score",
    }),
    "compliance_officer": frozenset({
        "masked_account_number", "account_type", "status",
        "balance", "credit_score", "kyc_status", "risk_rating",
    }),
    "admin": None,  # unrestricted
}


# ── 0. Validation helper ──────────────────────────────────────────────────────

def validate_or_error(validate_fn, value: str) -> "Optional[dict]":
    """
    Call a validate_* function; return None on success or an error dict on failure.
    Lets tool functions do:  if err := validate_or_error(validate_account_id, x): return err
    """
    try:
        validate_fn(value)
        return None
    except (ValueError, TypeError) as e:
        return {"error": str(e), "code": "INVALID_FORMAT"}


# ── 1. ID validation ──────────────────────────────────────────────────────────

def validate_customer_id(customer_id: str) -> None:
    """Raises ValueError with a clear message if format is wrong."""
    if not _CUS_RE.match(customer_id):
        raise ValueError(
            f"Invalid customer_id '{customer_id}'. "
            "Expected format: CUS-XXXXX where X is a digit (e.g. CUS-10042)"
        )


def validate_account_id(account_id: str) -> None:
    if not _ACC_RE.match(account_id):
        raise ValueError(
            f"Invalid account_id '{account_id}'. "
            "Expected format: ACC-XXXXX where X is a digit (e.g. ACC-10042)"
        )


def validate_product_id(product_id: str) -> None:
    if not _PROD_RE.match(product_id):
        raise ValueError(
            f"Invalid product_id '{product_id}'. "
            "Expected format: PROD-XX-XX (e.g. PROD-PL-01)"
        )


# ── 2. Account number masking ─────────────────────────────────────────────────

def mask_account_number(account_id: str) -> str:
    """
    Returns the last 4 characters visible, rest masked.
    ACC-10042  →  ****0042
    The spec says "last four digits" — we keep the last 4 chars of the string.
    """
    if not account_id:
        return "****"
    suffix = account_id[-4:]
    return f"****{suffix}"


# ── 3. Role-based field minimization ─────────────────────────────────────────

def minimize_account_fields(account_data: dict, caller_scope: str) -> dict:
    """
    Filters account_data to only the fields allowed for caller_scope,
    and replaces account_id with masked_account_number.

    Why replace rather than add?
      If both fields exist, any code that accidentally uses account_id
      gets the real value. Removing it prevents the leak entirely.
    """
    if caller_scope not in _SCOPE_ALLOWED_FIELDS:
        raise ValueError(
            f"Unknown caller_scope '{caller_scope}'. "
            f"Must be one of: {sorted(_SCOPE_ALLOWED_FIELDS.keys())}"
        )

    # Mask account number first, before field filtering
    data = dict(account_data)
    raw_id = data.pop("account_id", None)
    if raw_id:
        data["masked_account_number"] = mask_account_number(raw_id)

    allowed = _SCOPE_ALLOWED_FIELDS[caller_scope]
    if allowed is None:  # admin
        return data

    return {k: v for k, v in data.items() if k in allowed}


# ── 4 & 5. KYC gates ─────────────────────────────────────────────────────────

def can_send_communication(kyc_status: str, channel: str) -> tuple[bool, str]:
    """
    Marketing channels are blocked unless KYC is VERIFIED.
    Transactional channels (sms, email) are allowed regardless.

    Returns (allowed: bool, reason: str).
    The tool should check allowed and raise if False.
    """
    _MARKETING_CHANNELS = {"sms_marketing", "email_marketing", "push_notification"}
    if channel in _MARKETING_CHANNELS and kyc_status != "VERIFIED":
        return False, (
            f"Marketing communication blocked: customer KYC is '{kyc_status}', "
            "must be VERIFIED. Use a transactional channel or complete KYC first."
        )
    return True, ""


def can_submit_loan(kyc_status: str) -> tuple[bool, str]:
    """
    Loan applications are blocked unless KYC is VERIFIED.
    This gate lives here (not in the AI client) so it cannot be bypassed
    by prompt engineering.
    """
    if kyc_status != "VERIFIED":
        return False, (
            f"Loan application blocked: customer KYC is '{kyc_status}', "
            "must be VERIFIED before a loan application can be submitted."
        )
    return True, ""


# ── 6. Input sanitization ─────────────────────────────────────────────────────

def sanitize_free_text(text: str) -> str:
    """
    Applied to every outbound message body and free-text input.
    Raises ValueError on violation (caller should return the error to the AI).

    Steps:
      1. Length check  — prevents DoS via huge payloads
      2. HTML stripping — removes tags that could cause XSS if rendered
      3. Injection scan — regex-based detection of known attack signatures
    """
    if not isinstance(text, str):
        raise TypeError("Free-text input must be a string")

    if len(text) > _MAX_FREE_TEXT_LEN:
        raise ValueError(
            f"Message too long: {len(text)} characters "
            f"(maximum is {_MAX_FREE_TEXT_LEN})"
        )

    # Strip HTML tags
    stripped = re.sub(r"<[^>]+>", "", text)
    stripped = html.unescape(stripped)

    # Scan for injection signatures on the original (pre-strip) text
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            raise ValueError(
                "Message contains disallowed content "
                "(possible injection attempt detected)"
            )

    return stripped.strip()


# ── 7. PII redaction for logs ─────────────────────────────────────────────────

def redact_for_logging(args_dict: dict) -> dict:
    """
    Masks the values of any key that looks like a PII field before
    the dict is written to a log line.

    Why mask by key name rather than value pattern?
      Value patterns (regex on credit card numbers etc.) miss context.
      Key names are explicit contracts: if you name a param 'account_id',
      it IS a PII field regardless of its runtime value.
    """
    return {
        k: "***REDACTED***" if k in _PII_FIELDS else v
        for k, v in args_dict.items()
    }
