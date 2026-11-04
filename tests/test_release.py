import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.audit_release import _public_certifi_bundle, _relative_name, audit
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


PUBLIC_CA = b"-----BEGIN CERTIFICATE-----\nYQ==\n-----END CERTIFICATE-----\n"
PRIVATE_KEY = b"-----BEGIN PRIVATE KEY-----\nYQ==\n-----END PRIVATE KEY-----\n"


@pytest.mark.parametrize("edition,exe", [("free", "TimeFitVoice_Free.exe"), ("pro", "TimeFitVoice_Pro.exe")])
def test_certifi_bundle_only_allowed_when_identical_to_installed_source(tmp_path, edition, exe):
    root = tmp_path / "rc"
    (root / "_internal" / "certifi").mkdir(parents=True)
    (root / "_internal" / "timefit" / "ui" / "web").mkdir(parents=True)
    (root / "samples").mkdir()
    for name in (exe, "samples/sample.srt", "samples/sample.csv", "はじめにお読みください.md",
                 "利用条件.txt", "THIRD_PARTY_LICENSES.txt", "_internal/timefit/ui/web/index.html",
                 "_internal/timefit/ui/web/style.css", "_internal/timefit/ui/web/app.js"):
        (root / name).write_text("safe", encoding="utf-8")
    (root / "_internal" / "edition.txt").write_text(edition, encoding="ascii")
    bundle = root / "_internal" / "certifi" / "cacert.pem"
    bundle.write_bytes(PUBLIC_CA)
    installed = tmp_path / "installed-certifi-cacert.pem"
    installed.write_bytes(PUBLIC_CA)
    toc = tmp_path / "Analysis-00.toc"
    toc.write_text("[('timefit.ui.app', 'module.py', 'PYMODULE')]", encoding="utf-8")
    assert audit(root, edition, toc, installed) == []

    bundle.write_bytes(PUBLIC_CA + b"changed")
    assert any("forbidden file" in issue for issue in audit(root, edition, toc, installed))
    bundle.write_bytes(PUBLIC_CA)
    (root / "_internal" / "other.pem").write_bytes(PUBLIC_CA)
    assert any("other.pem" in issue for issue in audit(root, edition, toc, installed))
    (root / "_internal" / "other.pem").unlink()
    bundle.write_bytes(PRIVATE_KEY)
    installed.write_bytes(PRIVATE_KEY)
    assert any("cacert.pem" in issue for issue in audit(root, edition, toc, installed))

    bundle.write_bytes(PUBLIC_CA)
    installed.write_bytes(PUBLIC_CA)
    (root / "_internal" / "_pytest").mkdir()
    issues = audit(root, edition, toc, installed)
    assert any("development package" in issue for issue in issues)
    (root / "_internal" / "_pytest").rmdir()
    (root / "_internal" / "google" / "genai").mkdir(parents=True)
    test_client = root / "_internal" / "google" / "genai" / "_test_api_client.py"
    test_client.write_text("# test helper", encoding="utf-8")
    assert any("_test_api_client.py" in issue for issue in audit(root, edition, toc, installed))
    test_client.unlink()
    (root / "_internal" / ".env").write_text("GEMINI_API_KEY=dummy", encoding="utf-8")
    (root / "_internal" / "config.json").write_text("{}", encoding="utf-8")
    issues = audit(root, edition, toc, installed)
    assert any(".env" in issue for issue in issues)
    assert any("config.json" in issue for issue in issues)


def test_certifi_path_is_exact_but_windows_case_and_separators_are_accepted(tmp_path):
    installed = tmp_path / "cacert.pem"
    installed.write_bytes(PUBLIC_CA)
    assert _relative_name(Path(r"_INTERNAL\CERTIFI\CACERT.PEM")) == "_internal/certifi/cacert.pem"
    assert _public_certifi_bundle(Path(r"_INTERNAL\CERTIFI\CACERT.PEM"), PUBLIC_CA, installed)
    assert not _public_certifi_bundle(Path(r"_internal\other\cacert.pem"), PUBLIC_CA, installed)
    assert not _public_certifi_bundle(Path(r"_internal\certifi\private.pem"), PUBLIC_CA, installed)
    assert not _public_certifi_bundle(Path(r"_internal\certifi\cacert.pem"), PUBLIC_CA, tmp_path / "missing.pem")


def test_analysis_rejects_development_modules(tmp_path):
    # Analysis-00.toc contains both requested hidden imports and collected TOC triples.
    root = tmp_path / "rc"
    root.mkdir()
    toc = tmp_path / "Analysis-00.toc"
    toc.write_text(repr(([], ["google.genai.tests.missing", "google.genai._test_api_client"],
                         [("google.genai.types", "types.py", "PYMODULE")])), encoding="utf-8")
    assert not any("collected development module" in issue for issue in audit(root, "free", toc))

    toc.write_text(repr(([], ["google.genai.tests.missing"],
                         [("google.genai._test_api_client", r"C:\venv\google\genai\_test_api_client.py", "PYMODULE"),
                          ("_pytest", r"C:\venv\_pytest\__init__.py", "PYMODULE-1")])), encoding="utf-8")
    issues = audit(root, "free", toc)
    assert any("google.genai._test_api_client" in issue and "C:\\venv" in issue for issue in issues)
    assert any("_pytest" in issue and "PYMODULE-1" in issue for issue in issues)
    assert not any("google.genai.tests.missing" in issue for issue in issues)

    toc.write_text("not a Python literal", encoding="utf-8")
    assert any("cannot inspect collected modules" in issue for issue in audit(root, "free", toc))
    toc.unlink()
    assert any("cannot inspect collected modules" in issue for issue in audit(root, "free", toc))
