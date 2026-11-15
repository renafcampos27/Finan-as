# -*- coding: utf-8 -*-
"""Safe launcher for the PySide6 Liquid Glass interface.
It writes startup errors to logs/arranque_liquid_glass.log and shows a Windows message box.
"""
from __future__ import annotations

import os
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
STATE_ROOT = Path(
    os.environ.get("FINANCAS_PESSOAIS_STATE_DIR")
    or Path(os.environ.get("LOCALAPPDATA", BASE)) / "FinancasPessoais"
)
LOG_DIR = STATE_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "arranque_liquid_glass.log"

os.environ.setdefault("QT_OPENGL", "software")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")

try:
    from main import main
    main()
except SystemExit:
    raise
except Exception:
    message = traceback.format_exc()
    LOG_FILE.write_text(message, encoding="utf-8")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "A app não conseguiu arrancar.\n\n"
            f"Foi criado um relatório de erro em:\n{LOG_FILE}\n\n"
            "Abre esse ficheiro ou usa run_windows_liquid_glass_debug.bat para veres o erro completo.",
            "Finanças Pessoais — erro no arranque",
            0x10,
        )
    except Exception:
        pass
    raise
