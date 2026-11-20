#!/usr/bin/env python3
"""Month-end reconciliation CLI.

Usage:
    python reconcile.py --ledger sample_data/ledger.csv --bank sample_data/bank_statement.csv \
        --out site/report.json

Writes site/report.json (consumed by site/index.html) and site/exceptions.csv.
Never reads ground_truth.json -- this is the production path, working only from
the two input CSVs.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from recon.engine import ReconciliationResult, reconcile
from recon.normalize import load_bank_statement, load_ledger

ASSUMPTIONS = [
    "Single currency (INR); all money is Decimal, rounded half-up to 2 dp, matching both "
    "the platform and the bank.",
    "Platform fee: 2.36% (2% MDR + 18% GST on the MDR, collapsed into one rate), rounded "
    "half-up once per line. Refunds carry no fee.",
    "Business-day calendar: RBI holidays (Mahatma Gandhi Jayanti, Dussehra, Diwali / Bali "
    "Pratipada), Sundays, and the 2nd/4th Saturdays are non-working; the 1st/3rd/5th "
    "Saturdays are working days.",
    "Settlement lag is 1-2 business days. When a ledger row has no bank line, the earliest "
    "lag consistent with that absence is used to compute its expected settle date for the "
    "timing_difference bucket.",
    "Batch merchants settle one netted bank line per capture date. An unexplained batch "
    "diff is recomputed as the fee applied once to the batch gross; if that explains the "
    "diff exactly, the whole batch -- never a single line -- is flagged rounding_variance.",
    "A ledger row that duplicates another (same merchant, same amount, within 10 minutes) "
    "where only one side ever settles is flagged duplicate; the settled side keeps its "
    "normal match.",
    "A refund is matched like any other transaction via its own bank reference. If no "
    "originating sale exists anywhere in the ledger, it is flagged unmatched_refund even "
    "though it settled cleanly -- a zero-variance control exception, not a cash discrepancy.",
    "Matching never reads ground_truth.json or any file outside the two input CSVs.",
]

EXCEPTION_CSV_FIELDS = [
    "id", "bucket", "ledger_txn_ids", "bank_row", "ledger_amt", "bank_amt", "diff",
    "owner", "severity", "explanation", "journal_entry",
]


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def build_report(result: ReconciliationResult, ledger_path: Path, bank_path: Path,
                  period_end: date) -> dict:
    ledger_bucket_counts = dict(sorted(result.ledger["bucket"].value_counts().to_dict().items()))
    bank_bucket_counts = dict(sorted(result.bank["bucket"].value_counts().to_dict().items()))

    return {
        "meta": {
            "period_end": period_end.isoformat(),
            "ledger_file": str(ledger_path),
            "bank_file": str(bank_path),
            "ledger_rows": int(len(result.ledger)),
            "bank_rows": int(len(result.bank)),
        },
        "totals": {k: str(v) for k, v in result.totals.items()},
        "bridge": {k: str(v) for k, v in result.bridge.items()},
        "stage_counts": result.stage_counts,
        "ledger_bucket_counts": ledger_bucket_counts,
        "bank_bucket_counts": bank_bucket_counts,
        "exceptions": result.exceptions,
        "assumptions": ASSUMPTIONS,
    }


def write_exceptions_csv(result: ReconciliationResult, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXCEPTION_CSV_FIELDS)
        writer.writeheader()
        for exc in result.exceptions:
            row = {k: exc.get(k) for k in EXCEPTION_CSV_FIELDS}
            row["ledger_txn_ids"] = ";".join(exc["ledger_txn_ids"])
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--bank", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="path to write report.json")
    parser.add_argument("--period-end", default="2026-10-31")
    args = parser.parse_args()

    period_end = date.fromisoformat(args.period_end)
    ledger = load_ledger(args.ledger)
    bank = load_bank_statement(args.bank)
    result = reconcile(ledger, bank, period_end)

    report = build_report(result, args.ledger, args.bank, period_end)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=_json_default) + "\n")

    exceptions_path = args.out.parent / "exceptions.csv"
    write_exceptions_csv(result, exceptions_path)

    print(f"ledger: {len(result.ledger)} rows | bank: {len(result.bank)} rows | "
          f"exceptions: {len(result.exceptions)} | residual: {result.bridge['RESIDUAL']} "
          f"-> {args.out}, {exceptions_path}")


if __name__ == "__main__":
    main()
