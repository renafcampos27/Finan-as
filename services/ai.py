"""Gemini integration and AI-specific financial context orchestration."""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Callable, Optional

from services.secret_store import SecretStore, SecretStoreError, default_secret_store
from utils.helpers import add_months, safe_float

if TYPE_CHECKING:
    from database.repo import Repo
    from services.analytics import AnalyticsService
    from services.backup import BackupService
    from services.payments import PaymentsService


GEMINI_RECOMMENDED_TEXT_MODELS = [
    # Keep the combo editable because Google can add, rename or retire models.
    "gemini-3.5-flash",
    "gemini-3.1-pro",
    "gemini-3-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-flash-latest",
    "gemini-pro-latest",
]

_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MAX_GEMINI_RESPONSE_BYTES = 4 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_EMAIL_PATTERN = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_API_KEY_PATTERN = re.compile(
    r"(?i)\b(?:AIza[0-9A-Za-z-_]{30,}|sk-[A-Za-z0-9]{30,}|ghp_[A-Za-z0-9]{30,}|bearer\s+[A-Za-z0-9._-]{20,})\b"
)
_IBAN_PATTERN = re.compile(r"\b[A-Z]{2}\s?\d{2}(?:[\s-]?[A-Z0-9]){11,30}\b", re.I)
_NISS_PATTERN = re.compile(r"(?i)\b(?:NISS[:\s]*)?([12]\d{10})\b")
_CC_BI_PATTERN = re.compile(
    r"(?i)\b(?:CC|BI|CART[ÃA]O\s+DE\s+CIDAD[ÃA]O)[:\s]*\d{7,9}(?:[\s-]?[0-9A-Z]{3,4})?\b"
)
_CARD_MASKED_PATTERN = re.compile(
    r"(?i)\b(?:(?:CART[ÃA]O|CARD|VISA|MASTERCARD)\s+)?(?:[X*]{4}[\s-]*){1,3}\d{4}\b"
)
_NIF_PATTERN = re.compile(r"(?i)\b(?:NIF[:\s]*)?(?:PT\s?)?([1235689](?:[\s-]?\d){8})\b")
_POSTAL_CODE_PT_PATTERN = re.compile(r"\b\d{4}[-\s]\d{3}\b")
_PHONE_PT_PATTERN = re.compile(
    r"(?:\+351|00351)?[\s.-]?(?:9[1236]\d|2\d{2})[\s.-]?\d{3}[\s.-]?\d{3}\b"
)
_BANK_COUNTERPARTY_PATTERN = re.compile(
    r"(?i)\b(?:"
    r"MB\s?WAY(?:\s+DE|\s+P/|\s+ENVIADO\s+A|\s+RECEBIDO\s+DE)|"
    r"TRANSF(?:\.|ER[ÊE]NCIA)?(?:\s+DE|\s+P/|\s+A\s+FAVOR\s+DE|\s+SEPA)|"
    r"TRF(?:\s+DE|\s+P/)|"
    r"SEPA\s+(?:DE|DD|DIRECT\s+DEBIT)|"
    r"DEB\s+DIR|"
    r"ENVIADO\s+POR|"
    r"RECEBIDO\s+DE|"
    r"DESTINAT[ÁA]RIO:?|"
    r"BENEFICI[ÁA]RIO:?|"
    r"ORDENANTE:?"
    r")\s+[^\W\d_]+(?:\s+(?!\b(?:e|com|nif|niss|cc|bi|iban|ordenante|destinat[áa]rio|benefici[áa]rio)\b)[^\W\d_]+)*\b"
)


_LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)(?:\d[\s-]?){8,16}(?!\d)")


def _is_useful_gemini_text_model(
    model_name: str,
    supported_actions: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Return whether a Gemini model supports ordinary text generation."""
    name = str(model_name or "").strip()
    low = name.lower().split("/")[-1]
    if not low.startswith("gemini"):
        return False
    blocked = (
        "embedding",
        "imagen",
        "veo",
        "lyria",
        "tts",
        "live",
        "nano-banana",
        "robotics",
        "deep-research",
        "antigravity",
        "computer-use",
        "image",
        "audio",
    )
    if any(token in low for token in blocked):
        return False
    if supported_actions is not None:
        actions = {str(action) for action in supported_actions}
        if actions and "generateContent" not in actions:
            return False
    return True


class GeminiCancelledError(RuntimeError):
    """Raised when the user explicitly cancels a Gemini request."""


class _RetryableGeminiError(RuntimeError):
    def __init__(self, status: int, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.status = int(status)
        self.retry_after = max(0.0, float(retry_after or 0))


class PreparedGeminiCall:
    """Callable network operation that can actively close its HTTP connection."""

    requires_sqlite_lock: bool = False

    def __init__(self, client: "GeminiClient", callback: Callable[[], Any]):
        self.client = client
        self.callback = callback
        self.requires_sqlite_lock = False

    def __call__(self) -> Any:
        return self.callback()

    def cancel(self) -> None:
        self.client.cancel()


class GeminiClient:
    """Cancellable Gemini REST client with deadline and transient retries."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        api_host: str = "generativelanguage.googleapis.com",
        connection_factory: Optional[
            Callable[[str, float], http.client.HTTPConnection]
        ] = None,
        request_timeout: float = 60.0,
        list_timeout: float = 45.0,
        max_attempts: int = 3,
        retry_base_delay: float = 0.75,
        max_response_bytes: int = MAX_GEMINI_RESPONSE_BYTES,
    ):
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "gemini-2.5-flash").strip()
        self.api_host = str(api_host or "generativelanguage.googleapis.com").strip()
        self.request_timeout = max(1.0, float(request_timeout))
        self.list_timeout = max(1.0, float(list_timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_delay = max(0.0, float(retry_base_delay))
        requested_limit = int(max_response_bytes or MAX_GEMINI_RESPONSE_BYTES)
        self.max_response_bytes = max(
            1,
            min(MAX_GEMINI_RESPONSE_BYTES, requested_limit),
        )
        self._connection_factory = connection_factory or (
            lambda host, timeout: http.client.HTTPSConnection(host, timeout=timeout)
        )
        self._cancel_event = threading.Event()
        self._connection_lock = threading.Lock()
        self._active_connection: Optional[http.client.HTTPConnection] = None

    def cancel(self) -> None:
        """Cancel the active operation and close its socket when already connected."""
        self._cancel_event.set()
        with self._connection_lock:
            connection = self._active_connection
        if connection is not None:
            try:
                connection.close()
            except OSError:
                # Cancellation remains successful even if the socket closed first.
                return

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise GeminiCancelledError("Pedido Gemini cancelado.")

    def _set_active_connection(self, connection: http.client.HTTPConnection) -> None:
        with self._connection_lock:
            self._active_connection = connection
        self._check_cancelled()

    def _clear_active_connection(self, connection: http.client.HTTPConnection) -> None:
        with self._connection_lock:
            if self._active_connection is connection:
                self._active_connection = None

    @staticmethod
    def _error_message(body: bytes) -> str:
        if not body:
            return "sem detalhes"
        try:
            data = json.loads(body.decode("utf-8"))
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:320]
        except (
            AttributeError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            pass
        return body.decode("utf-8", errors="replace").strip()[:320] or "sem detalhes"

    @staticmethod
    def _retry_after_seconds(value: Optional[str]) -> float:
        try:
            return min(120.0, max(0.0, float(value or 0)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _retry_delay_from_body(body: bytes) -> float:
        """Extract Google `RetryInfo.retryDelay` (BUG-AI-01) from a 429 body."""
        if not body:
            return 0.0
        try:
            data = json.loads(body.decode("utf-8"))
        except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return 0.0
        if not isinstance(data, dict):
            return 0.0
        error = data.get("error")
        if not isinstance(error, dict):
            return 0.0
        details = error.get("details")
        if not isinstance(details, list):
            return 0.0
        for detail in details:
            if not isinstance(detail, dict):
                continue
            if str(detail.get("@type", "")).endswith("RetryInfo"):
                raw = str(detail.get("retryDelay", "") or "").strip()
                match = re.fullmatch(r"(\d+(?:\.\d+)?)s", raw)
                if match:
                    try:
                        return min(120.0, max(0.0, float(match.group(1))))
                    except (TypeError, ValueError):
                        return 0.0
        return 0.0

    def _read_response(
        self,
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        deadline: float,
    ) -> bytes:
        content_length = response.getheader("Content-Length")
        if content_length:
            try:
                announced_size = int(str(content_length).strip())
            except (TypeError, ValueError):
                announced_size = -1
            if announced_size > self.max_response_bytes:
                raise RuntimeError(
                    "A resposta Gemini excede o limite seguro de "
                    f"{self.max_response_bytes / (1024 * 1024):.1f} MB."
                )

        sock = getattr(connection, "sock", None)
        if sock is not None:
            try:
                sock.settimeout(min(2.0, max(0.25, deadline - time.monotonic())))
            except (OSError, ValueError):
                pass

        chunks: list[bytes] = []
        received = 0
        while True:
            self._check_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("global deadline exceeded")
            try:
                remaining_capacity = self.max_response_bytes - received
                chunk = response.read(
                    min(_RESPONSE_CHUNK_BYTES, remaining_capacity + 1)
                )
            except socket.timeout:
                if time.monotonic() >= deadline:
                    raise TimeoutError("global deadline exceeded") from None
                continue

            if not chunk:
                break
            if len(chunk) > remaining_capacity:
                raise RuntimeError(
                    "A resposta Gemini excede o limite seguro de "
                    f"{self.max_response_bytes / (1024 * 1024):.1f} MB."
                )
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    def _wait_before_retry(
        self,
        *,
        attempt: int,
        deadline: float,
        retry_after: float = 0.0,
    ) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        delay = retry_after or self.retry_base_delay * (2 ** max(0, attempt - 1))
        delay = min(delay, max(0.0, remaining))
        if delay > 0 and self._cancel_event.wait(delay):
            raise GeminiCancelledError("Pedido Gemini cancelado.")
        self._check_cancelled()
        return time.monotonic() < deadline

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[dict[str, Any]],
        total_timeout: float,
    ) -> dict[str, Any]:
        self._check_cancelled()
        deadline = time.monotonic() + max(1.0, float(total_timeout))
        encoded = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/json",
            "x-goog-api-key": self.api_key,
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"

        last_error: Optional[BaseException] = None
        for attempt in range(1, self.max_attempts + 1):
            self._check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection = self._connection_factory(
                self.api_host,
                min(10.0, max(0.25, remaining)),
            )
            try:
                self._set_active_connection(connection)
                connection.request(method, path, body=encoded, headers=headers)
                response = connection.getresponse()
                body = self._read_response(response, connection, deadline)
                status = int(response.status or 0)
                retry_after = self._retry_after_seconds(
                    response.getheader("Retry-After")
                )
                if status == 429:
                    # BUG-AI-01: Google RetryInfo carries the authoritative
                    # quota-reset delay; prefer it over the capped header value.
                    retry_after = max(
                        retry_after, self._retry_delay_from_body(body)
                    )
                if status in _RETRYABLE_HTTP_STATUSES:
                    raise _RetryableGeminiError(
                        status,
                        self._error_message(body),
                        retry_after,
                    )
                if status < 200 or status >= 300:
                    raise RuntimeError(
                        f"Gemini devolveu HTTP {status}: {self._error_message(body)}"
                    )
                if not body:
                    # BUG-AI-02: an empty 200 body is truncation too; returning
                    # {} silently would mask the failure.
                    raise _RetryableGeminiError(
                        200, "A resposta Gemini não contém JSON válido.", 0.0
                    )
                try:
                    data = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    # BUG-AI-02: a truncated HTTP 200 body must retry instead of
                    # aborting the loop on the first attempt.
                    raise _RetryableGeminiError(
                        200, "A resposta Gemini não contém JSON válido.", 0.0
                    ) from exc
                if not isinstance(data, dict):
                    raise _RetryableGeminiError(
                        200, "A resposta Gemini tem um formato inesperado.", 0.0
                    )
                return data
            except GeminiCancelledError:
                raise
            except _RetryableGeminiError as exc:
                last_error = exc
                if attempt >= self.max_attempts or not self._wait_before_retry(
                    attempt=attempt,
                    deadline=deadline,
                    retry_after=exc.retry_after,
                ):
                    break
            except (
                socket.timeout,
                TimeoutError,
                ConnectionError,
                OSError,
                http.client.HTTPException,
            ) as exc:
                if self._cancel_event.is_set():
                    raise GeminiCancelledError("Pedido Gemini cancelado.") from exc
                last_error = exc
                if attempt >= self.max_attempts or not self._wait_before_retry(
                    attempt=attempt,
                    deadline=deadline,
                ):
                    break
            finally:
                self._clear_active_connection(connection)
                try:
                    connection.close()
                except OSError:
                    pass

        self._check_cancelled()
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"O pedido Gemini excedeu o limite global de {total_timeout:.0f} segundos."
            ) from last_error
        if isinstance(last_error, _RetryableGeminiError):
            raise RuntimeError(
                f"Gemini indisponível após {self.max_attempts} tentativas "
                f"(HTTP {last_error.status}): {last_error}"
            ) from last_error
        raise RuntimeError(
            f"Falha temporária ao contactar Gemini após {self.max_attempts} tentativas: "
            f"{last_error or 'erro de rede'}"
        ) from last_error

    def list_models(self) -> list[str]:
        if not self.api_key:
            raise RuntimeError(
                "Configura a chave Gemini antes de atualizar a lista de modelos."
            )
        data = self._request_json(
            "GET",
            "/v1beta/models",
            payload=None,
            total_timeout=self.list_timeout,
        )
        names: list[str] = []
        for model in data.get("models", []):
            raw_name = str(model.get("name", "") or "").strip()
            actions = model.get("supportedGenerationMethods") or model.get(
                "supported_actions"
            )
            model_id = raw_name.split("/")[-1]
            if _is_useful_gemini_text_model(model_id, actions):
                names.append(model_id)

        merged: list[str] = []
        for name in [*GEMINI_RECOMMENDED_TEXT_MODELS, *names]:
            if name and name not in merged:
                merged.append(name)
        return merged

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        response_mime_type: str | None = None,
        temperature: float = 0.25,
    ) -> str:
        if not self.api_key:
            raise RuntimeError("A chave Gemini API não está configurada.")
        prompt = str(prompt or "").strip()
        if not prompt:
            raise RuntimeError("O pedido para a IA está vazio.")

        generation_config: dict[str, Any] = {"temperature": temperature}
        if response_mime_type:
            generation_config["responseMimeType"] = response_mime_type

        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        if system_instruction:
            payload["system_instruction"] = {
                "parts": [{"text": str(system_instruction).strip()}]
            }

        model = urllib.parse.quote(self.model, safe="")
        data = self._request_json(
            "POST",
            f"/v1beta/models/{model}:generateContent",
            payload=payload,
            total_timeout=self.request_timeout,
        )
        # BUG-AI-03: surface explicit safety blocks instead of a generic
        # "no usable text" message.
        prompt_feedback = data.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            raise RuntimeError(
                "A resposta Gemini foi bloqueada pelo filtro de segurança "
                f"(motivo: {prompt_feedback['blockReason']})."
            )
        candidates = data.get("candidates") or []
        if candidates:
            candidate = candidates[0] if isinstance(candidates[0], dict) else {}
            finish_reason = str(candidate.get("finishReason", "") or "").strip()
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join(str(part.get("text", "")) for part in parts)
            if text.strip():
                return text.strip()
            if finish_reason.upper() == "SAFETY":
                raise RuntimeError(
                    "A resposta Gemini foi bloqueada pelo filtro de segurança "
                    "(finishReason: SAFETY)."
                )
        raise RuntimeError("A resposta Gemini não contém texto utilizável.")


@dataclass(frozen=True)
class _CredentialResolution:
    """Internal credential resolution outcome ensuring single-source consistency."""

    source: str
    api_key: str
    has_api_key: bool
    key_hint: str
    use_env: bool
    env_key_present: bool


class AIService:
    """Coordinate Gemini, secure preferences and finance context creation."""

    def __init__(
        self,
        repo: "Repo",
        secret_store: Optional[SecretStore] = None,
        backup_service: Optional["BackupService"] = None,
        analytics_service: Optional["AnalyticsService"] = None,
        payments_service: Optional["PaymentsService"] = None,
    ):
        self.repo = repo
        if payments_service is None:
            from services.payments import PaymentsService

            payments_service = PaymentsService(repo)
        self.payments_service = payments_service
        if analytics_service is None:
            from services.analytics import AnalyticsService

            analytics_service = AnalyticsService(
                repo,
                payments_service=payments_service,
            )
        self.analytics_service = analytics_service
        if backup_service is None:
            from services.backup import BackupService

            backup_service = BackupService(repo)
        self.backup_service = backup_service
        self.secret_store: Optional[SecretStore] = secret_store
        self._secret_store_error = ""
        self._migration_checked = False
        self._stored_key_cache: Optional[str] = None
        self._cache_lock = threading.Lock()

        if self.secret_store is None:
            try:
                self.secret_store = default_secret_store()
            except Exception as exc:
                self._secret_store_error = str(exc)

    @staticmethod
    def sanitize_text(value: Any) -> str:
        """Redact personal identifiers while retaining useful semantics."""
        text = str(value or "").strip()
        text = _EMAIL_PATTERN.sub("[email oculto]", text)
        text = _API_KEY_PATTERN.sub("[chave de api oculta]", text)
        text = _IBAN_PATTERN.sub("[IBAN oculto]", text)
        text = _CARD_MASKED_PATTERN.sub("[cartão oculto]", text)
        text = _PHONE_PT_PATTERN.sub("[contacto oculto]", text)
        text = _NISS_PATTERN.sub("[NISS oculto]", text)
        text = _CC_BI_PATTERN.sub("[documento oculto]", text)
        text = _NIF_PATTERN.sub("[NIF oculto]", text)
        text = _POSTAL_CODE_PT_PATTERN.sub("[código postal oculto]", text)
        text = _BANK_COUNTERPARTY_PATTERN.sub("[contraparte oculta]", text)
        text = _LONG_NUMBER_PATTERN.sub("[número oculto]", text)
        # BUG-AI-04: neutralize ledger sandbox delimiters so a hostile
        # transaction description cannot escape <financial_ledger> and inject
        # instructions into the model prompt.
        text = text.replace("</financial_ledger>", "[/financial_ledger]")
        text = text.replace("<financial_ledger>", "[financial_ledger]")
        return text

    @classmethod
    def sanitize_transaction_rows(
        cls,
        rows: list[Any],
        *,
        keep_description: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the minimum transaction fields needed by an AI operation."""
        sanitized: list[dict[str, Any]] = []
        for source in rows:
            row: dict[str, Any] = {}
            for source_keys, target_key in (
                (("id",), "id"),
                (("date", "data"), "data"),
                (("amount", "valor"), "valor"),
                (("category", "categoria"), "categoria"),
                (("subcategory", "subcategoria"), "subcategoria"),
            ):
                value = None
                for key in source_keys:
                    if hasattr(source, "get"):
                        val = source.get(key)
                    else:
                        try:
                            val = source[key]
                        except (IndexError, KeyError, TypeError):
                            val = None
                    if val not in (None, ""):
                        value = val
                        break
                if value not in (None, ""):
                    if target_key in ("categoria", "subcategoria"):
                        row[target_key] = cls.sanitize_text(value)
                    else:
                        row[target_key] = value
            desc = None
            for key in ("description", "descricao"):
                if hasattr(source, "get"):
                    val = source.get(key)
                else:
                    try:
                        val = source[key]
                    except (IndexError, KeyError, TypeError):
                        val = None
                if val:
                    desc = val
                    break
            if keep_description and desc:
                row["descricao"] = cls.sanitize_text(desc)
            sanitized.append(row)
        return sanitized

    @staticmethod
    def detailed_payload_fields(rows: list[dict[str, Any]]) -> list[str]:
        labels = {
            "id": "identificador",
            "data": "data",
            "date": "data",
            "descricao": "descrição",
            "description": "descrição",
            "valor": "valor",
            "amount": "valor",
            "categoria": "categoria",
            "category": "categoria",
            "subcategoria": "subcategoria",
            "subcategory": "subcategoria",
        }
        fields = {labels[key] for row in rows for key in row if key in labels}
        order = (
            "identificador",
            "data",
            "descrição",
            "valor",
            "categoria",
            "subcategoria",
        )
        return [field for field in order if field in fields]

    def _read_stored_key(self, settings: dict[str, Any]) -> str:
        with self._cache_lock:
            if self._migration_checked and self._stored_key_cache is not None:
                return self._stored_key_cache

            legacy_key = str(settings.get("legacy_api_key", "") or "").strip()
            if self.secret_store is None:
                self._migration_checked = True
                self._stored_key_cache = legacy_key
                return legacy_key

            try:
                secure_key = self.secret_store.read().strip()
                self._secret_store_error = ""
                self._stored_key_cache = secure_key or legacy_key
            except Exception as exc:
                self._secret_store_error = str(exc)
                self._stored_key_cache = legacy_key

            self._migration_checked = True
            return self._stored_key_cache or ""

    def migrate_ai_secrets(
        self,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict[str, Any]:
        """Perform secret migration and backup scrubbing off the GUI thread."""
        settings = self.repo.ai_settings()
        legacy_key = str(settings.get("legacy_api_key", "") or "").strip()
        key_migrated = False

        if legacy_key and self.secret_store is not None:
            try:
                secure_key = self.secret_store.read().strip()
                if not secure_key:
                    self.secret_store.write(legacy_key)
                    secure_key = self.secret_store.read().strip()
                    if secure_key != legacy_key:
                        raise SecretStoreError(
                            "A chave não pôde ser confirmada no Gestor de Credenciais."
                        )
                elif secure_key != legacy_key:
                    raise SecretStoreError(
                        "A chave antiga difere da credencial Windows; confirma a chave nas Definições."
                    )
                self.repo.clear_legacy_ai_api_key()
                key_migrated = True
                with self._cache_lock:
                    self._stored_key_cache = secure_key
                    self._secret_store_error = ""
            except Exception as exc:
                self._secret_store_error = str(exc)
                return {
                    "scrubbed": 0,
                    "failed": 1,
                    "cancelled": False,
                    "error": str(exc),
                    "done": False,
                }

        if cancel_check and cancel_check():
            return {"scrubbed": 0, "failed": 0, "cancelled": True, "done": False}

        scrub_result = self.backup_service.scrub_ai_secret_from_backups(
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        if (
            not scrub_result.get("cancelled")
            and int(scrub_result.get("failed", 0)) == 0
        ):
            self.repo.mark_ai_secret_migrated()
            done = True
        else:
            done = False

        return {
            **scrub_result,
            "key_migrated": key_migrated,
            "done": done,
        }

    def _resolve_credentials(
        self, settings: Optional[dict[str, Any]] = None
    ) -> _CredentialResolution:
        if settings is None:
            settings = self.repo.ai_settings()
        use_env = bool(settings.get("use_env"))
        env_key = os.environ.get("GEMINI_API_KEY", "").strip()
        env_present = bool(env_key)

        if use_env:
            if env_key:
                return _CredentialResolution(
                    source="environment",
                    api_key=env_key,
                    has_api_key=True,
                    key_hint=f"•••• {env_key[-4:]}",
                    use_env=True,
                    env_key_present=env_present,
                )
            return _CredentialResolution(
                source="none",
                api_key="",
                has_api_key=False,
                key_hint="",
                use_env=True,
                env_key_present=env_present,
            )

        stored_key = self._read_stored_key(settings)
        if stored_key:
            return _CredentialResolution(
                source="windows",
                api_key=stored_key,
                has_api_key=True,
                key_hint=f"•••• {stored_key[-4:]}",
                use_env=False,
                env_key_present=env_present,
            )
        return _CredentialResolution(
            source="none",
            api_key="",
            has_api_key=False,
            key_hint="",
            use_env=False,
            env_key_present=env_present,
        )

    def preferences(self) -> dict[str, Any]:
        settings = self.repo.ai_settings()
        res = self._resolve_credentials(settings)
        public_settings = {
            key: value
            for key, value in settings.items()
            if key not in {"legacy_api_key"}
        }
        migration_done = bool(settings.get("secret_migration_done"))
        legacy_key_present = bool(settings.get("legacy_api_key"))
        return {
            **public_settings,
            "has_api_key": res.has_api_key,
            "key_source": res.source,
            "key_hint": res.key_hint,
            "env_key_present": res.env_key_present,
            "secure_store_available": self.secret_store is not None,
            "secure_store_error": self._secret_store_error,
            "legacy_key_pending": legacy_key_present,
            "migration_pending": (not migration_done) or legacy_key_present,
        }

    def save_preferences(
        self,
        *,
        enabled: bool,
        use_env: bool,
        api_key: str,
        model: str,
        privacy_aggregated: bool,
        anonymize: bool,
        ask_before_details: bool,
    ) -> None:
        previous_secret = ""
        credential_changed = False
        confirmed_key = ""
        if not use_env:
            if self.secret_store is None:
                raise SecretStoreError(
                    self._secret_store_error
                    or "O Gestor de Credenciais do Windows não está disponível."
                )
            try:
                requested_key = str(api_key or "").strip()
                previous_secret = self.secret_store.read().strip()
                legacy_key = str(
                    self.repo.ai_settings().get("legacy_api_key", "") or ""
                ).strip()
                candidate_key = requested_key or previous_secret or legacy_key
                confirmed_key = previous_secret
                if candidate_key and candidate_key != previous_secret:
                    self.secret_store.write(candidate_key)
                    credential_changed = True
                    confirmed_key = self.secret_store.read().strip()
                    if confirmed_key != candidate_key:
                        raise SecretStoreError(
                            "A chave não pôde ser confirmada no Gestor de Credenciais."
                        )
                if enabled and not confirmed_key:
                    raise SecretStoreError(
                        "Indica uma chave Gemini antes de ativar o assistente."
                    )
            except Exception as exc:
                if credential_changed and self.secret_store is not None:
                    try:
                        if previous_secret:
                            self.secret_store.write(previous_secret)
                        else:
                            self.secret_store.delete()
                    except Exception as restore_exc:
                        self._secret_store_error = (
                            f"{exc}; a credencial anterior também não pôde ser reposta: "
                            f"{restore_exc}"
                        )
                        raise SecretStoreError(self._secret_store_error) from exc
                self._secret_store_error = str(exc)
                raise SecretStoreError(
                    f"Não foi possível guardar a chave com segurança: {exc}"
                ) from exc
        elif enabled and not os.environ.get("GEMINI_API_KEY", "").strip():
            raise SecretStoreError(
                "A variável GEMINI_API_KEY não está definida neste computador."
            )

        try:
            self.repo.save_ai_settings(
                enabled=enabled,
                use_env=use_env,
                model=model,
                privacy_aggregated=privacy_aggregated,
                anonymize=anonymize,
                ask_before_details=ask_before_details,
                clear_legacy_api_key=True,
            )
        except Exception as exc:
            if credential_changed and self.secret_store is not None:
                try:
                    if previous_secret:
                        self.secret_store.write(previous_secret)
                    else:
                        self.secret_store.delete()
                except Exception as restore_exc:
                    self._secret_store_error = (
                        "A configuração não foi guardada e a credencial anterior "
                        f"não pôde ser reposta: {restore_exc}"
                    )
                    with self._cache_lock:
                        self._stored_key_cache = previous_secret
                        self._migration_checked = True
                    raise SecretStoreError(self._secret_store_error) from exc
            with self._cache_lock:
                self._stored_key_cache = previous_secret
                self._migration_checked = True
            raise

        if not use_env:
            with self._cache_lock:
                self._stored_key_cache = confirmed_key
                self._migration_checked = True
            self._secret_store_error = ""

    def _client(self, *, model: str = "", require_enabled: bool = True) -> GeminiClient:
        settings = self.repo.ai_settings()
        if require_enabled and not settings.get("enabled"):
            raise RuntimeError("A IA Gemini está desligada nas Definições.")
        res = self._resolve_credentials(settings)
        if not res.has_api_key or not res.api_key:
            if res.use_env:
                raise RuntimeError(
                    "A variável de ambiente GEMINI_API_KEY não está definida neste computador."
                )
            raise RuntimeError(
                "Configura a chave API Gemini nas Definições ou na variável GEMINI_API_KEY."
            )
        selected_model = str(model or settings.get("model") or "gemini-2.5-flash")
        return GeminiClient(res.api_key, selected_model)

    def prepare_generate(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        response_mime_type: Optional[str] = None,
        temperature: float = 0.25,
    ) -> PreparedGeminiCall:
        client = self._client()
        return PreparedGeminiCall(
            client,
            lambda: client.generate(
                prompt,
                system_instruction=system_instruction,
                response_mime_type=response_mime_type,
                temperature=temperature,
            ),
        )

    def prepare_list_models(self, *, model: str = "") -> PreparedGeminiCall:
        client = self._client(model=model, require_enabled=False)
        return PreparedGeminiCall(client, client.list_models)

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: Optional[str] = None,
        response_mime_type: Optional[str] = None,
        temperature: float = 0.25,
    ) -> str:
        return self.prepare_generate(
            prompt,
            system_instruction=system_instruction,
            response_mime_type=response_mime_type,
            temperature=temperature,
        )()

    def list_models(self, *, model: str = "") -> list[str]:
        return self.prepare_list_models(model=model)()

    def month_context(
        self,
        ym: str,
        *,
        include_descriptions: bool = False,
        anonymize: bool = True,
    ) -> dict[str, Any]:
        year, month = int(ym[:4]), int(ym[5:7])
        start, end, _ = self.repo.month_period(year, month)
        totals = self.repo.totals_for_payment_period(start, end)
        previous = add_months(date(year, month, 1), -1)
        previous_start, previous_end, _ = self.repo.month_period(
            previous.year, previous.month
        )
        previous_totals = self.repo.totals_for_payment_period(
            previous_start, previous_end
        )
        target = self.repo.month_target(ym)
        categories = self.repo.top_categories_payment_period(start, end, 12)
        payment_methods = self.analytics_service.payment_method_totals(start, end)

        goals = []
        for goal in self.repo.monthly_goals(ym):
            progress, current, status = self.analytics_service.monthly_goal_progress(
                goal
            )
            cat_label = goal.get("category")
            if anonymize and cat_label:
                cat_label = self.sanitize_text(cat_label)
            goal_name = goal.get("name")
            if anonymize and goal_name:
                goal_name = self.sanitize_text(goal_name)
            goals.append(
                {
                    "nome": goal_name,
                    "tipo": goal.get("goal_type"),
                    "categoria": cat_label,
                    "meta": safe_float(goal.get("target_value")),
                    "progresso_percentual": progress,
                    "valor_actual": current,
                    "estado": status,
                }
            )

        quality = self.repo.transaction_data_quality_counts(start, end)
        recurring_rows = self.payments_service.likely_recurring_expenses(10)
        recurring = []
        for row in recurring_rows:
            rec_cat = row.get("category")
            if anonymize and rec_cat:
                rec_cat = self.sanitize_text(rec_cat)
            item: dict[str, Any] = {
                "categoria": rec_cat,
                "valor": row.get("amount"),
                "ocorrencias": row.get("c"),
                "primeira_data": row.get("first_date"),
                "ultima_data": row.get("last_date"),
            }
            if include_descriptions and row.get("description"):
                item["descricao"] = (
                    self.sanitize_text(row.get("description"))
                    if anonymize
                    else str(row.get("description"))
                )
            recurring.append(item)

        context = {
            "mes": ym,
            "periodo": {"inicio": start, "fim": end},
            "totais": totals,
            "mes_anterior": previous_totals,
            "metas_do_mes": target,
            "categorias_top": [
                {
                    "categoria": self.sanitize_text(category) if anonymize and category else category,
                    "total": value,
                }
                for category, value, _colour in categories
            ],
            "formas_de_pagamento": [
                {
                    "forma": self.sanitize_text(name) if anonymize and name else name,
                    "total": value,
                }
                for name, value, _colour in payment_methods
            ],
            "metas_mensais": goals,
            "possiveis_recorrentes": recurring,
            "qualidade_dados": {
                "movimentos_sem_categoria_ou_genericos": quality[
                    "uncategorized_or_generic"
                ],
                "despesas_acima_100_sem_anexo": quality["expenses_without_attachment"],
                "datas_invalidas_na_base": quality["invalid_dates"],
                "valores_invalidos_na_base": quality["invalid_amounts"],
            },
        }
        if include_descriptions:
            sample = self.repo.transaction_context_sample(start, end, 80)
            context["movimentos_amostra"] = (
                self.sanitize_transaction_rows(sample)
                if anonymize
                else [
                    {
                        "data": row.get("date") if hasattr(row, "get") else row["date"],
                        "descricao": row.get("description") if hasattr(row, "get") else row["description"],
                        "valor": row.get("amount") if hasattr(row, "get") else row["amount"],
                        "categoria": row.get("category") if hasattr(row, "get") else row["category"],
                        "subcategoria": row.get("subcategory") if hasattr(row, "get") else row["subcategory"],
                    }
                    for row in sample
                ]
            )
        return context

    def uncategorized_context(self, limit: int = 40) -> list[dict[str, Any]]:
        return self.repo.uncategorized_transactions(limit)

    def save_history(
        self,
        *,
        month: str,
        action: str,
        question: str,
        prompt: str,
        response: str,
    ) -> int:
        return self.repo.save_ai_chat_history(
            month=month,
            action=action,
            question=question,
            prompt=prompt,
            response=response,
        )

    def history_months(self) -> list[str]:
        return self.repo.ai_chat_history_months()

    def history(
        self, month: str = "", limit: Optional[int] = None, offset: int = 0
    ) -> list[Any]:
        return self.repo.ai_chat_history(month, limit, offset)

    def history_count(self, month: str = "") -> int:
        return self.repo.ai_chat_history_count(month)

    def history_entry(self, entry_id: int) -> Optional[Any]:
        return self.repo.ai_chat_history_entry(entry_id)

    def delete_history_entry(self, entry_id: int) -> None:
        self.repo.delete_ai_chat_history_entry(entry_id)

    def clear_history_month(self, month: str) -> None:
        self.repo.clear_ai_chat_history_month(month)
