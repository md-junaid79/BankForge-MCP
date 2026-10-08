# BankForge — Architecture Document

**Project:** NeoBank India MCP Server Ecosystem  
**Version:** 1.0  
**Author:** Mohammed Moinuddin, Cognizant  

---

## 1. Overview

BankForge is a digital banking backend built as three independently deployable MCP (Model Context Protocol) servers backed by a single SQLite database. It demonstrates how AI agents can securely access banking data through a structured tool/resource/prompt interface while enforcing compliance controls at the server boundary — not at the AI client.

The system was built against MCP v2.3.0 (`mcp[cli]`).

---

## 2. Server Decomposition

| Server | File | Responsibility |
|---|---|---|
| Accounts Server | `accounts_server.py` | Account balances, transaction history, customer profile |
| Products Server | `products_server.py` | Loan catalogue, eligibility, application submission |
| Compliance & Comms Server | `compliance_comms_server.py` | KYC status, compliance checks, communications, audit log |

Each server registers with MCP under its own name and runs as an independent process. In production these would be three separate deployments. In local development all three share the same `neobank.db` file.

### Why three servers and not one?

**Domain separation.** The three domains have different access patterns, different compliance requirements, and different consumers:

- A loan officer needs accounts data and product eligibility — never needs to write audit logs directly.
- A compliance officer needs KYC and fraud flags — never needs to see raw transaction amounts.
- A teller needs account status and masked numbers — never needs product catalogue.

Splitting by domain also means a compromise of the accounts server cannot expose KYC or communications data, because those tables simply aren't accessible from `AccountsDB`.

---

## 3. Shared Module Layer (`shared/`)

Three modules are shared across all servers. They live in `shared/` and are imported directly — no HTTP, no inter-process calls, no service mesh.

### `shared/database.py` — Data Isolation by Accessor Class

All three servers share one SQLite file. Isolation is enforced **structurally**, not by runtime checks:

```
AccountsDB    → methods exist only for: accounts, customers, transactions
ProductsDB    → methods exist only for: loan_products, loan_applications
ComplianceDB  → methods exist only for: customers, communications_log, fraud_flags, audit_log
```

There is no method on `AccountsDB` that can touch `fraud_flags`. A developer cannot accidentally write cross-domain queries — the method simply does not exist. This is stronger than a runtime permission check because it holds even under refactoring pressure.

The one deliberate cross-domain operation is in `products_server.submit_loan_application`, which imports `ComplianceDB` specifically for the KYC gate. This is documented explicitly in the server header. In a real microservices deployment this call would be an HTTP request to `compliance_comms_server`; here it is a direct function call on the shared DB layer, which is equivalent in terms of isolation semantics.

SQLite is configured with:
```sql
PRAGMA journal_mode=WAL;   -- concurrent readers, single writer
PRAGMA foreign_keys=ON;    -- referential integrity enforced
```

### `shared/guardrails.py` — All Security Controls in One Place

Every input validation, output minimization, and KYC gate lives here. The design principle: **security controls are not scattered across tool functions**. Instead, each tool calls a guardrail function and acts on its result. This makes the controls auditable in one file.

Key controls:

| Function | Purpose |
|---|---|
| `validate_customer_id / account_id / product_id` | Regex enforcement of CUS-XXXXX / ACC-XXXXX / PROD-XX-XX formats |
| `validate_or_error(fn, value)` | Converts a ValueError into a structured `{"error": ..., "code": "INVALID_FORMAT"}` dict — tools return this directly without crashing |
| `mask_account_number(id)` | Returns `****XXXX` (last 4 chars only) — applied in every account-related response |
| `minimize_account_fields(data, scope)` | Removes fields the caller scope isn't allowed to see; replaces `account_id` with `masked_account_number` |
| `can_submit_loan(kyc_status)` | Returns `(allowed: bool, reason: str)` — KYC gate for loan applications |
| `can_send_communication(kyc_status, channel)` | Returns `(allowed: bool, reason: str)` — blocks marketing channels for PENDING/REJECTED customers |
| `sanitize_free_text(text)` | Strips HTML, detects injection patterns (`ignore previous instructions`, template syntax `{{...}}`, script tags), enforces 2000-char limit |
| `redact_for_logging(args_dict)` | Replaces PII field values with `***REDACTED***` before writing to structured logs |

### `shared/logging_config.py` — Structured JSON Logging with `@trace`

All tool calls are wrapped with a `@trace(logger, redact=...)` decorator that emits JSON log lines:

```json
{"event": "ENTER", "fn": "get_account_summary", "call_id": "a3f2...", "args": {"account_id": "***REDACTED***"}}
{"event": "EXIT",  "fn": "get_account_summary", "call_id": "a3f2...", "duration_ms": 4.2}
```

The decorator handles both sync and async functions. PII is redacted before the ENTER log is written — the raw argument value never appears in log output. `call_id` links ENTER/EXIT/FAILED lines for a single invocation, enabling request tracing without a distributed tracing system.

---

## 4. Role-Based Field Visibility

Accounts data contains fields that different roles are permitted to see. Field minimization happens at the tool boundary — the DB query returns all joined fields, then `minimize_account_fields` strips what the caller scope cannot see:

| Scope | Fields visible |
|---|---|
| `teller` | `masked_account_number`, `account_type`, `status` |
| `loan_officer` | + `balance`, `credit_score` |
| `compliance_officer` | + `kyc_status`, `risk_rating` |
| `admin` | all fields (still with masked account number, never raw `account_id`) |

The raw `account_id` is **never returned** to any caller. Every response replaces it with `masked_account_number` (`****XXXX`). This holds even for admin scope.

---

## 5. KYC Gate Design

KYC enforcement is at the **Tool level**, not at the AI client. This is a deliberate design choice: an AI client that decides to skip a KYC check (due to prompt manipulation or model error) cannot bypass the gate, because the gate runs inside the server function before any DB write.

Two tools enforce KYC:

- `products_server.submit_loan_application` — blocks if `kyc_status != VERIFIED`
- `compliance_comms_server.send_customer_communication` — blocks marketing channels if `kyc_status != VERIFIED`; allows transactional channels (SMS OTP, notifications) for all statuses

Every KYC block writes an audit log entry before returning, creating an immutable record of the blocked attempt.

---

## 6. MCP Interface Design

Each server exposes three interface types per MCP v2 spec:

| Type | Description | Example |
|---|---|---|
| **Tools** | Actions — read or write, return structured dicts | `get_account_summary`, `submit_loan_application` |
| **Resources** | Read-only data accessible by URI — for context injection | `customer://{customer_id}/profile` |
| **Prompts** | Reusable instruction templates — tell the AI *how* to use the tools | `transaction_analysis_prompt` |

### Key prompt design principle

Prompts do **not** embed raw data. They return instruction strings that tell the AI agent which tools to call and in what order. For example, `transaction_analysis_prompt` returns:

```
"Use the get_transaction_history tool to fetch transactions for this account, 
 then analyze the results for patterns..."
```

This keeps data out of the prompt context and forces the AI to call tools to retrieve it — which means every data access goes through the guardrails layer.

---

## 7. Compliance Controls

### Large Transaction Reporting (STR)

Per RBI guidelines, transactions ≥ ₹10,00,000 (1,000,000 INR) require a Suspicious Transaction Report. `run_compliance_check` detects this threshold and sets `large_txn_flag: True` in the response with an explicit STR reminder. The transaction is not blocked — the flag is advisory, requiring the human compliance officer to file the report.

### Fraud Flag Blocking

If a customer has an `OPEN` or `INVESTIGATING` fraud flag with severity `HIGH` or `CRITICAL`, `run_compliance_check` returns `decision: BLOCK`. This prevents transaction processing for flagged accounts.

### Audit Trail

`write_audit_log` is called on every significant outcome — loan application submitted, KYC gate blocked, communication sent. The audit log is append-only (no update/delete methods exist on `ComplianceDB`) and every entry records `action_type`, `performed_by`, `customer_id`, `outcome`, and a metadata JSON blob.

---

## 8. What Did Not Work (and How It Was Fixed)

### 8.1 `FastMCP` → `MCPServer` (MCP v2.3.0 API change)

**Problem:** The capstone spec and most online tutorials reference `from mcp.server.fastmcp import FastMCP`. In MCP v2.3.0 (installed via `pip install mcp[cli]`), the class was renamed and moved:

```python
# What the spec showed — does NOT work in v2.3.0
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("server_name")

# What actually works in v2.3.0
from mcp.server.mcpserver import MCPServer
mcp = MCPServer("server_name")
```

**Fix:** Changed all three server files. The decorator API (`@mcp.tool()`, `@mcp.resource(uri)`, `@mcp.prompt()`, `mcp.run()`) is identical — only the import path and class name changed.

**Learning:** When a library gives `ModuleNotFoundError`, inspect the installed package directly (`dir(mcp.server)`) rather than trusting documentation that may be written for an older version.

### 8.2 Validators Raising Instead of Returning

**Problem:** The initial implementation had `validate_customer_id()` raise `ValueError` on bad input. Tool functions caught this with `try/except` and manually built error dicts — duplicated error-handling code in every tool.

**Fix:** Added `validate_or_error(fn, value)` to guardrails:

```python
def validate_or_error(validate_fn, value):
    try:
        validate_fn(value)
        return None
    except ValueError as e:
        return {"error": str(e), "code": "INVALID_FORMAT"}
```

Tools now use the walrus operator pattern for clean, zero-duplication validation:

```python
if err := validate_or_error(validate_customer_id, customer_id):
    return err
```

### 8.3 Test DB Isolation

**Problem:** Server modules create their DB accessor instances (`db = AccountsDB()`) at module import time, reading `DB_PATH` from the environment at that moment. If `conftest.py` set the env var too late — after the server modules were already imported — tests would write to the real `neobank.db` instead of a temp file.

**Fix:** `conftest.py` sets `os.environ["DB_PATH"]` before any test file imports a server module. Since pytest loads `conftest.py` before collecting test files, the env var is always in place when the server modules are first imported.

---

## 9. Project Structure

```
4.BankForge/
├── accounts_server.py          # MCP server — accounts domain
├── products_server.py          # MCP server — loan products domain
├── compliance_comms_server.py  # MCP server — compliance & communications
├── shared/
│   ├── __init__.py
│   ├── database.py             # Schema, seed data, three accessor classes
│   ├── guardrails.py           # All input validation and security controls
│   └── logging_config.py       # JSON logger + @trace decorator
├── communication_templates/
│   ├── welcome.md
│   ├── loan_approval.md
│   ├── loan_rejection.md
│   ├── kyc_reminder.md
│   └── statement.md
├── tests/
│   ├── test_guardrails.py      # 30 unit tests — no DB required
│   ├── test_accounts.py        # 17 integration tests
│   ├── test_products.py        # 36 integration tests
│   └── test_compliance.py      # 31 integration tests
├── conftest.py                 # Pytest fixture — temp DB isolation
├── run_local_demo.py           # Full demo — Showcase + 3 Stress Tests
├── requirements.txt
└── neobank.db                  # SQLite database (created by --seed)
```

---

## 10. Key Design Decisions Summary

| Decision | Alternative Considered | Why This Approach |
|---|---|---|
| Single SQLite file, three accessor classes | Separate DB files per server | Simpler deployment; isolation enforced by class structure not file permissions |
| KYC gate at Tool level | KYC gate at AI client / prompt level | Tool-level gate cannot be bypassed by prompt injection or model error |
| `validate_or_error` returns dict, never raises | Try/except in every tool | Eliminates duplicated error-handling; tools stay readable |
| Prompts return instructions, not data | Prompts embed pre-fetched data | Forces all data access through guardrails; keeps prompt context small |
| `@trace` decorator with PII redaction | Manual log calls in each function | Consistent structured logs with zero per-function boilerplate |
| Markdown templates with `{{variable}}` | Jinja2 or similar | Zero extra dependencies; injection blocked by sanitize_free_text before rendering |
