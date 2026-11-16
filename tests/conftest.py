"""Global test isolation for writable application state."""

from __future__ import annotations

import logging
import faulthandler
import os
import shutil
import tempfile
from pathlib import Path

_process_watchdog_file = None
_watchdog_seconds = os.environ.pop("FINANCAS_PESSOAIS_TEST_WATCHDOG_SECONDS", None)
_watchdog_path = os.environ.pop("FINANCAS_PESSOAIS_TEST_WATCHDOG_LOG", None)
if _watchdog_seconds:
    # The runner bounds the entire process, including collection and native
    # shutdown where pytest's per-test Python timer may no longer be active.
    # This capability belongs to this process. Nested probes retain their
    # parent's filesystem boundary and must opt in with their own owned path.
    if _watchdog_path:
        _process_watchdog_file = open(
            _watchdog_path, "a", encoding="utf-8",
        )
    faulthandler.dump_traceback_later(
        float(_watchdog_seconds), repeat=True,
        file=_process_watchdog_file,
    )


def _native_phase(node: str, phase: str) -> None:
    if _process_watchdog_file is not None:
        _process_watchdog_file.write(f"{node}: {phase}\n")
        _process_watchdog_file.flush()


# This runs before test modules import application services. Logs, the default
# database, backups and preferences therefore belong to the test session and
# can never be mixed into the user's real LocalAppData profile.
TEST_STATE_ROOT = Path(tempfile.mkdtemp(prefix="financas-pessoais-pytest-"))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
for name in ("home", "localappdata", "appdata", "tmp", "profile", "cache", "bootstrap"):
    (TEST_STATE_ROOT / name).mkdir()
for variable, folder in {
    "HOME": "home",
    "USERPROFILE": "home",
    "LOCALAPPDATA": "localappdata",
    "APPDATA": "appdata",
    "XDG_DATA_HOME": "localappdata",
    "XDG_CONFIG_HOME": "appdata",
    "TMP": "tmp",
    "TEMP": "tmp",
    "TMPDIR": "tmp",
    "RUFF_CACHE_DIR": "cache",
}.items():
    os.environ[variable] = str(TEST_STATE_ROOT / folder)
tempfile.tempdir = str(TEST_STATE_ROOT / "tmp")
os.environ["FINANCAS_PESSOAIS_STATE_DIR"] = str(TEST_STATE_ROOT / "profile")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["FINANCAS_PESSOAIS_TESTING"] = "1"
# Tests that exercise environment-key selection inject their own synthetic key.
os.environ.pop("GEMINI_API_KEY", None)
os.environ["FINANCAS_PESSOAIS_TEST_ROOT"] = str(TEST_STATE_ROOT)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
# Demo seeding is opt-in. Run legacy populated baselines with
# --finance-profile=demo; new suites select the profile with fixtures/markers.
os.environ.setdefault("FINANCAS_PESSOAIS_DEMO_DATA", "0")

# Child Python probes inherit the same write boundary, even without pytest.
bootstrap = TEST_STATE_ROOT / "bootstrap"
(bootstrap / "sitecustomize.py").write_text(
    "import os, sys\nfrom pathlib import Path\n"
    f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
    "from scripts.test_sandbox import activate\n"
    "_finance_sentinel = activate(Path(os.environ['FINANCAS_PESSOAIS_TEST_ROOT']))\n",
    encoding="utf-8",
)
os.environ["PYTHONPATH"] = os.pathsep.join(
    [str(bootstrap), str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")]
)

from scripts.test_sandbox import activate

WRITE_SENTINEL = activate(TEST_STATE_ROOT)


import pytest


@pytest.fixture
def native_diagnostics(request):
    return lambda phase: _native_phase(request.node.nodeid, phase)


def pytest_addoption(parser):
    parser.addoption("--finance-profile", choices=("empty", "demo"), default=None)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    config._inicache["cache_dir"] = str(TEST_STATE_ROOT / "cache" / "pytest")
    if config.option.basetemp and not Path(
        config.option.basetemp
    ).resolve().is_relative_to(TEST_STATE_ROOT):
        raise pytest.UsageError("--basetemp must belong to the finance test session")
    config.option.basetemp = str(TEST_STATE_ROOT / "pytest-tmp")
    profile = config.getoption("--finance-profile")
    if profile:
        os.environ["FINANCAS_PESSOAIS_DEMO_DATA"] = "1" if profile == "demo" else "0"


@pytest.fixture(autouse=True)
def _explicit_profile(request, monkeypatch):
    marker = request.node.get_closest_marker("finance_profile")
    if marker:
        profile = marker.args[0]
        if profile not in {"empty", "demo"}:
            raise pytest.UsageError("finance_profile must be empty or demo")
        monkeypatch.setenv(
            "FINANCAS_PESSOAIS_DEMO_DATA", "1" if profile == "demo" else "0"
        )


@pytest.fixture
def empty_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCAS_PESSOAIS_DEMO_DATA", "0")
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(tmp_path / "empty-profile"))
    return tmp_path / "empty-profile"


@pytest.fixture
def demo_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCAS_PESSOAIS_DEMO_DATA", "1")
    monkeypatch.setenv("FINANCAS_PESSOAIS_STATE_DIR", str(tmp_path / "demo-profile"))
    return tmp_path / "demo-profile"


@pytest.fixture
def memory_secret_store():
    from services.secret_store import MemorySecretStore

    return MemorySecretStore()


@pytest.fixture(autouse=True)
def _cleanup_qt_state(request):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid

    node = request.node.nodeid
    _native_phase(node, "fixture setup: QApplication lookup")
    app = QApplication.instance()
    existing = set(app.topLevelWidgets()) if isinstance(app, QApplication) else set()
    _native_phase(node, "fixture setup finished")
    yield
    _native_phase(node, "fixture cleanup begin")
    try:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            tracing_ui = bool(os.environ.get("FINANCAS_UI_STATE_WORKER"))
            if tracing_ui:
                print("UI phase: fixture cleanup begin", flush=True)
            owned_tax_dialogs = []
            for widget in list(app.topLevelWidgets()):
                if not isValid(widget) or widget in existing:
                    continue
                if widget.isModal() or widget.isVisible():
                    _native_phase(node, f"close {type(widget).__name__}")
                    widget.close()
                    _native_phase(node, "close returned")
                if (
                    isValid(widget)
                    and widget.objectName() == "PortugueseTaxDialog"
                    and not widget.isVisible()
                ):
                    # Fiscal fixtures create parentless dialogs. Release those
                    # while QApplication is alive, without flushing deferred
                    # deletion owned by unrelated window/animation fixtures.
                    widget.deleteLater()
                    owned_tax_dialogs.append(widget)
            _native_phase(node, "fixture processEvents begin")
            app.processEvents()
            _native_phase(node, "fixture processEvents returned")
            for widget in owned_tax_dialogs:
                if isValid(widget):
                    QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
            if tracing_ui:
                print("UI phase: fixture cleanup finished", flush=True)
        _native_phase(node, "fixture cleanup finished")
    except Exception as exc:
        _native_phase(node, f"fixture cleanup exception: {type(exc).__name__}: {exc}")


def pytest_sessionfinish(session, exitstatus) -> None:
    try:
        from utils.app_logging import shutdown_logging

        shutdown_logging()
    except Exception:
        pass
    logging.shutdown()
    if WRITE_SENTINEL.journal.exists():
        violations = WRITE_SENTINEL.journal.read_text(encoding="utf-8").splitlines()
        if violations:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter:
                reporter.write_sep(
                    "!", f"Test isolation: {len(violations)} blocked operations"
                )
                for violation in violations[:10]:
                    reporter.write_line(violation)
    WRITE_SENTINEL.enabled = False
    shutil.rmtree(TEST_STATE_ROOT, ignore_errors=True)
