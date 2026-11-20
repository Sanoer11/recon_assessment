"""Handcrafted fixtures, independent of generate_sample.py, covering every bucket,
plus direct unit tests of the reference-parsing regexes."""
from datetime import date
from decimal import Decimal

from recon.engine import extract_batch_ref, extract_hex_ref, reconcile
from recon.normalize import load_bank_statement, load_ledger

PERIOD_END = date(2026, 10, 31)

LEDGER_CSV = """\
txn_id,created_at,amount,currency,fee,merchant_id,type,status
txn_aaaa11112222,2026-10-01T10:00:00+05:30,1000.00,INR,23.60,M003,sale,succeeded
txn_bbbb11112222,2026-10-05T09:00:00+05:30,500.00,INR,11.80,M002,sale,succeeded
txn_bbbb22223333,2026-10-05T09:05:00+05:30,300.00,INR,7.08,M002,sale,succeeded
txn_cccc11112222,2026-10-06T11:00:00+05:30,9.23,INR,0.22,M005,sale,succeeded
txn_cccc22223333,2026-10-06T11:05:00+05:30,10.85,INR,0.26,M005,sale,succeeded
txn_cccc33334444,2026-10-06T11:10:00+05:30,15.67,INR,0.37,M005,sale,succeeded
txn_dddd11112222,2026-10-31T12:00:00+05:30,777.00,INR,18.34,M007,sale,succeeded
txn_eeee11112222,2026-10-10T13:05:42+05:30,250.00,INR,5.90,M009,sale,succeeded
txn_eeee99998888,2026-10-10T13:05:47+05:30,250.00,INR,5.90,M009,sale,succeeded
txn_eeee55556666,2026-10-13T13:05:42+05:30,250.00,INR,5.90,M009,sale,succeeded
txn_ffff11112222,2026-10-15T10:00:00+05:30,-100.00,INR,0.00,M010,refund,succeeded
txn_cafe11112222,2026-10-02T09:00:00+05:30,150.00,INR,3.54,M012,sale,succeeded
txn_cafe99998888,2026-10-20T09:00:00+05:30,-150.00,INR,0.00,M012,refund,succeeded
"""

BANK_CSV = """\
settle_date,amount,reference,description,running_balance
2026-10-01,0.00,BAL B/F,OPENING BALANCE,100000.00
2026-10-03,976.40,NEFT/UTIBN26276111111/PAYGW SETTL txn_aaaa11112222,NEFT CR,100976.40
04/10/2026,146.46,NEFT/UTIBN26277222222/PAYGW SETTL txn_cafe11112222,NEFT CR,101122.86
2026-10-07,781.12,NEFT/UTIBN26280333333/PAYGW BATCH B-M002-20261005,NEFT CR,101903.98
08-Oct-2026,34.91,PAYGW SETTL BM00520261006,NEFT CR,101938.89
2026-10-12,244.10,PAYGW/EEEE111122,NEFT CR,102182.99
2026-10-15,244.10,HDFCN26288444444-STL-TXNEEEE55556666-M009,NEFT CR,102427.09
2026-10-16,-100.00,PAYGW RFND txn_ffff11112222,NEFT DR,102327.09
2026-10-22,-150.00,NEFT/UTIBN26295555555/RFND/CAFE999988,NEFT DR,102177.09
2026-10-31,-59.00,ACCT MAINT CHGS INCL GST,CHRG,102118.09
"""


def _load(tmp_path):
    ledger_path = tmp_path / "ledger.csv"
    bank_path = tmp_path / "bank.csv"
    ledger_path.write_text(LEDGER_CSV)
    bank_path.write_text(BANK_CSV)
    ledger = load_ledger(ledger_path)
    bank = load_bank_statement(bank_path)
    return reconcile(ledger, bank, PERIOD_END)


def test_handcrafted_fixture_buckets(tmp_path):
    result = _load(tmp_path)
    buckets = dict(zip(result.ledger["txn_id"], result.ledger["bucket"]))

    assert buckets["txn_aaaa11112222"] == "matched"
    assert buckets["txn_bbbb11112222"] == "matched"
    assert buckets["txn_bbbb22223333"] == "matched"
    assert buckets["txn_cccc11112222"] == "rounding_variance"
    assert buckets["txn_cccc22223333"] == "rounding_variance"
    assert buckets["txn_cccc33334444"] == "rounding_variance"
    assert buckets["txn_dddd11112222"] == "timing_difference"
    assert buckets["txn_eeee11112222"] == "matched"
    assert buckets["txn_eeee99998888"] == "duplicate"
    assert buckets["txn_eeee55556666"] == "matched"
    assert buckets["txn_ffff11112222"] == "unmatched_refund"
    assert buckets["txn_cafe11112222"] == "matched"
    assert buckets["txn_cafe99998888"] == "matched"


def test_handcrafted_fixture_rounding_variance_is_explained(tmp_path):
    result = _load(tmp_path)
    exc = next(e for e in result.exceptions if e["bucket"] == "rounding_variance")
    assert set(exc["ledger_txn_ids"]) == {
        "txn_cccc11112222", "txn_cccc22223333", "txn_cccc33334444"}
    gross = Decimal(exc["details"]["gross"])
    batch_fee = Decimal(exc["details"]["batch_fee_recomputed"])
    bank_amount = Decimal(exc["details"]["bank_amount"])
    assert gross == Decimal("35.75")
    assert batch_fee == Decimal("0.84")
    assert bank_amount - (gross - batch_fee) == 0
    assert Decimal(exc["diff"]) == Decimal("0.01")


def test_handcrafted_fixture_timing_difference_settle_date(tmp_path):
    result = _load(tmp_path)
    exc = next(e for e in result.exceptions if "txn_dddd11112222" in e["ledger_txn_ids"])
    assert exc["details"]["expected_settle_date"] == "2026-11-02"


def test_handcrafted_fixture_duplicate_points_at_original(tmp_path):
    result = _load(tmp_path)
    exc = next(e for e in result.exceptions if "txn_eeee99998888" in e["ledger_txn_ids"])
    assert exc["details"]["duplicate_of"] == "txn_eeee11112222"


def test_handcrafted_fixture_bank_buckets(tmp_path):
    result = _load(tmp_path)
    bank_buckets = dict(zip(result.bank["row"], result.bank["bucket"]))
    assert bank_buckets[10] == "bank_only"  # the CHRG line


def test_handcrafted_fixture_bridge_residual_zero(tmp_path):
    result = _load(tmp_path)
    assert result.bridge["RESIDUAL"] == Decimal("0.00")


def test_handcrafted_fixture_stage_counts(tmp_path):
    result = _load(tmp_path)
    # every settled unit here resolves via S1 (txn ref) or S2 (batch ref); none need S3
    assert result.stage_counts["S3"] == 0
    assert result.stage_counts["S1"] + result.stage_counts["S2"] == 8


# ---------------------------------------------------------------------------------
# Reference parsing: truncated / uppercased / full / batch forms
# ---------------------------------------------------------------------------------
def test_extract_hex_ref_full_form():
    assert extract_hex_ref("NEFT/UTIBN26276111111/PAYGW SETTL txn_5c80f3a1e9b4") == "5c80f3a1e9b4"


def test_extract_hex_ref_truncated_uppercased_form():
    assert extract_hex_ref("PAYGW/5C80F3A1E9") == "5c80f3a1e9"


def test_extract_hex_ref_prefixed_uppercased_form():
    assert extract_hex_ref("HDFCN26276111111-STL-TXN5C80F3A1E9B4-M006") == "5c80f3a1e9b4"


def test_extract_hex_ref_refund_forms():
    assert extract_hex_ref("PAYGW RFND txn_5c80f3a1e9b4") == "5c80f3a1e9b4"
    assert extract_hex_ref("NEFT/UTIBN26276111111/RFND/5C80F3A1E9") == "5c80f3a1e9"


def test_extract_hex_ref_returns_none_for_batch_reference():
    assert extract_hex_ref("NEFT/UTIBN26276111111/PAYGW BATCH B-M011-20261015") is None


def test_extract_batch_ref_dashed_and_concatenated_forms():
    assert extract_batch_ref("NEFT/UTIBN26276111111/PAYGW BATCH B-M011-20261015") == "B-M011-20261015"
    assert extract_batch_ref("PAYGW SETTL BM01120261015") == "B-M011-20261015"


def test_extract_hex_ref_handles_whitespace_noise():
    assert extract_hex_ref("  PAYGW/5C80F3A1E9  ") == "5c80f3a1e9"
    assert extract_hex_ref("NEFT/UTIBN26276111111/PAYGW  SETTL txn_5c80f3a1e9b4") == "5c80f3a1e9b4"
