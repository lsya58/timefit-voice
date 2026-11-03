import pytest
import requests

from timefit import batch, edition
from timefit.audio import WavData, fit_length, parse_wav, place_clips
from timefit.config import AppConfig, load_config, save_config
from timefit.engine import VoicevoxClient
from timefit.errors import ApiKeyInvalidError, ConfigError, EngineNotRunningError, LLMError
from timefit.llm import GeminiRewriter, sanitize_output

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


def test_free_edition_limits_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(edition, "EDITION", "free")
    src = tmp_path / "s.csv"
    src.write_text("\n".join(f"3,2,テスト{i}" for i in range(15)), encoding="utf-8")
    summary = batch.run_job(src, tmp_path / "o", AppConfig(), engine=FakeEngine(), rewriter=None, log=lambda m: None)
    assert len(summary.results) == edition.FREE_LINE_LIMIT
    assert summary.skipped_by_limit == 5


def test_llm_error_on_one_line_does_not_stop_job(tmp_path):
    class Broken:
        def rewrite(self, *a, **k):
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


def test_gemini_retries_on_rate_limit():
    models = _FakeModels([_api_error(429), _api_error(429)])
    sleeps = []
    rw = GeminiRewriter("key", "m", sleep=sleeps.append, client=_FakeClient(models))
    assert rw.rewrite("元の原稿", 5) == "短くした原稿"
    assert models.calls == 3 and sleeps == [2, 4]


def test_gemini_switches_to_fallback_model_when_busy():
    calls = []

    class Models(_FakeModels):
        def generate_content(self, **kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "main":
                raise _api_error(503, "high demand")
            return super().generate_content(**kwargs)

    logs = []
    rw = GeminiRewriter("key", "main", fallback_models=["lite"], sleep=lambda s: None,
                        client=_FakeClient(Models([])), log=logs.append)
    assert rw.rewrite("元の原稿", 5) == "短くした原稿"
    assert calls == ["main", "main", "main", "lite"]
    assert any("lite に切り替え" in m for m in logs)


def test_gemini_all_models_busy_raises_llm_error():
    models = _FakeModels([_api_error(503)] * 6)
    rw = GeminiRewriter("key", "a", fallback_models=["b"], sleep=lambda s: None, client=_FakeClient(models))
    with pytest.raises(LLMError, match="混雑"):
        rw.rewrite("元の原稿", 5)


def test_gemini_invalid_key():
    models = _FakeModels([_api_error(400, "API key not valid. Please pass a valid API key.")])
    rw = GeminiRewriter("key", "m", sleep=lambda s: None, client=_FakeClient(models))
    with pytest.raises(ApiKeyInvalidError):
        rw.rewrite("元の原稿", 5)


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
