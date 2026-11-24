# Run with Windows PowerShell 5.1 or PowerShell 7. Installers are always mocked.
param([Parameter(Mandatory = $true)][string]$PythonExecutable)

$setup = Join-Path $PSScriptRoot '../SETUP_WINDOWS.ps1'
. $setup
$script:Assertions = 0
function Assert-Basket {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "FAILED: $Message" }
    $script:Assertions++
}
function Assert-BasketThrows {
    param([scriptblock]$Action, [string]$Message)
    $didThrow = $false
    try { & $Action } catch { $didThrow = $true }
    Assert-Basket $didThrow $Message
}

$fixture = Join-Path ([IO.Path]::GetTempPath()) ('basket-setup-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $fixture | Out-Null
$savedLocal = $env:LOCALAPPDATA
$savedArchitecture = $env:PROCESSOR_ARCHITECTURE
$savedArchitecture64 = $env:PROCESSOR_ARCHITEW6432
try {
    $info = Get-BasketPythonInfo $PythonExecutable
    Assert-Basket ($null -ne $info) 'a real compatible interpreter is detected'
    Assert-Basket ($info.version -match '^3\.(12|13|14)\.') 'the detected version is reported'
    Assert-Basket ($null -eq (Get-BasketPythonInfo (Join-Path $fixture 'missing.exe'))) 'a missing executable is rejected'
    $venvInfo = Get-BasketPythonInfo $PythonExecutable -RequireVenv
    Assert-Basket ($null -ne $venvInfo -and $venvInfo.pip -and $venvInfo.venv) 'the real test virtual environment is usable'
    function Get-BasketPythonCandidates { return @((Join-Path $fixture 'absent.exe'), $PythonExecutable, $PythonExecutable) }
    $script:BasketProjectRoot = Join-Path $fixture 'new-project'
    Assert-Basket ($null -ne (Find-BasketPython)) 'a working interpreter is found after an unusable candidate'

    $index = '<a href="3.14.9/">a</a><a href="3.14.10/">b</a><a href="3.14.11rc1/">c</a><a href="3.15.0/">d</a>'
    Assert-Basket ((Get-BasketReleaseVersion $index) -eq '3.14.10') 'stable patch versions sort numerically and exclude previews and other minors'
    Assert-BasketThrows { Get-BasketReleaseVersion '<a href="3.14.1rc1/">preview</a>' } 'no stable release is an error'

    # A copied, unusable environment is backed up; real data remains byte-identical.
    $script:BasketProjectRoot = Join-Path $fixture 'Корзина с пробелами'
    New-Item -ItemType Directory -Path (Join-Path $script:BasketProjectRoot '.venv') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $script:BasketProjectRoot 'instance') -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $script:BasketProjectRoot '.venv/old.txt') -Value 'old-environment'
    $database = Join-Path $script:BasketProjectRoot 'instance/basket.sqlite3'
    [IO.File]::WriteAllBytes($database, [byte[]](1, 2, 3, 4))
    Repair-BasketEnvironment
    $backups = @(Get-ChildItem -LiteralPath $script:BasketProjectRoot -Force | Where-Object Name -Like '.venv-backup-*')
    Assert-Basket ($backups.Count -eq 1) 'one environment backup exists'
    Assert-Basket (Test-Path -LiteralPath (Join-Path $backups[0].FullName 'old.txt')) 'old environment files are retained'
    Assert-Basket (-not (Test-Path -LiteralPath (Join-Path $script:BasketProjectRoot '.venv'))) 'the launcher can create a fresh environment'
    Assert-Basket (([Convert]::ToBase64String([IO.File]::ReadAllBytes($database))) -eq 'AQIDBA==') 'the database is untouched'

    # Exercise native invocation with a harmless Python fixture in a Unicode path.
    $fakeLauncher = Join-Path $script:BasketProjectRoot 'launcher.py'
    Set-Content -LiteralPath $fakeLauncher -Encoding UTF8 -Value 'from pathlib import Path; Path(__file__).with_suffix(".ran").write_text("ran"); raise SystemExit(7)'
    Assert-Basket ((Start-BasketApplication $info) -eq 7) 'native Python exit status is preserved'
    Assert-Basket (Test-Path -LiteralPath (Join-Path $script:BasketProjectRoot 'launcher.ran')) 'a script path with spaces and Cyrillic runs correctly'

    # Direct-download branch: only mocked official-index/download/signature/process calls.
    $env:LOCALAPPDATA = Join-Path $fixture 'Профиль пользователя'
    $env:PROCESSOR_ARCHITEW6432 = ''
    $script:MockSignature = 'valid'
    $script:MockInstallerExit = 0
    $script:MockDownloadFailure = $false
    $script:DownloadUris = @()
    $script:InstallerStarts = @()
    $script:DownloadedFile = ''
    function Invoke-WebRequest {
        param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec)
        $script:DownloadUris += [string]$Uri
        if (-not $OutFile) { return [pscustomobject]@{ Content = $index } }
        $script:DownloadedFile = $OutFile
        [IO.File]::WriteAllText($OutFile, 'mock-installer-never-executed')
        if ($script:MockDownloadFailure) { throw 'simulated connection failure' }
    }
    function Get-AuthenticodeSignature {
        param($LiteralPath)
        $publisher = if ($script:MockSignature -eq 'other') { 'CN=Other Publisher' } else { 'CN=Python Software Foundation, O=Python Software Foundation' }
        $status = if ($script:MockSignature -eq 'invalid') { 'NotSigned' } else { 'Valid' }
        return [pscustomobject]@{ Status = $status; SignerCertificate = [pscustomobject]@{ Subject = $publisher } }
    }
    function Start-Process {
        param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru)
        $script:InstallerStarts += [pscustomobject]@{ Path = $FilePath; Arguments = $ArgumentList }
        return [pscustomobject]@{ ExitCode = $script:MockInstallerExit }
    }
    foreach ($architecture in @('AMD64', 'ARM64', 'x86')) {
        $env:PROCESSOR_ARCHITECTURE = $architecture
        Install-BasketPythonDirect
        $suffix = if ($architecture -eq 'AMD64') { '-amd64' } elseif ($architecture -eq 'ARM64') { '-arm64' } else { '' }
        Assert-Basket ($script:DownloadUris[-1] -eq "https://www.python.org/ftp/python/3.14.10/python-3.14.10$suffix.exe") "official URL for $architecture"
        Assert-Basket (-not (Test-Path -LiteralPath (Split-Path -Parent $script:DownloadedFile))) 'temporary download is removed'
        Assert-Basket ($script:InstallerStarts[-1].Arguments -match 'InstallAllUsers=0') 'installation is per-user'
        Assert-Basket ($script:InstallerStarts[-1].Arguments.Contains(('TargetDir="' + (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314') + '"'))) 'install paths with spaces and Cyrillic are quoted'
    }
    foreach ($signature in @('invalid', 'other')) {
        $script:MockSignature = $signature
        $before = $script:InstallerStarts.Count
        Assert-BasketThrows { Install-BasketPythonDirect } 'an untrusted signature prevents execution'
        Assert-Basket ($script:InstallerStarts.Count -eq $before) 'no untrusted installer was started'
        Assert-Basket (-not (Test-Path -LiteralPath (Split-Path -Parent $script:DownloadedFile))) 'untrusted download is removed'
    }
    $script:MockSignature = 'valid'
    $script:MockDownloadFailure = $true
    $before = $script:InstallerStarts.Count
    Assert-BasketThrows { Install-BasketPythonDirect } 'a failed download stops installation'
    Assert-Basket ($script:InstallerStarts.Count -eq $before) 'a partial download is not executed'
    Assert-Basket (-not (Test-Path -LiteralPath (Split-Path -Parent $script:DownloadedFile))) 'partial download is removed'
    $script:MockDownloadFailure = $false
    $script:MockInstallerExit = 1602
    Assert-BasketThrows { Install-BasketPythonDirect } 'cancelled direct installation remains cancelled'

    # WinGet delegation and errors. The fake command is a local PS fixture, not WinGet.
    $fakeWinget = Join-Path $fixture 'fake-winget.ps1'
    Set-Content -LiteralPath $fakeWinget -Value '$global:BasketTestWingetArguments = @($args); $global:LASTEXITCODE = $global:BasketTestWingetExit'
    $global:BasketTestWingetExit = 0
    $script:HasWinget = $true
    $script:DirectFallbacks = 0
    function Get-Command {
        param($Name, $CommandType, $ErrorAction)
        if ($script:HasWinget) { return [pscustomobject]@{ Source = $fakeWinget } }
        return $null
    }
    function Install-BasketPythonDirect { $script:DirectFallbacks++ }
    Install-BasketPython
    Assert-Basket ($script:DirectFallbacks -eq 0) 'WinGet success does not invoke another installer'
    Assert-Basket ($global:BasketTestWingetArguments -contains 'Python.Python.3.14') 'WinGet receives the exact package ID'
    Assert-Basket ($global:BasketTestWingetArguments -contains 'user') 'WinGet installs for the current user'
    Assert-Basket ($global:BasketTestWingetArguments -contains '--silent') 'WinGet installs without a setup wizard'
    $global:BasketTestWingetExit = 1223
    Assert-BasketThrows { Install-BasketPython } 'a WinGet failure is returned'
    Assert-Basket ($script:DirectFallbacks -eq 0) 'cancellation never triggers a second installer'
    $script:HasWinget = $false
    Install-BasketPython
    Assert-Basket ($script:DirectFallbacks -eq 1) 'missing WinGet uses the official-download fallback'

    # End-to-end orchestration, with external installation and server process stubbed.
    $script:AvailablePython = $info
    $script:Installs = 0
    $script:Repairs = 0
    $script:Starts = 0
    $script:FailInstall = $false
    function Find-BasketPython { return $script:AvailablePython }
    function Install-BasketPython {
        $script:Installs++
        if ($script:FailInstall) { throw 'simulated installation failure' }
        $script:AvailablePython = $info
    }
    function Repair-BasketEnvironment { $script:Repairs++ }
    function Start-BasketApplication { param($PythonInfo) $script:Starts++; return 0 }
    Assert-Basket ((Invoke-BasketSetup) -eq 0) 'existing Python starts the application'
    Assert-Basket ($script:Installs -eq 0 -and $script:Starts -eq 1 -and $script:Repairs -eq 1) 'healthy installation does not download Python'
    Assert-Basket ((Invoke-BasketSetup -InstallOnly) -eq 0) 'install-only mode succeeds'
    Assert-Basket ($script:Starts -eq 1 -and $script:Repairs -eq 1) 'install-only does not start or modify the app environment'
    $script:AvailablePython = $null
    Assert-Basket ((Invoke-BasketSetup) -eq 0) 'missing Python is installed before starting'
    Assert-Basket ($script:Installs -eq 1 -and $script:Starts -eq 2) 'installation resumes in the same process'
    $script:AvailablePython = $null
    $script:FailInstall = $true
    Assert-Basket ((Invoke-BasketSetup) -eq 1) 'installation failure returns an error'
    Assert-Basket ($script:Starts -eq 2 -and $script:Repairs -eq 2) 'failure stops before app or environment changes'
    Write-Host "PASS: $script:Assertions Windows setup assertions (external installers mocked)."
}
finally {
    $env:LOCALAPPDATA = $savedLocal
    $env:PROCESSOR_ARCHITECTURE = $savedArchitecture
    $env:PROCESSOR_ARCHITEW6432 = $savedArchitecture64
    Remove-Variable -Name BasketTestWingetArguments, BasketTestWingetExit -Scope Global -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction SilentlyContinue
}
