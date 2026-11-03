"""台本ファイル1つをまとめて処理する（CLI と GUI の共通部分）"""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import edition
from .config import AppConfig
from .engine import VoicevoxClient
from .errors import ApiKeyMissingError, EngineError, LLMError, TimeFitError
from .fitter import FitResult, FitStatus, fit_text, plan_prosody, rewrite_target_chars
from .engine import estimate_duration, with_prosody
from .inputs import ScriptLine, load_script
from .llm import GeminiRewriter, Rewriter
from .timing import Recorder, Stats, TimedEngine
from .outputs import credit_text, wav_filename, write_combined_wav, write_report, write_srt

Log = Callable[[str], None]
LineStart = Callable[[int, int, ScriptLine], None]
Progress = Callable[[int, int, ScriptLine, FitResult, str], None]  # (何行目, 全行数, 行, 結果, WAVファイル名)


@dataclass
class JobSummary:
    """1回の処理の結果。あとから1行だけ作り直すときにも使う。"""

    output_dir: Path
    kind: str = "csv"
    lines: list[ScriptLine] = field(default_factory=list)    # 処理した行（results と同じ順）
    results: list[FitResult] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    speakers: list[int] = field(default_factory=list)
    approved: set[int] = field(default_factory=set)          # 要約を確認済みの行番号
    skipped_by_limit: int = 0
    cancelled: bool = False
    timing: Stats | None = None

    def position(self, index: int) -> int:
        for pos, ln in enumerate(self.lines):
            if ln.index == index:
                return pos
        raise TimeFitError(f"{index}行目の結果が見つかりません。")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def counts(self) -> dict[str, int]:
        c = {s.value: 0 for s in FitStatus}
        for r in self.results:
            c[r.status.value] += 1
        return c


def gemini_rewriter(cfg: AppConfig, log: Log, on_event=lambda _e: None) -> GeminiRewriter:
    fallbacks = [m.strip() for m in cfg.gemini_fallback_model.split(",")]
    return GeminiRewriter(
        cfg.gemini_api_key, cfg.gemini_model, fallback_models=fallbacks, log=log,
        thinking_level=cfg.gemini_thinking, on_event=on_event,
    )


def make_rewriter(cfg: AppConfig, log: Log, on_event=lambda _e: None) -> Rewriter | None:
    if not cfg.allow_rewrite:
        return None
    if not cfg.gemini_api_key:
        log("※ Gemini の APIキーが未設定のため、要約は行いません（話速の調整だけで合わせます）。")
        return None
    return gemini_rewriter(cfg, log, on_event)


def run_job(
    input_path: str | Path,
    output_dir: str | Path,
    cfg: AppConfig,
    engine: VoicevoxClient | None = None,
    rewriter: Rewriter | None = None,
    log: Log = print,
    on_progress: Progress | None = None,
    on_line_start: LineStart | None = None,
    cancel: threading.Event | None = None,
) -> JobSummary:
    """入力ファイルを読み、1行ずつ尺合わせして、出力フォルダに結果をまとめて書き出す"""
    script = load_script(input_path)
    lines = script.lines
    rec = Recorder()
    engine = TimedEngine(engine or VoicevoxClient(cfg.engine_url), rec)

    log(f"音声エンジンを確認しています（{cfg.engine_url}）...")
    log(f"  バージョン: {engine.version()}")
    if rewriter is None:
        rewriter = make_rewriter(cfg, log, rec.on_gemini_event)
    elif isinstance(rewriter, GeminiRewriter):
        rewriter._on_event = rec.on_gemini_event

    summary = JobSummary(output_dir=Path(output_dir), kind=script.kind)
    summary.timing = rec.total
    limit = edition.line_limit()
    if limit is not None and len(lines) > limit:
        summary.skipped_by_limit = len(lines) - limit
        log(f"※ 無料版は1回 {limit} 行までです。残り {summary.skipped_by_limit} 行は処理しません（製品版なら無制限）。")
        lines = lines[:limit]

    out = summary.output_dir
    out.mkdir(parents=True, exist_ok=True)
    settings = cfg.fit_settings()

    for i, line in enumerate(lines, start=1):
        if cancel is not None and cancel.is_set():
            summary.cancelled = True
            log("中止しました。ここまでの結果を書き出します。")
            break
        speaker = line.speaker_id if line.speaker_id is not None else cfg.default_speaker
        if on_line_start:
            on_line_start(i, len(lines), line)
        log(f"[{i}/{len(lines)}] 目標 {line.target_sec:.2f}秒: {line.text}")
        llm_error = None
        with rec.line() as stats:
            try:
                result = fit_text(line.text, speaker, line.target_sec, engine, rewriter, settings, log)
            except LLMError as e:
                # 要約だけ失敗した行は、要約なしでできるところまで合わせる
                # （APIキーの誤りは LLMError ではないので、ここでは捕まえず全体を止める）
                llm_error = e
                log(f"  ⚠ {e}")
                log("  この行は要約なしで、できるところまで合わせます。")
                result = fit_text(
                    line.text, speaker, line.target_sec, engine, None, settings, log,
                    no_rewrite_reason="AI の要約に失敗しました",
                )
            name = wav_filename(line, result)
            (out / name).write_bytes(result.wav.to_bytes())
        result.timing = stats
        log(f"  {stats.summary()}")
        if not result.ok:
            log(f"  ⚠ {result.message}")
        if llm_error is not None:
            result.message = f"{result.message or 'AI の要約に失敗しました'}（詳細: {llm_error}）"
        summary.results.append(result)
        summary.files.append(name)
        summary.lines.append(line)
        summary.speakers.append(speaker)
        if on_progress:
            on_progress(i, len(lines), line, result, name)

    if summary.results:
        t0 = time.perf_counter()
        write_outputs(summary, cfg, engine, log)
        save_sec = time.perf_counter() - t0
        t = rec.total
        log(f"処理時間の内訳: {t.summary()}、ファイル保存 {save_sec:.1f}秒")
        if t.ai_calls:
            used = "、".join(f"{m}×{n}" for m, n in t.models.items()) or "なし"
            log(f"  AI: 成功したモデル {used} / トークン 入力{t.in_tokens}・出力{t.out_tokens}・思考{t.thought_tokens}")
    c = summary.counts()
    log("完了: " + "、".join(f"{k} {v}件" for k, v in c.items() if v))
    log(f"出力先: {out}")
    return summary


def write_outputs(summary: JobSummary, cfg: AppConfig, engine, log: Log) -> None:
    """行ごとの WAV 以外の出力（レポート・字幕・まとめ音声・クレジット）を書き出す"""
    out, kind, lines, results = summary.output_dir, summary.kind, summary.lines, summary.results
    write_report(out / "report.csv", lines, results, summary.files)
    write_srt(out / "fitted.srt", lines, results)
    try:
        write_combined_wav(out / "combined.wav", lines, results)
    except ValueError as e:
        log(f"※ まとめWAVは作れませんでした: {e}")

    speaker_ids = set(summary.speakers)
    try:
        styles = {s.style_id: s.character for s in engine.speakers()}
        names = [styles.get(sid, f"話者ID {sid}") for sid in speaker_ids]
    except EngineError:
        names = [f"話者ID {sid}" for sid in speaker_ids]
    (out / "credits.txt").write_text(credit_text(cfg.credit_prefix, names), encoding="utf-8")
    note = "字幕のタイミングどおり" if kind == "srt" else "行の順に隙間なく"
    log(f"まとめ音声 combined.wav（{note}）、字幕 fitted.srt、結果一覧 report.csv、クレジット credits.txt を書き出しました。")


# ---------- あとから1行だけ直す（要約の確認画面から使う） ----------

def refit_line(
    summary: JobSummary, index: int, text: str, cfg: AppConfig, engine=None, log: Log = print,
) -> FitResult:
    """ユーザーが選んだ／書いた文で1行だけ作り直し、出力ファイルも更新する。AI には送らない。"""
    text = text.strip()
    if not text:
        raise TimeFitError("原稿が空です。")
    pos = summary.position(index)
    line, old = summary.lines[pos], summary.results[pos]
    engine = engine or VoicevoxClient(cfg.engine_url)
    settings = cfg.fit_settings()
    settings.allow_rewrite = True  # rewriter を渡さないので要約はしない。失敗理由の文言のため
    log(f"[{index}行目を作り直し] {text}")
    result = fit_text(
        text, summary.speakers[pos], line.target_sec, engine, None, settings, log,
        no_rewrite_reason="この文では長すぎます", original=old.original_text,
    )
    result.candidates = list(old.candidates)

    out = summary.output_dir
    name = wav_filename(line, result)
    if summary.files[pos] != name:
        (out / summary.files[pos]).unlink(missing_ok=True)
    (out / name).write_bytes(result.wav.to_bytes())
    summary.results[pos] = result
    summary.files[pos] = name
    summary.approved.add(index)
    write_outputs(summary, cfg, engine, log)
    return result


def suggest_candidates(summary: JobSummary, index: int, cfg: AppConfig, engine=None, log: Log = print) -> list[str]:
    """AI に別の案を出してもらい、これまでの案に足して返す"""
    if not cfg.gemini_api_key:
        raise ApiKeyMissingError("AI に案を出してもらうには、設定で Gemini の APIキーを登録してください。")
    pos = summary.position(index)
    line, old = summary.lines[pos], summary.results[pos]
    engine = engine or VoicevoxClient(cfg.engine_url)
    rewriter = gemini_rewriter(cfg, log)
    return add_candidates(old, rewriter, engine, summary.speakers[pos], line.target_sec, cfg)


def add_candidates(result: FitResult, rewriter: Rewriter, engine, speaker: int, target_sec: float, cfg: AppConfig) -> list[str]:
    settings = cfg.fit_settings()
    query = engine.audio_query(result.original_text, speaker)
    target_chars = rewrite_target_chars(result.original_text, query, target_sec, settings)
    avoid = result.candidates[-6:] or None
    new = rewriter.candidates(result.original_text, target_chars, avoid=avoid, n=settings.candidate_count)
    for c in new:
        if c not in result.candidates and c != result.original_text:
            result.candidates.append(c)
    return result.candidates


def estimate_text(text: str, speaker: int, target_sec: float, cfg: AppConfig, engine=None) -> dict:
    """合成せずに、その文がどのくらいの長さになり、収まるかを見積もる（入力中のプレビュー用）"""
    engine = engine or VoicevoxClient(cfg.engine_url, timeout=10)
    query = engine.audio_query(text, speaker)
    natural = estimate_duration(with_prosody(query, 1.0, 1.0))
    plan = plan_prosody(query, target_sec, cfg.fit_settings())
    return {
        "natural": natural,
        "fits": plan is not None,
        "pause": plan[0] if plan else None,
        "speed": plan[1] if plan else None,
        "chars": len(text),
    }
