"""GitHub stable-release discovery and bounded, verified Windows update staging.

The GitHub repository and HTTPS connection are the trust boundary. SHA-256
detects incomplete or changed assets; it is not an independent publisher signature.
No financial data is read or written here. Installation requires an explicit UI
decision and the application's normal successful shutdown.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY = "renafcampos27/Finan-as"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
NETWORK_TIMEOUT = 30
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 20_000
HELPER_NAME = "APLICAR_ATUALIZACAO.ps1"
_VERSION = re.compile(r"(?:v)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }
)


class UpdateError(RuntimeError):
    """An update was unavailable, unsafe, incomplete or incompatible."""


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    version: str
    tag: str
    page_url: str
    asset_name: str
    asset_url: str
    asset_size: int
    checksum_url: str
    digest: str | None = None

    @property
    def html_url(self) -> str:
        return self.page_url


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    release: ReleaseInfo
    work_dir: Path
    package_root: Path
    archive_path: Path
    sha256: str
    manifest_sha256: str = ""
    installer_sha256: str = ""


def version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match or len(value) > 48:
        raise UpdateError("A versão publicada não tem um formato estável reconhecido.")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _allowed_https(url: str, hosts: frozenset[str]) -> bool:
    parsed = urllib.parse.urlsplit(url)
    try:
        return (
            parsed.scheme == "https"
            and parsed.hostname in hosts
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
    except ValueError:
        return False


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_https(newurl, _DOWNLOAD_HOSTS):
            raise UpdateError(
                "O GitHub redirecionou a descarga para um destino não permitido."
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(url: str):
    if url != LATEST_API_URL and not _allowed_https(url, _DOWNLOAD_HOSTS):
        raise UpdateError("O endereço de atualização não pertence ao GitHub permitido.")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json"
            if url == LATEST_API_URL
            else "application/octet-stream",
            "User-Agent": "FinancasPessoais-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return urllib.request.build_opener(_SafeRedirect()).open(
        request, timeout=NETWORK_TIMEOUT
    )


def _read_bounded(url: str, limit: int) -> bytes:
    with _open_url(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("A resposta de atualização ultrapassou o tamanho permitido.")
    return data


def release_from_metadata(metadata: Any, current_version: str) -> ReleaseInfo | None:
    current = version_tuple(current_version)
    if not isinstance(metadata, dict):
        raise UpdateError("O GitHub devolveu metadados de versão inválidos.")
    if metadata.get("draft") is not False or metadata.get("prerelease") is not False:
        return None
    tag = metadata.get("tag_name", "")
    version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else ""
    if version_tuple(version) <= current:
        return None
    page_url = f"{RELEASES_URL}/tag/{tag}"
    if metadata.get("html_url") != page_url:
        raise UpdateError("A versão recebida não pertence ao repositório da aplicação.")
    asset_name = f"LiquidGlass_App_v{version}.zip"
    checksum_name = f"LiquidGlass_App_v{version}.sha256.txt"
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise UpdateError(
            "A versão publicada não inclui uma lista de ficheiros válida."
        )
    selected = {}
    for name in (asset_name, checksum_name):
        matches = [
            item
            for item in assets
            if isinstance(item, dict) and item.get("name") == name
        ]
        if len(matches) != 1 or matches[0].get("state") != "uploaded":
            raise UpdateError(
                "A versão publicada ainda não tem um pacote Windows completo."
            )
        asset = matches[0]
        expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
        if asset.get("browser_download_url") != expected_url:
            raise UpdateError("O pacote publicado tem um endereço inesperado.")
        size = asset.get("size")
        maximum = MAX_ARCHIVE_BYTES if name == asset_name else 4096
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 < size <= maximum
        ):
            raise UpdateError("O tamanho do pacote publicado não é válido.")
        selected[name] = asset
    asset = selected[asset_name]
    digest = asset.get("digest")
    if digest is not None:
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or not _SHA256.fullmatch(digest[7:])
        ):
            raise UpdateError("A impressão digital publicada pelo GitHub não é válida.")
        digest = digest[7:].lower()
    return ReleaseInfo(
        version,
        tag,
        page_url,
        asset_name,
        asset["browser_download_url"],
        asset["size"],
        selected[checksum_name]["browser_download_url"],
        digest,
    )


def check_for_update(current_version: str) -> ReleaseInfo | None:
    """Check the public stable channel without sending local data or credentials."""
    try:
        metadata = json.loads(_read_bounded(LATEST_API_URL, MAX_METADATA_BYTES))
        return release_from_metadata(metadata, current_version)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # Empty repository / no public stable release yet.
        raise UpdateError(
            "Não foi possível consultar versões no GitHub. Tenta novamente mais tarde."
        ) from exc
    except (OSError, ValueError) as exc:
        raise UpdateError(
            "Não foi possível verificar atualizações. Confirma a ligação à Internet."
        ) from exc


def _checksum(data: bytes, asset_name: str) -> str:
    try:
        fields = data.decode("ascii").strip().split()
    except UnicodeError as exc:
        raise UpdateError("O ficheiro de verificação não é válido.") from exc
    if len(fields) != 2 or not _SHA256.fullmatch(fields[0]) or fields[1] != asset_name:
        raise UpdateError(
            "O ficheiro de verificação não corresponde ao pacote esperado."
        )
    return fields[0].lower()


def _safe_member(info: zipfile.ZipInfo) -> str:
    name = info.orig_filename
    path = PurePosixPath(name)
    parts = name.rstrip("/").split("/")
    if (
        not name
        or name != info.filename
        or "\\" in name
        or any(char in name for char in ':\x00*?"<>|')
        or path.is_absolute()
        or any(p in ("", ".", "..") for p in parts)
        or any(p.endswith((".", " ")) for p in parts)
        or any(
            re.fullmatch(
                r"(?i)(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?",
                p,
            )
            for p in parts
        )
        or any(ord(char) < 32 for char in name)
        or len(name) > 240
        or parts[0] != "LiquidGlass"
        or stat.S_ISLNK(info.external_attr >> 16)
        or (
            stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
        )
        or bool(info.flag_bits & 1)
        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
    ):
        raise UpdateError(
            "O pacote contém um caminho ou tipo de ficheiro não permitido."
        )
    return name


def _extract_package(
    archive_path: Path, destination: Path, version: str
) -> tuple[Path, str, str]:
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if (
            len(members) > MAX_FILES
            or sum(item.file_size for item in members) > MAX_EXTRACTED_BYTES
        ):
            raise UpdateError("O pacote excede os limites de extração permitidos.")
        seen: set[str] = set()
        for info in members:
            name = _safe_member(info)
            if name.rstrip("/").casefold() in seen:
                raise UpdateError("O pacote contém caminhos repetidos.")
            seen.add(name.rstrip("/").casefold())
        required = {
            "liquidglass/liquidglass.exe",
            "liquidglass/release_manifest.json",
            "liquidglass/instalar_financas_pessoais.ps1",
        }
        if not required.issubset(seen):
            raise UpdateError("O pacote de atualização está incompleto.")
        manifest_info = archive.getinfo("LiquidGlass/RELEASE_MANIFEST.json")
        if manifest_info.file_size > MAX_METADATA_BYTES:
            raise UpdateError("O manifesto do pacote excede o tamanho permitido.")
        manifest_bytes = archive.read(manifest_info)
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict) or manifest.get("version") != version:
            raise UpdateError("A versão do pacote não corresponde à versão publicada.")
        installer_info = archive.getinfo("LiquidGlass/INSTALAR_FINANCAS_PESSOAIS.ps1")
        if installer_info.file_size > MAX_METADATA_BYTES:
            raise UpdateError("O instalador excede o tamanho permitido.")
        installer_hash = hashlib.sha256(archive.read(installer_info)).hexdigest()
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        # Full inventory/hash validation runs again in the existing installer.
        archive.extractall(destination)
    return destination / "LiquidGlass", manifest_hash, installer_hash


def download_update(
    release: ReleaseInfo,
    cache_root: Path,
    progress: Callable[[int, int], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> PreparedUpdate:
    """Stage a verified package in a unique directory; clean partial failures."""
    cache_root = Path(cache_root).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="financas-update-", dir=cache_root))
    try:

        def check_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise UpdateError("A descarga da atualização foi cancelada.")

        check_cancelled()
        (work_dir / ".update-workdir").write_text("FinancasPessoais", encoding="ascii")
        expected_hash = _checksum(
            _read_bounded(release.checksum_url, 4096), release.asset_name
        )
        if release.digest is not None and expected_hash != release.digest:
            raise UpdateError("O SHA-256 do pacote difere do publicado pelo GitHub.")
        archive_path = work_dir / release.asset_name
        received = 0
        digest = hashlib.sha256()
        with (
            _open_url(release.asset_url) as response,
            archive_path.open("xb") as output,
        ):
            while block := response.read(1024 * 1024):
                check_cancelled()
                received += len(block)
                if received > release.asset_size or received > MAX_ARCHIVE_BYTES:
                    raise UpdateError("A descarga excedeu o tamanho publicado.")
                output.write(block)
                digest.update(block)
                if progress:
                    progress(received, release.asset_size)
        if received != release.asset_size or digest.hexdigest() != expected_hash:
            raise UpdateError(
                "A descarga ficou incompleta ou falhou a verificação SHA-256."
            )
        check_cancelled()
        package_root, manifest_hash, installer_hash = _extract_package(
            archive_path, work_dir / "extracted", release.version
        )
        check_cancelled()
        return PreparedUpdate(
            release,
            work_dir,
            package_root,
            archive_path,
            expected_hash,
            manifest_hash,
            installer_hash,
        )
    except Exception as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(
            "Não foi possível preparar a atualização. Nenhum ficheiro da aplicação foi substituído."
        ) from exc


def managed_install_root() -> Path | None:
    """Never redirect a source/portable installation into a different install."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    local_data = os.environ.get("LOCALAPPDATA")
    if not local_data:
        return None
    expected = (Path(local_data) / "Programs" / "FinancasPessoaisLiquidGlass").resolve()
    actual = Path(sys.executable).resolve().parent
    if actual != expected or not (actual / "RELEASE_MANIFEST.json").is_file():
        return None
    return actual


def launch_windows_update(
    prepared: PreparedUpdate, parent_pid: int | None = None
) -> subprocess.Popen:
    """Launch only after the normal data-protection shutdown has succeeded.

    The helper waits for this process to exit, then reuses the package installer.
    A source or portable build must offer the public release page instead.
    """
    install_root = managed_install_root()
    if install_root is None:
        raise UpdateError(
            "Esta instalação requer atualização manual através da página de versões."
        )
    if not _SHA256.fullmatch(prepared.manifest_sha256) or not _SHA256.fullmatch(
        prepared.installer_sha256
    ):
        raise UpdateError(
            "A atualização não tem as verificações necessárias para instalação."
        )
    helper_source = install_root / HELPER_NAME
    if not helper_source.is_file():
        raise UpdateError(
            "O assistente de atualização não está disponível nesta instalação."
        )
    helper_copy = prepared.work_dir / HELPER_NAME
    shutil.copy2(helper_source, helper_copy)
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    command = [
        str(powershell),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(helper_copy),
        "-PackageRoot",
        str(prepared.package_root),
        "-InstallRoot",
        str(install_root),
        "-ParentProcessId",
        str(parent_pid or os.getpid()),
        "-ExpectedVersion",
        prepared.release.version,
        "-ExpectedManifestSha256",
        prepared.manifest_sha256,
        "-ExpectedInstallerSha256",
        prepared.installer_sha256,
    ]
    with (prepared.work_dir / "update.log").open("ab") as log:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            # The new application is independent, not a worker of this bundle.
            env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
        )
