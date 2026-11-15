"""Fonte única de verdade para cor, tipografia, raio e contraste.

Este módulo não importa PySide6 de propósito: pode ser importado e testado
sem `QApplication`, o que mantém os testes de design baratos.

Superfícies de referência (medidas a partir das cores reais da app):

    claro  página #F4F8FF · cartão #FDFEFF
    escuro página #0B1020 · cartão #1D2435

`CARD_SURFACE` é a superfície onde vive um cartão. `TEXT_SURFACE` é a
superfície mais desfavorável para texto (a página clara é mais escura do que o
cartão, e o cartão escuro é mais claro do que a página), logo é a que serve de
critério para garantir o mínimo de contraste em todo o lado.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from typing import Final, Mapping

# --------------------------------------------------------------------------- #
# 1. Superfícies de referência
# --------------------------------------------------------------------------- #

CARD_SURFACE: Final[Mapping[str, str]] = {"light": "#FDFEFF", "dark": "#1D2435"}
TEXT_SURFACE: Final[Mapping[str, str]] = {"light": "#F4F8FF", "dark": "#1D2435"}

MIN_TEXT_CONTRAST: Final[float] = 4.5
MIN_MARK_CONTRAST: Final[float] = 3.0


# --------------------------------------------------------------------------- #
# 2. Cores semânticas (significado, nunca séries)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SemanticPalette:
    success: str
    warning: str
    danger: str
    info: str
    neutral: str


SEMANTIC: Final[Mapping[str, SemanticPalette]] = {
    "light": SemanticPalette(
        success="#16803A",  # 4.71:1 sobre a página clara
        warning="#B45309",  # 4.71:1
        danger="#C0261B",  # 5.58:1
        info="#0B6BD6",  # 4.82:1
        neutral="#627288",  # 4.61:1
    ),
    "dark": SemanticPalette(
        success="#30D158",  # 7.66:1 sobre o cartão escuro
        warning="#FFB020",  # 8.47:1
        danger="#FF6961",  # 5.49:1
        info="#4DA3FF",  # 5.90:1
        neutral="#A7B2C4",  # 7.23:1
    ),
}

SEMANTIC_ROLES: Final[tuple[str, ...]] = (
    "success",
    "warning",
    "danger",
    "info",
    "neutral",
)


# --------------------------------------------------------------------------- #
# 3. Paleta categórica (séries de gráficos)
# --------------------------------------------------------------------------- #

# Ordem fixa: a categoria N recebe sempre SERIES[mode][N % 10]. Base Okabe-Ito.
SERIES: Final[Mapping[str, tuple[str, ...]]] = {
    "light": (
        "#0072B2",
        "#009E73",
        "#B45309",
        "#7C3AED",
        "#C0261B",
        "#0F766E",
        "#4D7C0F",
        "#BE185D",
        "#1D4ED8",
        "#475569",
    ),
    "dark": (
        "#56B4E9",
        "#4ADE80",
        "#F0B429",
        "#C4A7FD",
        "#FF8A80",
        "#5EEAD4",
        "#BEF264",
        "#F9A8D4",
        "#93C5FD",
        "#94A3B8",
    ),
}

SERIES_OTHER: Final[Mapping[str, str]] = {"light": "#64748B", "dark": "#94A3B8"}


# --------------------------------------------------------------------------- #
# 4. Ramp de alfa (substitui os rgba() escritos à mão)
# --------------------------------------------------------------------------- #

ALPHA: Final[Mapping[str, float]] = {
    "tint": 0.12,
    "tint_strong": 0.22,
    "line": 0.34,
    "line_strong": 0.48,
}


def mode_of(dark: bool) -> str:
    return "dark" if dark else "light"


def triplet(color: str) -> str:
    """'#0A84FF' -> '10,132,255' (formato aceite pelo QSS dentro de rgba())."""
    value = str(color).strip().lstrip("#")
    if len(value) == 3:
        value = "".join(character * 2 for character in value)
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return f"{red},{green},{blue}"


def rgba(color: str, alpha: float) -> str:
    return f"rgba({triplet(color)},{max(0.0, min(1.0, float(alpha))):.2f})"


def quantise_alpha(alpha: float) -> str:
    """Degrau do ramp mais próximo de um alfa legado."""
    return min(ALPHA, key=lambda name: abs(ALPHA[name] - float(alpha)))


# --------------------------------------------------------------------------- #
# 5. WCAG 2.x: luminância, contraste e derivação de tons legíveis
# --------------------------------------------------------------------------- #


def _srgb(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _channels(color: str) -> tuple[int, int, int]:
    value = str(color).strip().lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def luminance(color: str) -> float:
    red, green, blue = _channels(color)
    return 0.2126 * _srgb(red) + 0.7152 * _srgb(green) + 0.0722 * _srgb(blue)


def contrast_ratio(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _to_hls(color: str) -> tuple[float, float, float]:
    red, green, blue = (channel / 255.0 for channel in _channels(color))
    return colorsys.rgb_to_hls(red, green, blue)


def _from_hls(hue: float, light: float, sat: float) -> str:
    red, green, blue = colorsys.hls_to_rgb(
        hue, max(0.0, min(1.0, light)), max(0.0, min(1.0, sat))
    )
    return "#%02X%02X%02X" % (
        round(red * 255),
        round(green * 255),
        round(blue * 255),
    )


def ensure_contrast(color: str, surface: str, target: float = MIN_TEXT_CONTRAST) -> str:
    """Escurece (fundo claro) ou clareia (fundo escuro) até atingir `target`."""
    if contrast_ratio(color, surface) >= target:
        return str(color).strip().upper()
    hue, light, sat = _to_hls(color)
    darken = luminance(surface) > 0.35
    for step in range(1, 101):
        candidate = _from_hls(
            hue,
            light - 0.01 * step if darken else light + 0.01 * step,
            sat * (1.0 - 0.10 * step / 100.0),
        )
        if contrast_ratio(candidate, surface) >= target:
            return candidate
    return str(color).strip().upper()


def blend_alpha(foreground: str, background: str, alpha: float) -> str:
    """Simula em hex o resultado de pintar `foreground` a `alpha` sobre `background`."""
    front = _channels(foreground)
    back = _channels(background)
    return "#%02X%02X%02X" % tuple(
        round(front[index] * alpha + back[index] * (1.0 - alpha)) for index in range(3)
    )


def accent_text(accent: str, mode: str) -> str:
    """Tom do accent legível como texto (>= 4.5:1)."""
    return ensure_contrast(accent, TEXT_SURFACE[mode], MIN_TEXT_CONTRAST)


def accent_on_tint(accent: str, mode: str, step: str = "tint") -> str:
    """Tom do accent legível por cima de `rgba(accent, ALPHA[step])`."""
    surface = blend_alpha(accent, TEXT_SURFACE[mode], ALPHA[step])
    return ensure_contrast(accent, surface, MIN_TEXT_CONTRAST)


def accent_mark(accent: str, mode: str) -> str:
    """Tom do accent para marcas de gráfico (>= 3:1)."""
    return ensure_contrast(accent, CARD_SURFACE[mode], MIN_MARK_CONTRAST)


# --------------------------------------------------------------------------- #
# 6. Tipografia
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TypeScale:
    """Escala única: QSS e QPainter passam a falar em píxeis, não em pontos."""

    xs: int = 10
    sm: int = 11
    base: int = 12
    md: int = 13
    lg: int = 14
    xl: int = 16
    xxl: int = 18
    display: int = 24
    hero: int = 28

    def _pairs(self) -> tuple[tuple[str, int], ...]:
        return (
            ("xs", self.xs),
            ("sm", self.sm),
            ("base", self.base),
            ("md", self.md),
            ("lg", self.lg),
            ("xl", self.xl),
            ("xxl", self.xxl),
            ("display", self.display),
            ("hero", self.hero),
        )

    def cascade(self, factor: float = 1.0, minimum: int = 9) -> dict[str, int]:
        factor = max(0.8, min(1.6, float(factor)))
        return {
            name: max(minimum, round(value * factor)) for name, value in self._pairs()
        }

    def px(self, role: str, factor: float = 1.0) -> int:
        return self.cascade(factor)[role]


TYPE: Final[TypeScale] = TypeScale()

# 9 pesos no QSS -> 4 reais. A fonte não distingue 520 de 560.
WEIGHT: Final[Mapping[int, int]] = {
    500: 500,
    520: 600,
    560: 600,
    600: 600,
    650: 600,
    700: 700,
    750: 700,
    760: 700,
    800: 800,
    850: 800,
}


# --------------------------------------------------------------------------- #
# 7. Raio e espaço
# --------------------------------------------------------------------------- #

RADIUS: Final[Mapping[str, int]] = {
    "sm": 6,
    "md": 10,
    "lg": 12,
    "xl": 14,
    "xxl": 16,
    "card": 18,
    "pill": 24,
}

RADIUS_MAP: Final[Mapping[int, str]] = {
    3: "sm",
    4: "sm",
    5: "sm",
    6: "sm",
    8: "md",
    9: "md",
    10: "md",
    11: "md",
    12: "lg",
    14: "xl",
    15: "xl",
    16: "xxl",
    17: "xxl",
    18: "card",
    24: "pill",
    30: "pill",
}

SPACE: Final[Mapping[str, int]] = {"1": 4, "2": 8, "3": 12, "4": 16, "5": 20, "6": 24, "8": 32}


# --------------------------------------------------------------------------- #
# 8. Montagem dos placeholders do QSS
# --------------------------------------------------------------------------- #


def semantic_tokens(mode: str) -> dict[str, str]:
    """@success@, @success_tint@, @success_on_tint@ … para um tema."""
    palette = SEMANTIC[mode]
    card = CARD_SURFACE[mode]
    tokens: dict[str, str] = {}
    for role in SEMANTIC_ROLES:
        base = getattr(palette, role)
        tokens[role] = base
        for step, alpha in ALPHA.items():
            tokens[f"{role}_{step}"] = rgba(base, alpha)
            if step in ("tint", "tint_strong"):
                tokens[f"{role}_on_{step}"] = ensure_contrast(
                    base, blend_alpha(base, card, alpha), MIN_TEXT_CONTRAST
                )
    return tokens


def accent_tokens(accent: str, secondary: str, mode: str) -> dict[str, str]:
    tokens: dict[str, str] = {
        "primary": accent,
        "secondary": secondary,
        "accent_text": accent_text(accent, mode),
        "secondary_text": accent_text(secondary, mode),
        "accent_on_tint": accent_on_tint(accent, mode, "tint"),
        "accent_on_tint_strong": accent_on_tint(accent, mode, "tint_strong"),
        "secondary_on_tint": accent_on_tint(secondary, mode, "tint"),
    }
    for step, alpha in ALPHA.items():
        tokens[f"accent_{step}"] = rgba(accent, alpha)
        tokens[f"secondary_{step}"] = rgba(secondary, alpha)
    return tokens


def type_tokens(scale: TypeScale, factor: float) -> dict[str, str]:
    return {f"fs_{name}": f"{value}px" for name, value in scale.cascade(factor).items()}


def radius_tokens() -> dict[str, str]:
    return {f"r_{name}": f"{value}px" for name, value in RADIUS.items()}


def qss_tokens(
    *, accent: str, secondary: str, mode: str, factor: float = 1.0
) -> dict[str, str]:
    """Todos os placeholders derivados que `Theme.stylesheet()` substitui."""
    tokens: dict[str, str] = {}
    tokens.update(accent_tokens(accent, secondary, mode))
    tokens.update(semantic_tokens(mode))
    tokens.update(type_tokens(TYPE, factor))
    tokens.update(radius_tokens())
    return tokens


def series_color(index: int, mode: str) -> str:
    palette = SERIES[mode]
    return palette[index % len(palette)]


# --------------------------------------------------------------------------- #
# 9. Larguras de chrome que dependem do texto
# --------------------------------------------------------------------------- #

#: Espaço que a sidebar ocupa sem ser texto: margens da coluna (8+8), padding do
#: botão (6+6), borda (1+1) e barra de scroll (6). Medido na janela real:
#: sidebar 224 -> botão 188; sidebar 142 -> botão 114.
SIDEBAR_CHROME_PX: Final[int] = 36
SIDEBAR_CHROME_COMPACT_PX: Final[int] = 28


def scaled_sidebar_width(
    base: int, factor: float = 1.0, *, chrome: int = SIDEBAR_CHROME_PX
) -> int:
    """Largura da sidebar que acompanha a escala de texto sem cortar rótulos.

    Só a fatia ocupada por texto cresce; padding, borda e margens são fixos.
    A 100 % devolve exatamente `base`, o que preserva o contrato dos 142 px.

        scaled_sidebar_width(224, 1.00) -> 224
        scaled_sidebar_width(224, 1.25) -> 271   (o rótulo mais longo pede 245)
        scaled_sidebar_width(142, 1.25, chrome=SIDEBAR_CHROME_COMPACT_PX) -> 171
    """

    factor = max(0.8, min(1.6, float(factor)))
    text = max(0, int(base) - int(chrome))
    return int(chrome) + int(text * factor + 0.5)
