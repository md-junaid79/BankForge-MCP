"""Integration tests for accounts_server tools and resources."""

import logging
import pytest

logging.disable(logging.CRITICAL)  # suppress @trace output during tests

from accounts_server import (
    get_account_summary, get_accounts_for_customer, get_transaction_history,
)

W = lambda fn: fn.__wrapped__   # call the unwrapped function directly


class TestGetAccountSummary:
    def test_teller_sees_masked_only(self):
        r = W(get_account_summary)("ACC-10042", "teller")
        assert "masked_account_number" in r
        assert "balance" not in r
        assert "account_id" not in r
        assert r["masked_account_number"] == "****0042"

    def test_loan_officer_sees_balance(self):
        r = W(get_account_summary)("ACC-10042", "loan_officer")
        assert "balance" in r
        assert r["balance"] == 250000.0

    def test_compliance_officer_sees_kyc(self):
        r = W(get_account_summary)("ACC-10042", "compliance_officer")
        assert "kyc_status" in r
        assert r["kyc_status"] == "VERIFIED"

    def test_missing_account_returns_not_found(self):
        r = W(get_account_summary)("ACC-99999", "teller")
        assert r["code"] == "NOT_FOUND"

    def test_bad_format_returns_invalid_format(self):
        r = W(get_account_summary)("bad-id", "teller")
        assert r["code"] == "INVALID_FORMAT"

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError):
            W(get_account_summary)("ACC-10042", "superuser")


class TestGetAccountsForCustomer:
    def test_valid_customer_returns_accounts(self):
        r = W(get_accounts_for_customer)("CUS-10042", "teller")
        assert r["count"] == 1
        assert r["customer_id"] == "CUS-10042"
        assert len(r["accounts"]) == 1

    def test_accounts_are_masked(self):
        r = W(get_accounts_for_customer)("CUS-10042", "teller")
        for acct in r["accounts"]:
            assert "account_id" not in acct
            assert "masked_account_number" in acct

    def test_missing_customer_returns_not_found(self):
        r = W(get_accounts_for_customer)("CUS-99999", "teller")
        assert r["code"] == "NOT_FOUND"

    def test_bad_customer_format(self):
        r = W(get_accounts_for_customer)("bad", "teller")
        assert r["code"] == "INVALID_FORMAT"


class TestGetTransactionHistory:
    def test_returns_transactions(self):
        r = W(get_transaction_history)("ACC-10042", 10)
        assert r["transaction_count"] > 0
        assert isinstance(r["transactions"], list)

    def test_account_id_is_masked_in_response(self):
        r = W(get_transaction_history)("ACC-10042", 5)
        assert r["account_id"].startswith("****")
        assert "ACC-10042" not in r["account_id"]

    def test_limit_respected(self):
        r = W(get_transaction_history)("ACC-10042", 2)
        assert r["transaction_count"] <= 2

    def test_limit_clamped_to_100(self):
        r = W(get_transaction_history)("ACC-10042", 999)
        assert r["transaction_count"] <= 100

    def test_missing_account(self):
        r = W(get_transaction_history)("ACC-99999", 5)
        assert r["code"] == "NOT_FOUND"

    def test_bad_account_format(self):
        r = W(get_transaction_history)("bad", 5)
        assert r["code"] == "INVALID_FORMAT"

    def test_most_recent_first(self):
        r = W(get_transaction_history)("ACC-10042", 10)
        timestamps = [t["timestamp"] for t in r["transactions"]]
        assert timestamps == sorted(timestamps, reverse=True)
