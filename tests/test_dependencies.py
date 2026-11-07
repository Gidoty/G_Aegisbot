"""The exchange adapter was audited against one ccxt version. A different version must not slip in unnoticed."""
import re
from pathlib import Path

import ccxt

AUDITED_CCXT = "4.5.92"


def test_the_installed_ccxt_is_the_audited_version(capsys):
    print(f"installed ccxt version: {ccxt.__version__}")
    assert ccxt.__version__ == AUDITED_CCXT


def test_requirements_pin_ccxt_exactly_to_the_audited_version():
    text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    lines = [ln.strip() for ln in text.splitlines() if re.match(r"\s*ccxt\b", ln)]
    assert lines == [f"ccxt=={AUDITED_CCXT}"]
