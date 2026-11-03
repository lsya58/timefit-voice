"""台本ファイル（CSV / SRT / VTT）の読み込み"""

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import InputFileError


@dataclass
class ScriptLine:
    index: int               # 1始まりの通し番号
    text: str
    target_sec: float
    speaker_id: int | None = None   # None なら既定の話者
    start_sec: float | None = None  # 字幕入力のときだけ入る


@dataclass
class Script:
    kind: str  # "csv" | "srt"
    lines: list[ScriptLine]


def read_text_file(path: Path) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise InputFileError(f"ファイルを開けませんでした: {path}（{e.strerror}）") from None
    # Excel で保存した CSV は Shift_JIS(cp932) のことが多い
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise InputFileError("ファイルの文字コードを判別できませんでした。UTF-8 で保存し直してください。")


def load_script(path: str | Path) -> Script:
    path = Path(path)
    text = read_text_file(path)
    ext = path.suffix.lower()
    if ext in (".srt", ".vtt"):
        return Script("srt", parse_subtitles(text))
    if ext in (".csv", ".txt", ".tsv"):
        return Script("csv", parse_csv(text, delimiter="\t" if ext == ".tsv" else ","))
    raise InputFileError(f"対応していないファイル形式です（{ext}）。CSV / SRT / VTT を指定してください。")


# ---------- 字幕 ----------

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_TIME_LINE = re.compile(_TS + r"\s*-->\s*" + _TS)
_TAG = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def _to_sec(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_subtitles(text: str) -> list[ScriptLine]:
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
    lines: list[ScriptLine] = []
    for block in blocks:
        rows = [r for r in block.split("\n") if r.strip()]
        time_idx = next((i for i, r in enumerate(rows) if _TIME_LINE.search(r)), None)
        if time_idx is None:
            continue  # WEBVTT ヘッダーや NOTE など
        m = _TIME_LINE.search(rows[time_idx])
        start = _to_sec(*m.groups()[:4])
        end = _to_sec(*m.groups()[4:])
        body = "".join(_TAG.sub("", r).strip() for r in rows[time_idx + 1:])
        if not body:
            continue
        if end <= start:
            raise InputFileError(f"字幕「{body[:20]}」の終了時刻が開始時刻より前になっています。")
        lines.append(ScriptLine(index=len(lines) + 1, text=body, target_sec=end - start, start_sec=start))
    if not lines:
        raise InputFileError("字幕ファイルから字幕を1つも読み取れませんでした。")
    return lines


# ---------- CSV ----------

_HEADER_ALIASES = {
    "speaker": {"speaker", "speaker_id", "話者", "話者id", "キャラクターid", "キャラid"},
    "seconds": {"seconds", "sec", "target_sec", "秒数", "目標秒数", "尺"},
    "text": {"text", "原稿", "台本", "セリフ", "本文"},
}


def _header_map(row: list[str]) -> dict[str, int] | None:
    found = {}
    for i, cell in enumerate(row):
        key = cell.strip().lower().replace(" ", "")
        for name, aliases in _HEADER_ALIASES.items():
            if key in aliases:
                found[name] = i
    if "seconds" in found and "text" in found:
        return found
    return None


def parse_csv(text: str, delimiter: str = ",") -> list[ScriptLine]:
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    lines: list[ScriptLine] = []
    header = None
    for row_no, row in enumerate(rows, start=1):
        if not any(c.strip() for c in row) or row[0].strip().startswith("#"):
            continue
        if header is None and not lines:
            header = _header_map(row)
            if header is not None:
                continue
        speaker_cell, sec_cell, text_cell = _split_row(row, header, row_no)
        try:
            sec = float(sec_cell)
        except ValueError:
            raise InputFileError(f"{row_no}行目: 秒数「{sec_cell}」が数値ではありません。") from None
        if sec <= 0:
            raise InputFileError(f"{row_no}行目: 秒数は 0 より大きくしてください。")
        speaker = None
        if speaker_cell:
            try:
                speaker = int(speaker_cell)
            except ValueError:
                raise InputFileError(f"{row_no}行目: 話者ID「{speaker_cell}」が整数ではありません。") from None
        if not text_cell:
            raise InputFileError(f"{row_no}行目: 原稿が空です。")
        lines.append(ScriptLine(index=len(lines) + 1, text=text_cell, target_sec=sec, speaker_id=speaker))
    if not lines:
        raise InputFileError("CSV から原稿を1行も読み取れませんでした。")
    return lines


def _split_row(row: list[str], header: dict[str, int] | None, row_no: int) -> tuple[str, str, str]:
    cells = [c.strip() for c in row]
    if header:
        def get(name):
            i = header.get(name)
            return cells[i] if i is not None and i < len(cells) else ""
        return get("speaker"), get("seconds"), get("text")
    # ヘッダーなし: 「話者ID, 秒数, 原稿」または「秒数, 原稿」
    if len(cells) >= 3:
        return cells[0], cells[1], ",".join(cells[2:]).strip()
    if len(cells) == 2:
        return "", cells[0], cells[1]
    raise InputFileError(f"{row_no}行目: 列が足りません（「話者ID, 秒数, 原稿」の形式にしてください）。")
