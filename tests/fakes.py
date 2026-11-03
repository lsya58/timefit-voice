"""テスト用の偽 VOICEVOX / 偽 LLM。ネットワークには一切つながない。"""

from timefit.audio import WavData, frames_for
from timefit.engine import SpeakerStyle, estimate_duration

RATE = 24000
PAUSE_CHARS = "、。！？"


class FakeEngine:
    """1文字=1モーラ（子音0.05秒+母音0.1秒）、句読点=0.3秒の間 として音声の長さを決める。

    bias を 1 より大きくすると、実際の音声が見積もりより長くなる（見積もりのずれの再現用）。
    """

    def __init__(self, bias: float = 1.0, pause_scale_supported: bool = True):
        self.bias = bias
        self.pause_scale_supported = pause_scale_supported
        self.synth_calls: list[dict] = []

    def version(self):
        return "fake-1.0"

    def speakers(self):
        return [SpeakerStyle("ずんだもん", "ノーマル", 3), SpeakerStyle("四国めたん", "ノーマル", 2)]

    def audio_query(self, text, speaker):
        phrases, moras = [], []
        for ch in text:
            if ch in PAUSE_CHARS:
                if moras:
                    phrases.append({"moras": moras, "pause_mora": {"vowel_length": 0.3}})
                    moras = []
            else:
                moras.append({"consonant_length": 0.05, "vowel_length": 0.1})
        if moras:
            phrases.append({"moras": moras, "pause_mora": None})
        q = {
            "accent_phrases": phrases,
            "speedScale": 1.0,
            "prePhonemeLength": 0.1,
            "postPhonemeLength": 0.1,
            "pauseLength": None,
        }
        if self.pause_scale_supported:
            q["pauseLengthScale"] = 1.0
        return q

    def synthesis(self, query, speaker):
        self.synth_calls.append(query)
        sec = estimate_duration(query) * self.bias
        # 中身は小さな非ゼロ値（無音と区別するため）
        return WavData(b"\x01\x00" * frames_for(sec, RATE), RATE, 1, 2).to_bytes()


class FakeRewriter:
    """指示された文字数に切り詰めるだけの偽の要約。

    shrink=False のときは1文字しか削らない（何度要約しても収まらない状況の再現用）。
    """

    def __init__(self, shrink: bool = True):
        self.shrink = shrink
        self.calls: list[dict] = []

    def candidates(self, text, target_chars, previous=None, avoid=None, n=3):
        self.calls.append({"text": text, "target_chars": target_chars, "previous": previous, "avoid": avoid, "n": n})
        if not self.shrink:
            base = previous or text
            return [base[:-1]]
        out = [text[:target_chars], text[:max(1, int(target_chars * 0.8))], "短い案"]
        if avoid:
            out = [f"{o}ね" for o in out]
        return out[:n]
