"""尺合わせのロジック

段階:
  1. 等倍で収まるか
  2. 句読点の間を詰めれば収まるか（対応エンジンのみ）
  3. 話速を上げれば収まるか（上限 max_speed まで）
  4. LLM で要約して 1〜3 をやり直す（max_rewrite_attempts 回まで）
最後に、足りない分は無音を足し、わずかなはみ出しは末尾の無音を削って、目標の長さちょうどにする。
"""

from dataclasses import dataclass, field
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
    candidate_count: int = 3  # 1回の要約で AI に出してもらう案の数


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
    candidates: list[str] = field(default_factory=list)  # AI が出した案（採用したものも含む）

    @property
    def duration(self) -> float:
        return self.wav.duration

    @property
    def ok(self) -> bool:
        return self.status != FitStatus.FAILED

    @property
    def rewritten(self) -> bool:
        return self.text != self.original_text


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


def rewrite_target_chars(text: str, query: dict, target_sec: float, s: FitSettings) -> int:
    """等倍で目標の尺（の rewrite_target_ratio 倍）になる文字数の目安"""
    natural = estimate_duration(with_prosody(query, 1.0, 1.0))
    ratio = target_sec * s.rewrite_target_ratio / natural if natural > 0 else 1.0
    return max(5, int(len(text) * ratio))


@dataclass
class _Candidate:
    text: str
    query: dict
    natural: float               # 等倍での見積もり秒数
    plan: tuple[float, float] | None


def _evaluate(engine, texts: list[str], speaker, target_sec, s) -> list[_Candidate]:
    out = []
    for t in texts:
        q = engine.audio_query(t, speaker)
        out.append(_Candidate(t, q, estimate_duration(with_prosody(q, 1.0, 1.0)), plan_prosody(q, target_sec, s)))
    # 収まる案を先に、その中では情報が多い（長い）順に試す
    return sorted(out, key=lambda c: (c.plan is None, -c.natural))


def fit_text(
    text: str,
    speaker: int,
    target_sec: float,
    engine,
    rewriter: Rewriter | None,
    settings: FitSettings | None = None,
    log: Log = lambda _m: None,
    no_rewrite_reason: str | None = None,
    original: str | None = None,
) -> FitResult:
    """text を target_sec に合わせる。

    original を渡すと「元の原稿」として扱う（ユーザーが選び直した文で作り直すとき用）。
    """
    s = settings or FitSettings()
    if target_sec <= 0:
        raise ValueError("目標秒数は 0 より大きくしてください")

    original = (original or text).strip()
    pending = [text.strip()]
    suggestions: list[str] = []   # AI が出した案（確認画面で選び直せるように全部残す）
    attempts = 0
    shortest: _Candidate | None = None

    while True:
        for cand in _evaluate(engine, pending, speaker, target_sec, s):
            if shortest is None or cand.natural < shortest.natural:
                shortest = cand
            if cand.plan is None:
                continue
            att = _synthesize_to_fit(engine, cand.query, speaker, target_sec, *cand.plan, s)
            if att.fits:
                status = _status_for(att, cand.text != original)
                log(f"  → {status.value}（話速 {att.speed:.2f}倍、間 {att.pause_scale:.2f}倍）")
                return FitResult(
                    wav=_finalize(att.wav, target_sec, s),
                    original_text=original, text=cand.text, target_sec=target_sec,
                    speed=att.speed, pause_scale=att.pause_scale, status=status, rewrite_attempts=attempts,
                    speech_sec=min(att.wav.duration, target_sec), candidates=suggestions,
                )

        if not s.allow_rewrite or rewriter is None:
            if not s.allow_rewrite:
                reason = "要約がオフです"
            else:
                reason = no_rewrite_reason or "APIキー未設定のため要約できません"
            return _failed(engine, speaker, original, shortest, target_sec, attempts, reason, suggestions, s)
        if attempts >= s.max_rewrite_attempts:
            reason = f"{attempts}回要約しても収まりませんでした"
            return _failed(engine, speaker, original, shortest, target_sec, attempts, reason, suggestions, s)

        attempts += 1
        target_chars = rewrite_target_chars(shortest.text, shortest.query, target_sec, s)
        log(f"  → 話速{s.max_speed}倍でも収まらないため要約します（{attempts}回目、目標 {target_chars}文字）")
        previous = shortest.text if shortest.text != original else None
        pending = [c for c in rewriter.candidates(original, target_chars, previous, n=s.candidate_count)
                   if c not in suggestions]
        if not pending:
            return _failed(engine, speaker, original, shortest, target_sec, attempts,
                           "AI が新しい案を出せませんでした", suggestions, s)
        suggestions.extend(pending)
        for i, c in enumerate(pending, start=1):
            log(f"     案{i}: {c}")


def _failed(engine, speaker, original, shortest: _Candidate, target_sec, attempts, reason, suggestions, s) -> FitResult:
    """収まらなかったときは、一番短い文を上限まで詰めて作る（削って意味を壊すことはしない）"""
    min_pause = s.min_pause_scale if supports_pause_scale(shortest.query) else 1.0
    wav = parse_wav(engine.synthesis(with_prosody(shortest.query, s.max_speed, min_pause), speaker))
    over = wav.duration - target_sec
    return FitResult(
        wav=wav, original_text=original, text=shortest.text, target_sec=target_sec,
        speed=s.max_speed, pause_scale=min_pause, status=FitStatus.FAILED,
        rewrite_attempts=attempts, speech_sec=wav.duration, candidates=suggestions,
        message=f"{reason}。目標より {over:.2f}秒 長いです（原稿を短くしてください）",
    )
