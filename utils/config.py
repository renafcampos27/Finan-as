from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

APP_NAME = "Finanças Pessoais"
DEFAULT_DB_NAME = "financas_pessoais.sqlite3"
DB_NAME = DEFAULT_DB_NAME
GREEN = "#10B981"
GREEN_2 = "#059669"
BLUE = "#60A5FA"
MUTED = "#64748B"


def resource_path(relative_path: str) -> Path:
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = Path(sys._MEIPASS)
    except AttributeError:
        # We are not running in PyInstaller bundle
        base_path = Path(__file__).resolve().parent.parent

    return base_path / relative_path


def app_dir() -> Path:
    """Return the installation folder; persistent data lives elsewhere."""
    if getattr(sys, "frozen", False):
        # We are running in a PyInstaller bundle
        return Path(sys.executable).resolve().parent
    else:
        # We are running in a normal Python environment
        # utils/config.py is in the 'utils' folder, so the app root is its parent
        return Path(__file__).resolve().parent.parent


def app_state_root() -> Path:
    """Return the stable per-user folder that survives program replacements."""
    override = os.environ.get("FINANCAS_PESSOAIS_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_root = os.environ.get("LOCALAPPDATA")
    if local_root:
        return (Path(local_root) / "FinancasPessoais").resolve()
    xdg_root = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg_root) if xdg_root else Path.home() / ".local" / "share"
    return (root / "FinancasPessoais").resolve()


@dataclass(slots=True, frozen=True)
class AppPaths:
    """All writable locations belonging to one finance database."""

    root: Path
    data: Path
    database: Path
    attachments: Path
    backups: Path
    history: Path
    configuration: Path
    backup_preferences: Path
    logs: Path
    protections: Path
    instance_lock: Path
    uses_stable_profile: bool

    def create_runtime_directories(self) -> None:
        for folder in (
            self.data,
            self.attachments,
            self.backups,
            self.history,
            self.configuration,
            self.logs,
            self.protections,
        ):
            folder.mkdir(parents=True, exist_ok=True)


def _is_explicit_database_name(value: str) -> bool:
    candidate = Path(str(value))
    return candidate.is_absolute() or candidate.name != DEFAULT_DB_NAME


def paths_for_database(database_file: Path | None = None) -> AppPaths:
    """Resolve coherent data paths without mixing unrelated test/user stores."""
    if database_file is None:
        if _is_explicit_database_name(DB_NAME):
            database = Path(DB_NAME).expanduser().resolve()
            uses_stable_profile = False
        else:
            database = (app_state_root() / "dados" / DEFAULT_DB_NAME).resolve()
            uses_stable_profile = True
    else:
        database = Path(database_file).expanduser().resolve()
        stable_database = (app_state_root() / "dados" / DEFAULT_DB_NAME).resolve()
        uses_stable_profile = database == stable_database

    if uses_stable_profile:
        root = app_state_root()
        data = root / "dados"
        logs = root / "logs"
        protections = root / "protecoes_de_versao"
        instance_lock = root / "financas_pessoais.lock"
    else:
        data = database.parent
        root = data
        logs = data / "logs"
        protections = data / "backups" / "protecoes_de_versao"
        identity = database.stem or "financas_pessoais"
        instance_lock = data / f".{identity}.lock"

    backups = data / "backups"
    configuration = data / "configuracao"
    return AppPaths(
        root=root,
        data=data,
        database=database,
        attachments=data / "anexos",
        backups=backups,
        history=backups / "historico_alteracoes",
        configuration=configuration,
        backup_preferences=configuration / "backup_preferences.json",
        logs=logs,
        protections=protections,
        instance_lock=instance_lock,
        uses_stable_profile=uses_stable_profile,
    )


def db_path() -> Path:
    return paths_for_database().database


def backups_dir() -> Path:
    folder = paths_for_database().backups
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def attachments_dir() -> Path:
    folder = paths_for_database().attachments
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def backup_preferences_path(database_file: Path | None = None) -> Path:
    return paths_for_database(database_file).backup_preferences


def history_dir(database_file: Path | None = None) -> Path:
    folder = paths_for_database(database_file).history
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def logs_dir() -> Path:
    folder = paths_for_database().logs
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def instance_lock_path() -> Path:
    path = paths_for_database().instance_lock
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def version_protection_dir(database_file: Path | None = None) -> Path:
    """Store mandatory upgrade snapshots outside the application folder.

    Production data is protected under the user's stable local-data directory,
    so replacing the program folder cannot remove the recovery copies. Tests
    and explicitly relocated databases keep their protection beside that
    database to avoid touching the real user profile.
    """
    database = Path(database_file or db_path()).resolve()
    application = app_dir().resolve()
    stable_database = (app_state_root() / "dados" / DEFAULT_DB_NAME).resolve()
    is_default_database = database.name == DEFAULT_DB_NAME and database in {
        application / DEFAULT_DB_NAME,
        stable_database,
    }
    if is_default_database:
        folder = app_state_root() / "protecoes_de_versao"
    else:
        folder = database.parent / "backups" / "protecoes_de_versao"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def parse_date(value: str) -> date:
    """Parse an ISO date and never replace invalid data with today's date."""
    try:
        text = str(value).strip()
        if len(text) != 10:
            raise ValueError
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Data inválida: {value!r}. Esperado AAAA-MM-DD.") from exc


def month_bounds(day: date | None = None) -> tuple[str, str]:
    d = day or date.today()
    start = d.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()


def short_month_year(day: date) -> str:
    return f"{day.month:02d}/{str(day.year)[-2:]}"


def backup_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def today_iso() -> str:
    return date.today().isoformat()


def pct(value: float) -> str:
    return f"{value:.1f}%"
