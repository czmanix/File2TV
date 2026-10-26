# Stáhne ffmpeg pro přibalení k aplikaci do složky ffmpeg\ (build.ps1 ho zkopíruje vedle .exe):
# BtbN ffmpeg 9.0, LGPL, „shared“ (ffmpeg.exe a ffprobe.exe sdílí DLL), bez ffplay.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-win64-lgpl-shared-9.0.zip"
$tmp = Join-Path $env:TEMP "file2tv-ffmpeg"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $tmp | Out-Null
$ProgressPreference = "SilentlyContinue"
Write-Host "Stahuji $url"
Invoke-WebRequest $url -OutFile "$tmp\ffmpeg.zip"
Expand-Archive "$tmp\ffmpeg.zip" "$tmp\x"
$root = Get-ChildItem "$tmp\x" -Directory | Select-Object -First 1
Remove-Item -Recurse -Force ffmpeg -ErrorAction SilentlyContinue
New-Item -ItemType Directory ffmpeg | Out-Null
Get-ChildItem "$($root.FullName)\bin" | Where-Object Name -ne "ffplay.exe" | Copy-Item -Destination ffmpeg
Copy-Item "$($root.FullName)\LICENSE.txt" ffmpeg\LICENSE.txt
Remove-Item -Recurse -Force $tmp
& .\ffmpeg\ffmpeg.exe -hide_banner -version | Select-Object -First 1
