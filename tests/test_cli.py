"""End-to-end CLI tests: report.json/exceptions.csv shape and byte-for-byte determinism."""
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_cli(out_path: Path) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "reconcile.py"),
         "--ledger", str(ROOT / "sample_data" / "ledger.csv"),
         "--bank", str(ROOT / "sample_data" / "bank_statement.csv"),
         "--out", str(out_path)],
        check=True, cwd=ROOT, capture_output=True, text=True,
    )


def test_cli_produces_report_and_exceptions_csv(tmp_path):
    out = tmp_path / "report.json"
    _run_cli(out)
    assert out.exists()
    assert (tmp_path / "exceptions.csv").exists()

    report = json.loads(out.read_text())
    for key in ("meta", "totals", "bridge", "stage_counts", "ledger_bucket_counts",
                "bank_bucket_counts", "exceptions", "assumptions"):
        assert key in report
    assert Decimal(report["bridge"]["RESIDUAL"]) == Decimal("0.00")
    assert len(report["assumptions"]) > 0


def test_cli_is_byte_for_byte_deterministic(tmp_path):
    out1 = tmp_path / "run1" / "report.json"
    out2 = tmp_path / "run2" / "report.json"
    _run_cli(out1)
    _run_cli(out2)
    assert out1.read_text() == out2.read_text()
