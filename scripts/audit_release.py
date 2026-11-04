"""Fail closed before zipping a Windows release candidate."""

import os
import re
import sys
from pathlib import Path


def audit(root: Path, edition: str) -> list[str]:
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
        if path.is_symlink():
            errors.append(f"symlink: {rel}")
            continue
        if not path.is_file():
            continue
        if path.name.lower() in {".env", "config.json", "credentials.json"} or path.suffix.lower() in {".pem", ".p12", ".key"}:
            errors.append(f"forbidden file: {rel}")
        content = path.read_bytes()
        if any(secret in content for secret in secrets if len(secret) >= 8):
            errors.append(f"known API key found: {rel}")
        if re.search(rb"AIza[0-9A-Za-z_-]{30,}", content):
            errors.append(f"possible Google API key found: {rel}")
    return errors


if __name__ == "__main__":
    issues = audit(Path(sys.argv[1]), sys.argv[2])
    for issue in issues:
        print(issue)
    if issues:
        sys.exit(1)
    print("Release audit passed (allowlist and secret scan).")
