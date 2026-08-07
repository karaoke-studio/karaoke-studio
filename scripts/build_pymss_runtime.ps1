param(
    [ValidateSet("windows-cu128", "windows-cpu")]
    [string]$Variant = "windows-cu128",
    [string]$HostPython = "python",
    [string]$WorkDir = "",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $WorkDir) {
    $WorkDir = Join-Path $repoRoot "build\pymss-runtime-$Variant"
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $repoRoot "dist\pymss-runtime-$Variant"
}
$workPath = [System.IO.Path]::GetFullPath($WorkDir)
$outputPath = [System.IO.Path]::GetFullPath($OutputDir)
$repoPath = [System.IO.Path]::GetFullPath($repoRoot)
if (-not $workPath.StartsWith($repoPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "WorkDir must stay inside the repository: $workPath"
}
if (-not $outputPath.StartsWith($repoPath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputDir must stay inside the repository: $outputPath"
}

$contractJson = & $HostPython (Join-Path $repoRoot "scripts\build_pymss_runtime.py") --print-contract
if ($LASTEXITCODE -ne 0) { throw "Reading PyMSS runtime contract failed" }
$contract = $contractJson | ConvertFrom-Json
$pythonVersion = [string]$contract.embedded_python_version
$pythonSha256 = ([string]$contract.embedded_python_sha256).ToUpperInvariant()
$pymssVersion = [string]$contract.pymss_version
$pymssCoreVersion = [string]$contract.pymss_core_version

if (Test-Path -LiteralPath $workPath) {
    Remove-Item -LiteralPath $workPath -Recurse -Force
}
if (Test-Path -LiteralPath $outputPath) {
    Remove-Item -LiteralPath $outputPath -Recurse -Force
}
New-Item -ItemType Directory -Path $workPath | Out-Null
New-Item -ItemType Directory -Path $outputPath | Out-Null

$runtime = Join-Path $workPath "runtime"
New-Item -ItemType Directory -Path $runtime | Out-Null
$pythonArchive = Join-Path $workPath "python-$pythonVersion-embed-amd64.zip"
$pythonUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-embed-amd64.zip"
Invoke-WebRequest -Uri $pythonUrl -OutFile $pythonArchive -UseBasicParsing
$actualPythonHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $pythonArchive).Hash
if ($actualPythonHash -ne $pythonSha256) {
    throw "Embedded Python SHA-256 mismatch: $actualPythonHash"
}
Expand-Archive -LiteralPath $pythonArchive -DestinationPath $runtime

$pth = Join-Path $runtime "python312._pth"
$pthLines = Get-Content -LiteralPath $pth | Where-Object { $_ -ne "#import site" }
$pthLines += "Lib\site-packages"
$pthLines += "import site"
Set-Content -LiteralPath $pth -Value $pthLines -Encoding ASCII
$sitePackages = Join-Path $runtime "Lib\site-packages"
New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null

$dependencies = @(
    "pip==25.1.1",
    "av==17.0.1",
    "librosa==0.11.0",
    "numpy==2.2.6",
    "PyYAML==6.0.3",
    "tqdm==4.67.3",
    "fastapi==0.136.3",
    "uvicorn[standard]==0.48.0"
)
& $HostPython -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: --target $sitePackages @dependencies
if ($LASTEXITCODE -ne 0) { throw "Installing PyMSS dependencies failed" }
& $HostPython -m pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: --no-deps --target $sitePackages "pymss-core==$pymssCoreVersion" "pymss==$pymssVersion"
if ($LASTEXITCODE -ne 0) { throw "Installing PyMSS failed" }
if (Test-Path -LiteralPath (Join-Path $sitePackages "torch")) {
    throw "torch must not be present in the published PyMSS base runtime"
}

$runtimePython = Join-Path $runtime "python.exe"
$smokeCode = "import fastapi, numpy, pip, uvicorn; from importlib.metadata import version; assert version('pymss') == '$pymssVersion'; assert version('pymss-core') == '$pymssCoreVersion'; print(version('pymss'), pip.__version__)"
& $runtimePython -c $smokeCode
if ($LASTEXITCODE -ne 0) { throw "Portable PyMSS runtime smoke test failed" }

Push-Location $repoRoot
try {
    & $HostPython scripts\build_pymss_runtime.py --runtime-dir $runtime --output-dir $outputPath --variant $Variant
    if ($LASTEXITCODE -ne 0) { throw "Packaging PyMSS runtime failed" }
} finally {
    Pop-Location
}
