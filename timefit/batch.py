"""台本ファイル1つをまとめて処理する（CLI と GUI の共通部分）"""

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import edition
from .config import AppConfig
from .engine import VoicevoxClient
from .errors import EngineError, LLMError
from .fitter import FitResult, FitStatus, fit_text
from .inputs import load_script
from .llm import GeminiRewriter, Rewriter
from .outputs import credit_text, wav_filename, write_combined_wav, write_report, write_srt

Log = Callable[[str], None]
Progress = Callable[[int, int, FitResult], None]


@dataclass
class JobSummary:
    output_dir: Path
    results: list[FitResult] = field(default_factory=list)
    skipped_by_limit: int = 0
    cancelled: bool = False

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in FitStatus}
        for r in self.results:
            c[r.status.value] += 1
        return c


def make_rewriter(cfg: AppConfig, log: Log) -> Rewriter | None:
    if not cfg.allow_rewrite:
        return None
    if not cfg.gemini_api_key:
        log("※ Gemini の APIキーが未設定のため、要約は行いません（話速の調整だけで合わせます）。")
        return None
    return GeminiRewriter(cfg.gemini_api_key, cfg.gemini_model)


def run_job(
    input_path: str | Path,
    output_dir: str | Path,
    cfg: AppConfig,
    engine: VoicevoxClient | None = None,
    rewriter: Rewriter | None = None,
    log: Log = print,
    on_progress: Progress | None = None,
    cancel: threading.Event | None = None,
) -> JobSummary:
    """入力ファイルを読み、1行ずつ尺合わせして、出力フォルダに結果をまとめて書き出す"""
    script = load_script(input_path)
    lines = script.lines
    engine = engine or VoicevoxClient(cfg.engine_url)

    log(f"音声エンジンを確認しています（{cfg.engine_url}）...")
    log(f"  バージョン: {engine.version()}")
    if rewriter is None:
        rewriter = make_rewriter(cfg, log)

    summary = JobSummary(output_dir=Path(output_dir))
    limit = edition.line_limit()
    if limit is not None and len(lines) > limit:
        summary.skipped_by_limit = len(lines) - limit
        log(f"※ 無料版は1回 {limit} 行までです。残り {summary.skipped_by_limit} 行は処理しません（製品版なら無制限）。")
        lines = lines[:limit]

    out = summary.output_dir
    out.mkdir(parents=True, exist_ok=True)
    settings = cfg.fit_settings()
    files: list[str] = []
    done_lines = []

    for i, line in enumerate(lines, start=1):
        if cancel is not None and cancel.is_set():
            summary.cancelled = True
            log("中止しました。ここまでの結果を書き出します。")
            break
        speaker = line.speaker_id if line.speaker_id is not None else cfg.default_speaker
        log(f"[{i}/{len(lines)}] 目標 {line.target_sec:.2f}秒: {line.text}")
        try:
            result = fit_text(line.text, speaker, line.target_sec, engine, rewriter, settings, log)
        except LLMError as e:
            # 要約だけ失敗した行は、要約なしでできるところまで合わせる
            # （APIキーの誤りは LLMError ではないので、ここでは捕まえず全体を止める）
            log(f"  ⚠ {e} この行は要約なしで処理します。")
            result = fit_text(line.text, speaker, line.target_sec, engine, None, settings, log)
            result.message = f"{e} {result.message}".strip()

        name = wav_filename(line, result)
        (out / name).write_bytes(result.wav.to_bytes())
        if not result.ok:
            log(f"  ⚠ {result.message}")
        summary.results.append(result)
        files.append(name)
        done_lines.append(line)
        if on_progress:
            on_progress(i, len(lines), result)

    if summary.results:
        _write_extras(out, script.kind, done_lines, summary.results, files, cfg, engine, log)
    c = summary.counts()
    log("完了: " + "、".join(f"{k} {v}件" for k, v in c.items() if v))
    log(f"出力先: {out}")
    return summary


def _write_extras(out, kind, lines, results, files, cfg, engine, log) -> None:
    write_report(out / "report.csv", lines, results, files)
    write_srt(out / "fitted.srt", lines, results)
    try:
        write_combined_wav(out / "combined.wav", lines, results)
    except ValueError as e:
        log(f"※ まとめWAVは作れませんでした: {e}")

    speaker_ids = {ln.speaker_id if ln.speaker_id is not None else cfg.default_speaker for ln in lines}
    try:
        styles = {s.style_id: s.character for s in engine.speakers()}
        names = [styles.get(sid, f"話者ID {sid}") for sid in speaker_ids]
    except EngineError:
        names = [f"話者ID {sid}" for sid in speaker_ids]
    (out / "credits.txt").write_text(credit_text(cfg.credit_prefix, names), encoding="utf-8")
    note = "字幕のタイミングどおり" if kind == "srt" else "行の順に隙間なく"
    log(f"まとめ音声 combined.wav（{note}）、字幕 fitted.srt、結果一覧 report.csv、クレジット credits.txt を書き出しました。")
