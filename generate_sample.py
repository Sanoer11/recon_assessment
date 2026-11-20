#!/usr/bin/env python3
"""
generate_sample.py - synthetic test data for the October 2026 payments reconciliation.

Writes, into --out (default: ./sample_data):
    ledger.csv            platform transactions (clean, machine-generated)
    bank_statement.csv    bank settlements for October 2026 (deliberately messy)
    ground_truth.json     every planted gap, natural exception, decoy and clean match,
                          plus flat {row -> expected bucket} maps for test assertions
    bank_statement_nov_lookahead.csv   only with --with-nov-lookahead

Usage:
    python generate_sample.py [--out DIR] [--with-nov-lookahead]

Determinism: stdlib only, fixed seeds, one RNG stream per concern, no set iteration.
Same command -> byte-identical files. See ASSUMPTIONS.md for every modelling choice.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
SEED = 20261031
IST = timezone(timedelta(hours=5, minutes=30))
CURRENCY = "INR"
PERIOD_START, PERIOD_END = date(2026, 10, 1), date(2026, 10, 31)
OPENING_BALANCE = Decimal("500000.00")
N_SALES = 500                       # total sale rows in the ledger, planted ones included
FEE_RATE = Decimal("0.0236")        # 2% MDR + 18% GST on the MDR, collapsed into one rate
LAG_CHOICES, LAG_WEIGHTS = (1, 2), (70, 30)   # settlement lag in business days
ACCOUNT_FEE = Decimal("-590.00")    # monthly maintenance Rs 500 + 18% GST, bank-only

# RBI holiday calendar, Mumbai centre (Negotiable Instruments Act). Sundays and the
# 2nd/4th Saturdays are handled by rule in is_business_day().
BANK_HOLIDAYS = {
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali (Bali Pratipada)",
}

# merchant_id -> (relative volume weight, settlement mode)
#   batch       one bank line per merchant per capture date, netted
#   one_to_one  one bank line per sale
MERCHANTS = {
    "M001": (9, "batch"),      "M002": (8, "batch"),      "M003": (7, "one_to_one"),
    "M004": (6, "one_to_one"), "M005": (6, "batch"),      "M006": (5, "one_to_one"),
    "M007": (5, "one_to_one"), "M008": (5, "batch"),      "M009": (4, "one_to_one"),
    "M010": (4, "one_to_one"), "M011": (8, "batch"),      "M012": (3, "one_to_one"),
    "M013": (3, "one_to_one"), "M014": (3, "batch"),      "M015": (3, "one_to_one"),
    "M016": (2, "one_to_one"), "M017": (2, "batch"),      "M018": (2, "one_to_one"),
    "M019": (2, "batch"),      "M020": (2, "one_to_one"),
}

# Merchants whose acquirer contract computes the fee once on each settlement batch's
# gross instead of trusting the platform's per-line fees. Every one of their batches
# follows this rule, so natural drift (if any) is recorded, not hidden.
BATCH_PRICED_MERCHANTS = ("M011",)

CHARM_PRICES = (199, 249, 299, 349, 399, 449, 499, 599, 699, 799, 899, 999,
                1199, 1299, 1499, 1799, 1999, 2499, 2999, 3499, 4999, 7999)
HOUR_WEIGHTS = (1, 1, 1, 1, 1, 1, 2, 3, 4, 6, 8, 9, 10, 9, 8, 7, 7, 8, 9, 10, 10, 9, 6, 3)

# --------------------------------------------------------------------------------------
# Planted gaps - hard-coded so changes to the baseline RNG can never move them
# --------------------------------------------------------------------------------------
# (a) Late settlement: Sat 31 Oct is a working (5th) Saturday; +1 business day -> Mon 2 Nov.
GAP_A = dict(txn_id="txn_e41b07c9d2a6", merchant="M004", amount="2350.00",
             created_at="2026-10-31T18:42:19+05:30", lag=1)

# (b) Aggregate rounding: 20 M011 sales captured Thu 15 Oct, settled Fri 16 Oct as one
# batch. Each line's fee is correctly rounded half-up; the bank rounds once on the gross.
# These amounts were chosen so the per-line sub-paisa errors add up to +0.06.
GAP_B = dict(merchant="M011", capture_date="2026-10-15", lag=1, amounts=(
    "405.78", "438.89", "560.48", "582.11", "609.13", "628.66", "659.60", "936.31",
    "1061.75", "1091.85", "1690.98", "1797.73", "2111.26", "2485.06", "3034.98",
    "3215.51", "3754.93", "4077.84", "4194.39", "4729.54"))
GAP_B_EXPECTED_DELTA = Decimal("0.06")   # bank credit minus ledger-expected net

# (c) Duplicate in the ledger only: double-submit 5 s apart, settles once at the bank.
GAP_C = dict(merchant="M006", amount="1499.00",
             original=("txn_5c80f3a1e9b4", "2026-10-21T13:05:42+05:30"),
             duplicate=("txn_9d27b6e0c315", "2026-10-21T13:05:47+05:30"), lag=1)
# Decoy for (c): a legitimate same-merchant, same-amount sale two days later.
DECOY_SAME_AMOUNT = dict(txn_id="txn_2a6e91f4b870", merchant="M006", amount="1499.00",
                         created_at="2026-10-23T19:31:08+05:30")

# (d) Refund whose original sale (24 Sep 2026) is in neither file.
GAP_D = dict(txn_id="txn_b3f61d8a4c07", merchant="M013", amount="-1186.40",
             created_at="2026-10-08T11:27:03+05:30", lag=1,
             original_sale_note="sale of 1186.40 at M013 on 2026-09-24, before the window")

N_DECOY_REFUNDS = 3

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
PAISA = Decimal("0.01")


def money(x: Decimal) -> Decimal:
    """Round half-up to 2 dp - the only rounding used anywhere, on both sides."""
    return x.quantize(PAISA, rounding=ROUND_HALF_UP)


def platform_fee(amount: Decimal) -> Decimal:
    """Per-line platform fee. Refunds carry no fee (the original fee is not returned)."""
    return money(amount * FEE_RATE) if amount > 0 else Decimal("0.00")


def is_business_day(d: date) -> bool:
    if d.weekday() == 6:                                   # Sunday
        return False
    if d.weekday() == 5 and (d.day - 1) // 7 in (1, 3):     # 2nd / 4th Saturday
        return False
    return d not in BANK_HOLIDAYS


def add_business_days(d: date, n: int) -> date:
    while n:
        d += timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d


def hex_of(txn_id: str) -> str:
    return txn_id.removeprefix("txn_")


def inr_grouped(x: Decimal) -> str:
    """Indian digit grouping: 125430.50 -> 1,25,430.50"""
    sign, whole, frac = ("-" if x < 0 else ""), *f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, groups = whole[:-3], [whole[-3:]]
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups)
    return f"{sign}{whole}.{frac}"


# --------------------------------------------------------------------------------------
# Domain objects
# --------------------------------------------------------------------------------------
@dataclass
class Txn:
    txn_id: str
    created_at: datetime
    amount: Decimal
    merchant_id: str
    type: str                 # sale | refund
    status: str = "succeeded"

    @property
    def fee(self) -> Decimal:
        return platform_fee(self.amount)

    @property
    def net(self) -> Decimal:
        return self.amount - self.fee

    @property
    def capture_date(self) -> date:
        return self.created_at.date()


@dataclass
class Settlement:
    """One bank line: a batch, a single sale, or a single refund."""
    key: str                  # batch id or txn id
    kind: str                 # batch | one_to_one | refund
    merchant_id: str
    capture_date: date
    settle_date: date
    txns: list

    @property
    def gross(self) -> Decimal:
        return sum((t.amount for t in self.txns), Decimal("0"))

    @property
    def ledger_expected_net(self) -> Decimal:
        return sum((t.net for t in self.txns), Decimal("0"))

    @property
    def bank_amount(self) -> Decimal:
        if self.kind == "batch" and self.merchant_id in BATCH_PRICED_MERCHANTS:
            return self.gross - money(self.gross * FEE_RATE)
        return self.ledger_expected_net

    @property
    def drift(self) -> Decimal:
        return self.bank_amount - self.ledger_expected_net


# --------------------------------------------------------------------------------------
# Ledger generation
# --------------------------------------------------------------------------------------
def new_txn_id(rng: random.Random, taken: dict) -> str:
    while True:
        tid = f"txn_{rng.getrandbits(48):012x}"
        if hex_of(tid)[:10] not in taken:
            taken[hex_of(tid)[:10]] = tid
            return tid


def random_amount(rng: random.Random) -> Decimal:
    if rng.random() < 0.45:
        return Decimal(rng.choice(CHARM_PRICES)).quantize(PAISA)
    raw = rng.lognormvariate(7.0, 0.85)                     # median ~Rs 1,100
    return money(Decimal(str(min(max(raw, 99.0), 75000.0))))


def random_time(rng: random.Random, d: date) -> datetime:
    hour = rng.choices(range(24), HOUR_WEIGHTS)[0]
    return datetime.combine(d, time(hour, rng.randrange(60), rng.randrange(60)), IST)


def build_planted(rng: random.Random, taken: dict) -> dict:
    """Planted rows. Ids are fixed; gap (b) timestamps/ids use their own RNG stream."""
    def fixed(tid, ts, amount, merchant, kind="sale"):
        taken[hex_of(tid)[:10]] = tid
        return Txn(tid, datetime.fromisoformat(ts), Decimal(amount), merchant, kind)

    gap_b_day = date.fromisoformat(GAP_B["capture_date"])
    gap_b = [Txn(new_txn_id(rng, taken), random_time(rng, gap_b_day), Decimal(a),
                 GAP_B["merchant"], "sale") for a in GAP_B["amounts"]]
    return {
        "a": fixed(GAP_A["txn_id"], GAP_A["created_at"], GAP_A["amount"], GAP_A["merchant"]),
        "b": sorted(gap_b, key=lambda t: t.created_at),
        "c_orig": fixed(*GAP_C["original"], GAP_C["amount"], GAP_C["merchant"]),
        "c_dup": fixed(*GAP_C["duplicate"], GAP_C["amount"], GAP_C["merchant"]),
        "decoy_same_amount": fixed(DECOY_SAME_AMOUNT["txn_id"], DECOY_SAME_AMOUNT["created_at"],
                                   DECOY_SAME_AMOUNT["amount"], DECOY_SAME_AMOUNT["merchant"]),
        "d": fixed(GAP_D["txn_id"], GAP_D["created_at"], GAP_D["amount"], GAP_D["merchant"],
                   "refund"),
    }


def build_baseline(rng: random.Random, n: int, taken: dict) -> list:
    reserved = {(GAP_B["merchant"], date.fromisoformat(GAP_B["capture_date"]))}
    banned = {(GAP_D["merchant"], -Decimal(GAP_D["amount"])),        # keeps (d) an orphan
              (GAP_C["merchant"], Decimal(GAP_C["amount"]))}         # keeps (c) unambiguous
    days = [PERIOD_START + timedelta(days=i) for i in range((PERIOD_END - PERIOD_START).days + 1)]
    ids, weights = list(MERCHANTS), [w for w, _ in MERCHANTS.values()]
    out = []
    while len(out) < n:
        merchant, day = rng.choices(ids, weights)[0], rng.choice(days)
        amount = random_amount(rng)
        if (merchant, day) in reserved or (merchant, amount) in banned:
            continue
        out.append(Txn(new_txn_id(rng, taken), random_time(rng, day), amount, merchant, "sale"))
    return out


def build_decoy_refunds(rng: random.Random, baseline: list, taken: dict) -> list:
    """Full refunds of in-window sales, so 'every refund is an orphan' fails."""
    pool = sorted((t for t in baseline
                   if date(2026, 10, 3) <= t.capture_date <= date(2026, 10, 22)),
                  key=lambda t: t.txn_id)
    refunds = []
    for orig in rng.sample(pool, N_DECOY_REFUNDS):
        day = orig.capture_date + timedelta(days=rng.randint(1, 5))
        refunds.append((Txn(new_txn_id(rng, taken), random_time(rng, day), -orig.amount,
                            orig.merchant_id, "refund"), orig))
    return refunds


# --------------------------------------------------------------------------------------
# Settlement engine: ledger rows -> bank lines
# --------------------------------------------------------------------------------------
def settle(ledger: list, never_settles: set, forced_lags: dict, rng: random.Random) -> list:
    units = {}
    for t in sorted(ledger, key=lambda t: (t.created_at, t.txn_id)):
        if t.txn_id in never_settles:
            continue
        if t.type == "refund":
            key, kind = t.txn_id, "refund"
        elif MERCHANTS[t.merchant_id][1] == "batch":
            key, kind = f"B-{t.merchant_id}-{t.capture_date:%Y%m%d}", "batch"
        else:
            key, kind = t.txn_id, "one_to_one"
        units.setdefault(key, (kind, []))[1].append(t)

    settlements = []
    for key in sorted(units):
        kind, txns = units[key]
        drawn = rng.choices(LAG_CHOICES, LAG_WEIGHTS)[0]    # draw always: stable stream
        lag = forced_lags.get(key, drawn)
        cap = txns[0].capture_date
        settlements.append(Settlement(key, kind, txns[0].merchant_id, cap,
                                      add_business_days(cap, lag), txns))
    return settlements


# --------------------------------------------------------------------------------------
# Bank statement rendering (all realism noise lives here)
# --------------------------------------------------------------------------------------
def utr(rng: random.Random, d: date) -> str:
    return f"{rng.choice(('UTIB', 'HDFC'))}N{d:%y}{d.timetuple().tm_yday:03d}{rng.randrange(10**6):06d}"


def reference_for(s: Settlement, rng: random.Random) -> str:
    u = utr(rng, s.settle_date)
    if s.kind == "batch":
        return rng.choice((f"NEFT/{u}/PAYGW BATCH {s.key}",
                           f"PAYGW SETTL {s.key.replace('-', '')}"))
    h = hex_of(s.key)
    if s.kind == "refund":
        return rng.choice((f"PAYGW RFND {s.key}", f"NEFT/{u}/RFND/{h[:10].upper()}"))
    return rng.choice((f"NEFT/{u}/PAYGW SETTL {s.key}",            # full id
                       f"PAYGW/{h[:10].upper()}",                   # prefix stripped, truncated
                       f"{u}-STL-TXN{h.upper()}-{s.merchant_id}"))  # prefixed, upper-cased


def description_for(amount: Decimal, kind: str) -> str:
    if kind == "fee":
        return "CHRG"
    if kind == "opening":
        return "OPENING BALANCE"
    if amount < 0:
        return "NEFT DR"
    return "RTGS CR" if amount >= Decimal("200000") else "NEFT CR"


def messy(text: str, rng: random.Random) -> str:
    r = rng.random()
    if r < 0.15:
        return f"  {text}"
    if r < 0.30:
        return f"{text}   "
    if r < 0.40 and " " in text:
        return text.replace(" ", "  ", 1)
    return text


def fmt_date(d: date, rng: random.Random) -> str:
    return rng.choices((f"{d:%Y-%m-%d}", f"{d:%d/%m/%Y}", f"{d:%d-%b-%Y}"), (50, 30, 20))[0]


def render_bank(lines: list, opening: Decimal, rng: random.Random) -> tuple:
    """lines: list of (settle_date, kind, amount, reference). Returns (csv rows, closing)."""
    rows, balance = [], opening
    for settle_date, kind, amount, reference in lines:
        balance += amount
        grouped = abs(amount) >= 1000 and rng.random() < 0.5
        fmt = inr_grouped if grouped else (lambda x: f"{x:.2f}")
        rows.append({
            "settle_date": fmt_date(settle_date, rng),
            "amount": fmt(amount),
            "reference": messy(reference, rng),
            "description": messy(description_for(amount, kind), rng),
            "running_balance": fmt(balance),
        })
    return rows, balance


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def generate(out_dir: Path, with_lookahead: bool) -> dict:
    rng_planted = random.Random(SEED + 1)   # gap (b) ids/times, decoy refunds
    rng_base = random.Random(SEED)          # baseline sales
    rng_settle = random.Random(SEED + 2)    # settlement lags
    rng_noise = random.Random(SEED + 3)     # references, formats, whitespace

    taken: dict = {}
    planted = build_planted(rng_planted, taken)
    n_planted_sales = 1 + len(planted["b"]) + 2 + 1          # a + b + c pair + decoy
    baseline = build_baseline(rng_base, N_SALES - n_planted_sales, taken)
    decoy_refunds = build_decoy_refunds(rng_planted, baseline, taken)

    ledger = sorted(
        baseline + planted["b"] + [planted[k] for k in ("a", "c_orig", "c_dup",
                                                         "decoy_same_amount", "d")]
        + [r for r, _ in decoy_refunds],
        key=lambda t: (t.created_at, t.txn_id))

    gap_b_key = f"B-{GAP_B['merchant']}-{GAP_B['capture_date'].replace('-', '')}"
    forced = {GAP_A["txn_id"]: GAP_A["lag"], gap_b_key: GAP_B["lag"],
              GAP_C["original"][0]: GAP_C["lag"], GAP_D["txn_id"]: GAP_D["lag"]}
    settlements = settle(ledger, {GAP_C["duplicate"][0]}, forced, rng_settle)

    in_window = [s for s in settlements if s.settle_date <= PERIOD_END]
    spill = [s for s in settlements if s.settle_date > PERIOD_END]
    order = {"batch": 0, "one_to_one": 0, "refund": 1}
    in_window.sort(key=lambda s: (s.settle_date, order[s.kind], s.merchant_id, s.key))
    spill.sort(key=lambda s: (s.settle_date, order[s.kind], s.merchant_id, s.key))

    # ---- October bank statement: opening row, settlements, month-end charge
    oct_lines = [(PERIOD_START, "opening", Decimal("0.00"), "BAL B/F")]
    oct_lines += [(s.settle_date, s.kind, s.bank_amount, reference_for(s, rng_noise))
                  for s in in_window]
    oct_lines += [(PERIOD_END, "fee", ACCOUNT_FEE, "ACCT MAINT CHGS OCT26 INCL GST")]
    bank_rows, closing = render_bank(oct_lines, OPENING_BALANCE, rng_noise)

    # Lookahead references are drawn from a separate stream so the flag never
    # changes the October file.
    rng_look = random.Random(SEED + 4)
    nov_lines = [(s.settle_date, s.kind, s.bank_amount, reference_for(s, rng_look))
                 for s in spill]
    nov_rows, _ = render_bank(nov_lines, closing, rng_look)

    # ---- ground truth
    truth = build_truth(ledger, planted, decoy_refunds, in_window, spill, oct_lines,
                        gap_b_key, closing, nov_lines)
    check_invariants(ledger, settlements, in_window, spill, oct_lines, truth, closing, planted,
                     gap_b_key)

    # ---- write
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "ledger.csv",
              ["txn_id", "created_at", "amount", "currency", "fee", "merchant_id", "type", "status"],
              [{"txn_id": t.txn_id, "created_at": t.created_at.isoformat(),
                "amount": f"{t.amount:.2f}", "currency": CURRENCY, "fee": f"{t.fee:.2f}",
                "merchant_id": t.merchant_id, "type": t.type, "status": t.status}
               for t in ledger])
    bank_cols = ["settle_date", "amount", "reference", "description", "running_balance"]
    write_csv(out_dir / "bank_statement.csv", bank_cols, bank_rows)
    if with_lookahead:
        write_csv(out_dir / "bank_statement_nov_lookahead.csv", bank_cols, nov_rows)
    (out_dir / "ground_truth.json").write_text(json.dumps(truth, indent=2) + "\n",
                                               encoding="utf-8")
    return truth


def build_truth(ledger, planted, decoy_refunds, in_window, spill, oct_lines, gap_b_key,
                closing, nov_lines) -> dict:
    # bank_row = 1-based data row in bank_statement.csv (header excluded); row 1 = opening
    row_of = {s.key: i + 2 for i, s in enumerate(in_window)}
    ref_of = {s.key: oct_lines[i + 1][3] for i, s in enumerate(in_window)}
    fee_row = len(oct_lines)
    ledger_bucket: dict = {}
    bank_bucket: dict = {"1": "non_transaction", str(fee_row): "bank_only"}

    def ids(txns):
        return [t.txn_id for t in txns]

    a, b, c_orig, c_dup, d = (planted[k] for k in ("a", "b", "c_orig", "c_dup", "d"))
    sb = next(s for s in in_window if s.key == gap_b_key)
    a_settle = next(s for s in spill if s.key == a.txn_id)
    planted_gaps = [
        {"id": "GAP_A_LATE_SETTLEMENT", "kind": "late_settlement",
         "expected_bucket": "timing_difference", "ledger_txn_ids": [a.txn_id], "bank_rows": [],
         "details": {"created_at": a.created_at.isoformat(), "amount": f"{a.amount:.2f}",
                     "fee": f"{a.fee:.2f}", "expected_bank_amount": f"{a.net:.2f}",
                     "expected_settle_date": a_settle.settle_date.isoformat(),
                     "why": "Sat 31 Oct is a working 5th Saturday; +1 business day = Mon 2 Nov, "
                            "after the statement closes."}},
        {"id": "GAP_B_AGGREGATE_ROUNDING", "kind": "aggregate_rounding",
         "expected_bucket": "rounding_variance", "ledger_txn_ids": ids(b),
         "bank_rows": [row_of[gap_b_key]], "bank_references": [ref_of[gap_b_key]],
         "details": {"batch_id": gap_b_key, "n_txns": len(b), "gross": f"{sb.gross:.2f}",
                     "sum_line_fees": f"{sum(t.fee for t in b):.2f}",
                     "bank_batch_fee": f"{money(sb.gross * FEE_RATE):.2f}",
                     "ledger_expected_net": f"{sb.ledger_expected_net:.2f}",
                     "bank_amount": f"{sb.bank_amount:.2f}", "delta": f"{sb.drift:.2f}",
                     "why": "each line fee rounded half-up individually; bank rounds once on "
                            "the batch gross"}},
        {"id": "GAP_C_LEDGER_DUPLICATE", "kind": "duplicate_in_ledger",
         "expected_bucket": "duplicate", "ledger_txn_ids": [c_dup.txn_id], "bank_rows": [],
         "details": {"duplicate_of": c_orig.txn_id,
                     "original_matched_bank_row": row_of[c_orig.txn_id],
                     "original_bank_reference": ref_of[c_orig.txn_id],
                     "seconds_apart": int((c_dup.created_at - c_orig.created_at).total_seconds()),
                     "amount": f"{c_dup.amount:.2f}", "merchant_id": c_dup.merchant_id,
                     "why": "client double-submit created two platform records; the acquirer "
                            "captured once, so only one settlement exists. The original is "
                            "an ordinary match."}},
        {"id": "GAP_D_ORPHAN_REFUND", "kind": "refund_without_original",
         "expected_bucket": "unmatched_refund", "ledger_txn_ids": [d.txn_id],
         "bank_rows": [row_of[d.txn_id]], "bank_references": [ref_of[d.txn_id]],
         "details": {"amount": f"{d.amount:.2f}", "merchant_id": d.merchant_id,
                     "original_sale": GAP_D["original_sale_note"],
                     "why": "refund and its bank debit agree; the sale it reverses is absent "
                            "from both files"}},
    ]
    for g in planted_gaps:
        for tid in g["ledger_txn_ids"]:
            ledger_bucket[tid] = g["expected_bucket"]
        for r in g["bank_rows"]:
            bank_bucket[str(r)] = g["expected_bucket"]

    natural = []
    for s in spill:
        if s.key == a.txn_id:
            continue
        natural.append({"kind": "month_end_spillover", "expected_bucket": "timing_difference",
                        "ledger_txn_ids": ids(s.txns), "bank_rows": [],
                        "details": {"settlement_key": s.key, "capture_date": s.capture_date.isoformat(),
                                    "expected_settle_date": s.settle_date.isoformat(),
                                    "expected_bank_amount": f"{s.bank_amount:.2f}"}})
    for s in in_window:
        if s.drift and s.key != gap_b_key:
            natural.append({"kind": "aggregate_rounding", "expected_bucket": "rounding_variance",
                            "ledger_txn_ids": ids(s.txns), "bank_rows": [row_of[s.key]],
                            "bank_references": [ref_of[s.key]],
                            "details": {"batch_id": s.key, "delta": f"{s.drift:.2f}"}})
    for n in natural:
        for tid in n["ledger_txn_ids"]:
            ledger_bucket[tid] = n["expected_bucket"]
        for r in n["bank_rows"]:
            bank_bucket[str(r)] = n["expected_bucket"]

    decoy_ids = {planted["decoy_same_amount"].txn_id} | {r.txn_id for r, _ in decoy_refunds}
    matches, decoys = [], []
    for s in in_window:
        if str(row_of[s.key]) in bank_bucket:
            continue
        m = {"bank_row": row_of[s.key], "reference": ref_of[s.key], "kind": s.kind,
             "ledger_txn_ids": ids(s.txns), "amount": f"{s.bank_amount:.2f}"}
        matches.append(m)
        bank_bucket[str(row_of[s.key])] = "matched"
        for tid in m["ledger_txn_ids"]:
            ledger_bucket[tid] = "matched"
    decoys.append({"kind": "same_merchant_same_amount", "expected_bucket": "matched",
                   "ledger_txn_ids": [planted["decoy_same_amount"].txn_id],
                   "looks_like": c_orig.txn_id,
                   "why": "same merchant and amount as the duplicate pair, but two days later"})
    for r, orig in decoy_refunds:
        decoys.append({"kind": "refund_with_original", "expected_bucket": "matched",
                       "ledger_txn_ids": [r.txn_id], "original_txn_id": orig.txn_id,
                       "bank_rows": [row_of[r.txn_id]],
                       "why": "full refund of an October sale present in the ledger"})
    assert all(ledger_bucket[t] == "matched" for t in decoy_ids)

    return {
        "meta": {
            "seed": SEED, "currency": CURRENCY, "timezone": "Asia/Kolkata (+05:30)",
            "period": {"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
            "opening_balance": f"{OPENING_BALANCE:.2f}", "closing_balance": f"{closing:.2f}",
            "fee_rate": str(FEE_RATE),
            "bank_row_numbering": "1-based data row in bank_statement.csv, header excluded; "
                                  "row 1 is the opening-balance row",
            "counts": {"ledger_rows": len(ledger),
                       "sales": sum(t.type == "sale" for t in ledger),
                       "refunds": sum(t.type == "refund" for t in ledger),
                       "bank_rows": len(oct_lines), "nov_lookahead_rows": len(nov_lines)},
        },
        "buckets": {
            "matched": "ledger row(s) and bank row agree exactly",
            "timing_difference": "ledger row whose settlement falls after the period end",
            "rounding_variance": "batch matched by membership; amount differs by a few paise "
                                 "from aggregate fee rounding",
            "duplicate": "ledger row that duplicates another ledger row; never settles",
            "unmatched_refund": "refund (ledger + bank) whose original sale is in neither file",
            "bank_only": "bank row with no ledger counterpart (bank charges)",
            "non_transaction": "statement furniture, e.g. opening balance",
        },
        "planted_gaps": planted_gaps,
        "natural_exceptions": natural,
        "decoys": decoys,
        "bank_only": [{"bank_row": fee_row, "reference": oct_lines[-1][3],
                       "amount": f"{ACCOUNT_FEE:.2f}", "expected_bucket": "bank_only"}],
        "non_transaction": [{"bank_row": 1, "reference": "BAL B/F",
                             "expected_bucket": "non_transaction"}],
        "matches": matches,
        "expected_ledger_bucket": dict(sorted(ledger_bucket.items())),
        "expected_bank_bucket": dict(sorted(bank_bucket.items(), key=lambda kv: int(kv[0]))),
    }


def check_invariants(ledger, settlements, in_window, spill, oct_lines, truth, closing,
                     planted, gap_b_key) -> None:
    lb, bb = truth["expected_ledger_bucket"], truth["expected_bank_bucket"]
    # every row classified exactly once
    assert sorted(lb) == sorted(t.txn_id for t in ledger), "ledger partition"
    assert sorted(map(int, bb)) == list(range(1, len(oct_lines) + 1)), "bank partition"
    # exactly four planted gaps, each in its intended bucket
    assert [g["expected_bucket"] for g in truth["planted_gaps"]] == [
        "timing_difference", "rounding_variance", "duplicate", "unmatched_refund"]
    # (a) settles after the period and is absent from October
    assert planted["a"].txn_id not in {s.key for s in in_window}
    # (b) every line fee is a correct half-up rounding; drift only appears in aggregate
    sb = next(s for s in settlements if s.key == gap_b_key)
    assert all(t.fee == money(t.amount * FEE_RATE) for t in sb.txns)
    assert len(sb.txns) == len(GAP_B["amounts"]) and sb.drift == GAP_B_EXPECTED_DELTA
    # (c) only one settlement exists for the pair
    keys = {t.txn_id for s in settlements for t in s.txns}
    assert planted["c_orig"].txn_id in keys and planted["c_dup"].txn_id not in keys
    # no unplanned near-duplicates (same merchant + amount within 10 minutes)
    sales = sorted((t for t in ledger if t.type == "sale"),
                   key=lambda t: (t.merchant_id, t.amount, t.created_at))
    for x, y in zip(sales, sales[1:]):
        if (x.merchant_id, x.amount) == (y.merchant_id, y.amount) and \
                (y.created_at - x.created_at) < timedelta(minutes=10):
            assert {x.txn_id, y.txn_id} == {planted["c_orig"].txn_id, planted["c_dup"].txn_id}
    # (d) no sale anywhere that the orphan refund could pair with
    d = planted["d"]
    assert not any(t.type == "sale" and t.merchant_id == d.merchant_id and t.amount == -d.amount
                   for t in ledger)
    # truncated references still resolve uniquely (10-hex prefix)
    prefixes = [hex_of(t.txn_id)[:10] for t in ledger]
    assert len(prefixes) == len(set(prefixes)), "txn_id prefix collision"
    # running balance reconciles
    assert closing == OPENING_BALANCE + sum((l[2] for l in oct_lines), Decimal("0"))
    # every in-window settlement except the (b) batch has zero drift unless recorded
    for s in in_window:
        if s.drift:
            assert truth["expected_bank_bucket"][str(in_window.index(s) + 2)] == "rounding_variance"
    assert all(s.settle_date > PERIOD_END for s in spill)
    # banks only credit on working days
    assert all(is_business_day(s.settle_date) for s in settlements)


def write_csv(path: Path, columns: list, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, default=Path("sample_data"))
    p.add_argument("--with-nov-lookahead", action="store_true",
                   help="also write the Nov 2-3 settlements of October captures")
    args = p.parse_args()
    truth = generate(args.out, args.with_nov_lookahead)
    c = truth["meta"]["counts"]
    print(f"ledger: {c['ledger_rows']} rows ({c['sales']} sales, {c['refunds']} refunds) | "
          f"bank: {c['bank_rows']} rows | planted gaps: {len(truth['planted_gaps'])} | "
          f"natural exceptions: {len(truth['natural_exceptions'])} | "
          f"decoys: {len(truth['decoys'])} -> {args.out}/")


if __name__ == "__main__":
    main()
