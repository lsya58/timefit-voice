"""WAV(PCM) をメモリ上で扱う小さな関数群。一時ファイルは作らない。"""

import io
import wave
from dataclasses import dataclass


@dataclass(frozen=True)
class WavData:
    frames: bytes
    rate: int
    channels: int
    sampwidth: int

    @property
    def frame_size(self) -> int:
        return self.channels * self.sampwidth

    @property
    def n_frames(self) -> int:
        return len(self.frames) // self.frame_size

    @property
    def duration(self) -> float:
        return self.n_frames / self.rate

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(self.channels)
            w.setsampwidth(self.sampwidth)
            w.setframerate(self.rate)
            w.writeframes(self.frames)
        return buf.getvalue()


def parse_wav(data: bytes) -> WavData:
    with wave.open(io.BytesIO(data), "rb") as w:
        return WavData(
            frames=w.readframes(w.getnframes()),
            rate=w.getframerate(),
            channels=w.getnchannels(),
            sampwidth=w.getsampwidth(),
        )


def _silence_bytes(sampwidth: int, n_bytes: int) -> bytes:
    # 8bit PCM は符号なしなので無音は 0x80、それ以外は 0
    return (b"\x80" if sampwidth == 1 else b"\x00") * n_bytes


def silence(rate: int, channels: int, sampwidth: int, n_frames: int) -> WavData:
    return WavData(_silence_bytes(sampwidth, n_frames * channels * sampwidth), rate, channels, sampwidth)


def fit_length(wav: WavData, n_frames: int) -> WavData:
    """末尾に無音を足す/末尾を削って、ちょうど n_frames にする"""
    size = n_frames * wav.frame_size
    if len(wav.frames) >= size:
        frames = wav.frames[:size]
    else:
        frames = wav.frames + _silence_bytes(wav.sampwidth, size - len(wav.frames))
    return WavData(frames, wav.rate, wav.channels, wav.sampwidth)


def frames_for(seconds: float, rate: int) -> int:
    return max(0, round(seconds * rate))


def place_clips(clips: list[tuple[float, WavData]], total_sec: float) -> WavData:
    """各クリップを開始秒の位置に置いた1本のWAVを作る。重なった部分は後のクリップで上書き。"""
    if not clips:
        raise ValueError("クリップがありません")
    first = clips[0][1]
    fmt = (first.rate, first.channels, first.sampwidth)
    for _, c in clips:
        if (c.rate, c.channels, c.sampwidth) != fmt:
            raise ValueError("音声の形式（サンプリングレート等）が揃っていません")

    fs = first.frame_size
    total_frames = max(
        frames_for(total_sec, first.rate),
        max(frames_for(start, first.rate) + c.n_frames for start, c in clips),
    )
    buf = bytearray(_silence_bytes(first.sampwidth, total_frames * fs))
    for start, c in clips:
        offset = frames_for(start, first.rate) * fs
        buf[offset:offset + len(c.frames)] = c.frames
    return WavData(bytes(buf), *fmt)
