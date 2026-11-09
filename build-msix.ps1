# Zabalí dist\File2TV (z build.ps1) do dist\File2TV-<ver>.msix pro Microsoft Store.
# Store balíček podepíše sám, proto se tu nepodepisuje. Výchozí identita je z Partner Center
# (Product management → Product identity, Store ID 9MVMM63ZZ1SC).
# Vyzkoušení bez Storu (s Developer Mode):
#   Add-AppxPackage -Register "$env:TEMP\file2tv-build\msix\AppxManifest.xml"
param(
    [string]$IdentityName = "czmanix.File2TV",
    [string]$Publisher = "CN=694AC60A-47EB-4AAD-A1CC-F51DDC7D9B1F",
    [string]$PublisherDisplayName = "czmanix"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path dist\File2TV\File2TV.exe)) { throw "Chybí dist\File2TV\File2TV.exe – nejdřív .\build.ps1" }

# makeappx a makepri z NuGet balíčku Windows SDK BuildTools (celé SDK není potřeba).
$work = Join-Path $env:TEMP "file2tv-build"
$sdk = Join-Path $work "sdk-buildtools"
if (-not (Test-Path $sdk)) {
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest "https://www.nuget.org/api/v2/package/Microsoft.Windows.SDK.BuildTools/10.0.28000.2957" -OutFile "$work\buildtools.zip"
    Expand-Archive "$work\buildtools.zip" $sdk
    Remove-Item "$work\buildtools.zip"
}
$bin = (Get-ChildItem $sdk -Recurse -Filter makeappx.exe | Where-Object FullName -like "*\x64\*" | Select-Object -First 1).DirectoryName

# Obsah balíčku: aplikace, ikony a manifest.
$layout = Join-Path $work "msix"
Remove-Item -Recurse -Force $layout -ErrorAction SilentlyContinue
Copy-Item dist\File2TV $layout -Recurse
& $py -c @"
import sys
from pathlib import Path
from PIL import Image
from tvcast.icon import image
a = Path(sys.argv[1]); a.mkdir()
def logo(name, size, icon):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    img.alpha_composite(image(icon), ((size - icon) // 2,) * 2)
    img.save(a / name)
logo('StoreLogo.png', 50, 50)
logo('Square150x150Logo.png', 150, 100)
logo('Square44x44Logo.png', 44, 44)
for s in (16, 24, 32, 48, 256):
    logo(f'Square44x44Logo.targetsize-{s}_altform-unplated.png', s, s)
"@ "$layout\Assets"
if ($LASTEXITCODE) { exit $LASTEXITCODE }

$version = (& $py -c "import tvcast; print(tvcast.__version__)").Trim()
# „Otevřít v programu“ pro stejné přípony, jaké nabízí dialog pro výběr videí.
$fileTypes = (& $py -c "from tvcast.media import VIDEO_SUFFIXES as s; print('\n'.join(f'              <uap:FileType>{x}</uap:FileType>' for x in sorted(s)))") -join "`r`n"
(Get-Content msix\AppxManifest.xml -Raw -Encoding UTF8).
    Replace("{FileTypes}", $fileTypes).Replace("{IdentityName}", $IdentityName).Replace("{Publisher}", [Security.SecurityElement]::Escape($Publisher)).
    Replace("{PublisherDisplayName}", [Security.SecurityElement]::Escape($PublisherDisplayName)).
    Replace("{Version}", "$version.0") | Set-Content "$layout\AppxManifest.xml" -Encoding UTF8

# resources.pri – aby Windows našly ikony pro hlavní panel (targetsize-*).
& "$bin\makepri.exe" createconfig /cf "$work\priconfig.xml" /dq en-US /o | Out-Null
& "$bin\makepri.exe" new /pr $layout /cf "$work\priconfig.xml" /mn "$layout\AppxManifest.xml" /of "$layout\resources.pri" /o | Out-Null
if ($LASTEXITCODE) { exit $LASTEXITCODE }

$msix = "dist\File2TV-$version.msix"
& "$bin\makeappx.exe" pack /d $layout /p $msix /o | Out-Null
if ($LASTEXITCODE) { exit $LASTEXITCODE }
Write-Host ("Hotovo: $msix ({0:N1} MB)" -f ((Get-Item $msix).Length / 1MB))
