import json
from datetime import date
from pathlib import Path

import pytest

from recon.engine import reconcile
from recon.normalize import load_bank_statement, load_ledger

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample_data"
PERIOD_END = date(2026, 10, 31)


@pytest.fixture(scope="session")
def sample_ledger():
    return load_ledger(SAMPLE_DIR / "ledger.csv")


@pytest.fixture(scope="session")
def sample_bank():
    return load_bank_statement(SAMPLE_DIR / "bank_statement.csv")


@pytest.fixture(scope="session")
def sample_result(sample_ledger, sample_bank):
    return reconcile(sample_ledger, sample_bank, PERIOD_END)


@pytest.fixture(scope="session")
def ground_truth():
    return json.loads((SAMPLE_DIR / "ground_truth.json").read_text())
