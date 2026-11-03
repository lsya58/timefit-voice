"""無料版 / 製品版の切り替え

配布用の exe を作るときは scripts/build.ps1 が timefit/_build_edition.py を生成して版を決める。
開発中（そのファイルが無いとき）は製品版として動く。
"""

try:
    from ._build_edition import EDITION  # type: ignore
except ImportError:
    EDITION = "pro"

FREE_LINE_LIMIT = 10


def is_free() -> bool:
    return EDITION == "free"


def edition_label() -> str:
    return "無料版" if is_free() else "製品版"


def line_limit() -> int | None:
    return FREE_LINE_LIMIT if is_free() else None
