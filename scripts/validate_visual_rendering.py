#!/usr/bin/env python3
"""Render real Windows screenshots and reject missing-font/tofu validation."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import shutil
from statistics import median
import sys
import tempfile
import time
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "visual_validation",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Render one representative state instead of the complete matrix.",
    )
    parser.add_argument(
        "--stress-only",
        action="store_true",
        help=(
            "Render dense data, long labels and extreme values instead of the "
            "ordinary regression matrix."
        ),
    )
    parser.add_argument(
        "--scale-factor",
        type=float,
        choices=(1.0, 1.25, 1.5, 2.0),
        default=1.0,
        help="Run the real Windows render at 100, 125, 150 or 200 percent scaling.",
    )
    return parser.parse_args()


def _wait_until(app, predicate, timeout_ms: int = 10_000) -> bool:
    deadline = time.monotonic() + max(1, timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        from PySide6.QtTest import QTest

        QTest.qWait(25)
    app.processEvents()
    return bool(predicate())


def _colour_diversity(image) -> int:
    colours: set[int] = set()
    step_x = max(1, image.width() // 160)
    step_y = max(1, image.height() // 100)
    for y in range(0, image.height(), step_y):
        for x in range(0, image.width(), step_x):
            colours.add(int(image.pixel(x, y)))
            if len(colours) >= 256:
                return len(colours)
    return len(colours)


def _median_lightness(image) -> float:
    step_x = max(2, image.width() // 80)
    step_y = max(2, image.height() // 50)
    samples = [
        image.pixelColor(x, y).lightness()
        for y in range(0, image.height(), step_y)
        for x in range(0, image.width(), step_x)
        if image.pixelColor(x, y).alpha() > 220
    ]
    return float(median(samples)) if samples else 0.0


def _perceptual_hash(image, size: int = 16) -> str:
    """Return a compact average hash for meaningful cross-run comparisons."""

    luminance: list[int] = []
    width = max(1, image.width())
    height = max(1, image.height())
    for row in range(size):
        y = min(height - 1, round((row + 0.5) * height / size))
        for column in range(size):
            x = min(width - 1, round((column + 0.5) * width / size))
            colour = image.pixelColor(x, y)
            luminance.append(
                round(
                    0.2126 * colour.red()
                    + 0.7152 * colour.green()
                    + 0.0722 * colour.blue()
                )
            )
    average = sum(luminance) / max(1, len(luminance))
    bits = "".join("1" if value >= average else "0" for value in luminance)
    return "".join(
        f"{int(bits[index : index + 4], 2):x}" for index in range(0, len(bits), 4)
    )


def _settle_render(app, duration_ms: int = 120) -> None:
    """Let queued layouts finish and dispose widgets replaced by a refresh."""
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(max(1, int(duration_ms)), loop.quit)
    loop.exec()
    for _ in range(2):
        QCoreApplication.sendPostedEvents(
            None,
            QEvent.Type.DeferredDelete,
        )
        app.processEvents()


def _seed_stress_data(window) -> list[str]:
    """Create dense, extreme but isolated data for visual regression."""

    from utils.helpers import add_months

    colours = (
        "#0072B2",
        "#D55E00",
        "#009E73",
        "#CC79A7",
        "#E69F00",
        "#56B4E9",
        "#8B5CF6",
        "#EF4444",
        "#14B8A6",
        "#84CC16",
        "#F97316",
        "#6366F1",
    )
    category_names = [
        (
            f"Categoria extraordinariamente detalhada {index + 1:02d} · "
            f"descrição longa para validar alinhamento"
        )
        for index in range(30)
    ]
    connection = window.repo.db.conn
    connection.executemany(
        """
        INSERT OR IGNORE INTO categories(name,kind,colour,budget)
        VALUES(?, 'expense', ?, ?)
        """,
        [
            (
                name,
                colours[index % len(colours)],
                180.0 + index * 37.0,
            )
            for index, name in enumerate(category_names)
        ],
    )
    current_month = date.today().replace(day=1)
    movements = []
    for month_offset in range(15):
        month = add_months(current_month, -month_offset, 1)
        movements.append(
            (
                month.replace(day=2).isoformat(),
                f"Salário líquido de referência visual {month:%Y-%m}",
                4_800.0 + month_offset * 15.0,
                "Rendimentos",
                "",
                "Conta principal",
                "income",
                "",
            )
        )
        for index, category in enumerate(category_names):
            day = 3 + (index * 5 + month_offset) % 24
            ordinary = 12.5 + index * 8.75 + month_offset * 1.15
            movements.append(
                (
                    month.replace(day=day).isoformat(),
                    (
                        f"Movimento visual {index + 1:02d} com uma descrição "
                        "propositadamente extensa para testar cortes e alinhamentos"
                    ),
                    -ordinary,
                    category,
                    f"Subcategoria extensa {index % 7 + 1}",
                    "Conta principal",
                    "expense",
                    "",
                )
            )
    movements.extend(
        [
            (
                current_month.replace(day=6).isoformat(),
                "Entrada extrema para testar milhões sem sobreposição",
                1_234_567.89,
                "Rendimentos",
                "",
                "Conta principal",
                "income",
                "",
            ),
            (
                current_month.replace(day=7).isoformat(),
                "Saída extrema para testar milhões sem sobreposição",
                -987_654.32,
                category_names[0],
                "Valor extremo",
                "Conta principal",
                "expense",
                "",
            ),
        ]
    )
    connection.executemany(
        """
        INSERT INTO transactions(
            date,description,amount,category,subcategory,account,kind,notes
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        movements,
    )
    connection.commit()
    return category_names


def _stress_overlap_pairs(dashboard) -> list[tuple[str, str]]:
    from PySide6.QtCore import QPoint

    visible: list[tuple[str, object]] = []
    for key, _setting_key, card, _span in dashboard.dashboard_cards:
        if card.isVisibleTo(dashboard):
            visible.append((key, card))
    overlaps: list[tuple[str, str]] = []
    for left_index, (left_key, left_card) in enumerate(visible):
        left_rect = left_card.rect().translated(left_card.mapTo(dashboard, QPoint()))
        for right_key, right_card in visible[left_index + 1 :]:
            right_rect = right_card.rect().translated(
                right_card.mapTo(dashboard, QPoint())
            )
            intersection = left_rect.intersected(right_rect)
            if intersection.width() > 2 and intersection.height() > 2:
                overlaps.append((left_key, right_key))
    return overlaps


def _run_stress_validation(
    *,
    args,
    app,
    window,
    dashboard,
    output_dir: Path,
    families: list[str],
    probe_text: str,
) -> int:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QGridLayout, QLabel, QWidget

    from ui.components import (
        BalanceChart,
        BudgetForecastChart,
        CashflowTimelineChart,
        ChartDetailDialog,
        DonutChart,
        GlassCard,
    )
    from ui.core import Theme
    from utils.helpers import eur

    categories = _seed_stress_data(window)
    dashboard.change_density("detailed")
    all_index = dashboard.category_limit_combo.findData(0)
    if all_index >= 0:
        dashboard.category_limit_combo.setCurrentIndex(all_index)
    annual_button = next(
        (
            button
            for button in dashboard.range_group.buttons()
            if button.property("filterValue") == 365
        ),
        None,
    )
    if annual_button is None:
        raise RuntimeError("O filtro anual não foi encontrado no teste de stress.")
    previous_request_id = dashboard._dashboard_request_id
    annual_button.click()
    if not _wait_until(
        app,
        lambda: (
            dashboard._dashboard_request_id > previous_request_id
            and not any(worker.isRunning() for worker in dashboard._workers.values())
        ),
        timeout_ms=20_000,
    ):
        raise RuntimeError("O cenário visual denso não terminou o cálculo.")

    cases = (
        (960, 720, False, False),
        (1280, 900, True, False),
        (1600, 900, False, True),
        (1920, 1080, False, False),
    )
    captures: list[dict] = []
    detail_capture: dict[str, object] = {}
    chart_attributes = (
        "chart",
        "cashflow_pulse",
        "savings_efficiency",
        "waterfall",
        "budget_chart",
        "cashflow_chart",
        "donut",
        "treemap",
        "bubble",
        "sankey",
        "heatmap",
    )
    for case_index, (width, height, dark, privacy) in enumerate(cases):
        window.resize(width, height)
        window.repo.set_setting("liquid_dark", "1" if dark else "0")
        window.repo.set_setting("liquid_privacy", "1" if privacy else "0")
        previous_request_id = dashboard._dashboard_request_id
        window.apply_theme()
        if not _wait_until(
            app,
            lambda previous_request_id=previous_request_id: (
                dashboard._dashboard_request_id > previous_request_id
                and not any(
                    worker.isRunning() for worker in dashboard._workers.values()
                )
            ),
            timeout_ms=20_000,
        ):
            raise RuntimeError("O Dashboard denso não terminou a atualização.")
        window.goto("dashboard")
        window.sync_content_size()
        _settle_render(app, 180)
        dashboard._enforce_category_chart_separation()
        window.sync_content_size()
        _settle_render(app, 120)

        horizontal_scroll = window.content_scroll.horizontalScrollBar().maximum()
        if horizontal_scroll:
            raise RuntimeError(f"O cenário denso criou scroll horizontal em {width}px.")
        overlaps = _stress_overlap_pairs(dashboard)
        if overlaps:
            geometry = {
                key: [
                    card.mapTo(dashboard, QPoint()).x(),
                    card.mapTo(dashboard, QPoint()).y(),
                    card.width(),
                    card.height(),
                ]
                for key, _setting_key, card, _span in dashboard.dashboard_cards
                if key in {value for pair in overlaps for value in pair}
            }
            raise RuntimeError(
                f"Cartões sobrepostos no cenário denso: {overlaps} · {geometry}"
            )
        paint_errors = {
            attribute: str(getattr(getattr(dashboard, attribute), "_paint_error", ""))
            for attribute in chart_attributes
            if str(getattr(getattr(dashboard, attribute), "_paint_error", ""))
        }
        if paint_errors:
            raise RuntimeError(f"Gráficos com erro no cenário denso: {paint_errors}")
        if len(getattr(dashboard.treemap, "data", {})) < len(categories):
            raise RuntimeError("O treemap perdeu categorias no cenário denso.")
        if len(getattr(dashboard.sankey, "items", [])) < len(categories):
            raise RuntimeError("O Sankey voltou a truncar categorias no cenário denso.")

        clipped_labels = []
        checked_labels = [
            dashboard.cashflow_hint,
            dashboard.budget_hint,
            dashboard.cashflow_pulse_hint,
            dashboard.savings_efficiency_hint,
            dashboard.donut_comparison_hint,
        ]
        for label in checked_labels:
            if (
                label.isVisibleTo(dashboard)
                and label.wordWrap()
                and label.width() > 0
                and label.heightForWidth(label.width()) > label.height() + 2
            ):
                clipped_labels.append(label.objectName() or label.text()[:36])
        if clipped_labels:
            raise RuntimeError(
                f"Textos explicativos cortados no cenário denso: {clipped_labels}"
            )

        pixmap = window.grab()
        if pixmap.isNull() or _colour_diversity(pixmap.toImage()) < 32:
            raise RuntimeError("A captura de stress ficou vazia ou sem detalhe.")
        theme_name = "dark" if dark else "light"
        privacy_name = "privacy" if privacy else "values"
        target = output_dir / (
            f"stress_{args.scale_factor:g}x_{width}_{theme_name}_{privacy_name}.png"
        )
        if not pixmap.save(str(target), "PNG"):
            raise RuntimeError(f"Não foi possível guardar {target}.")
        captures.append(
            {
                "file": target.name,
                "viewport": [width, height],
                "theme": theme_name,
                "privacy": privacy,
                "scale_factor": args.scale_factor,
                "device_pixel_ratio": float(pixmap.devicePixelRatio()),
                "sampled_colours": _colour_diversity(pixmap.toImage()),
                "perceptual_hash": _perceptual_hash(pixmap.toImage()),
                "categories": len(getattr(dashboard.treemap, "data", {})),
                "sankey_items": len(getattr(dashboard.sankey, "items", [])),
                "dashboard_height": dashboard.height(),
                "horizontal_scroll": horizontal_scroll,
                "overlaps": overlaps,
                "clipped_labels": clipped_labels,
            }
        )

        if case_index == 0:
            item = (
                dashboard.donut.data[0]
                if getattr(dashboard.donut, "data", None)
                else (categories[0], 0.0)
            )
            payload = dashboard.donut.detail_payload(item)
            dialog = ChartDetailDialog(
                payload,
                window.theme,
                action_label="Abrir movimentos",
                parent=window,
            )
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            dialog.show()
            _settle_render(app, 120)
            dialog_pixmap = dialog.grab()
            if (
                dialog_pixmap.isNull()
                or _colour_diversity(dialog_pixmap.toImage()) < 20
            ):
                raise RuntimeError("O painel de detalhe não ficou legível no stress.")
            detail_text = " ".join(
                label.text() for label in dialog.findChildren(QLabel)
            )
            if "Movimentos" not in detail_text and "movimentos" not in detail_text:
                raise RuntimeError("O painel de detalhe perdeu a explicação.")
            if privacy and eur(0, True) not in detail_text:
                raise RuntimeError("O detalhe de stress expôs valores em privacidade.")
            detail_target = output_dir / (
                f"chart_detail_{args.scale_factor:g}x_light_values.png"
            )
            if not dialog_pixmap.save(str(detail_target), "PNG"):
                raise RuntimeError(f"Não foi possível guardar {detail_target}.")
            detail_capture = {
                "file": detail_target.name,
                "size": [dialog.width(), dialog.height()],
                "scale_factor": args.scale_factor,
                "rows": len(list(payload.get("rows") or [])),
                "sampled_colours": _colour_diversity(dialog_pixmap.toImage()),
            }
            dialog.close()

    empty_theme = Theme(dark=False, privacy=False)
    empty_host = QWidget()
    empty_host.setObjectName("VisualEmptyStateHost")
    empty_host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    empty_layout = QGridLayout(empty_host)
    empty_layout.setContentsMargins(12, 12, 12, 12)
    empty_layout.setSpacing(12)
    empty_charts = (
        ("Saldo sem histórico", BalanceChart(lambda: empty_theme)),
        ("Categorias sem movimentos", DonutChart(lambda: empty_theme)),
        ("Tesouraria sem previsão", CashflowTimelineChart(lambda: empty_theme)),
        ("Orçamento sem dados", BudgetForecastChart(lambda: empty_theme)),
    )
    empty_charts[0][1].set_points([])
    empty_charts[1][1].set_data([])
    empty_charts[2][1].set_data({}, 60)
    empty_charts[3][1].set_rows([], "Mês sem dados")
    for index, (title, chart) in enumerate(empty_charts):
        card = GlassCard(title, strong=index % 2 == 0)
        card.v.addWidget(chart)
        empty_layout.addWidget(card, index // 2, index % 2)
    empty_host.resize(960, 760)
    empty_host.show()
    _settle_render(app, 160)
    empty_errors = {
        title: str(getattr(chart, "_paint_error", ""))
        for title, chart in empty_charts
        if str(getattr(chart, "_paint_error", ""))
    }
    if empty_errors:
        raise RuntimeError(f"Estados vazios com erro: {empty_errors}")
    empty_pixmap = empty_host.grab()
    empty_diversity = _colour_diversity(empty_pixmap.toImage())
    if empty_pixmap.isNull() or empty_diversity < 18:
        raise RuntimeError("Os estados vazios não ficaram visualmente legíveis.")
    empty_target = output_dir / f"empty_states_{args.scale_factor:g}x.png"
    if not empty_pixmap.save(str(empty_target), "PNG"):
        raise RuntimeError(f"Não foi possível guardar {empty_target}.")
    empty_capture = {
        "file": empty_target.name,
        "scale_factor": args.scale_factor,
        "charts": len(empty_charts),
        "sampled_colours": empty_diversity,
    }
    empty_host.close()

    manifest = {
        "mode": "stress",
        "platform": app.platformName(),
        "scale_factor": args.scale_factor,
        "font_family": window.font().family(),
        "font_families_available": len(families),
        "glyph_probe": probe_text,
        "seeded_categories": len(categories),
        "captures": captures,
        "chart_detail": detail_capture,
        "empty_states": empty_capture,
    }
    (output_dir / "visual_stress.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=True))
    return 0


def main() -> int:
    args = _arguments()
    if os.name != "nt":
        print("A validação tipográfica real requer o motor gráfico do Windows.")
        return 2

    # The offscreen Qt plugin exposes no system fonts on Windows and turns all
    # text into identical square placeholders. Force the real raster platform.
    os.environ["QT_QPA_PLATFORM"] = "windows"
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ["QT_SCALE_FACTOR"] = f"{args.scale_factor:g}"
    state_root = Path(tempfile.mkdtemp(prefix="financas-visual-windows-"))
    os.environ["FINANCAS_PESSOAIS_STATE_DIR"] = str(state_root)
    # This script always owns a disposable profile.  Populate it explicitly so
    # direct runs validate real chart/card content instead of depending on the
    # pytest process to provide the demo-data environment flag.
    os.environ["FINANCAS_PESSOAIS_DEMO_DATA"] = "1"

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    window = None
    app = None
    try:
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QColor, QFontDatabase, QFontMetrics, QPalette
        from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

        from main import LiquidFinanceApp
        from ui.core.theme import Theme, apply_tooltip_theme
        from ui.startup import StartupProgressWindow
        from utils.helpers import add_months, eur
        from utils.startup_progress import StartupProgress

        app = QApplication.instance() or QApplication([])
        if app.platformName().casefold() != "windows":
            raise RuntimeError(
                f"Motor visual inesperado: {app.platformName()!r}; esperado 'windows'."
            )

        families = QFontDatabase.families()
        if len(families) < 10:
            raise RuntimeError(
                "O teste visual não encontrou as fontes instaladas do Windows."
            )

        startup = StartupProgressWindow()
        startup.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        startup.show()
        startup.update_progress(
            StartupProgress(
                64,
                "Guardar documentos protegidos",
                "fatura_eletricidade_julho_2026.pdf",
                current=38,
                total=57,
            )
        )
        _settle_render(app, 60)
        startup_image = startup.grab().toImage()
        startup_target = output_dir / "startup_progress_windows.png"
        if not startup_image.save(str(startup_target)):
            raise RuntimeError("Não foi possível guardar a janela de arranque.")
        startup_capture = {
            "file": startup_target.name,
            "size": [startup.width(), startup.height()],
            "percent": startup.progress_bar.value(),
            "stage": startup.stage_label.text(),
            "detail": startup.detail_label.text(),
            "sampled_colours": _colour_diversity(startup_image),
        }
        startup.dismiss()
        app.processEvents()

        window = LiquidFinanceApp()
        window.repo.set_setting("backup_on_close", "0")
        window.backup_service.run_backup_on_close = lambda silent=True: None
        # Transient congratulations/reminders intentionally overlap the page;
        # suppress them so the screenshots validate the stable layout itself.
        window.show_dashboard_notice = lambda *_args, **_kwargs: None
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.resize(960, 640)
        window.show()
        dashboard = window.pages["dashboard"]
        dashboard.start_initial_load()
        if not _wait_until(
            app,
            lambda: (
                dashboard._dashboard_request_id >= 1
                and not any(
                    worker.isRunning() for worker in dashboard._workers.values()
                )
            ),
        ):
            raise RuntimeError("O Dashboard não terminou a preparação visual.")
        if getattr(dashboard, "startup_status_label", None) is not None:
            raise RuntimeError(
                "A mensagem temporária do arranque permaneceu sobre o Dashboard."
            )

        probe_text = "Finanças pessoais 0123456789 € % áàâãéêíóôõúç"
        metrics = QFontMetrics(window.font())
        missing = sorted(
            {
                character
                for character in probe_text
                if not character.isspace() and not metrics.inFontUcs4(ord(character))
            }
        )
        if missing:
            raise RuntimeError(
                "A fonte ativa não consegue desenhar: " + " ".join(missing)
            )
        if not any("emoji" in family.casefold() for family in families):
            raise RuntimeError("Não foi encontrada uma fonte de emoji do Windows.")

        message_box_captures = []
        if not args.stress_only:
            for dark, name in ((False, "light"), (True, "dark")):
                popup_theme = Theme(dark=dark)
                apply_tooltip_theme(popup_theme)
                # Reproduce the persistent production failure: after the app
                # theme is selected, a native/system palette takes over and
                # the global QSS is no longer available. The per-popup event
                # guard must still repair the real window before first paint.
                hostile_palette = QPalette(app.palette())
                hostile_surface = QColor("#050505" if not dark else "#FFFFFF")
                hostile_text = QColor("#142035" if not dark else "#F8FAFC")
                for group in (
                    QPalette.ColorGroup.Active,
                    QPalette.ColorGroup.Inactive,
                    QPalette.ColorGroup.Disabled,
                ):
                    for role in (
                        QPalette.ColorRole.Window,
                        QPalette.ColorRole.Base,
                        QPalette.ColorRole.Button,
                    ):
                        hostile_palette.setColor(group, role, hostile_surface)
                    for role in (
                        QPalette.ColorRole.WindowText,
                        QPalette.ColorRole.Text,
                        QPalette.ColorRole.ButtonText,
                    ):
                        hostile_palette.setColor(group, role, hostile_text)
                app.setStyleSheet("")
                app.setPalette(hostile_palette)
                message = QMessageBox()
                message.setWindowTitle("Apagar todos os movimentos")
                message.setIcon(QMessageBox.Warning)
                message.setText(
                    "Serão apagados todos os movimentos e os dados associados."
                )
                message.setInformativeText(
                    "Primeiro será criado e validado um backup completo. Continuar?"
                )
                message.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
                message.setDefaultButton(QMessageBox.No)
                message.setAttribute(
                    Qt.WidgetAttribute.WA_DontShowOnScreen,
                    True,
                )
                message.show()
                _settle_render(app, 50)
                popup_image = message.grab().toImage()
                popup_lightness = _median_lightness(popup_image)
                expected = popup_lightness < 90 if dark else popup_lightness > 205
                palette_surface = message.palette().color(QPalette.Window).name()
                expected_surface = QColor(popup_theme.message_surface).name()
                if (
                    popup_image.isNull()
                    or not expected
                    or palette_surface != expected_surface
                    or popup_theme.message_surface not in message.styleSheet()
                    or not message.testOption(QMessageBox.Option.DontUseNativeDialog)
                    or "\n" not in message.text()
                ):
                    raise RuntimeError(
                        "A caixa de mensagem não respeitou o tema "
                        f"{'escuro' if dark else 'claro'}."
                    )
                popup_target = output_dir / f"message_box_{name}_windows.png"
                if not popup_image.save(str(popup_target)):
                    raise RuntimeError(f"Não foi possível guardar {popup_target}.")
                message_box_captures.append(
                    {
                        "file": popup_target.name,
                        "theme": name,
                        "size": [popup_image.width(), popup_image.height()],
                        "median_lightness": popup_lightness,
                        "sampled_colours": _colour_diversity(popup_image),
                        "palette_surface": palette_surface,
                        "local_stylesheet": bool(message.styleSheet()),
                        "native_dialog_disabled": message.testOption(
                            QMessageBox.Option.DontUseNativeDialog
                        ),
                        "message_wrapped": "\n" in message.text(),
                    }
                )
                message.close()
            apply_tooltip_theme(window.theme)

        if args.stress_only:
            return _run_stress_validation(
                args=args,
                app=app,
                window=window,
                dashboard=dashboard,
                output_dir=output_dir,
                families=families,
                probe_text=probe_text,
            )

        states = (
            [(960, 640, False, False)]
            if args.quick
            else [
                (width, height, dark, privacy)
                for width, height in (
                    (960, 900),
                    (1280, 900),
                    (1600, 900),
                    (1920, 1080),
                )
                for dark, privacy in (
                    (False, False),
                    (False, True),
                    (True, False),
                    (True, True),
                )
            ]
        )
        captures = []
        feature_charts = []
        feature_cards = []
        period_filter_cards = []
        transaction_editor = {}
        for width, height, dark, privacy in states:
            window.resize(width, height)
            app.processEvents()
            window.repo.set_setting("liquid_dark", "1" if dark else "0")
            window.repo.set_setting("liquid_privacy", "1" if privacy else "0")
            previous_request_id = dashboard._dashboard_request_id
            window.apply_theme()
            if not _wait_until(
                app,
                lambda previous_request_id=previous_request_id: (
                    dashboard._dashboard_request_id > previous_request_id
                    and not any(
                        worker.isRunning() for worker in dashboard._workers.values()
                    )
                ),
            ):
                raise RuntimeError(
                    "O Dashboard não terminou a atualização do estado visual."
                )
            if window.theme.dark is not dark or window.theme.privacy is not privacy:
                raise RuntimeError(
                    "O tema capturado não corresponde ao estado solicitado."
                )
            window.goto("dashboard")
            app.processEvents()
            window.sync_content_size()
            _settle_render(app)

            metric_values: list[str] = []
            for metric_card in dashboard.metric_widgets:
                value_label = getattr(metric_card, "value_label", None)
                if value_label is None:
                    continue
                value = (
                    value_label.fullText()
                    if hasattr(value_label, "fullText")
                    else value_label.text()
                )
                metric_values.append(str(value))
            privacy_mask = eur(0, True)
            masked_values = sum(privacy_mask in value for value in metric_values)
            if privacy and masked_values < 3:
                raise RuntimeError(
                    "A captura de privacidade ainda expõe valores do Dashboard."
                )
            if not privacy and masked_values:
                raise RuntimeError(
                    "A captura normal contém valores indevidamente ocultos."
                )
            decision_priorities = list(
                getattr(dashboard.insights, "_data", {}).get("priorities", [])
            )
            decision_money_values = sum(
                1
                for priority in decision_priorities
                for metric in priority.get("metrics", [])
                if metric.get("kind", "money") == "money"
            ) + sum(
                1
                for priority in decision_priorities
                if float(priority.get("impact_amount", 0) or 0) > 0
            )
            decision_text = " ".join(
                label.text() for label in dashboard.insights.findChildren(QLabel)
            )
            decision_masked = privacy_mask in decision_text
            if privacy and decision_money_values and not decision_masked:
                raise RuntimeError(
                    "As prioridades financeiras expõem valores no modo de privacidade."
                )
            if not privacy and decision_masked:
                raise RuntimeError(
                    "As prioridades financeiras ocultaram valores no modo normal."
                )

            dashboard.set_dashboard_filters_expanded(True)
            _settle_render(app, 35)
            filter_groups = (
                dashboard.period_filter_group,
                dashboard.resolution_filter_group,
                dashboard.comparison_filter_group,
                dashboard.density_filter_group,
                dashboard.category_filter_group,
            )
            filter_row_counts = Counter(
                group.geometry().top() for group in filter_groups
            )
            filter_rows = len(filter_row_counts)
            expected_filter_rows = 1 if width >= 1600 else 2
            if filter_rows != expected_filter_rows:
                raise RuntimeError(
                    f"Filtros ocupam {filter_rows} linhas em {width}px; "
                    f"esperado {expected_filter_rows}."
                )
            if filter_rows > 1 and min(filter_row_counts.values()) < 2:
                raise RuntimeError(
                    "Um grupo de filtros voltou a ficar isolado numa linha."
                )
            expanded_filter_card_height = dashboard.filters_card.height()
            if expanded_filter_card_height > 320:
                raise RuntimeError(
                    "O cartão de filtros voltou a ocupar altura excessiva: "
                    f"{expanded_filter_card_height} px."
                )

            if any(
                group.geometry().right() > dashboard.filters_wrap.width() + 1
                or group.geometry().bottom() > dashboard.filters_wrap.height() + 1
                for group in filter_groups
            ):
                raise RuntimeError("Um grupo de filtros ficou cortado.")
            dashboard.set_dashboard_filters_expanded(False)
            _settle_render(app, 35)
            if dashboard.filters_wrap.isVisibleTo(dashboard):
                raise RuntimeError(
                    "Os filtros avançados não voltaram ao estado recolhido."
                )
            collapsed_filter_card_height = dashboard.filters_card.height()
            if collapsed_filter_card_height >= expanded_filter_card_height:
                raise RuntimeError("Recolher os filtros não libertou espaço vertical.")

            expected_compact_chrome = width < window.COMPACT_CHROME_BREAKPOINT
            if window._topbar_compact is not expected_compact_chrome:
                raise RuntimeError("A barra superior não respeitou a largura.")
            if expected_compact_chrome and window.sidebar.width() != 142:
                raise RuntimeError(
                    "A navegação contextual não usou a largura prevista."
                )
            if expected_compact_chrome and not window._sidebar_contextual:
                raise RuntimeError("A navegação estreita perdeu o contexto textual.")
            if not expected_compact_chrome and window.sidebar.width() < 184:
                raise RuntimeError("A navegação larga não recuperou os rótulos.")
            visible_nav_labels = [
                str(button.text() or "").strip()
                for button in window.nav_buttons.values()
                if button.isVisibleTo(window) and str(button.text() or "").strip()
            ]
            overflow_nav_labels = [
                str(button.text() or "")
                for button in window.nav_buttons.values()
                if button.isVisibleTo(window)
                and button.fontMetrics().horizontalAdvance(str(button.text() or ""))
                > max(1, button.width() - 18)
            ]
            nav_label_overflow = len(overflow_nav_labels)
            if expected_compact_chrome and not any(
                len(label.split(maxsplit=1)) > 1 for label in visible_nav_labels
            ):
                raise RuntimeError("A navegação contextual não mostrou nomes de áreas.")
            if nav_label_overflow:
                raise RuntimeError(
                    "Existem nomes cortados na navegação: "
                    + ", ".join(overflow_nav_labels)
                )

            status_labels = [
                getattr(metric_card, "status_label", None)
                for metric_card in dashboard.metric_widgets
            ]
            status_labels = [label for label in status_labels if label is not None]
            status_overflow = sum(
                label.isVisible()
                and label.heightForWidth(max(1, label.width())) > label.height() + 1
                for label in status_labels
            )
            if status_overflow:
                raise RuntimeError(
                    "Existe texto de estado cortado nos cartões de indicadores."
                )
            wrapped_label_overflow = []
            for label in dashboard.findChildren(QLabel):
                if (
                    not label.isVisibleTo(dashboard)
                    or not label.wordWrap()
                    or not label.text().strip()
                    or label.width() <= 0
                ):
                    continue
                required = label.heightForWidth(max(1, label.width()))
                if required > label.height() + 2:
                    wrapped_label_overflow.append(
                        label.objectName() or label.text()[:48]
                    )
            if wrapped_label_overflow:
                raise RuntimeError(
                    "Existem textos responsivos cortados no Dashboard: "
                    + ", ".join(wrapped_label_overflow)
                )

            dashboard_contexts = {
                key: str(
                    getattr(card, "context_badge", None).text()
                    if getattr(card, "context_badge", None) is not None
                    else ""
                )
                for key, _setting_key, card, _span in dashboard.dashboard_cards
            }
            if any(not value for value in dashboard_contexts.values()):
                raise RuntimeError(
                    "Existe um cartão do Dashboard sem contexto temporal visível."
                )
            metric_contexts = [
                str(
                    metric_card.context_badge.text()
                    if getattr(metric_card, "context_badge", None) is not None
                    else ""
                )
                for metric_card in dashboard.metric_widgets
                if getattr(metric_card, "value_label", None) is not None
            ]
            if any(not value for value in metric_contexts):
                raise RuntimeError(
                    "Existe um indicador principal sem contexto temporal."
                )
            chart_data_context = dashboard.chart_data_badge.text()
            budget_data_context = dashboard.budget_data_badge.text()
            if "Dados até" not in chart_data_context:
                raise RuntimeError("A previsão de saldo não mostra a data dos dados.")
            if "Dados até" not in budget_data_context:
                raise RuntimeError("A previsão orçamental não mostra a data dos dados.")

            dashboard_scroll = window.content_scroll.verticalScrollBar()
            dashboard_scroll.setValue(dashboard_scroll.maximum())
            _settle_render(app, 45)
            sticky_summary_visible = window.dashboard_sticky_host.isVisible()
            sticky_summary_text = window.dashboard_sticky_summary.text()
            if (
                not sticky_summary_visible
                or sticky_summary_text != dashboard.filters_summary.text()
            ):
                raise RuntimeError(
                    "O resumo compacto dos filtros não ficou visível durante o scroll."
                )
            dashboard_scroll.setValue(0)
            _settle_render(app, 45)
            if window.dashboard_sticky_host.isVisible():
                raise RuntimeError(
                    "O resumo sticky não desapareceu junto dos controlos completos."
                )

            pixmap = window.grab()
            if pixmap.isNull():
                raise RuntimeError(f"Captura vazia em {width} px.")
            ratio = max(1.0, float(pixmap.devicePixelRatio()))
            logical_size = (
                round(pixmap.width() / ratio),
                round(pixmap.height() / ratio),
            )
            if logical_size != (width, height):
                raise RuntimeError(
                    f"Captura com tamanho inesperado: {logical_size}, "
                    f"esperado {(width, height)}."
                )
            diversity = _colour_diversity(pixmap.toImage())
            if diversity < 32:
                raise RuntimeError(
                    f"Captura visual sem detalhe suficiente ({diversity} cores)."
                )
            theme_name = "dark" if dark else "light"
            privacy_name = "privacy" if privacy else "values"
            target = output_dir / (f"dashboard_{width}_{theme_name}_{privacy_name}.png")
            if not pixmap.save(str(target), "PNG"):
                raise RuntimeError(f"Não foi possível guardar {target}.")
            captures.append(
                {
                    "file": target.name,
                    "viewport": [width, height],
                    "theme": theme_name,
                    "privacy": privacy,
                    "device_pixel_ratio": ratio,
                    "sampled_colours": diversity,
                    "perceptual_hash": _perceptual_hash(pixmap.toImage()),
                    "metric_values": len(metric_values),
                    "masked_metric_values": masked_values,
                    "decision_priorities": len(decision_priorities),
                    "decision_money_values": decision_money_values,
                    "decision_values_masked": decision_masked,
                    "sidebar_width": window.sidebar.width(),
                    "sidebar_contextual": window._sidebar_contextual,
                    "visible_nav_labels": visible_nav_labels,
                    "nav_label_overflow": nav_label_overflow,
                    "topbar_compact": window._topbar_compact,
                    "filter_card_height": collapsed_filter_card_height,
                    "expanded_filter_card_height": expanded_filter_card_height,
                    "filter_controls_visible": dashboard.filters_wrap.isVisibleTo(
                        dashboard
                    ),
                    "filter_group_rows": filter_rows,
                    "filter_row_sizes": sorted(filter_row_counts.values()),
                    "filter_layout_mode": dashboard._filter_layout_mode,
                    "filter_summary": dashboard.filters_summary.text(),
                    "sticky_summary_visible_on_scroll": sticky_summary_visible,
                    "sticky_summary_text": sticky_summary_text,
                    "dashboard_card_contexts": dashboard_contexts,
                    "metric_contexts": metric_contexts,
                    "chart_data_context": chart_data_context,
                    "budget_data_context": budget_data_context,
                    "category_comparison_visible": (
                        dashboard.donut_comparison_hint.isVisibleTo(dashboard)
                    ),
                    "category_comparison_text": (
                        dashboard.donut_comparison_hint.text()
                    ),
                    "wrapped_metric_statuses": sum(
                        label.wordWrap() for label in status_labels
                    ),
                    "status_overflow": status_overflow,
                    "wrapped_label_overflow": len(wrapped_label_overflow),
                }
            )
            if width == 1920 and not dark and not privacy:
                from ui.dialogs import TransactionDialog

                movement = window.repo.q(
                    "SELECT * FROM transactions ORDER BY id DESC LIMIT 1"
                )[0]
                editor = TransactionDialog(window.repo, window, row=movement)
                editor.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                editor.show()
                _settle_render(app, 100)
                editor_scroll = editor._dialog_scroll.horizontalScrollBar().maximum()
                if editor_scroll:
                    raise RuntimeError(
                        "O editor de movimentos criou scroll horizontal."
                    )
                if window.graphicsEffect() is not None:
                    raise RuntimeError(
                        "O editor de movimentos voltou a desfocar a aplicação."
                    )
                editor_pixmap = editor.grab()
                editor_diversity = _colour_diversity(editor_pixmap.toImage())
                if editor_pixmap.isNull() or editor_diversity < 28:
                    raise RuntimeError(
                        "O editor de movimentos não ficou visualmente legível."
                    )
                editor_target = output_dir / "transaction_editor_1920_light.png"
                if not editor_pixmap.save(str(editor_target), "PNG"):
                    raise RuntimeError(f"Não foi possível guardar {editor_target}.")
                transaction_editor = {
                    "file": editor_target.name,
                    "viewport": [1920, 1080],
                    "size": [editor.width(), editor.height()],
                    "horizontal_scroll": editor_scroll,
                    "parent_blurred": window.graphicsEffect() is not None,
                    "field_labels": len(editor._field_labels),
                    "sampled_colours": editor_diversity,
                }
                editor.close()
            if width == 1600 and not dark and not privacy:
                for key, card, chart in (
                    (
                        "cashflow_pulse",
                        dashboard.cashflow_pulse_card,
                        dashboard.cashflow_pulse,
                    ),
                    (
                        "savings_efficiency",
                        dashboard.savings_efficiency_card,
                        dashboard.savings_efficiency,
                    ),
                ):
                    rows = list(getattr(chart, "rows", []) or [])
                    comparison_rows = list(getattr(chart, "comparison_rows", []) or [])
                    if not rows:
                        raise RuntimeError(f"O novo gráfico {key} ficou sem dados.")
                    if not comparison_rows:
                        raise RuntimeError(
                            f"O novo gráfico {key} não mostra o período de referência."
                        )
                    card_pixmap = card.grab()
                    if card_pixmap.isNull():
                        raise RuntimeError(
                            f"A captura do novo gráfico {key} está vazia."
                        )
                    chart_diversity = _colour_diversity(card_pixmap.toImage())
                    if chart_diversity < 24:
                        raise RuntimeError(
                            f"O novo gráfico {key} não tem detalhe visual suficiente."
                        )
                    if str(getattr(chart, "_paint_error", "") or ""):
                        raise RuntimeError(
                            f"O novo gráfico {key} falhou durante o desenho."
                        )
                    chart_target = output_dir / f"{key}_1600_light_values.png"
                    if not card_pixmap.save(str(chart_target), "PNG"):
                        raise RuntimeError(f"Não foi possível guardar {chart_target}.")
                    feature_charts.append(
                        {
                            "key": key,
                            "file": chart_target.name,
                            "rows": len(rows),
                            "comparison_rows": len(comparison_rows),
                            "size": [card.width(), card.height()],
                            "sampled_colours": chart_diversity,
                        }
                    )
                decision_card = dashboard.insights
                if not decision_card.priority_rows:
                    raise RuntimeError(
                        "O cartão de prioridades financeiras ficou sem conteúdo."
                    )
                decision_pixmap = decision_card.grab()
                if decision_pixmap.isNull():
                    raise RuntimeError(
                        "A captura das prioridades financeiras está vazia."
                    )
                decision_diversity = _colour_diversity(decision_pixmap.toImage())
                if decision_diversity < 24:
                    raise RuntimeError(
                        "As prioridades financeiras não têm detalhe visual suficiente."
                    )
                decision_target = (
                    output_dir / "decision_intelligence_1600_light_values.png"
                )
                if not decision_pixmap.save(str(decision_target), "PNG"):
                    raise RuntimeError(f"Não foi possível guardar {decision_target}.")
                feature_cards.append(
                    {
                        "key": "decision_intelligence",
                        "file": decision_target.name,
                        "priorities": len(decision_card.priority_rows),
                        "size": [decision_card.width(), decision_card.height()],
                        "sampled_colours": decision_diversity,
                    }
                )
                category_card = dashboard.donut_card
                category_hint = dashboard.donut_comparison_hint
                if not category_hint.isVisible() or "Vs." not in category_hint.text():
                    raise RuntimeError(
                        "A comparação por categoria não ficou visível no cartão."
                    )
                category_title = category_card.title_label
                title_gap = (
                    category_hint.geometry().top()
                    - category_title.mapTo(
                        category_card,
                        QPoint(0, category_title.height()),
                    ).y()
                    if category_title is not None
                    else 0
                )
                if category_hint.height() > 72 or title_gap > 20:
                    raise RuntimeError(
                        "A comparação por categoria ficou demasiado afastada do título."
                    )
                category_pixmap = category_card.grab()
                if category_pixmap.isNull():
                    raise RuntimeError(
                        "A captura da comparação por categoria está vazia."
                    )
                category_diversity = _colour_diversity(category_pixmap.toImage())
                if category_diversity < 24:
                    raise RuntimeError(
                        "A comparação por categoria não tem detalhe visual suficiente."
                    )
                category_target = (
                    output_dir / "category_comparison_1600_light_values.png"
                )
                if not category_pixmap.save(str(category_target), "PNG"):
                    raise RuntimeError(f"Não foi possível guardar {category_target}.")
                feature_cards.append(
                    {
                        "key": "category_comparison",
                        "file": category_target.name,
                        "comparison_text": category_hint.text(),
                        "comparison_height": category_hint.height(),
                        "title_gap": title_gap,
                        "size": [category_card.width(), category_card.height()],
                        "sampled_colours": category_diversity,
                    }
                )
                score_page = window.pages["score"]
                window.goto("score")
                _settle_render(app)
                score_context = (
                    score_page.breakdown_card.context_badge.text()
                    if score_page.breakdown_card.context_badge is not None
                    else ""
                )
                if (
                    "Dados até" not in score_context
                    or "dados até" not in score_page.score_status.text().casefold()
                ):
                    raise RuntimeError(
                        "O Score Financeiro 2.0 não mostra a atualidade dos dados."
                    )
                score_pixmap = score_page.breakdown_card.grab()
                score_diversity = _colour_diversity(score_pixmap.toImage())
                if score_pixmap.isNull() or score_diversity < 24:
                    raise RuntimeError(
                        "A captura do Score Financeiro 2.0 não é legível."
                    )
                score_target = output_dir / "score_context_1600_light_values.png"
                if not score_pixmap.save(str(score_target), "PNG"):
                    raise RuntimeError(f"Não foi possível guardar {score_target}.")
                feature_cards.append(
                    {
                        "key": "score_context",
                        "file": score_target.name,
                        "data_context": score_context,
                        "size": [
                            score_page.breakdown_card.width(),
                            score_page.breakdown_card.height(),
                        ],
                        "sampled_colours": score_diversity,
                    }
                )

                portfolio_page = window.pages["carteira"]
                window.goto("carteira")
                _settle_render(app)
                finance_page = window.pages["centro"]
                health_page = window.pages["saude"]
                visual_feature_charts = (
                    (
                        "net_worth_bridge",
                        "carteira",
                        portfolio_page.net_worth_bridge_card,
                        portfolio_page.net_worth_bridge_chart,
                    ),
                    (
                        "portfolio_contribution",
                        "carteira",
                        portfolio_page.portfolio_contribution_card,
                        portfolio_page.portfolio_contribution_chart,
                    ),
                    (
                        "fixed_commitments",
                        "centro",
                        finance_page.commitments_card,
                        finance_page.commitments_chart,
                    ),
                    (
                        "cash_runway_probability",
                        "centro",
                        finance_page.runway_card,
                        finance_page.runway_chart,
                    ),
                    (
                        "recurring_commitment_timeline",
                        "centro",
                        finance_page.recurring_timeline_card,
                        finance_page.recurring_timeline_chart,
                    ),
                    (
                        "budget_variance_heatmap",
                        "centro",
                        finance_page.budget_variance_card,
                        finance_page.budget_variance_chart,
                    ),
                    (
                        "data_quality_trend",
                        "saude",
                        health_page.data_quality_card,
                        health_page.data_quality_chart,
                    ),
                )
                for key, page_key, card, chart in visual_feature_charts:
                    window.goto(page_key)
                    _settle_render(app)
                    rows = list(getattr(chart, "rows", []) or [])
                    if len(rows) < 2:
                        raise RuntimeError(
                            f"O novo gráfico {key} não tem pontos suficientes."
                        )
                    if not chart.pin_detail_item(rows[0]):
                        raise RuntimeError(
                            f"O novo gráfico {key} não permitiu fixar uma seleção."
                        )
                    comparison = chart.detail_payload(rows[1]).get("comparison")
                    chart.clear_pinned_detail()
                    if not comparison or comparison.get("same_item"):
                        raise RuntimeError(
                            f"O novo gráfico {key} não comparou duas seleções."
                        )
                    card_pixmap = card.grab()
                    diversity = _colour_diversity(card_pixmap.toImage())
                    if (
                        card_pixmap.isNull()
                        or diversity < 24
                        or str(getattr(chart, "_paint_error", "") or "")
                    ):
                        raise RuntimeError(
                            f"A captura do novo gráfico {key} não é legível."
                        )
                    target = output_dir / f"{key}_1600_light_values.png"
                    if not card_pixmap.save(str(target), "PNG"):
                        raise RuntimeError(f"Não foi possível guardar {target}.")
                    feature_charts.append(
                        {
                            "key": key,
                            "file": target.name,
                            "rows": len(rows),
                            "comparison_rows": 1,
                            "size": [card.width(), card.height()],
                            "sampled_colours": diversity,
                        }
                    )
                window.goto("dashboard")
                _settle_render(app)

        # The seeded demo history covers only recent months. Add two isolated
        # reference movements inside the previous annual window so the real
        # Windows render also validates the dashed comparison layers.
        annual_reference_month = add_months(date.today(), -18, 1)
        window.repo.add_transaction(
            add_months(annual_reference_month, 0, 2).isoformat(),
            "Receita anual de referência visual",
            2_400,
            "Rendimentos",
            "Conta principal",
        )
        window.repo.add_transaction(
            add_months(annual_reference_month, 0, 12).isoformat(),
            "Despesa anual de referência visual",
            -1_450,
            "Casa e contas",
            "Conta principal",
        )

        window.resize(1600, 900)
        window.repo.set_setting("liquid_dark", "0")
        window.repo.set_setting("liquid_privacy", "0")
        previous_request_id = dashboard._dashboard_request_id
        window.apply_theme()
        if not _wait_until(
            app,
            lambda: (
                dashboard._dashboard_request_id > previous_request_id
                and not any(
                    worker.isRunning() for worker in dashboard._workers.values()
                )
            ),
        ):
            raise RuntimeError(
                "O Dashboard não terminou a preparação do cenário anual."
            )
        annual_button = next(
            (
                button
                for button in dashboard.range_group.buttons()
                if button.property("filterValue") == 365
            ),
            None,
        )
        if annual_button is None:
            raise RuntimeError("O filtro anual não foi encontrado.")
        previous_request_id = dashboard._dashboard_request_id
        annual_button.click()
        if not _wait_until(
            app,
            lambda: (
                dashboard._dashboard_request_id > previous_request_id
                and not any(
                    worker.isRunning() for worker in dashboard._workers.values()
                )
            ),
        ):
            raise RuntimeError("O filtro anual não terminou a atualização.")
        window.goto("dashboard")
        window.sync_content_size()
        _settle_render(app)

        annual_label = str(dashboard.period_label_cache)
        annual_savings_rows = list(dashboard.savings_efficiency.rows)
        annual_cashflow_rows = list(dashboard.cashflow_pulse.rows)
        annual_savings_reference = list(dashboard.savings_efficiency.comparison_rows)
        annual_cashflow_reference = list(dashboard.cashflow_pulse.comparison_rows)
        if annual_label != "último ano":
            raise RuntimeError(
                f"Período anual inesperado nos gráficos: {annual_label!r}."
            )
        if len(annual_savings_rows) < 12:
            raise RuntimeError(
                "A margem e taxa de poupança voltou a truncar o filtro anual."
            )
        if not annual_cashflow_rows:
            raise RuntimeError("O fluxo acumulado anual ficou sem pontos.")
        if not annual_cashflow_reference or not annual_savings_reference:
            raise RuntimeError(
                "A comparação deixou de acompanhar os gráficos do filtro anual."
            )
        if annual_cashflow_rows[0]["date"] != dashboard.start_cache:
            raise RuntimeError(
                "O fluxo acumulado não começa no início do filtro anual."
            )
        if annual_cashflow_rows[-1]["date"] != dashboard.end_cache:
            raise RuntimeError("O fluxo acumulado não termina no fim do filtro anual.")
        for key, card, rows, comparison_rows in (
            (
                "cashflow_pulse_annual",
                dashboard.cashflow_pulse_card,
                annual_cashflow_rows,
                annual_cashflow_reference,
            ),
            (
                "savings_efficiency_annual",
                dashboard.savings_efficiency_card,
                annual_savings_rows,
                annual_savings_reference,
            ),
        ):
            title_label = card.title_label
            title = str(title_label.text() if title_label is not None else "")
            if annual_label not in title:
                raise RuntimeError(f"O título de {key} não reflete o filtro anual.")
            hint_label = (
                dashboard.cashflow_pulse_hint
                if key == "cashflow_pulse_annual"
                else dashboard.savings_efficiency_hint
            )
            title_top = (
                title_label.mapTo(card, QPoint()).y() if title_label is not None else -1
            )
            hint_top = hint_label.mapTo(card, QPoint()).y()
            title_height = title_label.height() if title_label is not None else 0
            title_to_hint = hint_top - title_top - title_height
            if title_top > card.v.contentsMargins().top() + 2:
                raise RuntimeError(f"O título de {key} continua afastado do topo.")
            if title_to_hint < 0 or title_to_hint > 10:
                raise RuntimeError(
                    f"O intervalo entre título e descrição de {key} é "
                    f"{title_to_hint}px."
                )
            card_pixmap = card.grab()
            card_diversity = _colour_diversity(card_pixmap.toImage())
            target = output_dir / f"{key}_1600_light_values.png"
            if card_pixmap.isNull() or card_diversity < 24:
                raise RuntimeError(f"A captura anual de {key} não é legível.")
            if not card_pixmap.save(str(target), "PNG"):
                raise RuntimeError(f"Não foi possível guardar {target}.")
            period_filter_cards.append(
                {
                    "key": key,
                    "file": target.name,
                    "period_label": annual_label,
                    "period_start": dashboard.start_cache,
                    "period_end": dashboard.end_cache,
                    "rows": len(rows),
                    "comparison_rows": len(comparison_rows),
                    "title": title,
                    "size": [card.width(), card.height()],
                    "header_geometry": {
                        "title_top": title_top,
                        "title_height": title_height,
                        "title_width": (
                            title_label.width() if title_label is not None else 0
                        ),
                        "hint_top": hint_top,
                        "hint_height": hint_label.height(),
                        "title_to_hint": title_to_hint,
                    },
                    "sampled_colours": card_diversity,
                }
            )

        manifest = {
            "platform": app.platformName(),
            "scale_factor": args.scale_factor,
            "font_family": window.font().family(),
            "font_families_available": len(families),
            "glyph_probe": probe_text,
            "startup_window": startup_capture,
            "message_boxes": message_box_captures,
            "captures": captures,
            "feature_charts": feature_charts,
            "feature_cards": feature_cards,
            "period_filter_cards": period_filter_cards,
            "transaction_editor": transaction_editor,
        }
        (output_dir / "visual_validation.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Keep console output portable on Windows shells still using cp1252.
        # The saved manifest remains UTF-8 and preserves the original glyphs.
        print(json.dumps(manifest, ensure_ascii=True))
        return 0
    finally:
        if window is not None:
            try:
                window.auto_backup_timer.stop()
                window.subscription_reminder_timer.stop()
                window.pages["dashboard"].shutdown_workers()
                window.close()
                if app is not None:
                    app.processEvents()
                window.repo.db.conn.close()
            except Exception:
                pass
        try:
            from utils.app_logging import shutdown_logging

            shutdown_logging()
        except Exception:
            pass
        shutil.rmtree(state_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
