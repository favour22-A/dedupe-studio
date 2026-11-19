# Builds dist\DedupeStudio\DedupeStudio.exe and dist\DedupeStudio-win64.zip.
# Put this file and dedupe_studio_app.py next to dedupe_studio.py, then run:  .\build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .\dedupe_studio.py)) { throw "dedupe_studio.py not found in $PSScriptRoot" }

python -m venv .venv
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip pyinstaller pywebview
if (Test-Path .\requirements.txt) { & $py -m pip install -r requirements.txt }

$data = @("--add-data", "dedupe_studio.py;.")
Get-ChildItem -Path .\* -File -Include *.html, *.css, *.js, *.json, *.svg, *.png, *.ico |
    ForEach-Object { $data += @("--add-data", "$($_.Name);.") }
$icon = @(); if (Test-Path .\icon.ico) { $icon = @("--icon", "icon.ico") }

& $py -m PyInstaller --noconfirm --clean --windowed --onedir --name DedupeStudio `
    --paths . @icon @data dedupe_studio_app.py
Compress-Archive -Force -Path dist\DedupeStudio -DestinationPath dist\DedupeStudio-win64.zip
Write-Host "Done: dist\DedupeStudio\DedupeStudio.exe"
