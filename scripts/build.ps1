# Windows PowerShell: .\scripts\build.ps1 -Edition free / -Edition pro
# A clean venv is recommended: py -3 -m venv venv; .\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("pro", "free")]
    [string]$Edition
)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo
$python = Join-Path $repo "venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv\Scripts\python.exe がありません。先に venv を作成してください。" }
$version = (& $python -c "import timefit; print(timefit.__version__)").Trim()
if ($LASTEXITCODE -ne 0) { throw "バージョンを読み取れません。" }
$name = if ($Edition -eq "free") { "TimeFitVoice_Free" } else { "TimeFitVoice_Pro" }
$candidate = "${name}_v${version}-rc1"
$stage = Join-Path $repo "release\$candidate"
$zip = Join-Path $repo "release\$candidate.zip"
$marker = Join-Path $repo "edition.txt"

& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "テストが失敗しました。" }

try {
    Set-Content -Path $marker -Value $Edition -Encoding ascii
    & $python -m PyInstaller --noconfirm --clean --onedir --windowed `
        --contents-directory _internal --name $name `
        --collect-submodules google.genai `
        --add-data "timefit\ui\web;timefit\ui\web" `
        --add-data "edition.txt;." `
        --add-data "samples\sample.srt;samples" `
        main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller が失敗しました。" }
}
finally {
    Remove-Item $marker -ErrorAction SilentlyContinue
}

$built = Join-Path $repo "dist\$name"
if (-not (Test-Path (Join-Path $built "$name.exe"))) { throw "exe が生成されませんでした。" }
if ((Get-Content (Join-Path $built "_internal\edition.txt") -Raw).Trim() -ne $Edition) { throw "ビルド版が一致しません。" }
New-Item -ItemType Directory -Path (Join-Path $repo "release") -Force | Out-Null
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
Copy-Item $built $stage -Recurse
New-Item -ItemType Directory -Path (Join-Path $stage "samples") | Out-Null
Copy-Item "samples\sample.srt" (Join-Path $stage "samples\sample.srt")
Copy-Item "samples\sample.csv" (Join-Path $stage "samples\sample.csv")
Copy-Item "docs\user_guide.md" (Join-Path $stage "はじめにお読みください.md")
Copy-Item "利用条件.txt" $stage

$licenses = Join-Path $stage "THIRD_PARTY_LICENSES.txt"
& (Join-Path $repo "venv\Scripts\pip-licenses.exe") --format=plain-vertical --with-license-file --no-license-path |
    Out-File $licenses -Encoding utf8
if ($LASTEXITCODE -ne 0 -or (Get-Item $licenses).Length -eq 0) { throw "第三者ライセンス一覧を生成できませんでした。" }

& $python "scripts\audit_release.py" $stage $Edition
if ($LASTEXITCODE -ne 0) { throw "配布物の監査に失敗しました。ZIP は作りません。" }
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $stage -DestinationPath $zip
Write-Host "RC完成: $zip"
Get-FileHash $zip -Algorithm SHA256 | Format-List
