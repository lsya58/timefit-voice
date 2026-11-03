"""尺合わせのロジック

段階:
  1. 等倍で収まるか
  2. 句読点の間を詰めれば収まるか（対応エンジンのみ）
  3. 話速を上げれば収まるか（上限 max_speed まで）
  4. LLM で要約して 1〜3 をやり直す（max_rewrite_attempts 回まで）
最後に、足りない分は無音を足し、わずかなはみ出しは末尾の無音を削って、目標の長さちょうどにする。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .audio import WavData, fit_length, frames_for, parse_wav
from .engine import estimate_duration, supports_pause_scale, with_prosody
from .llm import Rewriter

Log = Callable[[str], None]


class FitMode(str, Enum):
    EXACT = "exact"    # 目標の長さちょうど（足りない分は無音）
    WITHIN = "within"  # 目標以内（短ければそのまま）


class FitStatus(str, Enum):
    NATURAL = "等倍"
    PAUSE = "間を調整"
    SPEED = "話速調整"
    REWRITE = "要約"
    FAILED = "収まらず"


@dataclass
class FitSettings:
    max_speed: float = 1.3
    min_pause_scale: float = 0.6
    mode: FitMode = FitMode.EXACT
    trim_tolerance_ms: int = 80  # 末尾をこの長さまでなら削ってよい（VOICEVOX は末尾に 0.1 秒の無音がある）
    max_rewrite_attempts: int = 3
    allow_rewrite: bool = True
    # 要約の目標: 等倍でこの割合の長さになるよう文字数を決める（少し余裕を持たせる）
    rewrite_target_ratio: float = 0.95


@dataclass
class FitResult:
    wav: WavData
    original_text: str
    text: str
    target_sec: float
    speed: float
    pause_scale: float
    status: FitStatus
    rewrite_attempts: int
    message: str = ""
    speech_sec: float = 0.0  # 無音を足す前の、実際にしゃべっている部分の長さ

    @property
    def duration(self) -> float:
        return self.wav.duration

    @property
    def ok(self) -> bool:
        return self.status != FitStatus.FAILED


@dataclass
class _Attempt:
    wav: WavData
    speed: float
    pause_scale: float
    fits: bool


def plan_prosody(query: dict, target_sec: float, s: FitSettings) -> tuple[float, float] | None:
    """見積もりから (pause_scale, speed) を決める。上限の話速でも収まらないなら None。"""
    natural = estimate_duration(with_prosody(query, 1.0, 1.0))
    if natural <= target_sec:
        return 1.0, 1.0

    pause_scale = 1.0
    base = natural
    if supports_pause_scale(query) and s.min_pause_scale < 1.0:
        shortest = estimate_duration(with_prosody(query, 1.0, s.min_pause_scale))
        if shortest <= target_sec:
            # 間の長さに対して時間は比例して変わるので、ちょうど収まる割合を逆算する
            pause_total = (natural - shortest) / (1.0 - s.min_pause_scale)
            pause_scale = 1.0 - (natural - target_sec) / pause_total
            return max(s.min_pause_scale, min(1.0, pause_scale)), 1.0
        pause_scale = s.min_pause_scale
        base = shortest

    speed = base / target_sec
    if speed <= s.max_speed:
        return pause_scale, speed
    return None


def _synthesize_to_fit(engine, query, speaker, target_sec, pause_scale, speed, s: FitSettings) -> _Attempt:
    """実際に合成して長さを測り、はみ出していれば話速を少し上げて作り直す"""
    tol = s.trim_tolerance_ms / 1000
    wav = None
    for _ in range(4):
        wav = parse_wav(engine.synthesis(with_prosody(query, speed, pause_scale), speaker))
        if wav.duration <= target_sec + tol:
            return _Attempt(wav, speed, pause_scale, True)
        if speed >= s.max_speed:
            break
        speed = min(s.max_speed, speed * wav.duration / target_sec * 1.002)
    return _Attempt(wav, speed, pause_scale, False)


def _finalize(wav: WavData, target_sec: float, s: FitSettings) -> WavData:
    target_frames = frames_for(target_sec, wav.rate)
    if s.mode == FitMode.EXACT or wav.n_frames > target_frames:
        return fit_length(wav, target_frames)
    return wav


def _status_for(attempt: _Attempt, rewritten: bool) -> FitStatus:
    if rewritten:
        return FitStatus.REWRITE
    if attempt.speed > 1.0:
        return FitStatus.SPEED
    if attempt.pause_scale < 1.0:
        return FitStatus.PAUSE
    return FitStatus.NATURAL


def fit_text(
    text: str,
    speaker: int,
    target_sec: float,
    engine,
    rewriter: Rewriter | None,
    settings: FitSettings | None = None,
    log: Log = lambda _m: None,
    no_rewrite_reason: str | None = None,
) -> FitResult:
    s = settings or FitSettings()
    if target_sec <= 0:
        raise ValueError("目標秒数は 0 より大きくしてください")

    original = text.strip()
    current = original
    attempts = 0
    best: tuple[str, _Attempt] | None = None  # 収まらなかった中で一番短いもの

    while True:
        query = engine.audio_query(current, speaker)
        plan = plan_prosody(query, target_sec, s)
        if plan is not None:
            pause_scale, speed = plan
            att = _synthesize_to_fit(engine, query, speaker, target_sec, pause_scale, speed, s)
        else:
            # 上限まで詰めた状態で作っておく（最終的に収まらなかったときに使う）
            min_pause = s.min_pause_scale if supports_pause_scale(query) else 1.0
            att = _Attempt(
                parse_wav(engine.synthesis(with_prosody(query, s.max_speed, min_pause), speaker)),
                s.max_speed, min_pause, False,
            )

        if att.fits:
            status = _status_for(att, attempts > 0)
            log(f"  → {status.value}（話速 {att.speed:.2f}倍、間 {att.pause_scale:.2f}倍）")
            return FitResult(
                wav=_finalize(att.wav, target_sec, s),
                original_text=original, text=current, target_sec=target_sec,
                speed=att.speed, pause_scale=att.pause_scale, status=status, rewrite_attempts=attempts,
                speech_sec=min(att.wav.duration, target_sec),
            )

        if best is None or att.wav.duration < best[1].wav.duration:
            best = (current, att)

        if not s.allow_rewrite or rewriter is None:
            if not s.allow_rewrite:
                reason = "要約がオフです"
            else:
                reason = no_rewrite_reason or "APIキー未設定のため要約できません"
            return _failed(original, best, target_sec, attempts, reason)
        if attempts >= s.max_rewrite_attempts:
            return _failed(original, best, target_sec, attempts, f"{attempts}回要約しても収まりませんでした")

        attempts += 1
        natural = estimate_duration(with_prosody(query, 1.0, 1.0))
        ratio = target_sec * s.rewrite_target_ratio / natural
        target_chars = max(5, int(len(current) * ratio))
        log(f"  → 話速{s.max_speed}倍でも収まらないため要約します（{attempts}回目、目標 {target_chars}文字）")
        previous = current if attempts > 1 else None
        current = rewriter.rewrite(original, target_chars, previous)
        log(f"     要約結果: {current}")


def _failed(original, best, target_sec, attempts, reason) -> FitResult:
    text, att = best
    over = att.wav.duration - target_sec
    return FitResult(
        wav=att.wav, original_text=original, text=text, target_sec=target_sec,
        speed=att.speed, pause_scale=att.pause_scale, status=FitStatus.FAILED,
        rewrite_attempts=attempts, speech_sec=att.wav.duration,
        message=f"{reason}。目標より {over:.2f}秒 長いです（原稿を短くしてください）",
    )
