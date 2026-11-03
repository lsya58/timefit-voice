"""処理時間の計測（どの工程に時間がかかっているかを、行ごと・全体で記録する）

原稿の本文や APIキーは記録しない。
"""

import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Stats:
    total_sec: float = 0.0
    ai_sec: float = 0.0          # Gemini との通信（失敗した呼び出しも含む）
    ai_calls: int = 0
    ai_results: Counter = field(default_factory=Counter)  # ok / 503 / 429-day / timeout など
    wait_sec: float = 0.0        # 再試行前の待ち時間
    synth_sec: float = 0.0       # VOICEVOX の音声合成
    synth_calls: int = 0
    query_sec: float = 0.0       # VOICEVOX の解析（audio_query）
    query_calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    thought_tokens: int = 0
    models: Counter = field(default_factory=Counter)  # 成功したモデル

    def errors_text(self) -> str:
        errs = {k: v for k, v in self.ai_results.items() if k != "ok"}
        return " ".join(f"{k}×{v}" for k, v in errs.items())

    def summary(self) -> str:
        parts = []
        if self.ai_calls:
            ai = f"AI {self.ai_sec:.1f}秒×{self.ai_calls}回"
            if self.wait_sec:
                ai += f"（待ち {self.wait_sec:.1f}秒）"
            if self.errors_text():
                ai += f"［{self.errors_text()}］"
            parts.append(ai)
        parts.append(f"合成 {self.synth_sec:.1f}秒×{self.synth_calls}回")
        parts.append(f"解析 {self.query_sec:.1f}秒×{self.query_calls}回")
        return f"⏱ {self.total_sec:.1f}秒（" + " / ".join(parts) + "）"

    def as_dict(self) -> dict:
        return {
            "total": round(self.total_sec, 2), "ai": round(self.ai_sec, 2), "aiCalls": self.ai_calls,
            "wait": round(self.wait_sec, 2), "aiErrors": self.errors_text(),
            "synth": round(self.synth_sec, 2), "synthCalls": self.synth_calls,
            "query": round(self.query_sec, 2), "queryCalls": self.query_calls,
        }


class Recorder:
    """行ごとの Stats と全体の Stats を同時に積み上げる"""

    def __init__(self):
        self.total = Stats()
        self.current: Stats | None = None

    def _targets(self):
        return [s for s in (self.current, self.total) if s is not None]

    @contextmanager
    def line(self):
        self.current = Stats()
        t0 = time.perf_counter()
        try:
            yield self.current
        finally:
            sec = time.perf_counter() - t0
            self.current.total_sec = sec
            self.total.total_sec += sec
            self.current = None

    def add_engine(self, kind: str, sec: float) -> None:
        for s in self._targets():
            if kind == "synthesis":
                s.synth_sec += sec
                s.synth_calls += 1
            else:
                s.query_sec += sec
                s.query_calls += 1

    def on_gemini_event(self, ev: dict) -> None:
        for s in self._targets():
            if ev.get("type") == "wait":
                s.wait_sec += ev.get("sec", 0.0)
                continue
            s.ai_sec += ev.get("sec", 0.0)
            s.ai_calls += 1
            s.ai_results[ev.get("result", "?")] += 1
            if ev.get("result") == "ok":
                s.models[ev.get("model", "?")] += 1
                s.in_tokens += ev.get("in_tokens", 0)
                s.out_tokens += ev.get("out_tokens", 0)
                s.thought_tokens += ev.get("thought_tokens", 0)


class TimedEngine:
    """音声エンジンの呼び出し時間を測る包み"""

    def __init__(self, engine, recorder: Recorder):
        self._engine = engine
        self._rec = recorder

    def audio_query(self, text, speaker):
        t0 = time.perf_counter()
        try:
            return self._engine.audio_query(text, speaker)
        finally:
            self._rec.add_engine("audio_query", time.perf_counter() - t0)

    def synthesis(self, query, speaker):
        t0 = time.perf_counter()
        try:
            return self._engine.synthesis(query, speaker)
        finally:
            self._rec.add_engine("synthesis", time.perf_counter() - t0)

    def __getattr__(self, name):
        return getattr(self._engine, name)
