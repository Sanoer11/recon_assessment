from datetime import date
from decimal import Decimal

import pytest

from recon.normalize import (
    load_bank_statement,
    parse_bank_date,
    parse_indian_amount,
)

BANK_COLUMNS = "settle_date,amount,reference,description,running_balance\n"


def _write_bank(tmp_path, rows: str):
    path = tmp_path / "bank.csv"
    path.write_text(BANK_COLUMNS + rows)
    return path


# ---------------------------------------------------------------------------------
# Amount parsing
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("1,25,430.50", Decimal("125430.50")),
    ("652.22", Decimal("652.22")),
    ("-590.00", Decimal("-590.00")),
    ("5,00,000.00", Decimal("500000.00")),
    ("  1,234.50  ", Decimal("1234.50")),
])
def test_parse_indian_amount(raw, expected):
    assert parse_indian_amount(raw) == expected


# ---------------------------------------------------------------------------------
# Date parsing: all three mixed formats
# ---------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["2026-10-14", "14/10/2026", "14-Oct-2026"])
def test_parse_bank_date_formats(raw):
    assert parse_bank_date(raw) == date(2026, 10, 14)


def test_parse_bank_date_rejects_unknown_format():
    with pytest.raises(ValueError):
        parse_bank_date("2026/14/10")


# ---------------------------------------------------------------------------------
# Opening balance row detection and exclusion
# ---------------------------------------------------------------------------------
def test_opening_balance_row_is_excluded(tmp_path):
    rows = (
        "2026-10-01,0.00,BAL B/F,OPENING BALANCE,500000.00\n"
        "2026-10-02,100.00,PAYGW/AAAAAAAAAA,NEFT CR,500100.00\n"
    )
    bs = load_bank_statement(_write_bank(tmp_path, rows))
    assert len(bs.df) == 1
    assert bs.opening_balance == Decimal("500000.00")
    assert bs.df.iloc[0]["row"] == 2


# ---------------------------------------------------------------------------------
# Whitespace noise in reference/description
# ---------------------------------------------------------------------------------
def test_whitespace_noise_is_normalized(tmp_path):
    rows = (
        "2026-10-01,0.00,BAL B/F,OPENING BALANCE,500000.00\n"
        "2026-10-02,100.00,  PAYGW/AAAAAAAAAA  ,NEFT  CR,500100.00\n"
    )
    bs = load_bank_statement(_write_bank(tmp_path, rows))
    row = bs.df.iloc[0]
    assert row["reference"] == "PAYGW/AAAAAAAAAA"
    assert row["description"] == "NEFT CR"
    assert row["reference_raw"] == "  PAYGW/AAAAAAAAAA  "


# ---------------------------------------------------------------------------------
# Running balance verification catches an injected break
# ---------------------------------------------------------------------------------
def test_running_balance_break_is_detected(tmp_path):
    rows = (
        "2026-10-01,0.00,BAL B/F,OPENING BALANCE,500000.00\n"
        "2026-10-02,100.00,PAYGW/AAAAAAAAAA,NEFT CR,500100.00\n"
        "2026-10-03,50.00,PAYGW/BBBBBBBBBB,NEFT CR,500200.00\n"  # should be 500150.00
    )
    bs = load_bank_statement(_write_bank(tmp_path, rows))
    assert bs.balance_breaks == [3]


def test_running_balance_consistent_on_clean_data(tmp_path):
    rows = (
        "2026-10-01,0.00,BAL B/F,OPENING BALANCE,500000.00\n"
        "2026-10-02,100.00,PAYGW/AAAAAAAAAA,NEFT CR,500100.00\n"
        "2026-10-03,-50.00,PAYGW RFND txn_bbbbbbbbbbbb,NEFT DR,500050.00\n"
    )
    bs = load_bank_statement(_write_bank(tmp_path, rows))
    assert bs.balance_breaks == []
