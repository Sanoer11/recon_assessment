# Month-end reconciliation — October 2026

## Problem

A payments platform's ledger (`ledger.csv`) and its bank statement
(`bank_statement.csv`) are produced by two independent systems that never
share a common key. At month end, Finance needs to know: for every rupee the
platform recorded, did the bank actually pay it out, and if not, why not?

`ledger.csv` is clean — 504 rows, one per sale or refund, with exact
timestamps, amounts and fees. `bank_statement.csv` is what a real bank
statement looks like: mixed date formats, Indian digit grouping on amounts
over ₹1,000, whitespace noise, batched settlements, and bank references that
only partially embed a transaction or batch id. The two files must be
reconciled using nothing but pattern-matching and domain rules — there is no
`original_txn_id` column and no shared key.

See `ASSUMPTIONS.md` for the exact modelling choices behind the synthetic
data (business-day calendar, fee formula, batching rule, planted gaps) and
`generate_sample.py` for how it was built.

## Approach: staged matching

`recon/engine.py` resolves every bank line in three stages, cheapest and
most-confident first, and records which stage resolved each row:

| Stage | What it does | Confidence |
|---|---|---|
| **S1** reference → txn | Extracts a full or truncated hex transaction id from the bank reference (`txn_…`, `PAYGW/…`, `…-STL-TXN…`, `RFND/…`) and resolves it to a unique ledger row via a 10-hex-char prefix index. | High |
| **S2** reference → batch | Extracts a `B-<merchant>-<YYYYMMDD>` batch id (dashed or concatenated) and groups the matching ledger sale rows for that merchant + capture date, comparing `Σ(amount − fee)` to the bank line. | High |
| **S3** fallback | For anything neither S1 nor S2 could place: amount + a merchant hint from the reference + a settle-date window derived from the business-day calendar. | Low |

On the real sample data every settlement resolves via S1 or S2 (233 + 144 =
377 of 377); S3 exists for robustness and is exercised by the handcrafted
fixtures and tests.

A matched **unit** (one bank line plus the ledger row(s) behind it) is then
classified:

- `diff == 0` → **matched**.
- `diff != 0` and it's a batch → recompute the fee once on the batch gross
  (`round(Σgross × 2.36%)`); if that reproduces the bank amount exactly, the
  *whole batch* — never a single line — is **rounding_variance**, with the
  gross, summed line fees and recomputed batch fee all shown as proof.
- Otherwise → **amount_mismatch** (a genuine, unexplained discrepancy).

Ledger rows no stage could place are classified by what explains their
absence:

- Same merchant + same amount as another row, within 10 seconds–10 minutes,
  where only the other row settled → **duplicate**.
- Otherwise, if even the *shortest* possible settlement lag (1 business day,
  on the RBI/Mumbai holiday calendar) would land after the period end →
  **timing_difference**, with the expected settle date shown. (If a 1-day
  lag would have landed inside the window but the row is still missing, the
  lag must have been 2 days — that's used instead, since the row's own
  absence constrains which lag actually applied.)
- Otherwise → **unsettled** (a genuine, unexplained gap — not expected on
  clean data, but what a deleted bank line looks like).

Finally, any refund that settled normally (ledger and bank agree) but whose
originating sale doesn't exist anywhere in the ledger is re-flagged
**unmatched_refund** — a control exception with zero cash impact, called out
separately in the bridge.

**Buckets**: `matched`, `timing_difference`, `rounding_variance`,
`duplicate`, `unmatched_refund`, `bank_only`, `non_transaction` (the
ground-truth set) plus two production-only buckets the engine can reach but
the sample data never does: `amount_mismatch` and `unsettled`, and
`duplicate_bank_line` for a second bank line claiming an already-settled
transaction.

None of `recon/normalize.py` or `recon/engine.py` ever reads
`ground_truth.json` — only `tests/` does, to grade the engine's output.

## The variance bridge

`bridge = October bank net settlements − October ledger expected net`, split
into one line per bucket plus `RESIDUAL`:

```
Ledger total  →  + timing_difference  →  + duplicate  →  + rounding_variance
             →  + unmatched_refund (= 0)  →  + bank_only  →  + RESIDUAL (= 0.00)  →  Bank total
```

Each bucket's contribution is `Σ(bank amount in that bucket) − Σ(ledger net
in that bucket)` — a tautological decomposition of the single number
`bank_total − ledger_total`, so `RESIDUAL` is `0.00` as long as every ledger
row and every bank row is classified exactly once. A non-zero residual means
a row was double-counted or dropped — a bug, not a rounding artifact — which
is exactly what the tests assert.

The orphan refund (planted gap d) is a special case worth calling out
explicitly: it settled cleanly at the bank, so its bridge contribution is
`0.00` — but it is still surfaced as an `unmatched_refund` exception, because
no sale exists anywhere in the ledger to justify it. Zero variance impact
does not mean zero risk.

## How to run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# run the reconciler
python reconcile.py --ledger sample_data/ledger.csv --bank sample_data/bank_statement.csv \
    --out site/report.json
# -> writes site/report.json and site/exceptions.csv

# view the report
cd site && python3 -m http.server 8000   # then open http://localhost:8000

# run the tests
pytest -q
```

## Regenerating the sample data

```bash
python generate_sample.py --out sample_data [--with-nov-lookahead]
```

Seeded and deterministic — re-running produces byte-identical CSVs and
`ground_truth.json`. See `ASSUMPTIONS.md` for every modelling choice.

## Test results

```
41 passed in ~2s
```

Covers, in `tests/`:

- **Ground truth** (`test_ground_truth.py`): every one of the 504 ledger rows
  and 378 bank rows lands in its `ground_truth.json` expected bucket (prints
  a confusion matrix on failure); all 4 planted gaps resolve with the
  correct ids and reasoning; all decoys (the same-amount sale 2 days later,
  the 3 refunds with real originals) stay `matched`; bridge `RESIDUAL ==
  0.00`.
- **Handcrafted fixtures** (`test_engine.py`): a 13-row ledger / 10-row bank
  pair, independent of the generator, covering every bucket (including a
  rounding-variance batch whose recomputed fee is checked to explain the
  diff exactly), plus direct regex tests for truncated, uppercased, and
  whitespace-noised references.
- **Parser tests** (`test_normalize.py`): all three date formats, Indian
  lakh-grouped amounts, opening-balance row detection, whitespace
  normalization, running-balance verification (including a deliberately
  broken one).
- **Mutation tests** (`test_mutations.py`): deleting a settled bank line
  turns that transaction `unsettled`; duplicating a bank line is flagged
  `duplicate_bank_line`; bumping an amount by ₹1.00 is flagged
  `amount_mismatch` while the bridge residual stays `0.00`.
- **Determinism** (`test_mutations.py`, `test_cli.py`): two independent
  `reconcile()` calls and two independent CLI invocations produce
  byte-identical results.

## Limitations

- **S3 fallback is unexercised on real data.** Every sample-data settlement
  resolves via S1/S2, so the amount+merchant-hint+window fallback is only
  proven correct by the handcrafted fixtures, not by the 500-transaction
  dataset.
- **Merchant settlement mode is hardcoded**, not inferred (`BATCH_MERCHANTS`
  in `recon/engine.py`), mirroring how a real reconciler would read it from
  a merchant-onboarding config table rather than guessing it from the files.
- **Duplicate detection is a 10-minute heuristic** (same merchant + amount),
  which is what the generator itself guarantees never happens by accident —
  a real platform would ideally supply an idempotency key instead.
- **No partial-period or multi-currency handling.** The whole pipeline
  assumes one currency (INR) and a single clean period boundary; a real
  close spanning currencies or carrying forward prior-period exceptions
  would need more state than `report.json` currently holds.
- **The site is read-only.** It renders whatever `report.json` says; there's
  no way to annotate, resolve, or re-run a reconciliation from the UI itself
  — by design, since the brief calls for a static site with no server.
