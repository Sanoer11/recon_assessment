"""Mutation tests: take the real sample data, perturb one thing, and confirm the
engine reacts the way a production reconciler should."""
from datetime import date
from decimal import Decimal

import pandas as pd

from recon.engine import reconcile
from recon.normalize import BankStatement

PERIOD_END = date(2026, 10, 31)


def _bank_copy(sample_bank: BankStatement) -> BankStatement:
    return BankStatement(
        df=sample_bank.df.copy(),
        opening_row=sample_bank.opening_row,
        opening_balance=sample_bank.opening_balance,
        closing_balance=sample_bank.closing_balance,
        balance_breaks=list(sample_bank.balance_breaks),
    )


def _one_to_one_matched_sale(sample_result, before: date | None = None):
    for r in sample_result.ledger.itertuples():
        if r.bucket != "matched" or r.stage != "S1" or r.type != "sale":
            continue
        if before and r.capture_date >= before:
            continue
        bank_row = next(b.row for b in sample_result.bank.itertuples()
                         if r.txn_id in b.ledger_txn_ids)
        return r.txn_id, bank_row
    raise AssertionError("no matching candidate found in sample data")


def test_deleting_a_bank_line_makes_the_txn_unsettled(sample_ledger, sample_bank, sample_result):
    # Pick a row captured early enough that the settlement-lag calendar can't
    # explain its absence -- it must come back as a genuine gap, not timing.
    txn_id, bank_row = _one_to_one_matched_sale(sample_result, before=date(2026, 10, 15))

    mutated = _bank_copy(sample_bank)
    mutated.df = mutated.df[mutated.df["row"] != bank_row].reset_index(drop=True)

    result = reconcile(sample_ledger, mutated, PERIOD_END)
    assert result.ledger.set_index("txn_id").loc[txn_id, "bucket"] == "unsettled"


def test_duplicating_a_bank_line_is_detected(sample_ledger, sample_bank, sample_result):
    txn_id, bank_row = _one_to_one_matched_sale(sample_result)

    mutated = _bank_copy(sample_bank)
    dup = mutated.df[mutated.df["row"] == bank_row].copy()
    new_row_num = int(mutated.df["row"].max()) + 1
    dup["row"] = new_row_num
    mutated.df = pd.concat([mutated.df, dup], ignore_index=True)

    result = reconcile(sample_ledger, mutated, PERIOD_END)
    bucket = result.bank.set_index("row").loc[new_row_num, "bucket"]
    assert bucket == "duplicate_bank_line"
    # the original settlement is untouched
    assert result.ledger.set_index("txn_id").loc[txn_id, "bucket"] == "matched"


def test_amount_mismatch_is_detected_and_bridge_still_balances(sample_ledger, sample_bank,
                                                                sample_result):
    txn_id, bank_row = _one_to_one_matched_sale(sample_result)

    mutated = _bank_copy(sample_bank)
    idx = mutated.df.index[mutated.df["row"] == bank_row][0]
    mutated.df.loc[idx, "amount"] = mutated.df.loc[idx, "amount"] + Decimal("1.00")

    result = reconcile(sample_ledger, mutated, PERIOD_END)
    assert result.ledger.set_index("txn_id").loc[txn_id, "bucket"] == "amount_mismatch"
    assert result.bridge["RESIDUAL"] == Decimal("0.00")


def test_reconcile_is_deterministic(sample_ledger, sample_bank):
    r1 = reconcile(sample_ledger, sample_bank, PERIOD_END)
    r2 = reconcile(sample_ledger, sample_bank, PERIOD_END)
    assert list(r1.ledger["bucket"]) == list(r2.ledger["bucket"])
    assert list(r1.bank["bucket"]) == list(r2.bank["bucket"])
    assert r1.bridge == r2.bridge
    assert r1.exceptions == r2.exceptions
