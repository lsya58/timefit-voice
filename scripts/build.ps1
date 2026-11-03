# 配布用の exe と zip を作る（Windows PowerShell で実行）
#
#   .\scripts\build.ps1 -Edition pro    # 製品版（BOOTH の有料商品）
#   .\scripts\build.ps1 -Edition free   # 無料版（1回10行まで）
#
# 事前に: python -m venv venv; .\venv\Scripts\Activate.ps1; pip install -r requirements-dev.txt

param(
    [ValidateSet("pro", "free")]
    [string]$Edition = "pro"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$version = (python -c "import timefit; print(timefit.__version__)").Trim()
$name = if ($Edition -eq "free") { "TimeFitVoice_Free" } else { "TimeFitVoice" }
Write-Host "== $name $version をビルドします =="

# 1. テスト
python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "テストが失敗したためビルドを中止します" }

# 2. 版の埋め込み（ビルド後に削除）
Set-Content -Path "timefit\_build_edition.py" -Value "EDITION = `"$Edition`"" -Encoding ascii
try {
    # 3. exe 化（画面版。黒いコンソール窓は出さない）
    pyinstaller --noconfirm --clean --onefile --windowed `
        --name $name `
        --collect-submodules google.genai `
        --add-data "timefit\ui\web;timefit\ui\web" `
        --add-data "samples;samples" `
        main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller が失敗しました" }
}
finally {
    Remove-Item "timefit\_build_edition.py" -ErrorAction SilentlyContinue
}

# 4. 配布フォルダを作って zip にする
$release = "release\${name}_v$version"
Remove-Item $release -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $release | Out-Null
Copy-Item "dist\$name.exe" $release
Copy-Item "docs\user_guide.md" "$release\はじめにお読みください.txt"
Copy-Item "samples" "$release\samples" -Recurse
pip-licenses --format=plain-vertical --with-license-file --no-license-path --ignore-packages pytest pyinstaller pyinstaller-hooks-contrib pip-licenses pluggy iniconfig | Out-File "$release\THIRD_PARTY_LICENSES.txt" -Encoding utf8

$zip = "release\${name}_v$version.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path "$release\*" -DestinationPath $zip
Write-Host "== 完成: $zip =="
