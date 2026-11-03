import csv

import pytest
import requests

from timefit import batch, edition
from timefit.audio import WavData, fit_length, parse_wav, place_clips
from timefit.config import AppConfig, load_config, save_config
from timefit.engine import VoicevoxClient
from timefit.errors import ApiKeyInvalidError, ConfigError, EngineNotRunningError, InputFileError, LLMError
from timefit.llm import GeminiRewriter, parse_candidates, sanitize_output

from .fakes import RATE, FakeEngine, FakeRewriter


# ---------- 音声 ----------

def test_fit_length_pads_and_trims():
    w = WavData(b"\x01\x00" * 100, RATE, 1, 2)
    assert fit_length(w, 150).n_frames == 150
    assert fit_length(w, 150).frames[200:] == b"\x00" * 100
    assert fit_length(w, 40).n_frames == 40


def test_place_clips_positions():
    a = WavData(b"\x01\x00" * 10, 100, 1, 2)
    out = place_clips([(0.0, a), (0.5, a)], 1.0)
    assert out.n_frames == 100
    assert out.frames[50 * 2:60 * 2] == a.frames
    assert out.frames[20 * 2:50 * 2] == b"\x00" * 60


def test_padded_clip_is_placed_once_at_subtitle_start():
    clip = WavData(b"\x01\x00" * 20 + b"\x00\x00" * 80, 100, 1, 2)
    out = place_clips([(2.0, clip)], 4.0)
    assert out.frames[:200 * 2] == b"\x00" * 200 * 2
    assert out.frames[200 * 2:220 * 2] == b"\x01\x00" * 20
    assert out.frames[220 * 2:] == b"\x00" * (400 - 220) * 2


def test_adjacent_clips_have_no_extra_gap():
    a = WavData(b"\x01\x00" * 50, 100, 1, 2)
    b = WavData(b"\x02\x00" * 50, 100, 1, 2)
    out = place_clips([(0.0, a), (0.5, b)], 1.0)
    assert out.frames[:50 * 2] == a.frames
    assert out.frames[50 * 2:] == b.frames


# ---------- 一括処理 ----------

SRT = """1
00:00:00,500 --> 00:00:02,500
あいうえおかきくけこ

2
00:00:03,000 --> 00:00:05,000
あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほ
"""


def test_run_job_writes_all_outputs(tmp_path):
    src = tmp_path / "script.srt"
    src.write_text(SRT, encoding="utf-8")
    out = tmp_path / "out"
    logs = []
    summary = batch.run_job(src, out, AppConfig(), engine=FakeEngine(), rewriter=FakeRewriter(), log=logs.append)

    assert len(summary.results) == 2
    assert summary.failed == 0
    assert summary.results[1].rewrite_attempts >= 1
    names = {p.name for p in out.iterdir()}
    assert {"report.csv", "fitted.srt", "combined.wav", "credits.txt"} <= names
    assert len([n for n in names if n.startswith("00")]) == 2
    combined = parse_wav((out / "combined.wav").read_bytes())
    assert combined.duration == pytest.approx(5.0, abs=0.001)
    assert "VOICEVOX:ずんだもん" in (out / "credits.txt").read_text(encoding="utf-8")
    assert "要約" in (out / "report.csv").read_text(encoding="utf-8-sig")


def test_report_distinguishes_padding_from_subtitle_gap(tmp_path):
    src = tmp_path / "gaps.srt"
    src.write_text("""1
00:00:01,000 --> 00:00:11,000
あいう

2
00:00:16,000 --> 00:00:18,000
えお
""", encoding="utf-8")
    out = tmp_path / "out"
    logs = []
    summary = batch.run_job(src, out, AppConfig(), engine=FakeEngine(), rewriter=None, log=logs.append)
    assert summary.quality_warnings == 1
    with (out / "report.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert float(rows[0]["実発話秒数"]) == pytest.approx(0.65, abs=0.001)
    assert float(rows[0]["追加無音秒数"]) == pytest.approx(9.35, abs=0.001)
    assert float(rows[0]["次字幕までの空白秒数"]) == pytest.approx(5.0)
    assert "発話後" in rows[0]["品質警告"]
    assert any("長い末尾無音" in m for m in logs)


def test_free_edition_limits_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(edition, "EDITION", "free")
    src = tmp_path / "s.csv"
    src.write_text("\n".join(f"3,2,テスト{i}" for i in range(15)), encoding="utf-8")
    summary = batch.run_job(src, tmp_path / "o", AppConfig(), engine=FakeEngine(), rewriter=None, log=lambda m: None)
    assert len(summary.results) == edition.FREE_LINE_LIMIT
    assert summary.skipped_by_limit == 5


def test_llm_error_on_one_line_does_not_stop_job(tmp_path):
    class Broken:
        def candidates(self, *a, **k):
            raise LLMError("上限に達しました。")

    src = tmp_path / "s.csv"
    src.write_text("3,1," + "あ" * 40 + "\n3,3,あいう\n", encoding="utf-8")
    summary = batch.run_job(src, tmp_path / "o", AppConfig(), engine=FakeEngine(), rewriter=Broken(), log=lambda m: None)
    assert [r.ok for r in summary.results] == [False, True]
    assert "上限" in summary.results[0].message
    assert "APIキー未設定" not in summary.results[0].message
    assert "要約に失敗" in summary.results[0].message


def test_cancel_stops_early(tmp_path):
    import threading

    ev = threading.Event()
    ev.set()
    src = tmp_path / "s.csv"
    src.write_text("3,2,あ\n3,2,い\n", encoding="utf-8")
    summary = batch.run_job(src, tmp_path / "o", AppConfig(), engine=FakeEngine(), log=lambda m: None, cancel=ev)
    assert summary.cancelled and summary.results == []


# ---------- エンジン接続 ----------

def test_engine_not_running_message():
    class Down:
        def request(self, *a, **k):
            raise requests.ConnectionError()

    with pytest.raises(EngineNotRunningError, match="VOICEVOX を起動"):
        VoicevoxClient(session=Down()).version()


# ---------- LLM ----------

def test_parse_candidates():
    assert parse_candidates('["案A", "「案B」", "案A", ""]') == ["案A", "案B"]
    assert parse_candidates('```json\n{"candidates": ["x", "y"]}\n```') == ["x", "y"]
    assert parse_candidates("JSONではない1案") == ["JSONではない1案"]


def test_sanitize_output():
    assert sanitize_output("要約：「江戸幕府が開かれた」") == "江戸幕府が開かれた"
    assert sanitize_output("```\n一行目\n二行目\n```") == "一行目。二行目"
    assert sanitize_output(None) == ""


class _FakeModels:
    def __init__(self, errors_to_raise, text="短くした原稿"):
        self.errors = list(errors_to_raise)
        self.text = text
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)

        class R:
            text = self.text
        return R()


class _FakeClient:
    def __init__(self, models):
        self.models = models


def _api_error(code, message="err"):
    from google.genai import errors

    return errors.ClientError(code, {"error": {"code": code, "message": message, "status": "X"}})


def _quota_error(daily=True, delay="8365s"):
    from google.genai import errors

    quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if daily else "GenerateRequestsPerMinute"
    return errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
    ]}})


class _RoutingModels:
    """モデル名ごとに、投げるエラーの列を決められる偽の Gemini"""

    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls = []
        self.configs = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        self.configs.append(config)
        queue = self.plan.get(model, [])
        if queue:
            raise queue.pop(0)

        class R:
            text = '["短くした原稿"]'
            usage_metadata = None
        return R()


def _rw(plan, fallbacks=("lite",), **kw):
    models = _RoutingModels(plan)
    sleeps, events = [], []
    rw = GeminiRewriter("key", "main", fallback_models=list(fallbacks), sleep=sleeps.append,
                        client=_FakeClient(models), on_event=events.append, **kw)
    return rw, models, sleeps, events


def test_gemini_retries_once_when_busy():
    rw, models, sleeps, _ = _rw({"main": [_api_error(503)]})
    assert rw.candidates("元の原稿", 5) == ["短くした原稿"]
    assert models.calls == ["main", "main"] and sleeps == [1.5]


def test_gemini_switches_to_fallback_model_when_busy():
    logs = []
    rw, models, _, _ = _rw({"main": [_api_error(503)] * 5}, log=logs.append)
    assert rw.candidates("元の原稿", 5) == ["短くした原稿"]
    assert models.calls == ["main", "main", "lite"]
    assert any("lite に切り替え" in m for m in logs)
    # 混雑したモデルはしばらく休ませる → 次の行ではいきなり予備を使う
    rw.candidates("次の原稿", 5)
    assert models.calls[-1] == "lite" and models.calls.count("main") == 2


def test_daily_quota_switches_immediately_without_waiting():
    rw, models, sleeps, events = _rw({"main": [_quota_error(daily=True)]})
    assert rw.candidates("元の原稿", 5) == ["短くした原稿"]
    assert models.calls == ["main", "lite"]
    assert sleeps == []  # 1日の上限は待っても無駄なので待たない
    assert [e["result"] for e in events if e["type"] == "gemini"] == ["429-day", "ok"]
    rw.candidates("次", 5)
    assert models.calls.count("main") == 1  # 回復するまで上限のモデルには問い合わせない


def test_short_rate_limit_waits_the_suggested_delay():
    rw, models, sleeps, _ = _rw({"main": [_quota_error(daily=False, delay="3s")]})
    assert rw.candidates("元の原稿", 5) == ["短くした原稿"]
    assert models.calls == ["main", "main"] and sleeps == [3.0]


def test_unsupported_thinking_level_is_dropped_for_that_model():
    err = _api_error(400, "Thinking level MINIMAL is not supported for this model.")
    rw, models, _, _ = _rw({"main": [err]}, thinking_level="minimal")
    assert rw.candidates("元の原稿", 5) == ["短くした原稿"]
    assert models.calls == ["main", "main"]
    assert models.configs[0].thinking_config is not None and models.configs[1].thinking_config is None


def test_timeout_switches_to_fallback():
    class ReadTimeout(Exception):
        pass

    rw, models, _, events = _rw({"main": [ReadTimeout("timed out")]})
    assert rw.candidates("元の原稿", 5) == ["短くした原稿"]
    assert models.calls == ["main", "lite"]
    assert events[0]["result"] == "timeout"


def test_gemini_invalid_key():
    rw, _, _, _ = _rw({"main": [_api_error(400, "API key not valid. Please pass a valid API key.")]})
    with pytest.raises(ApiKeyInvalidError):
        rw.candidates("元の原稿", 5)


def test_gemini_all_models_busy_raises_llm_error():
    rw, _, _, _ = _rw({"main": [_api_error(503)] * 3, "lite": [_api_error(503)] * 3})
    with pytest.raises(LLMError, match="混雑"):
        rw.candidates("元の原稿", 5)


def test_run_job_records_timing(tmp_path):
    src = tmp_path / "script.srt"
    src.write_text(SRT, encoding="utf-8")
    logs = []
    summary = batch.run_job(src, tmp_path / "out", AppConfig(), engine=FakeEngine(), rewriter=FakeRewriter(), log=logs.append)
    t = summary.results[1].timing
    assert t.synth_calls >= 1 and t.query_calls >= 1
    assert summary.timing.synth_calls >= t.synth_calls
    assert any(m.startswith("処理時間の内訳") for m in logs)
    header = (tmp_path / "out" / "report.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "AI呼び出し" in header and "合成回数" in header


# ---------- 設定 ----------

def test_config_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # .env を読まないように
    path = tmp_path / "config.json"
    cfg = AppConfig(gemini_api_key="abc", max_speed=1.25)
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.gemini_api_key == "abc" and loaded.max_speed == 1.25

    path.write_text('{"max_speed": 5}', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


# ---------- 要約の確認・作り直し ----------

def _job(tmp_path):
    src = tmp_path / "script.srt"
    src.write_text(SRT, encoding="utf-8")
    rw = FakeRewriter()
    summary = batch.run_job(src, tmp_path / "out", AppConfig(), engine=FakeEngine(), rewriter=rw, log=lambda m: None)
    return summary, rw


def test_refit_line_with_chosen_candidate_updates_outputs(tmp_path):
    summary, _ = _job(tmp_path)
    old = summary.results[1]
    assert old.rewritten and len(old.candidates) >= 2
    other = next(c for c in old.candidates if c != old.text)
    old_file = summary.files[1]

    r = batch.refit_line(summary, 2, other, AppConfig(), engine=FakeEngine(), log=lambda m: None)
    assert r.ok and r.text == other and r.original_text == old.original_text
    assert r.candidates == old.candidates  # 選び直しても案は残る
    assert 2 in summary.approved
    out = summary.output_dir
    assert (out / summary.files[1]).exists()
    if summary.files[1] != old_file:
        assert not (out / old_file).exists()
    assert other in (out / "fitted.srt").read_text(encoding="utf-8")


def test_refit_line_too_long_reports_failure(tmp_path):
    summary, _ = _job(tmp_path)
    r = batch.refit_line(summary, 2, "あ" * 60, AppConfig(), engine=FakeEngine(), log=lambda m: None)
    assert r.status.value == "収まらず" and "この文では長すぎます" in r.message


def test_refit_line_recalculates_padding_warning_and_outputs(tmp_path):
    src = tmp_path / "long-slot.srt"
    src.write_text("""1
00:00:00,000 --> 00:00:10,000
あいうえお
""", encoding="utf-8")
    cfg = AppConfig()
    summary = batch.run_job(src, tmp_path / "out", cfg, engine=FakeEngine(), rewriter=None, log=lambda m: None)
    result = batch.refit_line(summary, 1, "あ", cfg, engine=FakeEngine(), log=lambda m: None)
    assert result.quality_warning
    assert summary.quality_warnings == 1
    report = (tmp_path / "out" / "report.csv").read_text(encoding="utf-8-sig")
    assert "発話後に" in report
    combined = parse_wav((tmp_path / "out" / "combined.wav").read_bytes())
    assert combined.duration == pytest.approx(10.0, abs=0.001)


def test_add_candidates_asks_for_different_ones(tmp_path):
    summary, rw = _job(tmp_path)
    result = summary.results[1]
    before = list(result.candidates)
    after = batch.add_candidates(result, rw, FakeEngine(), 3, 2.0, AppConfig())
    assert after[:len(before)] == before and len(after) > len(before)
    assert rw.calls[-1]["avoid"]  # 既存の案を渡して、違う案を頼んでいる


def test_estimate_text():
    cfg = AppConfig()
    short = batch.estimate_text("あいう", 3, 2.0, cfg, engine=FakeEngine())
    long = batch.estimate_text("あ" * 60, 3, 2.0, cfg, engine=FakeEngine())
    assert short["fits"] and short["speed"] == 1.0
    assert not long["fits"] and long["natural"] > 2.0


def test_old_default_models_are_migrated_but_user_choices_are_kept(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.json"
    path.write_text('{"gemini_model": "gemini-3.8-flash", "gemini_fallback_model": "gemini-flash-lite-latest"}', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.gemini_model == "gemini-flash-lite-latest" and cfg.gemini_fallback_model == "gemini-3.8-flash"

    path.write_text('{"gemini_model": "gemini-3.8-flash", "gemini_fallback_model": "my-model"}', encoding="utf-8")
    assert load_config(path).gemini_model == "gemini-3.8-flash"


def test_unknown_speaker_id_is_reported_before_processing(tmp_path):
    src = tmp_path / "s.csv"
    src.write_text("3,2,あいう\n999,2,えお\n", encoding="utf-8")
    engine = FakeEngine()
    with pytest.raises(InputFileError, match="話者ID 999"):
        batch.run_job(src, tmp_path / "o", AppConfig(), engine=engine, log=lambda m: None)
    assert engine.synth_calls == []  # 1行も作らずに止める


def test_slow_synthesis_hint():
    from timefit.timing import Stats

    results = [type("R", (), {"speech_sec": 10.0})()]
    assert batch.synthesis_speed_hint(Stats(synth_sec=16.0, synth_calls=3), results).count("1.6倍")
    assert batch.synthesis_speed_hint(Stats(synth_sec=2.0, synth_calls=3), results) is None
