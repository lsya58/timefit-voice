"""画面と Python の橋渡し（ウィンドウなしで確認できる部分）"""

import pytest

from timefit.ui import app as ui


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # .env を読まないように
    return ui.Api()


def test_build_html_inlines_assets():
    html = ui.build_html()
    assert "/*__STYLE__*/" not in html and "/*__SCRIPT__*/" not in html
    assert "window.tf" in html and "--st-failed" in html


def test_preview_srt(api, tmp_path):
    p = tmp_path / "a.srt"
    p.write_text("1\n00:00:01,000 --> 00:00:02,500\nテスト\n", encoding="utf-8")
    res = api.preview(str(p))
    assert res["ok"] and res["kind"] == "srt"
    assert res["lines"][0]["target"] == pytest.approx(1.5)
    assert res["output"].endswith("a_output")


def test_preview_error_is_message(api, tmp_path):
    res = api.preview(str(tmp_path / "missing.csv"))
    assert not res["ok"] and "開けません" in res["error"]


def test_settings_hide_key_and_keep_it_when_blank(api):
    assert api.save_settings({"gemini_api_key": "secret-key-1234", "max_speed": 1.2})["ok"]
    s = api.get_settings()
    assert "gemini_api_key" not in s and s["hasApiKey"] and s["apiKeyHint"] == "…1234"
    assert api.save_settings({"gemini_api_key": "", "max_speed": 1.25})["ok"]
    assert api._cfg.gemini_api_key == "secret-key-1234"
    assert api._cfg.max_speed == 1.25


def test_invalid_settings_are_rolled_back(api):
    res = api.save_settings({"max_speed": 9})
    assert not res["ok"]
    assert api._cfg.max_speed == 1.3


def test_start_requires_input(api):
    assert not api.start({})["ok"]
