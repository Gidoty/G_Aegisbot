"""Test-session guard for protected files.

Installed before anything else is imported. Inside the REAL repository, every attempt to open a protected file (environment
files, keys, databases; see g_aegisbot/backtest/protected.py) raises PermissionError before the file is touched, and broad git
inspection (status, diff, ls-files, show, log, grep and similar) is refused. Temporary directories elsewhere are unaffected,
so tests can build scratch repositories that contain protected-looking names. The guard loads protected.py by file path so
that no package import (and no config import) happens first.
"""
import importlib.util
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("_aegis_protected", _REPO / "g_aegisbot" / "backtest" / "protected.py")
_protected = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_protected)
_REPO_PREFIX = os.path.realpath(_REPO) + os.sep
_BROAD_GIT = {"status", "diff", "ls-files", "show", "log", "grep", "blame", "stash", "cat-file", "ls-tree", "archive", "add",
              "commit", "checkout", "restore", "reset", "clean", "rm", "mv", "apply", "format-patch", "bundle"}
ATTEMPTS = []          # what the guard refused, so a test can prove it is active


def _inside_repo(path) -> bool:
    return (os.path.realpath(path) + os.sep).startswith(_REPO_PREFIX)


def _guard(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0])
        if not os.path.isabs(path) and not os.path.lexists(os.path.join(os.getcwd(), path)):
            return          # a relative open that names nothing in the working directory (for example a dir_fd open made by
                            # shutil.rmtree in a temporary directory) cannot be touching a file of the repository
        if _inside_repo(path) and _protected.is_protected(path):
            ATTEMPTS.append(("open", path))
            raise PermissionError(f"test guard: refusing to open a protected path {os.path.basename(path)!r}")
    elif event == "subprocess.Popen":
        argv = [os.fsdecode(a) for a in (args[1] or [])]
        if argv and os.path.basename(argv[0]) == "git":
            # conservative: ANY argument that names a broad subcommand counts, wherever options place it, and a `-C <dir>`
            # target counts as a working directory
            dirs = [args[2] or os.getcwd()] + [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-C"]
            if any(a in _BROAD_GIT for a in argv[1:]) and any(_inside_repo(d) for d in dirs):
                ATTEMPTS.append(("git", " ".join(argv)))
                raise PermissionError("test guard: refusing broad git inspection in the real repository")


sys.addaudithook(_guard)

import pytest
from sqlalchemy import create_engine

from g_aegisbot.db.base import Base
from g_aegisbot.db.session import get_session_factory
from g_aegisbot.security.crypto import SecretBox, generate_key


@pytest.fixture
def box():
    return SecretBox([generate_key()])


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    from g_aegisbot.db import models  # noqa: F401
    Base.metadata.create_all(engine)
    with get_session_factory(engine)() as s:
        yield s
