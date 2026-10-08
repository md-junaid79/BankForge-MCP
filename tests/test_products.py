"""Integration tests for products_server tools and resources."""

import logging
import pytest

logging.disable(logging.CRITICAL)

from products_server import (
    list_loan_products, get_loan_product_details,
    check_eligibility_criteria, submit_loan_application,
    get_loan_application_status,
)

W = lambda fn: fn.__wrapped__


class TestListLoanProducts:
    def test_returns_all_products(self):
        r = W(list_loan_products)()
        assert r["count"] == 5
        assert r["category_filter"] == "ALL"

    def test_filter_by_category(self):
        r = W(list_loan_products)("PERSONAL")
        assert r["count"] >= 2
        for p in r["products"]:
            assert p["category"] == "PERSONAL"

    def test_invalid_category(self):
        r = W(list_loan_products)("CRYPTO")
        assert r["code"] == "INVALID_INPUT"

    def test_category_case_insensitive(self):
        r = W(list_loan_products)("personal")
        assert r["count"] >= 2


class TestGetLoanProductDetails:
    def test_valid_product(self):
        r = W(get_loan_product_details)("PROD-PL-01")
        assert r["product_id"] == "PROD-PL-01"
        assert r["category"] == "PERSONAL"
        assert r["interest_rate"] == 10.5

    def test_missing_product(self):
        r = W(get_loan_product_details)("PROD-XX-99")
        assert r["code"] == "NOT_FOUND"

    def test_bad_format(self):
        r = W(get_loan_product_details)("BAD")
        assert r["code"] == "INVALID_FORMAT"


class TestCheckEligibilityCriteria:
    def test_low_eligible_for_personal(self):
        r = W(check_eligibility_criteria)("PROD-PL-01", "LOW")
        assert r["eligible"] is True

    def test_medium_eligible_for_personal(self):
        r = W(check_eligibility_criteria)("PROD-PL-01", "MEDIUM")
        assert r["eligible"] is True

    def test_high_not_eligible_for_personal(self):
        r = W(check_eligibility_criteria)("PROD-PL-01", "HIGH")
        assert r["eligible"] is False
        assert "HIGH" in r["reason"]

    def test_low_only_for_home_loan(self):
        r = W(check_eligibility_criteria)("PROD-HL-01", "MEDIUM")
        assert r["eligible"] is False

    def test_invalid_product_format(self):
        r = W(check_eligibility_criteria)("BAD", "LOW")
        assert r["code"] == "INVALID_FORMAT"

    def test_invalid_risk_rating(self):
        r = W(check_eligibility_criteria)("PROD-PL-01", "UNKNOWN")
        assert r["code"] == "INVALID_INPUT"


class TestSubmitLoanApplication:
    def test_kyc_gate_blocks_pending_customer(self):
        r = W(submit_loan_application)("CUS-00001", "PROD-PL-01", 100000, 12)
        assert r["code"] == "KYC_GATE_BLOCKED"

    def test_kyc_gate_blocks_rejected_customer(self):
        r = W(submit_loan_application)("CUS-00003", "PROD-PL-01", 100000, 12)
        assert r["code"] == "KYC_GATE_BLOCKED"

    def test_verified_low_risk_submits_successfully(self):
        r = W(submit_loan_application)("CUS-10042", "PROD-PL-01", 500000, 24)
        assert "application_id" in r
        assert r["status"] == "PENDING"
        assert r["application_id"].startswith("APP-")

    def test_amount_below_minimum_rejected(self):
        r = W(submit_loan_application)("CUS-10042", "PROD-PL-01", 10000, 12)
        assert r["code"] == "AMOUNT_OUT_OF_RANGE"

    def test_amount_above_maximum_rejected(self):
        r = W(submit_loan_application)("CUS-10042", "PROD-PL-01", 9999999, 12)
        assert r["code"] == "AMOUNT_OUT_OF_RANGE"

    def test_tenure_out_of_range(self):
        r = W(submit_loan_application)("CUS-10042", "PROD-PL-01", 500000, 3)
        assert r["code"] == "TENURE_OUT_OF_RANGE"

    def test_missing_customer(self):
        r = W(submit_loan_application)("CUS-99999", "PROD-PL-01", 100000, 12)
        assert r["code"] == "NOT_FOUND"

    def test_bad_customer_format(self):
        r = W(submit_loan_application)("bad", "PROD-PL-01", 100000, 12)
        assert r["code"] == "INVALID_FORMAT"

    def test_bad_product_format(self):
        r = W(submit_loan_application)("CUS-10042", "bad", 100000, 12)
        assert r["code"] == "INVALID_FORMAT"

    def test_ineligible_risk_rating(self):
        # CUS-00002 is VERIFIED but MEDIUM risk — HL-01 only accepts LOW
        r = W(submit_loan_application)("CUS-00002", "PROD-HL-01", 2000000, 120)
        assert r["code"] == "ELIGIBILITY_FAILED"


class TestGetLoanApplicationStatus:
    def test_valid_application(self):
        # First create one
        created = W(submit_loan_application)("CUS-10042", "PROD-PL-01", 75000, 12)
        app_id = created["application_id"]
        r = W(get_loan_application_status)(app_id)
        assert r["application_id"] == app_id
        assert r["status"] == "PENDING"

    def test_missing_application(self):
        r = W(get_loan_application_status)("APP-00000000")
        assert r["code"] == "NOT_FOUND"

    def test_bad_format(self):
        r = W(get_loan_application_status)("BAD-FORMAT")
        assert r["code"] == "INVALID_INPUT"
