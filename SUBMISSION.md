# Submission: Month-End Payments Reconciliation

**Candidate:** Sanoer Soni, B.Tech ECE (AI/ML), MIT-WPU, 2027
**Assessment:** AI Native Engineer Interns (Nov 2026)

**Result:** the October 2026 ledger (504 rows) and bank statement (378 rows) differ by **₹22,185.85**. The variance bridge explains every rupee of that difference, with **RESIDUAL ₹0.00**. All 4 planted gaps are detected and no decoy is flagged.

| # | Deliverable | Where |
|---|---|---|
| 1 | Brainstorming threads (unedited) | Brainstorming: [PASTE_BRAINSTORM_LINK]<br>Test-data generation: [PASTE_DATA_CHAT_LINK] |
| 2 | Raw prompt | `sample_data/prompts/raw_prompt.md` |
| 2 | Distilled prompt (fed to Claude Code) | `sample_data/prompts/distilled_prompt.md` |
| 2 | Prompt 0: test-data generation | `sample_data/prompts/data_gen_prompt.md` |
| 3 | Claude Code thread (incl. wrong turns) | `threads/claude_code_thread.txt` |
| 4 | Test cases | `tests/` (41 passing: `pytest -v`) |
| 5 | Deployed link | https://reconassessment-site.vercel.app/ |
| 5 | Code | https://github.com/Sanoer11/recon_assessment (zip attached) |
| 5 | Demo video | https://drive.google.com/file/d/166pG9u_vKz38rfs50GoblXS6eRZSF2Sa/view?usp=sharing |
| 5 | What it gets wrong in production | See below |

## Thread order
1. **Brainstorming:** understanding the problem, design choices, choosing the deploy target.
2. **Test-data generation:** generator design, planted gaps, decoys, ground truth.
3. **Claude Code build:** normalize → engine → tests → CLI → site → README, including the fixes after the live-deploy review.

## Test cases

| File | What it proves |
|---|---|
| `tests/test_ground_truth.py` | Every ledger row and bank row lands in its expected bucket from `ground_truth.json`. The 4 planted gaps are found with the correct ids, every decoy stays matched, and the bridge residual is ₹0.00 |
| `tests/test_engine.py` | Handcrafted fixtures, independent of the generator, for each bucket, plus the rounding proof (the recomputed batch fee explains the diff exactly), and reference extraction for full, truncated, uppercased, prefixed and whitespace-noisy forms |
| `tests/test_normalize.py` | Parsing of Indian-format amounts and mixed date formats (unknown formats rejected) and whitespace cleanup, plus the opening-balance and running-balance checks |
| `tests/test_mutations.py` | Delete a bank line → that txn is unsettled. Duplicate a bank line → detected. Change an amount by 1.00 → amount mismatch, residual still ₹0.00. Reconciling twice gives the same result |
| `tests/test_cli.py` | `reconcile.py` writes `report.json` and `exceptions.csv`, and two runs are byte-for-byte identical |

The reconciler never reads `ground_truth.json`; only the tests do.

## How to run
```bash
pip install -r requirements.txt
python generate_sample.py          # regenerate data (seeded, deterministic)
pytest -v                          # 41 tests
python reconcile.py --ledger sample_data/ledger.csv --bank sample_data/bank_statement.csv --out site/report.json
```

## What it would get wrong in production
Production bank statements rarely carry clean references, so many lines would fall to the low-confidence amount+date fallback, where two same-value settlements on the same day can be swapped silently. It assumes one currency, a fixed 2.36% fee and a static RBI holiday list, so FX conversion, merchant-specific pricing, chargebacks, partial refunds and unplanned bank closures would all show up as false exceptions. It also reconciles a single closed month, so a refund or settlement whose counterpart sits in another period gets flagged as an orphan or timing gap instead of being matched across months.
