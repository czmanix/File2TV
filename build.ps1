# Sestaví dist\File2TV\File2TV.exe (složka, ne jeden soubor – rychlejší start).
# ffmpeg.exe a ffprobe.exe (i s DLL u „shared“ buildu) přibalí ze složky ffmpeg\, když tam jsou;
# jinak aplikace použije ffmpeg z PATH.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"

# Pracovní soubory PyInstalleru mimo projekt – jejich nefunkční .exe by se pletl s tím v dist\.
$work = Join-Path $env:TEMP "file2tv-build"
New-Item -ItemType Directory -Force $work | Out-Null
# Ikona aplikace se kreslí v kódu, pro .exe ji uložíme jako .ico.
$ico = Join-Path $work "file2tv.ico"
& $py -c "import sys; from tvcast.icon import image; image().save(sys.argv[1], sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(256,256)])" $ico

$args = @("-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "File2TV",
          "--workpath", $work, "--specpath", $work, "--icon", $ico, "--add-data", "$PSScriptRoot\ui;ui",
          "--add-data", "$PSScriptRoot\tvcast\locales;tvcast\locales",
          "--collect-submodules", "webview.platforms", "--exclude-module", "tkinter")
& $py @args main.py
if ($LASTEXITCODE) { exit $LASTEXITCODE }
# ffmpeg až po sestavení vedle .exe: přes PyInstaller by se jeho DLL zkopírovaly podruhé (+150 MB).
if (Test-Path ffmpeg\ffmpeg.exe) { Copy-Item ffmpeg "dist\File2TV\ffmpeg" -Recurse }
$size = (Get-ChildItem "dist\File2TV" -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Hotovo: dist\File2TV\File2TV.exe ({0:N0} MB)" -f $size)
