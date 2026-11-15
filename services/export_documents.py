"""Pure document builders used by UI export actions.

Keeping HTML generation outside Qt widgets makes the escaping and privacy
rules independently testable and avoids growing the page classes further.
"""

from __future__ import annotations

import html
import threading
from collections.abc import Callable, Iterable
from typing import Any

from utils.helpers import eur, row_get, safe_float
from utils.output_security import privacy_export_cell


def _html_text(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _on_gui_thread() -> bool:
    """Return True when the caller runs on the Qt application (GUI) thread."""

    from PySide6.QtCore import QCoreApplication, QThread

    app = QCoreApplication.instance()
    if app is None:
        return False
    return QThread.currentThread() is app.thread()


def _render_on_gui_thread(work: Callable[[], Any]) -> Any:
    """BUG-SEC-002: run ``work`` on the GUI thread when needed.

    ``QPrinter`` (and the ``QTextDocument`` that paints through it) has GUI
    thread affinity on Windows: constructing or painting it from a secondary
    QThread crashes or corrupts the print spooler state. Background workers
    (``AsyncTask``/``AsyncPdfExporter``) must therefore marshal the render
    back to the GUI thread via a ``QueuedConnection``.

    When no ``QCoreApplication`` exists (pure headless harness without any Qt
    event machinery) the work runs inline: there is no event loop to dispatch
    to and no concurrent GUI thread to violate.
    """

    from PySide6.QtCore import QCoreApplication

    if QCoreApplication.instance() is None:
        return work()
    if _on_gui_thread():
        return work()

    from PySide6.QtCore import QObject, Qt, Signal

    result: list[Any] = []
    failure: list[BaseException] = []
    finished = threading.Event()

    class _Marshaller(QObject):
        execute = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.execute.connect(
                self._run, Qt.ConnectionType.QueuedConnection
            )

        def _run(self) -> None:
            # Runs on the GUI thread. Qt swallows exceptions raised inside
            # slots, so capture and re-raise them on the waiting thread.
            try:
                result.append(work())
            except BaseException as exc:  # re-raised below on the caller
                failure.append(exc)
            finally:
                finished.set()

    app = QCoreApplication.instance()
    marshaller = _Marshaller()
    # QObject affinity follows the creating thread; the queued connection
    # must execute on the GUI thread, so move the marshaller there first.
    if marshaller.thread() is not app.thread():
        marshaller.moveToThread(app.thread())
    try:
        marshaller.execute.emit()
        if not finished.wait(timeout=60):
            raise RuntimeError(
                "A gera\u00e7\u00e3o do PDF bloqueou: "
                "a thread gr\u00e1fica n\u00e3o processou o pedido."
            )
        if failure:
            raise failure[0]
        return result[0] if result else None
    finally:
        marshaller.deleteLater()


def build_movements_pdf_html(
    rows: Iterable[Any],
    *,
    privacy: bool = False,
) -> str:
    """Return a complete, escaped HTML movement report.

    User-entered descriptions, categories and account names are always escaped
    before being handed to ``QTextDocument``. Monetary values follow the
    application's current privacy state.
    """

    movement_rows = list(rows)
    parts = [
        "<!doctype html><html lang='pt'><head><meta charset='utf-8'>",
        "<style>",
        "body { color:#0f172a; font-family:sans-serif; }",
        "table { border-collapse:collapse; width:100%; font-size:10px; }",
        "th, td { border:1px solid #dbe3ef; padding:5px; text-align:left; }",
        "th { background-color:#eef4fb; font-weight:700; }",
        "h2 { color:#0f172a; margin-bottom:12px; }",
        ".amount { font-weight:700; text-align:right; }",
        "</style></head><body>",
        f"<h2>Relat\u00f3rio de Movimentos (Total: {'•••' if privacy else len(movement_rows)})</h2>",
        "<table>",
        (
            "<tr><th>Data</th><th>Descri\u00e7\u00e3o</th><th>Categoria</th>"
            "<th>Conta</th><th>Valor</th></tr>"
        ),
    ]

    for row in movement_rows:
        amount = safe_float(row_get(row, "amount"))
        amount_color = "#334155" if privacy else "#15803d" if amount >= 0 else "#dc2626"
        parts.extend(
            [
                "<tr>",
                f"<td>{_html_text(row_get(row, 'date'))}</td>",
                f"<td>{_html_text(privacy_export_cell(row_get(row, 'description'), 'description', privacy))}</td>",
                f"<td>{_html_text(privacy_export_cell(row_get(row, 'category'), 'category', privacy, mask_categories=True))}</td>",
                f"<td>{_html_text(privacy_export_cell(row_get(row, 'account'), 'account', privacy))}</td>",
                (
                    f"<td class='amount' style='color:{amount_color};'>"
                    f"{_html_text(eur(amount, privacy))}</td>"
                ),
                "</tr>",
            ]
        )

    parts.append("</table></body></html>")
    return "".join(parts)


def render_html_to_pdf(html_content: str, output_path: str) -> str:
    """Render HTML content to a PDF file safely.

    Verifies file writability on Windows before invoking Qt printer.
    Returns the resolved output path.
    """
    from pathlib import Path
    from PySide6.QtGui import QTextDocument
    from PySide6.QtPrintSupport import QPrinter

    resolved_path = Path(output_path).resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(resolved_path)
    # SEC-EXP-03: Pre-flight check on Windows for file locking/permissions
    # (safe on any thread: plain file I/O has no Qt GUI affinity).
    with open(resolved, "ab"):
        pass

    def _render() -> str:
        # BUG-SEC-002: QPrinter/QTextDocument must be constructed and painted
        # on the GUI thread (Windows print spooler affinity). When invoked
        # from a background worker this whole closure is marshalled there.
        doc = QTextDocument()
        doc.setHtml(html_content)
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(resolved)
        doc.print_(printer)
        return resolved

    return str(_render_on_gui_thread(_render))


def export_movements_to_pdf_file(
    rows: Iterable[Any],
    output_path: str,
    *,
    privacy: bool = False,
) -> str:
    """Render movements PDF directly to disk safely."""
    report_html = build_movements_pdf_html(rows, privacy=privacy)
    return render_html_to_pdf(report_html, output_path)


class AsyncPdfExporter:
    """Encapsulates background PDF generation for execution via AsyncTask or thread workers."""

    def __init__(
        self,
        rows: Iterable[Any],
        output_path: str,
        *,
        privacy: bool = False,
    ) -> None:
        self.rows = list(rows)
        self.output_path = output_path
        self.privacy = privacy
        # BUG-SEC-002 companion: rows are materialised here (GUI thread) and
        # the render itself never touches SQLite, so the AsyncTask worker must
        # not hold the shared connection lock while the render is marshalled
        # back to the GUI thread (that would risk lock-inversion deadlock).
        self.requires_sqlite_lock = False

    def __call__(self) -> str:
        return export_movements_to_pdf_file(
            self.rows,
            self.output_path,
            privacy=self.privacy,
        )
