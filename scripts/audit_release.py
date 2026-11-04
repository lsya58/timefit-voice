"""Fail closed before zipping a Windows release candidate."""

import hashlib
import os
import re
import sys
from pathlib import Path


CERTIFI_BUNDLE = "_internal/certifi/cacert.pem"
DEVELOPMENT_MODULE = re.compile(r"['\"](?:(?:pytest|_pytest|google\.genai\.tests)(?:\.|['\"]))", re.IGNORECASE)


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
            toc = analysis_toc.read_text(encoding="utf-8")
            if DEVELOPMENT_MODULE.search(toc):
                errors.append("development module in PyInstaller analysis")
        except OSError:
            errors.append("PyInstaller analysis file is missing or unreadable")
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
        if re.match(r"_internal/(?:pytest|_pytest|google/genai/tests)(?:/|$)", relative_name):
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
    print("Release audit passed (allowlist and secret scan).")
