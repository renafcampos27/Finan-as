"""Contratos da base de design: tokens, contraste e escala."""

from __future__ import annotations

import pytest

from ui.core import design_tokens as tokens

PALETTES = {
    "Azul": ("#0A84FF", "#30D158"),
    "Verde": ("#30D158", "#0A84FF"),
    "Roxo": ("#BF5AF2", "#FF375F"),
    "Rosa": ("#FF375F", "#FF9F0A"),
    "Laranja": ("#FF9F0A", "#FFD60A"),
}


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("role", list(tokens.SEMANTIC_ROLES))
def test_semantic_colours_are_readable(mode: str, role: str) -> None:
    colour = getattr(tokens.SEMANTIC[mode], role)
    assert tokens.contrast_ratio(colour, tokens.TEXT_SURFACE[mode]) >= 4.5


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("role", list(tokens.SEMANTIC_ROLES))
@pytest.mark.parametrize("step", ["tint", "tint_strong"])
def test_semantic_text_on_own_tint(mode: str, role: str, step: str) -> None:
    rendered = tokens.semantic_tokens(mode)[f"{role}_on_{step}"]
    background = tokens.blend_alpha(
        getattr(tokens.SEMANTIC[mode], role),
        tokens.CARD_SURFACE[mode],
        tokens.ALPHA[step],
    )
    assert tokens.contrast_ratio(rendered, background) >= 4.5


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("name", list(PALETTES))
def test_accent_text_is_readable_for_every_palette(mode: str, name: str) -> None:
    accent, secondary = PALETTES[name]
    assert tokens.contrast_ratio(
        tokens.accent_text(accent, mode), tokens.TEXT_SURFACE[mode]
    ) >= 4.5
    assert tokens.contrast_ratio(
        tokens.accent_text(secondary, mode), tokens.TEXT_SURFACE[mode]
    ) >= 4.5


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_series_are_unique_and_visible(mode: str) -> None:
    palette = tokens.SERIES[mode]
    assert len(palette) == 10
    assert len(set(palette)) == 10
    for colour in palette:
        assert tokens.contrast_ratio(colour, tokens.CARD_SURFACE[mode]) >= 3.0


@pytest.mark.parametrize(
    ("base", "chrome", "expected"),
    [
        (64, tokens.SIDEBAR_CHROME_COMPACT_PX, (60, 64, 68, 73)),
        (142, tokens.SIDEBAR_CHROME_COMPACT_PX, (131, 142, 153, 171)),
        (224, tokens.SIDEBAR_CHROME_PX, (205, 224, 243, 271)),
        (236, tokens.SIDEBAR_CHROME_PX, (216, 236, 256, 286)),
    ],
)
def test_sidebar_width_follows_the_text_scale(
    base: int, chrome: int, expected: tuple[int, int, int, int]
) -> None:
    for factor, width in zip((0.9, 1.0, 1.1, 1.25), expected, strict=True):
        assert tokens.scaled_sidebar_width(base, factor, chrome=chrome) == width


def test_sidebar_width_is_unchanged_at_100_percent() -> None:
    assert tokens.scaled_sidebar_width(142, 1.0, chrome=28) == 142
    assert tokens.scaled_sidebar_width(224, 1.0) == 224
