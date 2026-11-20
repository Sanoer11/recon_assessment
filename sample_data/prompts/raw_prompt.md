# Raw Prompt

The assessment brief, exactly as I first pasted it into the brainstorming chat:

```
The Problem
A payments company's books don't balance at month end. They know every transaction their platform processed. They know what the bank says actually arrived. The two should match. They don't. Find out why. Show them where the gaps are.

Test Data
No files provided. Generate your own — that's part of the assessment. A payments platform records a transaction instantly when a customer pays. The bank batches and settles funds 1–2 days later. At month end every transaction should have a matching settlement.
Plant these gap types: a transaction that settled the following month · a rounding difference that only shows when summed · a duplicate entry in one dataset · a refund with no matching original transaction.
State your assumptions. There are no wrong ones as long as you name them.

What to Submit
1 Brainstorming thread Full LLM convo. Do not clean it up.
2 Distilled prompt and  Raw Prompt (mandatory )  The single prompt you fed into Claude Code or Cursor or any other AI coding tool.
3 Claude Code thread Full execution thread including wrong turns.
4 Test cases What you wrote to verify your solution works.
5 Working output Deployed link + code zip + Demo video + 3 sentences on what it 
would get wrong in production.

i have 2 hours, i wanna ace this assessment
```

## How the raw brief became the distilled prompt

| Raw line | Decision in the distilled prompt |
|---|---|
| "Find out why. Show them where the gaps are." | Classify every ledger row and bank row into one bucket, and build a variance bridge that explains the whole difference with RESIDUAL = ₹0.00 |
| "Generate your own" test data | Separate seeded generator (`generate_sample.py`) with a machine-readable `ground_truth.json`; the reconciler never reads it, only the tests do |
| "The bank batches and settles funds 1–2 days later" | Batched + one-to-one settlements at T+1/T+2 business days on the RBI holiday calendar; staged matching S1 txn ref → S2 batch ref → S3 low-confidence fallback |
| "a transaction that settled the following month" | `timing_difference` bucket with the expected settle date shown |
| "a rounding difference that only shows when summed" | `rounding_variance` on the batch, proved by recomputing the batch-level fee; no single line flagged |
| "a duplicate entry in one dataset" | Ledger-side duplicate seconds apart; a same-merchant, same-amount decoy days later must stay matched |
| "a refund with no matching original transaction" | `unmatched_refund`, reported as a control exception even though its variance impact is ₹0 |
| "State your assumptions" | `ASSUMPTIONS.md` (INR, 2.36% fee incl. GST, calendar, batching, reference formats) plus an Assumptions section on the site |
| "Test cases" | Ground-truth comparison for every row, handcrafted fixtures, parser, mutation and determinism tests |
| "Deployed link" | Static site (`report.json` + `index.html`) on Vercel, so the link never sleeps or breaks |
