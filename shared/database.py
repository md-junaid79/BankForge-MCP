"""
Database layer — schema, seed data, and per-server accessor classes.

Data isolation design:
  All three servers share one SQLite file (neobank.db). Isolation is
  enforced structurally: each server imports only its own accessor class,
  and that class only exposes methods for its allowed tables.

  products_server imports ProductsDB  → no method touches customers/accounts
  accounts_server imports AccountsDB  → no method touches loan_applications
  compliance_comms_server imports ComplianceDB → no method touches loan_products

  This means isolation holds even if a developer makes a coding mistake —
  there is simply no method to call.

Running as a script:
  python shared/database.py --seed      # create tables + insert seed data
  python shared/database.py --reset     # drop all tables then re-seed
"""

import os
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional


DB_PATH = os.getenv("DB_PATH", "neobank.db")


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS customers (
    customer_id   TEXT PRIMARY KEY,          -- format: CUS-XXXXX
    name          TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    kyc_status    TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | VERIFIED | REJECTED
    risk_rating   TEXT NOT NULL DEFAULT 'MEDIUM',   -- LOW | MEDIUM | HIGH
    account_tier  TEXT NOT NULL DEFAULT 'STANDARD', -- STANDARD | PREMIUM | ELITE
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,          -- format: ACC-XXXXX
    customer_id   TEXT NOT NULL REFERENCES customers(customer_id),
    account_type  TEXT NOT NULL,             -- SAVINGS | CURRENT | LOAN
    balance       REAL NOT NULL DEFAULT 0.0,
    status        TEXT NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE | FROZEN | CLOSED
    credit_score  INTEGER,
    opened_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL REFERENCES accounts(account_id),
    amount        REAL NOT NULL,             -- positive = credit, negative = debit
    txn_type      TEXT NOT NULL,             -- CREDIT | DEBIT
    description   TEXT,
    timestamp     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS loan_products (
    product_id             TEXT PRIMARY KEY,  -- format: PROD-XX-XX
    name                   TEXT NOT NULL,
    category               TEXT NOT NULL,     -- PERSONAL | HOME | AUTO | BUSINESS
    interest_rate          REAL NOT NULL,
    min_amount             REAL NOT NULL,
    max_amount             REAL NOT NULL,
    min_tenure_months      INTEGER NOT NULL,
    max_tenure_months      INTEGER NOT NULL,
    eligible_risk_ratings  TEXT NOT NULL,     -- JSON array e.g. '["LOW","MEDIUM"]'
    description            TEXT
);

CREATE TABLE IF NOT EXISTS loan_applications (
    application_id  TEXT PRIMARY KEY,         -- format: APP-XXXXXXXX
    customer_id     TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    amount          REAL NOT NULL,
    tenure_months   INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | REJECTED | DISBURSED
    risk_rating     TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS communications_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   TEXT NOT NULL,
    channel       TEXT NOT NULL,
    message       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'SENT',
    sent_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fraud_flags (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   TEXT NOT NULL,
    account_id    TEXT,
    flag_type     TEXT NOT NULL,
    description   TEXT,
    severity      TEXT NOT NULL DEFAULT 'LOW',   -- LOW | MEDIUM | HIGH | CRITICAL
    status        TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | INVESTIGATING | RESOLVED
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type    TEXT NOT NULL,
    performed_by   TEXT NOT NULL,
    customer_id    TEXT,
    outcome        TEXT NOT NULL,            -- SUCCESS | FAILURE | BLOCKED
    metadata_json  TEXT,
    timestamp      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DROP_SQL = """
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS fraud_flags;
DROP TABLE IF EXISTS communications_log;
DROP TABLE IF EXISTS loan_applications;
DROP TABLE IF EXISTS loan_products;
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS customers;
"""

# ── Seed data ─────────────────────────────────────────────────────────────────
# Priya Sharma (CUS-10042) is the official showcase scenario customer.
# CUS-00001 has PENDING KYC — used to demonstrate KYC-blocked scenarios.
# CUS-00002 has VERIFIED KYC — normal second customer for variety.

_SEED_SQL = """
-- ── Customers ──────────────────────────────────────────────────────────────
INSERT OR IGNORE INTO customers VALUES
  ('CUS-10042','Priya Sharma',  'priya.sharma@neobank.in','9876543210','VERIFIED','LOW',   'PREMIUM', datetime('now')),
  ('CUS-00001','Raj Kumar',     'raj.kumar@email.com',   '9111111111','PENDING', 'MEDIUM','STANDARD',datetime('now')),
  ('CUS-00002','Anita Desai',   'anita.desai@email.com', '9222222222','VERIFIED','MEDIUM','STANDARD',datetime('now')),
  ('CUS-00003','Vikram Singh',  'vikram@email.com',      '9333333333','REJECTED','HIGH',  'STANDARD',datetime('now'));

-- ── Accounts ───────────────────────────────────────────────────────────────
INSERT OR IGNORE INTO accounts VALUES
  ('ACC-10042','CUS-10042','SAVINGS', 250000.00,'ACTIVE',780, datetime('now')),
  ('ACC-00001','CUS-00001','SAVINGS',   5000.00,'ACTIVE',NULL,datetime('now')),
  ('ACC-00002','CUS-00002','CURRENT',  50000.00,'ACTIVE',650, datetime('now')),
  ('ACC-00003','CUS-00003','SAVINGS',   1000.00,'FROZEN',400, datetime('now'));

-- ── Transactions for Priya (ACC-10042) ────────────────────────────────────
-- Includes a large transaction (>1,000,000) to trigger the compliance flag
INSERT OR IGNORE INTO transactions(account_id, amount, txn_type, description, timestamp) VALUES
  ('ACC-10042', 1200000.00,'CREDIT','Property sale proceeds',           datetime('now','-15 days')),
  ('ACC-10042',-1500000.00,'DEBIT', 'Real estate purchase - Flat 4B',   datetime('now','-14 days')),
  ('ACC-10042',   85000.00,'CREDIT','Salary credit - September',        datetime('now','-4 days')),
  ('ACC-10042',   -2500.00,'DEBIT', 'Electricity bill',                 datetime('now','-3 days')),
  ('ACC-10042',   -8000.00,'DEBIT', 'Rent payment',                     datetime('now','-2 days')),
  ('ACC-10042',    5000.00,'CREDIT','Interest credit',                  datetime('now','-1 day'));

-- Transactions for Anita (ACC-00002)
INSERT OR IGNORE INTO transactions(account_id, amount, txn_type, description, timestamp) VALUES
  ('ACC-00002',  30000.00,'CREDIT','Salary credit',   datetime('now','-5 days')),
  ('ACC-00002', -12000.00,'DEBIT', 'Rent payment',    datetime('now','-4 days')),
  ('ACC-00002',  -3500.00,'DEBIT', 'Utility bills',   datetime('now','-2 days'));

-- ── Loan Products ──────────────────────────────────────────────────────────
INSERT OR IGNORE INTO loan_products VALUES
  ('PROD-PL-01','Personal Loan - Flexi',      'PERSONAL',10.5,  50000, 2000000, 12, 60,'["LOW","MEDIUM"]',
   'Flexible personal loan for salaried and self-employed individuals'),
  ('PROD-PL-02','Personal Loan - Quick',      'PERSONAL',12.0,  25000,  500000,  6, 36,'["LOW","MEDIUM","HIGH"]',
   'Fast-disbursal personal loan with minimal documentation'),
  ('PROD-HL-01','Home Loan - Standard',       'HOME',     8.5,1000000,50000000,120,360,'["LOW"]',
   'Home purchase loan at competitive rates for verified low-risk customers'),
  ('PROD-AL-01','Auto Loan',                  'AUTO',     9.0, 100000, 3000000, 24, 84,'["LOW","MEDIUM"]',
   'Finance your dream vehicle with easy EMIs'),
  ('PROD-BL-01','Business Loan - SME',        'BUSINESS',11.5, 500000,10000000, 12,120,'["LOW","MEDIUM"]',
   'Working capital and expansion loans for small businesses');

-- ── Fraud Flags ────────────────────────────────────────────────────────────
INSERT OR IGNORE INTO fraud_flags(customer_id, account_id, flag_type, description, severity, status) VALUES
  ('CUS-00002','ACC-00002','UNUSUAL_LOGIN',  'Multiple failed login attempts from new device','MEDIUM','OPEN'),
  ('CUS-00003','ACC-00003','SUSPICIOUS_TXN', 'Pattern matches known money-mule activity',    'HIGH',  'INVESTIGATING');

-- ── Audit Log (bootstrap entry) ────────────────────────────────────────────
INSERT OR IGNORE INTO audit_log(action_type, performed_by, customer_id, outcome, metadata_json) VALUES
  ('DB_SEED','SYSTEM',NULL,'SUCCESS','{"note":"initial seed data loaded"}');
"""


# ── Connection helper ─────────────────────────────────────────────────────────

def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row) if row else None


# ── Public init/seed ──────────────────────────────────────────────────────────

def init_db(db_path: str = DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)


def seed_db(db_path: str = DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SEED_SQL)


def reset_db(db_path: str = DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_DROP_SQL)
    init_db(db_path)
    seed_db(db_path)


# ── AccountsDB — only queries: accounts, customers, transactions ──────────────

class AccountsDB:
    """
    Data accessor for accounts_server.
    Structurally cannot query loan_products, loan_applications, fraud_flags,
    audit_log, or communications_log — those methods simply do not exist.
    """

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        return _connect(self._db_path)

    def get_account(self, account_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT a.account_id, a.customer_id, a.account_type, a.balance, "
                "       a.status, a.credit_score, a.opened_at, "
                "       c.kyc_status, c.risk_rating "
                "FROM accounts a "
                "JOIN customers c ON a.customer_id = c.customer_id "
                "WHERE a.account_id = ?",
                (account_id,)
            ).fetchone()
            return _row_to_dict(row)

    def get_accounts_for_customer(self, customer_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT a.account_id, a.customer_id, a.account_type, a.balance, "
                "       a.status, a.credit_score, a.opened_at, "
                "       c.kyc_status, c.risk_rating "
                "FROM accounts a "
                "JOIN customers c ON a.customer_id = c.customer_id "
                "WHERE a.customer_id = ? "
                "ORDER BY a.opened_at DESC",
                (customer_id,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def get_transaction_history(self, account_id: str, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, account_id, amount, txn_type, description, timestamp "
                "FROM transactions "
                "WHERE account_id = ? "
                "ORDER BY timestamp DESC "
                "LIMIT ?",
                (account_id, limit)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def customer_exists(self, customer_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,)
            ).fetchone()
            return row is not None

    def account_exists(self, account_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            return row is not None


# ── ProductsDB — only queries: loan_products, loan_applications ───────────────

class ProductsDB:
    """
    Data accessor for products_server.
    No method here can query customers, accounts, transactions, fraud_flags,
    audit_log, or communications_log.
    """

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        return _connect(self._db_path)

    def list_loan_products(self, category: Optional[str] = None) -> list[dict]:
        with self._conn() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM loan_products WHERE category = ? ORDER BY interest_rate",
                    (category.upper(),)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM loan_products ORDER BY category, interest_rate"
                ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def get_loan_product(self, product_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM loan_products WHERE product_id = ?", (product_id,)
            ).fetchone()
            return _row_to_dict(row)

    def check_eligibility(self, product_id: str, applicant_risk_rating: str) -> dict:
        product = self.get_loan_product(product_id)
        if not product:
            return {"eligible": False, "reason": f"Product '{product_id}' not found"}
        eligible_ratings = json.loads(product["eligible_risk_ratings"])
        eligible = applicant_risk_rating.upper() in eligible_ratings
        return {
            "product_id": product_id,
            "applicant_risk_rating": applicant_risk_rating,
            "eligible": eligible,
            "eligible_risk_ratings": eligible_ratings,
            "reason": (
                "Applicant risk rating meets product criteria"
                if eligible else
                f"Risk rating '{applicant_risk_rating}' not in eligible ratings {eligible_ratings}"
            ),
        }

    def submit_loan_application(
        self,
        application_id: str,
        customer_id: str,
        product_id: str,
        amount: float,
        tenure_months: int,
        risk_rating: str,
    ) -> dict:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO loan_applications "
                "(application_id, customer_id, product_id, amount, tenure_months, status, risk_rating) "
                "VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
                (application_id, customer_id, product_id, amount, tenure_months, risk_rating)
            )
            return {
                "application_id": application_id,
                "customer_id": customer_id,
                "product_id": product_id,
                "amount": amount,
                "tenure_months": tenure_months,
                "status": "PENDING",
                "risk_rating": risk_rating,
            }

    def get_loan_application_status(self, application_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM loan_applications WHERE application_id = ?",
                (application_id,)
            ).fetchone()
            return _row_to_dict(row)

    def get_applications_for_customer(self, customer_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM loan_applications WHERE customer_id = ? ORDER BY created_at DESC",
                (customer_id,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]


# ── ComplianceDB — only queries: customers, communications_log, fraud_flags, audit_log ──

class ComplianceDB:
    """
    Data accessor for compliance_comms_server.
    No method here can query accounts, transactions, loan_products, or
    loan_applications.
    """

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        return _connect(self._db_path)

    def get_customer(self, customer_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT customer_id, name, email, phone, kyc_status, risk_rating, account_tier, created_at "
                "FROM customers WHERE customer_id = ?",
                (customer_id,)
            ).fetchone()
            return _row_to_dict(row)

    def get_kyc_status(self, customer_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT customer_id, kyc_status, risk_rating, account_tier "
                "FROM customers WHERE customer_id = ?",
                (customer_id,)
            ).fetchone()
            return _row_to_dict(row)

    def log_communication(
        self,
        customer_id: str,
        channel: str,
        message: str,
        status: str = "SENT",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO communications_log (customer_id, channel, message, status) "
                "VALUES (?, ?, ?, ?)",
                (customer_id, channel, message, status)
            )
            return cur.lastrowid

    def get_fraud_flags(self, customer_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM fraud_flags WHERE customer_id = ? ORDER BY created_at DESC",
                (customer_id,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def write_audit_log(
        self,
        action_type: str,
        performed_by: str,
        customer_id: Optional[str],
        outcome: str,
        metadata: Optional[dict] = None,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO audit_log "
                "(action_type, performed_by, customer_id, outcome, metadata_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    action_type,
                    performed_by,
                    customer_id,
                    outcome,
                    json.dumps(metadata) if metadata else None,
                )
            )
            return cur.lastrowid

    def get_audit_log(self, customer_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            if customer_id:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE customer_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (customer_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def customer_exists(self, customer_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,)
            ).fetchone()
            return row is not None


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BankForge database management")
    parser.add_argument("--seed",  action="store_true", help="Create tables and insert seed data")
    parser.add_argument("--reset", action="store_true", help="Drop all tables, recreate, and re-seed")
    parser.add_argument("--db",    default=DB_PATH, help=f"Path to SQLite file (default: {DB_PATH})")
    args = parser.parse_args()

    if args.reset:
        print(f"Resetting database at '{args.db}'...")
        reset_db(args.db)
        print("Done — tables dropped, recreated, and seeded.")
    elif args.seed:
        print(f"Initialising database at '{args.db}'...")
        init_db(args.db)
        seed_db(args.db)
        print("Done — tables created and seed data loaded.")
    else:
        parser.print_help()
