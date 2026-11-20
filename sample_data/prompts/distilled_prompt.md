# Distilled Prompt

The single build prompt I pasted into Claude Code, run in a folder that already held the test data (`generate_sample.py`, `ASSUMPTIONS.md`, `sample_data/`):

```
Build a month-end reconciliation tool for a payments platform. The test data already exists in this folder.

READ FIRST, before writing any code:
- ASSUMPTIONS.md, generate_sample.py, sample_data/ledger.csv, sample_data/bank_statement.csv, sample_data/ground_truth.json
- Learn from generate_sample.py exactly how bank references, batch ids, fees (2.36% incl. GST),
  business-day calendar (RBI holidays) and rounding work. Do not guess formats; derive them from the generator.
- Summarise back to me: schemas, bucket names in ground_truth.json, reference formats, and your matching plan.
  Then proceed without waiting.

HARD RULE: the reconciler must NEVER read ground_truth.json. Only the tests use it. The engine works from the
two CSVs alone, like it would in production.

STACK: Python 3.11, pandas, Decimal for all money (never float), pytest. Output is a STATIC site
(no server): Python writes site/report.json, and site/index.html renders it. Deploy target: Vercel.

1. recon/normalize.py
   - Bank: strip whitespace, parse mixed date formats, parse Indian-format amounts ("1,25,430.50"),
     identify and exclude the opening-balance row, verify that running_balance is consistent row by row.
   - Ledger: parse ISO timestamps, Decimal amounts and fees, compute net = amount - fee.

2. recon/engine.py (pure functions, no I/O). Match in stages, and record which stage matched each row:
   S1 Reference -> txn: extract a full or truncated txn id from the reference (case-insensitive) and resolve it
      to a unique ledger row.
   S2 Reference -> batch: extract the batch id (merchant + capture date), group the ledger rows, and compare
      sum(net) to the bank amount.
   S3 Fallback: for anything left, match on amount + merchant + settle-date window. Flag these as low confidence.
   Then classify every ledger row and every bank row into the ground-truth buckets
   (matched, timing_difference, rounding_variance, duplicate, unmatched_refund, bank_only, or whatever names
   ground_truth.json uses):
   - timing_difference: no October bank line, AND expected settle date (capture + 1-2 business days on the
     generator's calendar) falls after Oct 31. Show the expected settle date.
   - rounding_variance: batch diff is non-zero but tiny. PROVE the cause: recompute the fee on the batch total,
     rounded once, and show that it explains the diff exactly. No single line should be flagged.
   - duplicate: two ledger rows with the same merchant and amount a few seconds apart, only one of which settled.
     The decoy (same merchant and amount, days apart) must stay matched.
   - unmatched_refund: a refund whose original sale is not in the ledger. Refunds whose original is present stay
     matched.
   - bank_only: a bank line with no ledger counterpart (e.g. the monthly account charge).
   Every exception row gets: id, bucket, ledger_amt, bank_amt, diff, plain-English explanation, owner
   (Finance / Bank Ops / Engineering / Risk), suggested journal entry (Dr/Cr), and severity.

3. Variance bridge: October bank net settlements minus October ledger expected net, split into one line per
   bucket plus RESIDUAL. RESIDUAL must equal Decimal("0.00"); treat anything else as a bug to fix.
   Note that the orphan refund settled at the bank, so its effect on the variance is 0 but it is still a
   control exception. Say so in the report.

4. reconcile.py (CLI): python reconcile.py --ledger X --bank Y --out site/report.json
   Also writes site/exceptions.csv.

5. site/index.html: a single file, vanilla JS + Chart.js from a CDN, loading report.json.
   - KPI row: ledger total, bank total, variance, residual
   - Waterfall chart of the bridge
   - Exceptions table with bucket filter and search
   - Matching-stage stats (S1/S2/S3 counts)
   - "Download exceptions.csv" link
   - An Assumptions section rendered from the report
   Clean, professional look, works on mobile.

6. tests/ (pytest)
   - Every ledger row and bank row lands in its ground_truth.json expected bucket (use the flat map).
     Print a confusion matrix if anything fails.
   - The 4 planted gaps are detected with the correct ids. All decoys are matched.
   - Bridge residual == 0.00.
   - Rounding: the batch is flagged as rounding_variance, no individual line is flagged, and the recomputed
     batch fee explains the diff exactly.
   - Handcrafted fixtures (5-10 rows each, independent of the generator) for each bucket.
   - Mutation tests on copies of the data: delete a bank line -> that txn becomes unsettled; duplicate a bank
     line -> detected; change one amount by 1.00 -> an amount mismatch is detected and the bridge still has
     RESIDUAL 0.
   - Parser tests: all date formats, Indian commas, truncated and uppercased references, whitespace.
   - Determinism: running twice gives an identical report.json.
   Run pytest and fix until everything passes. Show the final output.

7. README.md: problem, approach (staged matching), buckets, the bridge, how to run, how to regenerate data,
   test results summary, and a Limitations section. Add requirements.txt and vercel.json (static, serve site/).

ORDER: normalize -> engine -> tests passing -> CLI -> site -> README.
If time is short, cut S3 fallback and the mutation tests last; never cut the bridge or the ground-truth tests.
Git commit after each step. Do not add Claude as co-author in commit messages.
```
