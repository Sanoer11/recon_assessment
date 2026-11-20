"""Tests that compare the engine's output against sample_data/ground_truth.json.

Only this test module (and the other files in tests/) may read ground_truth.json.
recon/normalize.py and recon/engine.py never do.
"""
import collections
from decimal import Decimal


def _confusion_matrix(mismatches):
    cm = collections.Counter((exp, got) for _, exp, got in mismatches)
    lines = ["confusion matrix (expected -> got): count"]
    for (exp, got), count in sorted(cm.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {exp!r:>22} -> {got!r:<22} {count}")
    for tid, exp, got in mismatches[:25]:
        lines.append(f"    id={tid} expected={exp} got={got}")
    return "\n".join(lines)


def test_every_ledger_row_lands_in_expected_bucket(sample_result, ground_truth):
    expected = ground_truth["expected_ledger_bucket"]
    got = dict(zip(sample_result.ledger["txn_id"], sample_result.ledger["bucket"]))

    mismatches = [(tid, exp, got.get(tid, "MISSING")) for tid, exp in expected.items()
                  if got.get(tid, "MISSING") != exp]
    assert not mismatches, (
        f"{len(mismatches)}/{len(expected)} ledger rows in the wrong bucket\n"
        + _confusion_matrix(mismatches)
    )


def test_every_bank_row_lands_in_expected_bucket(sample_result, ground_truth):
    expected = ground_truth["expected_bank_bucket"]
    got = {str(row): bucket for row, bucket in
           zip(sample_result.bank["row"], sample_result.bank["bucket"])}
    got["1"] = "non_transaction"  # opening row is excluded from the engine's dataframe

    mismatches = [(row, exp, got.get(row, "MISSING")) for row, exp in expected.items()
                  if got.get(row, "MISSING") != exp]
    assert not mismatches, (
        f"{len(mismatches)}/{len(expected)} bank rows in the wrong bucket\n"
        + _confusion_matrix(mismatches)
    )


def test_gap_a_late_settlement(sample_result, ground_truth):
    gap = next(g for g in ground_truth["planted_gaps"] if g["id"] == "GAP_A_LATE_SETTLEMENT")
    txn_id = gap["ledger_txn_ids"][0]
    row = sample_result.ledger.set_index("txn_id").loc[txn_id]
    assert row["bucket"] == "timing_difference"
    exc = next(e for e in sample_result.exceptions if txn_id in e["ledger_txn_ids"])
    assert exc["details"]["expected_settle_date"] == gap["details"]["expected_settle_date"]


def test_gap_b_aggregate_rounding(sample_result, ground_truth):
    gap = next(g for g in ground_truth["planted_gaps"] if g["id"] == "GAP_B_AGGREGATE_ROUNDING")
    ledger_ids = set(gap["ledger_txn_ids"])
    rows = sample_result.ledger.set_index("txn_id").loc[list(ledger_ids)]

    # every line in the batch is flagged rounding_variance -- never a single line
    assert set(rows["bucket"]) == {"rounding_variance"}

    exc = next(e for e in sample_result.exceptions if set(e["ledger_txn_ids"]) == ledger_ids)
    assert Decimal(exc["diff"]) == Decimal(gap["details"]["delta"]) == Decimal("0.06")
    # the batch-fee recompute must explain the diff exactly
    gross = Decimal(exc["details"]["gross"])
    batch_fee = Decimal(exc["details"]["batch_fee_recomputed"])
    ledger_expected_net = Decimal(exc["details"]["ledger_expected_net"])
    bank_amount = Decimal(exc["details"]["bank_amount"])
    assert bank_amount - (gross - batch_fee) == 0
    assert bank_amount - ledger_expected_net == Decimal("0.06")


def test_gap_c_duplicate_and_decoy(sample_result, ground_truth):
    gap = next(g for g in ground_truth["planted_gaps"] if g["id"] == "GAP_C_LEDGER_DUPLICATE")
    dup_id = gap["ledger_txn_ids"][0]
    orig_id = gap["details"]["duplicate_of"]
    ledger = sample_result.ledger.set_index("txn_id")
    assert ledger.loc[dup_id, "bucket"] == "duplicate"
    assert ledger.loc[orig_id, "bucket"] == "matched"

    decoy = next(d for d in ground_truth["decoys"] if d["kind"] == "same_merchant_same_amount")
    decoy_id = decoy["ledger_txn_ids"][0]
    assert ledger.loc[decoy_id, "bucket"] == "matched"


def test_gap_d_orphan_refund_and_decoys(sample_result, ground_truth):
    gap = next(g for g in ground_truth["planted_gaps"] if g["id"] == "GAP_D_ORPHAN_REFUND")
    orphan_id = gap["ledger_txn_ids"][0]
    ledger = sample_result.ledger.set_index("txn_id")
    assert ledger.loc[orphan_id, "bucket"] == "unmatched_refund"

    for decoy in ground_truth["decoys"]:
        if decoy["kind"] == "refund_with_original":
            assert ledger.loc[decoy["ledger_txn_ids"][0], "bucket"] == "matched"


def test_bridge_residual_is_zero(sample_result):
    assert sample_result.bridge["RESIDUAL"] == Decimal("0.00")


def test_unmatched_refund_has_zero_variance_effect(sample_result, ground_truth):
    assert sample_result.bridge.get("unmatched_refund", Decimal("0.00")) == Decimal("0.00")
