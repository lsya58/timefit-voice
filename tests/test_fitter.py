import pytest

from timefit.audio import parse_wav
from timefit.fitter import FitMode, FitSettings, FitStatus, fit_text

from .fakes import RATE, FakeEngine, FakeRewriter

# 1文字0.15秒。前後の無音0.2秒。
TEN_CHARS = "あいうえおかきくけこ"  # 等倍で 1.7 秒


def frames(sec):
    return round(sec * RATE)


def test_short_text_is_padded_to_exact_length():
    r = fit_text(TEN_CHARS, 3, 3.0, FakeEngine(), None)
    assert r.status == FitStatus.NATURAL
    assert r.wav.n_frames == frames(3.0)
    assert r.speed == 1.0


def test_within_mode_keeps_short_audio():
    r = fit_text(TEN_CHARS, 3, 3.0, FakeEngine(), None, FitSettings(mode=FitMode.WITHIN))
    assert r.status == FitStatus.NATURAL
    assert r.duration == pytest.approx(1.7, abs=0.001)


def test_pause_shrinking_is_used_before_speed():
    text = "あいうえお、かきくけこ"  # 1.7 + 間0.3 = 2.0 秒
    r = fit_text(text, 3, 1.9, FakeEngine(), None)  # 間を最大まで詰めると 1.88 秒
    assert r.status == FitStatus.PAUSE
    assert r.speed == 1.0
    assert 0.6 <= r.pause_scale < 1.0
    assert r.wav.n_frames == frames(1.9)


def test_speed_up_within_limit():
    r = fit_text(TEN_CHARS, 3, 1.5, FakeEngine(), None)  # 1.7/1.5 = 1.13倍
    assert r.status == FitStatus.SPEED
    assert 1.0 < r.speed <= 1.3
    assert r.wav.n_frames == frames(1.5)


def test_measured_length_is_corrected_when_estimate_is_off():
    engine = FakeEngine(bias=1.08)  # 実際は見積もりより 8% 長い
    r = fit_text(TEN_CHARS, 3, 1.5, engine, None)
    assert r.ok
    assert r.speed <= 1.3
    assert len(engine.synth_calls) >= 2  # 測り直して作り直している
    assert r.wav.n_frames == frames(1.5)


def test_no_pause_scale_support_falls_back_to_speed():
    text = "あいうえお、かきくけこ"
    engine = FakeEngine(pause_scale_supported=False)
    r = fit_text(text, 3, 1.85, engine, None)
    assert r.status == FitStatus.SPEED
    assert all("pauseLengthScale" not in q for q in engine.synth_calls)


def test_rewrite_when_too_long():
    text = "あ" * 40  # 6.2 秒
    rewriter = FakeRewriter()
    r = fit_text(text, 3, 3.0, FakeEngine(), rewriter)
    assert r.status == FitStatus.REWRITE
    assert r.rewrite_attempts >= 1
    assert len(r.text) < len(text)
    assert r.speed <= 1.3
    assert r.wav.n_frames == frames(3.0)
    assert rewriter.calls[0][0] == text  # 要約は必ず元の原稿から


def test_without_rewriter_reports_failure_with_best_effort_audio():
    r = fit_text("あ" * 40, 3, 3.0, FakeEngine(), None)
    assert r.status == FitStatus.FAILED
    assert not r.ok
    assert r.duration > 3.0  # 削って意味を壊さない
    assert r.speed == pytest.approx(1.3)
    assert "長いです" in r.message


def test_rewriter_that_never_shortens_stops_after_max_attempts():
    rewriter = FakeRewriter(shrink=False)
    r = fit_text("あ" * 40, 3, 3.0, FakeEngine(), rewriter, FitSettings(max_rewrite_attempts=2))
    assert r.status == FitStatus.FAILED
    assert len(rewriter.calls) == 2
    assert rewriter.calls[1][2] is not None  # 2回目は前回案を渡して、さらに短くさせる


def test_speed_never_exceeds_limit():
    engine = FakeEngine(bias=1.2)
    r = fit_text("あ" * 12, 3, 1.75, engine, FakeRewriter(), FitSettings(max_speed=1.25))
    assert all(q["speedScale"] <= 1.25 + 1e-9 for q in engine.synth_calls)
    assert r.speed <= 1.25 + 1e-9


def test_output_is_valid_wav():
    r = fit_text(TEN_CHARS, 3, 2.0, FakeEngine(), None)
    again = parse_wav(r.wav.to_bytes())
    assert again.n_frames == frames(2.0)
    assert again.rate == RATE
