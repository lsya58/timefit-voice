"""無料版 / 製品版の切り替え

配布用の exe を作るときは scripts/build.ps1 が edition.txt を同梱して版を決める。
開発中は製品版として動く。
"""

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    marker = Path(sys._MEIPASS) / "edition.txt"
    EDITION = marker.read_text(encoding="ascii").strip()
    if EDITION not in {"free", "pro"}:
        raise RuntimeError("配布版の識別情報が正しくありません。")
else:
    EDITION = "pro"

FREE_LINE_LIMIT = 10


def is_free() -> bool:
    return EDITION == "free"


def edition_label() -> str:
    return "無料版" if is_free() else "製品版"


def line_limit() -> int | None:
    return FREE_LINE_LIMIT if is_free() else None
