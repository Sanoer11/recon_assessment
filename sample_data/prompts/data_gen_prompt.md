# Prompt 0: Test Data Generation

Fed into Claude before the build. Full thread, including the follow-up corrections (switch to INR, full month-end volume, decoys), is linked in SUBMISSION.md.

---

```
Generate the test data for the reconciliation tool. No real data exists — you must create it, and the data-generation design is itself being assessed.

DOMAIN RULES
- The platform records a transaction the instant a customer pays (timestamp to the second).
- The bank settles funds 1–2 business days later, in batches.
- At month end, every platform transaction should have a matching settlement. The month under reconciliation is October 2026.

WHAT TO BUILD
A script `generate_sample.py` that writes two CSVs plus a ground-truth file:

1. `ledger.csv` — platform transactions. Columns: txn_id, created_at (ISO 8601), amount (string, 2 dp), currency, fee, merchant_id, type (sale|refund), status.
2. `bank_statement.csv` — bank settlements. Columns: settle_date (date only), amount, reference (free text that partially embeds a txn_id or batch id), description, running_balance.
3. `ground_truth.json` — every planted gap, which rows are involved, and the expected bucket the reconciler should assign.

Baseline: ~500 clean sales transactions across October 2026, spread over ~20 merchants, with each settled in the bank 1–2 business days later. Fees should be deducted so bank amount = ledger amount − fee. Some settlements should be batched (one bank line = several ledger rows netted), some one-to-one.

PLANT EXACTLY THESE GAPS, and make each one detectable but not trivial:
a. Late settlement — a transaction created Oct 30 or 31 that settles on Nov 2–3. It must be absent from the October bank data but present in the ledger.
b. Rounding difference visible only in aggregate — a batch of ~15–25 transactions where each individual fee is rounded half-up to 2 dp by the platform but the bank computes the fee on the batch total and rounds once. Each line looks fine; the batch sum is off by a few cents. Do NOT plant this as a single mis-rounded line.
c. Duplicate entry in one dataset only — the same sale appears twice in the ledger (same amount, merchant, timestamps a few seconds apart, different txn_id) but settles once at the bank. Choose which side has the duplicate and state why.
d. Refund with no matching original — a refund in the ledger (and settled negatively at the bank) whose original sale is not in either dataset (e.g. the sale was in September, before the reconciliation window).

Also add light realism noise so the matcher can't rely on exact-string equality: mixed date formats in the bank file, whitespace, bank references that truncate or prefix the txn_id, one bank-only line (monthly account fee).

ASSUMPTIONS
Write an ASSUMPTIONS.md listing every modelling choice you made, including at minimum: business-day calendar used, settlement lag rule, fee formula and rounding method on each side, batching rule, whether refunds are netted or settled separately, currency (single or multi), and how the bank reference string is constructed. There are no wrong assumptions — but every one must be named.

CONSTRAINTS
- Seeded RNG; regenerating must give identical files.
- Ground truth must be machine-readable so the reconciler's tests can assert each planted gap lands in the right bucket.
- Keep the generator readable; it will be reviewed alongside the reconciler.

Before writing code, state the assumptions you intend to make and the exact rows/amounts you'll use for the four planted gaps. I'll confirm, then you implement.
```
