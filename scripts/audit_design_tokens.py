#!/usr/bin/env python3
"""Falha se voltar a aparecer uma cor de paleta escrita à mão.

Uso:  python scripts/audit_design_tokens.py
Saída: lista ficheiro:linha e código de saída 1 se houver violações.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BANNED = re.compile(
    r"rgba\(\s*(?:10\s*,\s*132\s*,\s*255|48\s*,\s*209\s*,\s*88"
    r"|255\s*,\s*159\s*,\s*10|255\s*,\s*69\s*,\s*58|191\s*,\s*90\s*,\s*242)"
    r"|#(?:0A84FF|30D158|34C759|4ADE80|FF9F0A|FFB340|FFB020"
    r"|FF453A|FF3B30|F87171|BF5AF2)\b",
    re.IGNORECASE,
)

# Ficheiros isentos: valores fixados por testes existentes ou ainda por migrar (Fase 2).
ALLOWED = {
    "ui/core/theme.py",  # primary_map: cores das 5 paletas do utilizador
    "ui/core/design_tokens.py",  # a fonte de verdade
    "ui/core/tooltips.py",
    "ui/components/cards.py",  # _TONE_COLORS -> Fase 2
    "ui/components/chart_details.py",  # _TONE_COLOURS -> Fase 2
    "utils/helpers.py",  # constantes históricas -> Fase 2
}

TARGETS = ["assets/*.qss", "ui/**/*.py", "utils/*.py", "main.py"]


def main() -> int:
    violations: list[str] = []
    for pattern in TARGETS:
        for path in sorted(ROOT.glob(pattern)):
            relative = path.relative_to(ROOT).as_posix()
            if relative in ALLOWED:
                continue
            if "charts/" in relative:
                continue  # Fase 2
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if BANNED.search(line):
                    violations.append(f"{relative}:{number}: {line.strip()[:120]}")
    if violations:
        print("\n".join(violations))
        print(f"\n{len(violations)} cores de paleta escritas à mão.")
        return 1
    print("Sem cores de paleta escritas à mão.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
