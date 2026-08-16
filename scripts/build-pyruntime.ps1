# Build a reproducible embedded Python runtime for the packaged application.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root 'pyruntime'
$requirements = Join-Path $root 'python\requirements.txt'
$constraints = Join-Path $root 'python\requirements.lock'
$lockFile = Join-Path $PSScriptRoot 'pyruntime.lock.json'

if (!(Test-Path -LiteralPath $requirements) -or !(Test-Path -LiteralPath $constraints) -or !(Test-Path -LiteralPath $lockFile)) {
    throw 'Python runtime build inputs are missing.'
}

$runtimeLock = Get-Content -LiteralPath $lockFile -Raw | ConvertFrom-Json
$pyVersion = [string]$runtimeLock.python.version
$pythonUrl = [string]$runtimeLock.python.url
$pythonSha256 = [string]$runtimeLock.python.sha256
$getPipUrl = [string]$runtimeLock.getPip.url
$getPipSha256 = [string]$runtimeLock.getPip.sha256
$pipVersion = [string]$runtimeLock.getPip.pipVersion

function Get-VerifiedDownload([string]$uri, [string]$destination, [string]$expectedSha256) {
    Invoke-WebRequest -Uri $uri -OutFile $destination -UseBasicParsing
    # Use the .NET runtime directly so the build also works in stripped-down
    # PowerShell environments where the Get-FileHash cmdlet is unavailable.
    $stream = [System.IO.File]::OpenRead($destination)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $actual = ([System.BitConverter]::ToString($sha256.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
    if ($actual -ne $expectedSha256.ToLowerInvariant()) {
        Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        throw "Downloaded SHA-256 does not match lock: $uri"
    }
}

# Preserve the working runtime until a fully verified staging build is ready.
$stage = Join-Path $root ("pyruntime.staging-" + [guid]::NewGuid().ToString('N'))
$backup = Join-Path $root ("pyruntime.backup-" + [guid]::NewGuid().ToString('N'))
$promoted = $false

try {
    New-Item -ItemType Directory -Force $stage | Out-Null
    $archive = Join-Path $stage "python-$pyVersion-embed-amd64.zip"
    Write-Host "[pyruntime] Python $pyVersion download and verify"
    Get-VerifiedDownload $pythonUrl $archive $pythonSha256
    Expand-Archive -LiteralPath $archive -DestinationPath $stage -Force
    Remove-Item -LiteralPath $archive -Force

    $pth = (Get-ChildItem -LiteralPath $stage -Filter 'python*._pth')[0].FullName
    $lines = Get-Content -LiteralPath $pth
    $lines = $lines -replace '^\s*#\s*import\s+site', 'import site'
    $lines += 'Lib\site-packages'
    $lines += '..\python'
    Set-Content -LiteralPath $pth -Value $lines -Encoding ASCII

    $py = Join-Path $stage 'python.exe'
    $getPip = Join-Path $stage 'get-pip.py'
    Write-Host '[pyruntime] get-pip download and verify'
    Get-VerifiedDownload $getPipUrl $getPip $getPipSha256
    & $py $getPip "pip==$pipVersion" --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "get-pip failed (exit $LASTEXITCODE)" }
    $installedPipVersion = (& $py -c "import importlib.metadata; print(importlib.metadata.version('pip'))").Trim()
    if ($LASTEXITCODE -ne 0 -or $installedPipVersion -ne $pipVersion) {
        throw "Installed pip version does not match lock: expected $pipVersion, got $installedPipVersion"
    }
    Write-Host "[pyruntime] pip $installedPipVersion verified"
    Remove-Item -LiteralPath $getPip -Force

    Write-Host '[pyruntime] install locked runtime dependencies'
    & $py -m pip install --disable-pip-version-check --no-warn-script-location --no-cache-dir --only-binary=:all: -r $requirements -c $constraints
    if ($LASTEXITCODE -ne 0) { throw "requirements install failed (exit $LASTEXITCODE)" }

    Get-ChildItem -LiteralPath $stage -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $sitePackages = Join-Path $stage 'Lib\site-packages'
    foreach ($pattern in @('pip', 'pip-*.dist-info', 'watchfiles', 'watchfiles-*.dist-info')) {
        Get-ChildItem -LiteralPath $sitePackages -Filter $pattern -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
    $scriptsDir = Join-Path $stage 'Scripts'
    Get-ChildItem -LiteralPath $scriptsDir -Filter 'pip*.exe' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue

    & $py --version
    if ($LASTEXITCODE -ne 0) { throw "Embedded Python check failed (exit $LASTEXITCODE)" }
    if (Test-Path -LiteralPath $out) { Move-Item -LiteralPath $out -Destination $backup }
    Move-Item -LiteralPath $stage -Destination $out
    $promoted = $true

    $size = (Get-ChildItem -LiteralPath $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
    Write-Host ("[pyruntime] complete - {0:N0} MB" -f $size)
} catch {
    if (!(Test-Path -LiteralPath $out) -and (Test-Path -LiteralPath $backup)) {
        Move-Item -LiteralPath $backup -Destination $out
    }
    throw
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    if ($promoted -and (Test-Path -LiteralPath $backup)) { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue }
}
