import json
from types import SimpleNamespace

import pytest

from scripts.audit_release import audit
from timefit import config
from timefit.config import AppConfig
from timefit.errors import ConfigError
from timefit.ui import app as ui


def test_windows_key_is_protected_and_legacy_migrates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "sys", SimpleNamespace(platform="win32", frozen=False))
    monkeypatch.setattr(config, "protect_key", lambda value: "encrypted:" + value[::-1])
    monkeypatch.setattr(config, "unprotect_key", lambda value: value.removeprefix("encrypted:")[::-1])
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"gemini_api_key": "legacy-secret", "max_speed": 1.25}), encoding="utf-8")
    loaded = config.load_config(path)
    assert loaded.gemini_api_key == "legacy-secret"
    assert loaded.max_speed == 1.25
    assert "legacy-secret" not in path.read_text(encoding="utf-8")
    assert "gemini_api_key_protected" in path.read_text(encoding="utf-8")
    assert config.load_config(path).gemini_api_key == "legacy-secret"
    loaded.gemini_api_key = ""
    config.save_config(loaded, path)
    assert "gemini_api_key_protected" not in path.read_text(encoding="utf-8")


def test_failed_key_protection_does_not_destroy_legacy_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "sys", SimpleNamespace(platform="win32", frozen=False))
    monkeypatch.setattr(config, "protect_key", lambda value: (_ for _ in ()).throw(ConfigError("failure")))
    path = tmp_path / "config.json"
    original = '{"gemini_api_key": "legacy-secret"}'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigError):
        config.load_config(path)
    assert path.read_text(encoding="utf-8") == original


def test_frozen_output_is_user_writable_location(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    result = config.default_output_path(tmp_path / "read-only" / "sample.srt")
    assert result.parent.parent == tmp_path / "local" / "TimeFitVoice" / "outputs"
    assert result.name == "sample_output"
    assert result != config.default_output_path(tmp_path / "another" / "sample.srt")


def test_first_frozen_launch_creates_config(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(ui.sys, "frozen", True, raising=False)
    api = ui.Api()
    assert api.init()["configError"] is None
    assert (tmp_path / "TimeFitVoice" / "config.json").is_file()


def test_release_audit_rejects_secret_and_wrong_edition(tmp_path, monkeypatch):
    root = tmp_path / "rc"
    (root / "_internal").mkdir(parents=True)
    (root / "_internal" / "timefit" / "ui" / "web").mkdir(parents=True)
    (root / "samples").mkdir()
    for filename in ("samples/sample.srt", "samples/sample.csv", "TimeFitVoice_Free.exe",
                     "はじめにお読みください.md", "利用条件.txt", "THIRD_PARTY_LICENSES.txt",
                     "_internal/timefit/ui/web/index.html", "_internal/timefit/ui/web/style.css",
                     "_internal/timefit/ui/web/app.js"):
        (root / filename).write_text("safe", encoding="utf-8")
    (root / "_internal" / "edition.txt").write_text("free", encoding="ascii")
    assert audit(root, "free") == []
    assert any("edition" in issue for issue in audit(root, "pro"))
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value-123")
    (root / "samples" / "sample.csv").write_text("secret-value-123", encoding="utf-8")
    assert any("known API key" in issue for issue in audit(root, "free"))
