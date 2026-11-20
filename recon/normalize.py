"""Parsing and normalization for the two reconciliation inputs.

All money is Decimal, quantized to paise with ROUND_HALF_UP — the same rounding
rule the platform and the bank both use (see ASSUMPTIONS.md 3.7). Nothing in
this module reads ground_truth.json; the domain constants below (fee rate,
RBI holiday calendar) are production knowledge a real reconciler is expected
to be configured with, not derived from the test fixtures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

PAISA = Decimal("0.01")
FEE_RATE = Decimal("0.0236")  # 2% MDR + 18% GST on the MDR, collapsed into one rate

# RBI holiday calendar, Mumbai centre. Sundays and the 2nd/4th Saturdays are
# handled by rule in is_business_day(); the 1st/3rd/5th Saturdays are working days.
BANK_HOLIDAYS = {
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali (Bali Pratipada)",
}


def money(x) -> Decimal:
    """Round half-up to 2 dp — the only rounding used anywhere, on both sides."""
    if not isinstance(x, Decimal):
        x = Decimal(str(x))
    return x.quantize(PAISA, rounding=ROUND_HALF_UP)


def is_business_day(d: date) -> bool:
    if d.weekday() == 6:  # Sunday
        return False
    if d.weekday() == 5 and (d.day - 1) // 7 in (1, 3):  # 2nd / 4th Saturday
        return False
    return d not in BANK_HOLIDAYS


def add_business_days(d: date, n: int) -> date:
    while n:
        d += timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d


# --------------------------------------------------------------------------------------
# Amount / date parsing
# --------------------------------------------------------------------------------------
def parse_indian_amount(raw: str) -> Decimal:
    """Parse a signed amount that may use Indian lakh grouping, e.g. '1,25,430.50'."""
    cleaned = raw.strip().replace(",", "")
    return money(Decimal(cleaned))


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y")


def parse_bank_date(raw: str) -> date:
    """Parse the three mixed date formats the bank statement uses. MM/DD is never used."""
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised bank date format: {raw!r}")


def _clean_text(s: str) -> str:
    """Strip outer whitespace and collapse any doubled internal space to one."""
    return re.sub(r"\s+", " ", s.strip())


# --------------------------------------------------------------------------------------
# Bank statement
# --------------------------------------------------------------------------------------
@dataclass
class BankStatement:
    df: pd.DataFrame            # transactional rows only, one per bank line (opening excluded)
    opening_row: dict           # the excluded BAL B/F row
    opening_balance: Decimal
    closing_balance: Decimal
    balance_breaks: list = field(default_factory=list)  # row numbers where running_balance doesn't reconcile


def load_bank_statement(path: str | Path) -> BankStatement:
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    records = []
    for i, row in raw.iterrows():
        records.append({
            "row": i + 1,  # 1-based data row, header excluded
            "settle_date": parse_bank_date(row["settle_date"]),
            "amount": parse_indian_amount(row["amount"]),
            "reference_raw": row["reference"],
            "reference": _clean_text(row["reference"]),
            "description_raw": row["description"],
            "description": _clean_text(row["description"]),
            "running_balance": parse_indian_amount(row["running_balance"]),
        })

    if not records:
        raise ValueError("Bank statement is empty")

    opening = records[0]
    if opening["description"].upper() != "OPENING BALANCE" and "BAL B/F" not in opening["reference"].upper():
        raise ValueError("Expected the first bank row to be the opening-balance row")

    txn_records = records[1:]
    balance_breaks = []
    running = opening["running_balance"]
    for r in txn_records:
        expected = money(running + r["amount"])
        if expected != r["running_balance"]:
            balance_breaks.append(r["row"])
        running = r["running_balance"]  # resync so one bad row doesn't cascade

    df = pd.DataFrame(txn_records)
    closing = txn_records[-1]["running_balance"] if txn_records else opening["running_balance"]
    return BankStatement(
        df=df,
        opening_row=opening,
        opening_balance=opening["running_balance"],
        closing_balance=closing,
        balance_breaks=balance_breaks,
    )


# --------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------
def load_ledger(path: str | Path) -> pd.DataFrame:
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    records = []
    for _, row in raw.iterrows():
        amount = money(Decimal(row["amount"]))
        fee = money(Decimal(row["fee"]))
        created_at = datetime.fromisoformat(row["created_at"].strip())
        records.append({
            "txn_id": row["txn_id"].strip(),
            "created_at": created_at,
            "capture_date": created_at.date(),
            "amount": amount,
            "currency": row["currency"].strip(),
            "fee": fee,
            "net": money(amount - fee),
            "merchant_id": row["merchant_id"].strip(),
            "type": row["type"].strip(),
            "status": row["status"].strip(),
        })
    return pd.DataFrame(records)
