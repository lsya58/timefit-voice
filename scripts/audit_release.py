"""Fail closed before zipping a Windows release candidate."""

import ast
import hashlib
import os
import re
import sys
from pathlib import Path


CERTIFI_BUNDLE = "_internal/certifi/cacert.pem"
MODULE_TYPES = {"PYSOURCE", "EXTENSION", "PYMODULE", "PYMODULE-1", "PYMODULE-2"}


def _development_module(name: str) -> bool:
    name = name.casefold()
    return (name in {"pytest", "_pytest", "google.genai.tests", "google.genai._test_api_client"}
            or name.startswith(("pytest.", "_pytest.", "google.genai.tests.")))


def _collected_development_modules(toc_file: Path) -> list[tuple[str, str, str]]:
    """Read collected TOC triplets, not the requested/missing hidden-import names.

    Analysis-00.toc also stores input options such as hiddenimports; searching its
    raw text falsely treats unsuccessful import requests as packaged modules.
    """
    structure = ast.literal_eval(toc_file.read_text(encoding="utf-8"))
    found = set()

    def visit(value):
        if isinstance(value, (tuple, list)):
            if (len(value) == 3 and all(isinstance(item, str) for item in value)
                    and value[2] in MODULE_TYPES):
                name, source, kind = value
                if _development_module(name):
                    found.add((name, source, kind))
            else:
                for item in value:
                    visit(item)

    visit(structure)
    return sorted(found)


def _relative_name(path: Path) -> str:
    """Match packaged paths independently of Windows separator and case."""
    return str(path).replace("\\", "/").casefold()


def _public_certifi_bundle(path: Path, content: bytes, source: Path | None = None) -> bool:
    if _relative_name(path) != CERTIFI_BUNDLE:
        return False
    # A substituted certifi file must never make a private key acceptable.
    if (b"-----BEGIN CERTIFICATE-----" not in content
            or content.count(b"-----BEGIN CERTIFICATE-----") != content.count(b"-----END CERTIFICATE-----")
            or re.search(rb"-----BEGIN [^-\r\n]*PRIVATE KEY-----", content)):
        return False
    if source is None:
        try:
            import certifi
            source = Path(certifi.where())
        except (ImportError, OSError):
            return False
    try:
        return (source.is_file()
                and hashlib.sha256(content).digest() == hashlib.sha256(source.read_bytes()).digest())
    except OSError:
        return False


def audit(root: Path, edition: str, analysis_toc: Path | None = None,
          certifi_source: Path | None = None) -> list[str]:
    errors = []
    expected_exe = "TimeFitVoice_Free.exe" if edition == "free" else "TimeFitVoice_Pro.exe"
    required = {expected_exe, "_internal", "samples", "はじめにお読みください.md", "利用条件.txt", "THIRD_PARTY_LICENSES.txt"}
    actual = {p.name for p in root.iterdir()}
    if actual != required:
        errors.append(f"root entries differ: missing={sorted(required-actual)}, extra={sorted(actual-required)}")
    for name in ("samples/sample.srt", "samples/sample.csv", "_internal/timefit/ui/web/index.html",
                 "_internal/timefit/ui/web/style.css", "_internal/timefit/ui/web/app.js"):
        if not (root / name).is_file():
            errors.append(f"missing: {name}")
    marker = root / "_internal" / "edition.txt"
    if not marker.is_file() or marker.read_text(encoding="ascii").strip() != edition:
        errors.append("edition marker does not match")
    if (root / "samples").is_dir() and {p.name for p in (root / "samples").iterdir()} != {"sample.srt", "sample.csv"}:
        errors.append("unexpected sample files")
    if analysis_toc is not None:
        try:
            collected = _collected_development_modules(analysis_toc)
            for name, source, kind in collected[:20]:
                errors.append(f"collected development module in {analysis_toc.name}: {name} [{kind}] <- {source}")
            if len(collected) > 20:
                errors.append(f"{len(collected) - 20} more collected development modules in {analysis_toc.name}")
        except (OSError, ValueError, SyntaxError, UnicodeError, RecursionError) as exc:
            errors.append(f"cannot inspect collected modules in {analysis_toc}: {type(exc).__name__}: {exc}")
    secrets = set()
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(key):
            secrets.add(os.environ[key].encode())
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        for match in re.finditer(rb"(?m)^\s*(?:GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*['\"]?([^'\"\r\n#]+)", env_file.read_bytes()):
            value = match.group(1).strip()
            if value:
                secrets.add(value)
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        relative_name = _relative_name(rel)
        if (re.match(r"_internal/(?:pytest|_pytest|google/genai/tests)(?:/|$)", relative_name)
                or re.match(r"_internal/google/genai/_test_api_client(?:\.|/|$)", relative_name)):
            errors.append(f"development package in release: {rel}")
        if path.is_symlink():
            errors.append(f"symlink: {rel}")
            continue
        if not path.is_file():
            continue
        if path.name.casefold() in {".env", "config.json", "credentials.json"} or path.suffix.casefold() in {".p12", ".key"}:
            errors.append(f"forbidden file: {rel}")
        content = path.read_bytes()
        if path.suffix.casefold() == ".pem" and not _public_certifi_bundle(rel, content, certifi_source):
            errors.append(f"forbidden file: {rel}")
        if any(secret in content for secret in secrets if len(secret) >= 8):
            errors.append(f"known API key found: {rel}")
        if re.search(rb"AIza[0-9A-Za-z_-]{30,}", content):
            errors.append(f"possible Google API key found: {rel}")
    return errors


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: audit_release.py RELEASE_DIR free|pro ANALYSIS_TOC")
    issues = audit(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
    for issue in issues:
        print(issue)
    if issues:
        sys.exit(1)
    print("Release audit passed (collected-module TOC, allowlist, certificate, and secret scan).")
