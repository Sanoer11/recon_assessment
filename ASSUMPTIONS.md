# ASSUMPTIONS — synthetic data for the October 2026 reconciliation

Run with `python generate_sample.py [--out sample_data] [--with-nov-lookahead]`. The generator uses only the standard library and has four fixed seeds. Re-running it produces byte-identical files, including under different `PYTHONHASHSEED` values.

## 1. Calendar and time

| # | Assumption |
|---|---|
| 1.1 | **Single time zone, IST (+05:30).** `created_at` carries an explicit `+05:30` offset. A transaction's *capture date* is its IST calendar date. |
| 1.2 | **Business-day calendar: RBI holiday list, Mumbai centre.** Non-business days are Sundays, the 2nd and 4th Saturdays, Gandhi Jayanti (Fri 2 Oct), Dussehra (Tue 20 Oct) and Bali Pratipada (Tue 10 Nov). The **1st, 3rd and 5th Saturdays are working days**, so Sat 3 Oct, Sat 17 Oct and **Sat 31 Oct** settle normally. |
| 1.3 | The platform records sales every day, holidays included. Only bank settlement follows the business-day calendar. |
| 1.4 | NEFT and RTGS run 24×7 in India, but payment-gateway payouts are assumed to follow bank working days, which is the convention most gateways publish. |

## 2. Settlement

| # | Assumption |
|---|---|
| 2.1 | **Lag rule:** `settle_date = capture_date + N business days`, with N ∈ {1, 2} weighted 70/30. N is drawn once per settlement unit, so every transaction in a batch shares one lag. A capture on a non-business day starts counting from that day, so a Sunday capture with N = 1 settles on Monday. |
| 2.2 | **Batching rule:** merchants are fixed as either `batch` (M001, M002, M005, M008, M011, M014, M017, M019) or `one_to_one` (the other 12). A batch merchant gets **one bank line per capture date**, netted, with batch id `B-<merchant>-<YYYYMMDD of capture>`. A batch of one transaction is still a batch line. |
| 2.3 | **Refunds are never netted.** Each refund settles as its own negative (debit) bank line, whatever the merchant's mode. |
| 2.4 | **Full month of volume.** Sales run 1–31 October, so settlements naturally spill into November. All spillovers are recorded in the ground truth as `timing_difference` under `natural_exceptions`. Gap (a) is the hard-coded one among them. |
| 2.5 | **Clean opening cut-off.** No September captures settle in October. A real statement would contain them; they are left out so the October bank file holds no lines from a prior period that the ledger can't explain. This is a scope cut. |
| 2.6 | `bank_statement.csv` holds October settle dates only. `--with-nov-lookahead` writes `bank_statement_nov_lookahead.csv`, which contains **only the 2–3 Nov settlements of October captures**, so a reconciler can confirm timing items. It is off by default because the brief specifies two CSVs and a real October close happens before the November statement exists. Its references come from a separate RNG stream, so turning the flag on never changes the October file. |

## 3. Fees and rounding

| # | Assumption |
|---|---|
| 3.1 | **Single currency, INR.** All amounts are in rupees with 2 dp (paise). |
| 3.2 | **Platform fee per sale:** `fee = round_half_up(amount × 2.36%, 2dp)`. The 2.36% is a 2% MDR plus 18% GST on that MDR, **collapsed into one rate and rounded once**. Real gateways round the MDR and the GST separately; collapsing them keeps every rounding effect in this dataset a one-step effect that can be explained. |
| 3.3 | **Refund fee is 0.00.** The platform does not return the original MDR, so the bank debit equals the refund amount. |
| 3.4 | **Bank amount for a normal line:** Σ(amount − fee) over the unit's transactions, using the platform's own per-line fees. |
| 3.5 | **Batch-priced merchant M011:** M011's acquirer contract computes the fee once per batch: `round_half_up(Σgross × 2.36%)`. This applies to **every** M011 batch, not only the planted one. Three M011 batches drift by ₹0.01 naturally; they are recorded as `rounding_variance` under `natural_exceptions`. |
| 3.6 | **Why only M011:** applying batch pricing to all eight batch merchants was measured and gives 21 natural drifts, which would bury the planted gap. The contract-level scope keeps the rule consistent (no single anomalous batch) while keeping the rounding bucket meaningful. |
| 3.7 | Both sides round half-up (`decimal.ROUND_HALF_UP`), so drift comes **only** from rounding per line versus rounding once on the total, never from different rounding modes. |

## 4. Ledger

| # | Assumption |
|---|---|
| 4.1 | 500 sale rows (planted ones included) and 4 refund rows. The ledger itself is clean, with no formatting noise; all noise is on the bank side. |
| 4.2 | Sales are positive and refunds negative, with `type` set accordingly. |
| 4.3 | **`status` is always `succeeded`. Failed, pending and disputed transactions are out of scope** for this dataset; this is a deliberate scope cut, not an oversight. |
| 4.4 | `txn_id = "txn_" + 12 lowercase hex`. The generator asserts that the first 10 hex characters are unique, so truncated bank references still resolve to exactly one transaction. |
| 4.5 | Amounts: 45% are "charm prices" (₹199 … ₹7,999) and 55% are log-normal cart totals with a median of about ₹1,100, clamped to ₹99–₹75,000. |
| 4.6 | Merchant volume is weighted (M001 heaviest), and times of day follow an Indian e-commerce curve that peaks around midday and 19:00–22:00. |
| 4.7 | The ledger has no `original_txn_id` column (per the brief), so a reconciler can only link a refund to its sale by merchant, absolute amount and time order. |

## 5. Bank statement construction and noise

| # | Assumption |
|---|---|
| 5.1 | **Row 1 is the opening balance** (`BAL B/F`, amount 0.00, running balance ₹5,00,000.00). It lives in the file so `running_balance` adds up for a human reader, and its ground-truth bucket is `non_transaction`. |
| 5.2 | `amount` is signed: credits positive, debits negative. `running_balance` is the balance after the row. Rows are ordered by settle date, then credits before debits, then merchant, then key. |
| 5.3 | **Reference strings** use a fictional gateway "PAYGW" and an NEFT-style UTR (`<bank>N<yy><day-of-year><6 digits>`). There are three variants for one-to-one sales: the full id (`NEFT/<UTR>/PAYGW SETTL txn_…`), the prefix stripped and truncated to 10 uppercase hex (`PAYGW/5C80F3A1E9`), and a prefixed, uppercased form (`<UTR>-STL-TXN5C80F3A1E9B4-M006`). Batches use `NEFT/<UTR>/PAYGW BATCH B-M011-20261015` or `PAYGW SETTL BM01120261015`. Refunds use `PAYGW RFND txn_…` or `NEFT/<UTR>/RFND/<10 hex>`. |
| 5.4 | **Date formats are mixed:** ISO `2026-10-14` (50%), `14/10/2026` (30%), `14-Oct-2026` (20%). **MM/DD is never used**, so every date parses to exactly one value. |
| 5.5 | **Amounts of ₹1,000 or more** use Indian lakh grouping half the time (`1,25,430.50`). The running balance on the same row uses the same style. |
| 5.6 | **Whitespace noise:** random leading or trailing spaces, or one doubled internal space, in `reference` and `description`. |
| 5.7 | **Descriptions:** `NEFT CR`, `RTGS CR` (at ₹2,00,000 and above), `NEFT DR` and `CHRG`. They are informative, not identifying. |
| 5.8 | **The one bank-only line** is the month-end account maintenance charge on Sat 31 Oct: −₹590.00 (₹500 + 18% GST). |

## 6. Planted gaps

All planted rows are hard-coded constants; gap (b)'s ids and times come from their own RNG stream. Changing the baseline therefore cannot move them.

| Gap | Rows | Expected bucket | How it is detectable | Why it isn't trivial |
|---|---|---|---|---|
| **(a) Late settlement** | `txn_e41b07c9d2a6`, M004, Sat 31 Oct 18:42:19, ₹2,350.00, fee 55.46 | `timing_difference` | It is unmatched, and its expected settle date (Mon 2 Nov) falls after the period end. | It is one of 16 timing rows (the other 15 are natural spillovers). Treating Sat 31 Oct as a non-working day gives the right bucket for the wrong reason. |
| **(b) Aggregate rounding** | 20 M011 sales on Thu 15 Oct; bank row 188 on Fri 16 Oct | `rounding_variance` | Batch membership resolves via `BM01120261015`. Gross is ₹38,066.78, line fees total 898.44, the batch fee is 898.38, and the bank credits ₹37,168.40 against ₹37,168.34 expected: **+₹0.06**. | Every line's fee is a correct half-up rounding; the sub-paisa errors (0.0022–0.0045) only show in the sum. The three natural ₹0.01 M011 drifts sit beside it. |
| **(c) Duplicate (ledger side)** | Original `txn_5c80f3a1e9b4` 13:05:42 (matched, bank row 257) and duplicate `txn_9d27b6e0c315` 13:05:47 (unmatched), both M006 ₹1,499.00 on 21 Oct | `duplicate` (for `txn_9d27…`) | Same merchant, amount and status, 5 s apart, and only one settlement exists. | Decoy: the same M006 ₹1,499.00 recurs on 23 Oct and is matched. Amount plus merchant alone gives a false positive. |
| **(d) Orphan refund** | `txn_b3f61d8a4c07`, M013, 8 Oct, −₹1,186.40; bank row 95 on 9 Oct, −₹1,186.40 | `unmatched_refund` | The ledger and bank rows match each other, but no M013 ₹1,186.40 sale exists in either file. The original was notionally 24 Sep. | Decoys: 3 full refunds whose original sales are in the October ledger. "Every refund is an orphan" fails. |

**Why the duplicate is on the ledger side:** a client retry or double-submit that bypasses idempotency creates two platform records seconds apart. The acquirer captures once, so the bank sees one settlement. A bank-side duplicate would be two identical bank lines with no timestamps or txn_ids to compare, which is a different kind of problem from the one the brief describes.

## 7. ground_truth.json

- `planted_gaps` holds exactly the four above, in order a–d.
- `natural_exceptions` holds the month-end spillovers and M011's natural 1-paisa drifts.
- `decoys` lists traps that must stay `matched`.
- `bank_only` and `non_transaction` hold the charge and opening rows.
- `matches` lists every clean match with its bank row, reference and ledger ids.
- `expected_ledger_bucket` (`{txn_id: bucket}`) and `expected_bank_bucket` (`{"row": bucket}`) are the flat maps tests should assert against. **Every ledger row and every bank row appears exactly once**, and the generator asserts this partition.
- `bank_row` is the 1-based data row (header excluded).

## 8. Invariants checked on every run

The generator stops with an error if any of these fail:

- The ledger and bank partitions are complete and non-overlapping.
- There are exactly 4 planted gaps, in their buckets.
- Gap (a) is absent from October.
- Gap (b)'s per-line fees are all correct and its drift is exactly +0.06.
- Gap (c) has exactly one settlement.
- There are no unplanned same-merchant, same-amount sales within 10 minutes of each other.
- No sale exists that gap (d) could pair with.
- 10-hex prefixes are unique.
- The running balance reconciles to the closing balance.
- Every drifting batch is recorded.
- Every settlement lands on a business day.
