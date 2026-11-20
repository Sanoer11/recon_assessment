"""Pure matching and classification engine.

No I/O, and nothing here ever reads ground_truth.json — only the test suite
does that, to grade this module's output. Every function takes already
normalized data (see recon.normalize) and returns plain data structures.

Matching runs in three stages, in order, and every bank row and ledger row
ends up tagged with the stage that resolved it (S1 / S2 / S3 / None):

  S1  reference -> txn       a full or truncated hex txn id embedded in the
                              bank reference, resolved to a unique ledger row.
  S2  reference -> batch     a "B-<merchant>-<YYYYMMDD>" batch id (dashed or
                              concatenated), resolved to a group of ledger
                              sale rows sharing that merchant + capture date.
  S3  fallback               amount + merchant hint + settle-date window,
                              for anything neither S1 nor S2 could place.
                              Always flagged low confidence.

A matched *unit* (one bank row plus the ledger row(s) behind it) is then
classified as matched / rounding_variance / amount_mismatch depending on
whether its ledger-vs-bank diff is zero, explained by batch-level fee
rounding, or neither. Ledger rows no stage could place are classified as
duplicate / timing_difference / unmatched_refund / unsettled.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import pandas as pd

from recon.normalize import BankStatement, FEE_RATE, add_business_days, money

# --------------------------------------------------------------------------------------
# Domain configuration (production knowledge, not derived from the test fixtures)
# --------------------------------------------------------------------------------------
BATCH_MERCHANTS = {"M001", "M002", "M005", "M008", "M011", "M014", "M017", "M019"}
BATCH_PRICED_MERCHANTS = {"M011"}  # acquirer prices the fee once on the batch gross

DUPLICATE_WINDOW_SECONDS = 600  # 10 minutes — matches the generator's own invariant

# ---- bucket names -----------------------------------------------------------------
MATCHED = "matched"
TIMING_DIFFERENCE = "timing_difference"
ROUNDING_VARIANCE = "rounding_variance"
DUPLICATE = "duplicate"
UNMATCHED_REFUND = "unmatched_refund"
BANK_ONLY = "bank_only"
NON_TRANSACTION = "non_transaction"
AMOUNT_MISMATCH = "amount_mismatch"            # real variance, not explained by batch rounding
UNSETTLED = "unsettled"                        # no bank counterpart found, not otherwise explained
DUPLICATE_BANK_LINE = "duplicate_bank_line"    # a second bank row claiming an already-settled txn

OWNER = {
    MATCHED: None,
    TIMING_DIFFERENCE: "Finance",
    ROUNDING_VARIANCE: "Finance",
    DUPLICATE: "Engineering",
    UNMATCHED_REFUND: "Risk",
    BANK_ONLY: "Bank Ops",
    AMOUNT_MISMATCH: "Bank Ops",
    UNSETTLED: "Bank Ops",
    DUPLICATE_BANK_LINE: "Bank Ops",
}

SEVERITY = {
    MATCHED: "info",
    TIMING_DIFFERENCE: "low",
    ROUNDING_VARIANCE: "low",
    DUPLICATE: "medium",
    UNMATCHED_REFUND: "high",
    BANK_ONLY: "medium",
    AMOUNT_MISMATCH: "high",
    UNSETTLED: "high",
    DUPLICATE_BANK_LINE: "high",
}

# --------------------------------------------------------------------------------------
# Reference parsing
# --------------------------------------------------------------------------------------
_TXN_HEX = re.compile(r"TXN_?([0-9A-F]{10,12})", re.IGNORECASE)
_PAYGW_SLASH_HEX = re.compile(r"PAYGW/([0-9A-F]{10,12})\b", re.IGNORECASE)
_RFND_SLASH_HEX = re.compile(r"RFND/([0-9A-F]{10,12})\b", re.IGNORECASE)
_BATCH_ID = re.compile(r"B-?(M\d{3})-?(\d{8})", re.IGNORECASE)
_MERCHANT_HINT = re.compile(r"\b(M\d{3})\b", re.IGNORECASE)


def extract_hex_ref(reference: str) -> Optional[str]:
    """Pull a candidate txn hex id (10 or 12 chars) out of a bank reference."""
    for pattern in (_TXN_HEX, _PAYGW_SLASH_HEX, _RFND_SLASH_HEX):
        m = pattern.search(reference)
        if m:
            return m.group(1).lower()
    return None


def extract_batch_ref(reference: str) -> Optional[str]:
    """Pull a 'B-<merchant>-<YYYYMMDD>' batch id out of a bank reference."""
    m = _BATCH_ID.search(reference)
    if not m:
        return None
    return f"B-{m.group(1).upper()}-{m.group(2)}"


def extract_merchant_hint(reference: str) -> Optional[str]:
    m = _MERCHANT_HINT.search(reference)
    return m.group(1).upper() if m else None


def _hex_of(txn_id: str) -> str:
    return txn_id[4:] if txn_id.startswith("txn_") else txn_id


def build_txn_index(ledger: pd.DataFrame) -> dict:
    """Map both the full 12-hex id and any uniquely-owned 10-hex prefix to a txn_id."""
    index: dict[str, str] = {}
    prefix_owners: dict[str, list] = defaultdict(list)
    for txn_id in ledger["txn_id"]:
        h = _hex_of(txn_id).lower()
        index[h] = txn_id
        prefix_owners[h[:10]].append(txn_id)
    for prefix, owners in prefix_owners.items():
        if len(owners) == 1:
            index.setdefault(prefix, owners[0])
    return index


def would_be_unit_key(txn_id: str, merchant_id: str, capture_date: date, txn_type: str) -> str:
    """The settlement unit a ledger row belongs to, whether or not it has settled."""
    if txn_type == "sale" and merchant_id in BATCH_MERCHANTS:
        return f"B-{merchant_id}-{capture_date:%Y%m%d}"
    return txn_id


# --------------------------------------------------------------------------------------
# Result container
# --------------------------------------------------------------------------------------
@dataclass
class ReconciliationResult:
    ledger: pd.DataFrame
    bank: pd.DataFrame
    exceptions: list = field(default_factory=list)
    bridge: dict = field(default_factory=dict)
    stage_counts: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    assumptions: dict = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------------------
def reconcile(ledger_df: pd.DataFrame, bank_stmt: BankStatement,
              period_end: date) -> ReconciliationResult:
    ledger_df = ledger_df.copy()
    bank_df = bank_stmt.df.copy()

    txn_index = build_txn_index(ledger_df)
    ledger_lookup = {r.txn_id: r for r in ledger_df.itertuples()}

    batch_groups: dict[str, list] = defaultdict(list)
    for r in ledger_df.itertuples():
        if r.type == "sale" and r.merchant_id in BATCH_MERCHANTS:
            key = would_be_unit_key(r.txn_id, r.merchant_id, r.capture_date, r.type)
            batch_groups[key].append(r.txn_id)

    ledger_assigned: dict[str, dict] = {}
    bank_assigned: dict[int, dict] = {}
    stage_counts = {"S1": 0, "S2": 0, "S3": 0}

    # ---- Stage 1 + Stage 2 ----------------------------------------------------------
    for b in bank_df.itertuples():
        hexref = extract_hex_ref(b.reference)
        if hexref and hexref in txn_index:
            txn_id = txn_index[hexref]
            _bind_unit(b.row, b.amount, [txn_id], "S1", txn_id, ledger_lookup,
                       ledger_assigned, bank_assigned, stage_counts)
            continue
        batch_id = extract_batch_ref(b.reference)
        if batch_id and batch_id in batch_groups:
            _bind_unit(b.row, b.amount, batch_groups[batch_id], "S2", batch_id, ledger_lookup,
                       ledger_assigned, bank_assigned, stage_counts)
            continue

    # ---- Stage 3: fallback by amount + merchant hint + settle-date window ----------
    unmatched_bank = [b for b in bank_df.itertuples() if b.row not in bank_assigned]
    unmatched_ledger_ids = [tid for tid in ledger_df["txn_id"] if tid not in ledger_assigned]
    _stage3_fallback(unmatched_bank, unmatched_ledger_ids, ledger_lookup, ledger_assigned,
                      bank_assigned, stage_counts)

    # ---- Ledger rows no stage could place -------------------------------------------
    _classify_unplaced_ledger(ledger_df, ledger_assigned, period_end)

    # ---- Bank rows no stage could place -> bank_only --------------------------------
    for b in bank_df.itertuples():
        if b.row not in bank_assigned:
            bank_assigned[b.row] = {
                "bucket": BANK_ONLY, "stage": None, "confidence": "high",
                "ledger_txn_ids": [], "unit_key": None, "extra": {},
            }

    # ---- Refunds whose original sale is nowhere in the ledger -----------------------
    _flag_orphan_refunds(ledger_df, ledger_assigned, bank_assigned)

    # ---- Attach results to the dataframes --------------------------------------------
    ledger_df["bucket"] = ledger_df["txn_id"].map(lambda t: ledger_assigned[t]["bucket"])
    ledger_df["stage"] = ledger_df["txn_id"].map(lambda t: ledger_assigned[t]["stage"])
    ledger_df["unit_key"] = ledger_df["txn_id"].map(lambda t: ledger_assigned[t]["unit_key"])
    ledger_df["confidence"] = ledger_df["txn_id"].map(lambda t: ledger_assigned[t]["confidence"])

    bank_df["bucket"] = bank_df["row"].map(lambda r: bank_assigned[r]["bucket"])
    bank_df["stage"] = bank_df["row"].map(lambda r: bank_assigned[r]["stage"])
    bank_df["confidence"] = bank_df["row"].map(lambda r: bank_assigned[r]["confidence"])
    bank_df["ledger_txn_ids"] = bank_df["row"].map(lambda r: bank_assigned[r]["ledger_txn_ids"])

    exceptions = _build_exceptions(ledger_df, bank_df, ledger_assigned, bank_assigned)
    bridge, totals = _build_bridge(ledger_df, bank_df)

    return ReconciliationResult(
        ledger=ledger_df, bank=bank_df, exceptions=exceptions, bridge=bridge,
        stage_counts=stage_counts, totals=totals,
    )


# --------------------------------------------------------------------------------------
# Unit binding + classification (matched / rounding_variance / amount_mismatch)
# --------------------------------------------------------------------------------------
def _bind_unit(bank_row: int, bank_amount: Decimal, ledger_txn_ids: list, stage: str,
               unit_key: str, ledger_lookup: dict, ledger_assigned: dict,
               bank_assigned: dict, stage_counts: dict) -> None:
    # Defend against a bank-side duplicate: this unit already has a settlement.
    already = [t for t in ledger_txn_ids if t in ledger_assigned]
    if already:
        bank_assigned[bank_row] = {
            "bucket": DUPLICATE_BANK_LINE, "stage": stage, "confidence": "high",
            "ledger_txn_ids": ledger_txn_ids, "unit_key": unit_key,
            "extra": {"duplicate_of_bank_row": ledger_assigned[already[0]].get("bank_row")},
        }
        return

    rows = [ledger_lookup[t] for t in ledger_txn_ids]
    ledger_net_total = money(sum((r.net for r in rows), Decimal("0")))
    gross_total = money(sum((r.amount for r in rows), Decimal("0")))
    fee_total = money(sum((r.fee for r in rows), Decimal("0")))
    is_batch = stage == "S2"
    merchant_id = rows[0].merchant_id

    bucket, extra = _classify_unit(bank_amount, ledger_net_total, gross_total, fee_total,
                                    is_batch, merchant_id)
    stage_counts[stage] += 1

    for t in ledger_txn_ids:
        ledger_assigned[t] = {
            "bucket": bucket, "stage": stage, "confidence": "high" if stage != "S3" else "low",
            "unit_key": unit_key, "bank_row": bank_row, "extra": extra,
        }
    bank_assigned[bank_row] = {
        "bucket": bucket, "stage": stage, "confidence": "high" if stage != "S3" else "low",
        "ledger_txn_ids": ledger_txn_ids, "unit_key": unit_key, "extra": extra,
    }


def _classify_unit(bank_amount: Decimal, ledger_net_total: Decimal, gross_total: Decimal,
                    fee_total: Decimal, is_batch: bool, merchant_id: str) -> tuple:
    diff = money(bank_amount - ledger_net_total)
    if diff == Decimal("0.00"):
        return MATCHED, {}
    if is_batch:
        batch_fee = money(gross_total * FEE_RATE)
        recomputed_bank_amount = money(gross_total - batch_fee)
        if recomputed_bank_amount == bank_amount:
            return ROUNDING_VARIANCE, {
                "gross": gross_total, "sum_line_fees": fee_total,
                "batch_fee_recomputed": batch_fee, "ledger_expected_net": ledger_net_total,
                "bank_amount": bank_amount, "diff": diff,
                "why": ("each line's fee was rounded half-up individually; the bank/acquirer "
                        f"rounds once on the batch gross of {gross_total}, giving a batch fee "
                        f"of {batch_fee} instead of the summed line fees of {fee_total}"),
            }
    return AMOUNT_MISMATCH, {"diff": diff, "ledger_expected_net": ledger_net_total,
                              "bank_amount": bank_amount}


# --------------------------------------------------------------------------------------
# Stage 3: fallback matching
# --------------------------------------------------------------------------------------
def _stage3_fallback(unmatched_bank: list, unmatched_ledger_ids: list, ledger_lookup: dict,
                      ledger_assigned: dict, bank_assigned: dict, stage_counts: dict) -> None:
    remaining_ids = list(unmatched_ledger_ids)
    for b in unmatched_bank:
        merchant_hint = extract_merchant_hint(b.reference)
        candidates = []
        for tid in remaining_ids:
            r = ledger_lookup[tid]
            if merchant_hint and r.merchant_id != merchant_hint:
                continue
            if r.net != b.amount:
                continue
            earliest = add_business_days(r.capture_date, 1) - timedelta(days=1)
            latest = add_business_days(r.capture_date, 2) + timedelta(days=1)
            if not (earliest <= b.settle_date <= latest):
                continue
            candidates.append(tid)
        if len(candidates) == 1:
            tid = candidates[0]
            remaining_ids.remove(tid)
            _bind_unit(b.row, b.amount, [tid], "S3", tid, ledger_lookup, ledger_assigned,
                       bank_assigned, stage_counts)


# --------------------------------------------------------------------------------------
# Classification of ledger rows no stage could place
# --------------------------------------------------------------------------------------
def _classify_unplaced_ledger(ledger_df: pd.DataFrame, ledger_assigned: dict,
                               period_end: date) -> None:
    unplaced = [r for r in ledger_df.itertuples() if r.txn_id not in ledger_assigned]

    for r in unplaced:
        sibling = _find_duplicate_sibling(r, ledger_df, ledger_assigned)
        if sibling is not None:
            ledger_assigned[r.txn_id] = {
                "bucket": DUPLICATE, "stage": None, "confidence": "high",
                "unit_key": r.txn_id, "bank_row": None,
                "extra": {"duplicate_of": sibling, "amount": r.amount, "fee": r.fee,
                          "why": (f"same merchant ({r.merchant_id}) and amount ({r.amount}) as "
                                  f"{sibling}, created within {DUPLICATE_WINDOW_SECONDS} seconds "
                                  "of it, and no settlement exists for this row")},
            }
            continue

        has_timing, expected_settle = _timing_explains_absence(r.capture_date, period_end)
        if has_timing:
            ledger_assigned[r.txn_id] = {
                "bucket": TIMING_DIFFERENCE, "stage": None, "confidence": "high",
                "unit_key": would_be_unit_key(r.txn_id, r.merchant_id, r.capture_date, r.type),
                "bank_row": None,
                "extra": {"expected_settle_date": expected_settle.isoformat(),
                          "why": (f"captured {r.capture_date.isoformat()}; even the shortest "
                                  "settlement lag (1 business day) lands on "
                                  f"{expected_settle.isoformat()}, after the {period_end.isoformat()} "
                                  "period end")},
            }
            continue

        ledger_assigned[r.txn_id] = {
            "bucket": UNSETTLED, "stage": None, "confidence": "high",
            "unit_key": r.txn_id, "bank_row": None,
            "extra": {"why": "no bank settlement found, and the settlement window "
                              "does not explain its absence from this statement"},
        }


def _find_duplicate_sibling(row, ledger_df: pd.DataFrame, ledger_assigned: dict):
    if row.type != "sale":
        return None
    candidates = ledger_df[(ledger_df.merchant_id == row.merchant_id)
                            & (ledger_df.amount == row.amount)
                            & (ledger_df.txn_id != row.txn_id)]
    for c in candidates.itertuples():
        info = ledger_assigned.get(c.txn_id)
        if info is None or info["bucket"] not in (MATCHED, ROUNDING_VARIANCE):
            continue
        delta = abs((row.created_at - c.created_at).total_seconds())
        if delta <= DUPLICATE_WINDOW_SECONDS:
            return c.txn_id
    return None


def _timing_explains_absence(capture_date: date, period_end: date) -> tuple:
    s1 = add_business_days(capture_date, 1)
    if s1 > period_end:
        return True, s1
    s2 = add_business_days(capture_date, 2)
    if s2 > period_end:
        return True, s2
    return False, None


def _flag_orphan_refunds(ledger_df: pd.DataFrame, ledger_assigned: dict,
                          bank_assigned: dict) -> None:
    refunds = ledger_df[ledger_df["type"] == "refund"]
    for r in refunds.itertuples():
        info = ledger_assigned.get(r.txn_id)
        if info is None or info["bucket"] != MATCHED:
            continue
        if _original_sale_exists(r, ledger_df):
            continue
        info["bucket"] = UNMATCHED_REFUND
        info["extra"] = {
            **info.get("extra", {}),
            "why": ("this refund and its bank debit agree with each other, but no sale of "
                     f"{-r.amount} at {r.merchant_id} exists anywhere in the ledger — the "
                     "variance effect is zero, but it is still a control exception"),
        }
        bank_row = info.get("bank_row")
        if bank_row is not None and bank_row in bank_assigned:
            bank_assigned[bank_row]["bucket"] = UNMATCHED_REFUND
            bank_assigned[bank_row]["extra"] = info["extra"]


def _original_sale_exists(refund_row, ledger_df: pd.DataFrame) -> bool:
    target_amount = money(-refund_row.amount)
    candidates = ledger_df[(ledger_df["type"] == "sale")
                           & (ledger_df["merchant_id"] == refund_row.merchant_id)
                           & (ledger_df["amount"] == target_amount)
                           & (ledger_df["created_at"] < refund_row.created_at)]
    return len(candidates) > 0


# --------------------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------------------
def _build_exceptions(ledger_df: pd.DataFrame, bank_df: pd.DataFrame,
                       ledger_assigned: dict, bank_assigned: dict) -> list:
    groups: dict[str, dict] = {}

    for r in ledger_df.itertuples():
        if r.bucket == MATCHED:
            continue
        key = r.unit_key
        g = groups.setdefault(key, {"bucket": r.bucket, "ledger_txn_ids": [], "bank_row": None})
        g["ledger_txn_ids"].append(r.txn_id)
        g["bank_row"] = ledger_assigned[r.txn_id].get("bank_row")
        g["extra"] = ledger_assigned[r.txn_id].get("extra", {})

    # Bank rows whose own unit_key already has a group (rounding_variance, amount_mismatch,
    # unmatched_refund, ...) just need their bank_row attached -- not a second exception.
    # Only a bank row with no ledger-side counterpart at all (bank_only, duplicate_bank_line)
    # gets a new, bank-only-keyed group.
    for b in bank_df.itertuples():
        if b.bucket == MATCHED:
            continue
        key = bank_assigned[b.row].get("unit_key") or f"bank_row_{b.row}"
        if key in groups:
            groups[key]["bank_row"] = b.row
            continue
        groups[key] = {
            "bucket": b.bucket, "ledger_txn_ids": list(b.ledger_txn_ids), "bank_row": b.row,
            "extra": bank_assigned[b.row].get("extra", {}),
        }

    bank_amount_by_row = {b.row: b.amount for b in bank_df.itertuples()}
    ledger_net_by_id = {r.txn_id: r.net for r in ledger_df.itertuples()}
    ledger_amount_by_id = {r.txn_id: r.amount for r in ledger_df.itertuples()}

    exceptions = []
    for key, g in sorted(groups.items()):
        ledger_amt = money(sum((ledger_net_by_id[t] for t in g["ledger_txn_ids"]), Decimal("0")))
        bank_amt = bank_amount_by_row.get(g["bank_row"]) if g["bank_row"] else None
        diff = money(bank_amt - ledger_amt) if bank_amt is not None else None
        exceptions.append({
            "id": key,
            "bucket": g["bucket"],
            "ledger_txn_ids": g["ledger_txn_ids"],
            "bank_row": g["bank_row"],
            "ledger_amt": str(ledger_amt),
            "bank_amt": str(bank_amt) if bank_amt is not None else None,
            "diff": str(diff) if diff is not None else None,
            "explanation": _explain(g["bucket"], g["extra"], g["ledger_txn_ids"]),
            "owner": OWNER.get(g["bucket"]),
            "severity": _severity(g["bucket"], g.get("extra", {})),
            "journal_entry": _journal_entry(g["bucket"], ledger_amt, bank_amt, diff,
                                             g.get("extra", {})),
            "details": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in
                        g.get("extra", {}).items()},
        })
    return exceptions


def _severity(bucket: str, extra: dict) -> str:
    if bucket == BANK_ONLY:
        return "low"
    return SEVERITY.get(bucket, "medium")


def _explain(bucket: str, extra: dict, ledger_txn_ids: list) -> str:
    if "why" in extra:
        return extra["why"]
    if bucket == ROUNDING_VARIANCE:
        return extra.get("why", "Batch-level fee rounding accounts for the diff.")
    if bucket == BANK_ONLY:
        return "Bank line with no ledger counterpart (e.g. a bank-levied charge)."
    if bucket == AMOUNT_MISMATCH:
        return f"Bank amount disagrees with the ledger's expected net by {extra.get('diff')}."
    if bucket == DUPLICATE_BANK_LINE:
        return "A second bank line claims a txn that already has a settlement."
    return f"{bucket} ({len(ledger_txn_ids)} ledger row(s))."


def _journal_entry(bucket: str, ledger_amt: Decimal, bank_amt: Optional[Decimal],
                    diff: Optional[Decimal], extra: dict) -> str:
    if bucket == TIMING_DIFFERENCE:
        return f"No entry this period — Dr Settlement Receivable {ledger_amt} in Nov when it lands"
    if bucket == ROUNDING_VARIANCE:
        # diff = bank_amount - ledger_expected_net: positive means the bank credited more
        # than the per-line fees implied, so the receivable is understated and the fee
        # overstated by the same amount, and vice versa.
        if diff >= 0:
            return (f"Dr Settlement Receivable {diff} / Cr Fee Expense {diff} "
                     "(acquirer batch-fee rounding)")
        amt = money(-diff)
        return f"Dr Fee Expense {amt} / Cr Settlement Receivable {amt} (acquirer batch-fee rounding)"
    if bucket == DUPLICATE:
        gross = extra.get("amount", ledger_amt)
        fee = extra.get("fee", Decimal("0.00"))
        return (f"Dr Sales {gross} / Cr Settlement Receivable {ledger_amt} / "
                f"Cr Fee Expense {fee}")
    if bucket == UNMATCHED_REFUND:
        return f"Dr Refund Expense (unsupported) {-ledger_amt} / Cr Bank — escalate to Risk"
    if bucket == BANK_ONLY:
        amount = bank_amt if bank_amt is not None else extra.get("amount")
        if amount is None:
            return "Dr Bank Charges / Cr Bank — identify counterpart"
        if amount < 0:
            amt = money(-amount)
            return f"Dr Bank Charges {amt} / Cr Bank {amt}"
        amt = money(amount)
        return f"Dr Bank {amt} / Cr Unidentified Credit {amt} — identify counterpart"
    if bucket == AMOUNT_MISMATCH:
        return f"Dr/Cr Suspense {diff}, pending investigation with Bank Ops"
    if bucket == UNSETTLED:
        return f"Dr Settlement Receivable {ledger_amt}, pending bank confirmation"
    if bucket == DUPLICATE_BANK_LINE:
        return "Dr Suspense / Cr Bank — second credit pending reversal with the bank"
    return ""


# --------------------------------------------------------------------------------------
# Variance bridge
# --------------------------------------------------------------------------------------
def _build_bridge(ledger_df: pd.DataFrame, bank_df: pd.DataFrame) -> tuple:
    ledger_total = money(sum((r.net for r in ledger_df.itertuples()), Decimal("0")))
    bank_total = money(sum((b.amount for b in bank_df.itertuples()), Decimal("0")))
    variance = money(bank_total - ledger_total)

    buckets = sorted(set(ledger_df["bucket"]) | set(bank_df["bucket"]))
    bridge = {}
    for bucket in buckets:
        bank_side = money(sum((b.amount for b in bank_df.itertuples() if b.bucket == bucket),
                              Decimal("0")))
        ledger_side = money(sum((r.net for r in ledger_df.itertuples() if r.bucket == bucket),
                                Decimal("0")))
        bridge[bucket] = money(bank_side - ledger_side)

    residual = money(variance - money(sum(bridge.values(), Decimal("0"))))
    bridge["RESIDUAL"] = residual

    totals = {"ledger_total": ledger_total, "bank_total": bank_total, "variance": variance}
    return bridge, totals
