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


def _lock():
    root = Path(__file__).resolve().parents[1]
    pins = {}
    for ln in (root / "requirements.lock").read_text().splitlines():
        if ln.strip() and not ln.startswith("#"):
            name, version = ln.split("==")
            pins[re.sub(r"[-_.]+", "-", name).lower()] = version
    return root, pins


def test_the_lock_file_pins_every_direct_requirement_exactly_and_agrees_with_requirements_txt():
    root, pins = _lock()
    for ln in (root / "requirements.txt").read_text().splitlines():
        if ln.strip() and not ln.startswith("#"):
            name = re.split(r"[<>=!~\[ ]", ln.strip())[0]
            assert re.sub(r"[-_.]+", "-", name).lower() in pins, name
            if "==" in ln:
                assert pins[re.sub(r"[-_.]+", "-", name).lower()] == ln.split("==")[1].strip()
    assert pins["ccxt"] == AUDITED_CCXT


def test_the_lock_file_has_only_exact_pins():
    root, _ = _lock()
    for ln in (root / "requirements.lock").read_text().splitlines():
        if ln.strip() and not ln.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9._+!-]+", ln.strip()), ln


def test_the_dockerfile_installs_from_the_lock_file():
    text = (Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile").read_text()
    assert "pip install --no-cache-dir -r requirements.lock" in text


def test_the_systemd_units_use_the_vps_virtualenv_and_the_backup_timer_exists():
    d = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
    for unit in d.glob("*.service"):
        text = unit.read_text()
        assert "/opt/g-aegisbot/venv/bin/" in text and ".venv" not in text, unit.name
    assert "g_aegisbot.tools.backup" in (d / "g-aegisbot-backup.service").read_text()
    assert "OnCalendar" in (d / "g-aegisbot-backup.timer").read_text()
    assert "AEGIS_LIVE_ENABLED" not in "".join(u.read_text() for u in d.glob("*"))      # live is never switched on by a unit file
