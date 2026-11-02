[CmdletBinding()]
param(
    [string]$Python = ".\.venv\Scripts\python.exe",
    [string]$ModelDir = "",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$assetDir = Join-Path $projectRoot "assets"
$modelTarget = Join-Path $projectRoot "models\indra"

if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

Write-Host "Generating the SinLens Lens icon..." -ForegroundColor Cyan
& $Python (Join-Path $projectRoot "build_tools\generate_icon.py")
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

if (-not $ModelDir -and $env:INDRA_OCR_MODEL_DIR) {
    $ModelDir = $env:INDRA_OCR_MODEL_DIR
}
if (-not $ModelDir) {
    $cacheSnapshots = Join-Path $env:USERPROFILE ".cache\huggingface\hub\models--datalab-to--indra-ocr\snapshots"
    if (Test-Path -LiteralPath $cacheSnapshots) {
        $modelCandidate = Get-ChildItem -LiteralPath $cacheSnapshots -Directory |
            Where-Object { (Test-Path (Join-Path $_.FullName "det.onnx")) -and (Test-Path (Join-Path $_.FullName "rec.onnx")) } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($modelCandidate) { $ModelDir = $modelCandidate.FullName }
    }
}
if (-not $ModelDir -or -not (Test-Path -LiteralPath $ModelDir)) {
    throw "Indra OCR model folder not found. Pass -ModelDir C:\path\to\models."
}

$requiredModels = @("det.onnx", "rec.onnx", "order.onnx", "meta.json")
foreach ($modelName in $requiredModels) {
    if (-not (Test-Path -LiteralPath (Join-Path $ModelDir $modelName))) {
        throw "Missing model file: $modelName"
    }
}

Write-Host "Copying OCR model files into the build..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $modelTarget | Out-Null
Get-ChildItem -LiteralPath $ModelDir -File |
    Where-Object { $_.Extension -in @(".onnx", ".json", ".npz") } |
    Copy-Item -Destination $modelTarget -Force

Write-Host "Checking build dependencies..." -ForegroundColor Cyan
& $Python -m pip install -r (Join-Path $projectRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }

Write-Host "Building the Windows application..." -ForegroundColor Cyan
Push-Location $projectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean (Join-Path $projectRoot "sinlens.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
} finally {
    Pop-Location
}

if (-not $SkipInstaller) {
    $iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if (-not $iscc) {
        $isccCandidates = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
        )
        $isccPath = $isccCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    } else {
        $isccPath = $iscc.Source
    }
    if ($isccPath) {
        Write-Host "Building the SinLens installer..." -ForegroundColor Cyan
        & $isccPath (Join-Path $projectRoot "installer.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
        Write-Host "Installer created in .\installer" -ForegroundColor Green
    } else {
        Write-Warning "Inno Setup 6 was not found. Portable build is in .\dist\SinLens."
        Write-Warning "Install Inno Setup 6, then run: iscc installer.iss"
    }
}

Write-Host "SinLens build complete." -ForegroundColor Green
