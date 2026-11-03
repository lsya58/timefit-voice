"""結果ファイルの書き出し（WAV、まとめWAV、字幕、レポート、クレジット）"""

import csv
import re
from pathlib import Path

from .audio import place_clips
from .fitter import FitResult
from .inputs import ScriptLine

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]')


def wav_filename(line: ScriptLine, result: FitResult) -> str:
    snippet = _BAD_CHARS.sub("", result.text)[:12]
    return f"{line.index:03d}_{snippet}.wav"


def _srt_time(sec: float) -> str:
    ms = round(sec * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def timeline(lines: list[ScriptLine]) -> list[float]:
    """各行の開始秒。字幕入力なら字幕の時刻、CSV なら前の行の直後に並べる。"""
    starts, t = [], 0.0
    for ln in lines:
        start = ln.start_sec if ln.start_sec is not None else t
        starts.append(start)
        t = start + ln.target_sec
    return starts


def write_combined_wav(path: Path, lines: list[ScriptLine], results: list[FitResult]) -> None:
    starts = timeline(lines)
    end = max(s + ln.target_sec for s, ln in zip(starts, lines))
    combined = place_clips([(s, r.wav) for s, r in zip(starts, results)], end)
    path.write_bytes(combined.to_bytes())


def write_srt(path: Path, lines: list[ScriptLine], results: list[FitResult]) -> None:
    out = []
    for n, (start, ln, r) in enumerate(zip(timeline(lines), lines, results), start=1):
        out.append(f"{n}\n{_srt_time(start)} --> {_srt_time(start + ln.target_sec)}\n{r.text}\n")
    path.write_text("\n".join(out), encoding="utf-8")


REPORT_HEADER = [
    "番号", "状態", "目標秒数", "生成秒数", "実発話秒数", "追加無音秒数", "次字幕までの空白秒数",
    "話速", "間", "要約回数", "ファイル", "元の原稿", "最終原稿", "メモ", "品質警告",
    "処理秒数", "AI秒数", "AI呼び出し", "AIエラー", "合成秒数", "合成回数",
]


def write_report(path: Path, lines: list[ScriptLine], results: list[FitResult], files: list[str]) -> None:
    # Excel で文字化けしないよう BOM 付き UTF-8
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(REPORT_HEADER)
        for i, (ln, r, name) in enumerate(zip(lines, results, files)):
            if i + 1 < len(lines) and ln.start_sec is not None and lines[i + 1].start_sec is not None:
                subtitle_gap = max(0.0, lines[i + 1].start_sec - (ln.start_sec + ln.target_sec))
            else:
                subtitle_gap = 0.0
            w.writerow([
                ln.index, r.status.value, f"{r.target_sec:.3f}", f"{r.duration:.3f}",
                f"{r.speech_sec:.3f}", f"{r.padding_sec:.3f}", f"{subtitle_gap:.3f}",
                f"{r.speed:.2f}", f"{r.pause_scale:.2f}", r.rewrite_attempts, name,
                r.original_text, r.text, r.message, r.quality_warning, *_timing_cells(r.timing),
            ])


def _timing_cells(t) -> list:
    if t is None:  # 確認画面で作り直した行など
        return ["", "", "", "", "", ""]
    return [f"{t.total_sec:.2f}", f"{t.ai_sec:.2f}", t.ai_calls, t.errors_text(), f"{t.synth_sec:.2f}", t.synth_calls]


def credit_text(prefix: str, character_names: list[str]) -> str:
    names = sorted(set(character_names))
    lines = [f"{prefix}:{n}" for n in names]
    return (
        "\n".join(lines)
        + "\n\n※ 動画の概要欄などにクレジットを記載してください。"
        "表記方法や利用条件はキャラクターごとに異なるため、各キャラクターの利用規約を必ず確認してください。\n"
    )
