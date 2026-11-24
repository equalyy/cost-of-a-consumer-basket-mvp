# Windows PowerShell 5.1 compatible. Saved as UTF-8 with BOM.
[CmdletBinding()]
param([switch]$InstallOnly)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:BasketProjectRoot = $PSScriptRoot
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Get-BasketPythonInfo {
    param([string]$FilePath, [switch]$RequireVenv)
    if (-not $FilePath -or -not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        return $null
    }
    # Single quotes inside Python code survive Windows PowerShell's native argv.
    $probe = @'
import ensurepip, importlib.util, json, sqlite3, ssl, sys, venv
ok = sys.implementation.name == 'cpython' and (3, 12) <= sys.version_info[:2] < (3, 15) and sys.version_info.releaselevel == 'final'
is_venv = sys.prefix != sys.base_prefix
print(json.dumps({'path': sys.executable, 'version': '.'.join(map(str, sys.version_info[:3])), 'compatible': ok, 'venv': is_venv, 'pip': importlib.util.find_spec('pip') is not None}))
'@
    try {
        $output = @(& $FilePath -c $probe 2>$null)
        if ($LASTEXITCODE -ne 0 -or $output.Count -eq 0) { return $null }
        $info = $output[-1] | ConvertFrom-Json
        if (-not $info.compatible) { return $null }
        if ($RequireVenv -and (-not $info.venv -or -not $info.pip)) { return $null }
        return $info
    }
    catch { return $null }
}

function Get-BasketPythonCandidates {
    # Inspect installations without opening a Microsoft Store Python alias.
    foreach ($root in @(
        'HKCU:\Software\Python\PythonCore',
        'HKLM:\Software\Python\PythonCore',
        'HKLM:\Software\WOW6432Node\Python\PythonCore'
    )) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        foreach ($version in Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue) {
            $installPath = Join-Path $version.PSPath 'InstallPath'
            if (Test-Path -LiteralPath $installPath) {
                $key = Get-Item -LiteralPath $installPath
                $executable = $key.GetValue('ExecutablePath')
                if ($executable) { [string]$executable }
                $directory = $key.GetValue('')
                if ($directory) { Join-Path ([string]$directory) 'python.exe' }
            }
        }
    }
    foreach ($base in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base) { continue }
        foreach ($tag in @('314', '313', '312')) {
            Join-Path $base "Programs\Python\Python$tag\python.exe"
            Join-Path $base "Python$tag\python.exe"
        }
    }
    foreach ($name in @('python.exe', 'python3.exe')) {
        foreach ($command in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            if ($command.Source -notmatch '\\Microsoft\\WindowsApps\\') { $command.Source }
        }
    }
    $py = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($py) {
        try {
            # -0p lists installations, including versions hidden by PATH.
            $listing = @(& $py.Source -0p 2>$null)
            foreach ($line in $listing) {
                if ([string]$line -match '([A-Za-z]:\\[^\r\n]*\\python\.exe)\s*$') {
                    $Matches[1].Trim()
                }
            }
        }
        catch { }
    }
}

function Find-BasketPython {
    $existing = Get-BasketPythonInfo (Join-Path $script:BasketProjectRoot '.venv/Scripts/python.exe') -RequireVenv
    if ($null -ne $existing) { return $existing }
    $checked = @{}
    foreach ($candidate in Get-BasketPythonCandidates) {
        if ($checked.ContainsKey($candidate)) { continue }
        $checked[$candidate] = $true
        $info = Get-BasketPythonInfo $candidate
        if ($null -ne $info) { return $info }
    }
    return $null
}

function Get-BasketReleaseVersion {
    param([string]$IndexHtml)
    $versions = @([regex]::Matches($IndexHtml, 'href="(3\.14\.\d+)/"') |
        ForEach-Object { [version]$_.Groups[1].Value } |
        Sort-Object -Descending -Unique)
    if ($versions.Count -eq 0) { throw 'На python.org не найдена стабильная версия Python 3.14.' }
    return $versions[0].ToString()
}

function Install-BasketPythonDirect {
    Write-Host 'WinGet не найден. Скачиваю установщик с официального сайта python.org.'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $index = Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/' -UseBasicParsing -TimeoutSec 60
    $release = Get-BasketReleaseVersion $index.Content
    $architecture = "$env:PROCESSOR_ARCHITECTURE $env:PROCESSOR_ARCHITEW6432"
    $suffix = if ($architecture -match 'ARM64') { '-arm64' } elseif ($architecture -match 'AMD64') { '-amd64' } else { '' }
    $downloadDir = Join-Path ([IO.Path]::GetTempPath()) ('basket-python-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $downloadDir | Out-Null
    try {
        $installer = Join-Path $downloadDir "python-$release$suffix.exe"
        $url = "https://www.python.org/ftp/python/$release/python-$release$suffix.exe"
        Write-Host "Загрузка Python $release. Дождитесь завершения установки."
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing -TimeoutSec 180
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
            $signature.SignerCertificate.Subject -notmatch '(?:^|,\s*)CN=Python Software Foundation(?:,|$)') {
            throw 'Подпись Python Software Foundation не подтверждена. Установщик не запущен.'
        }
        $target = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314'
        $arguments = '/passive /norestart InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0 TargetDir="' + $target + '"'
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Установка не завершена, код $($process.ExitCode). Если вы её отменили, запустите батник позже."
        }
    }
    finally {
        Remove-Item -LiteralPath $downloadDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Install-BasketPython {
    $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'Устанавливаю Python 3.14 через WinGet для текущего пользователя.'
        & $winget.Source install --id Python.Python.3.14 --exact --source winget --scope user --silent --accept-source-agreements --accept-package-agreements | Out-Host
        $installExitCode = $LASTEXITCODE
        if ($installExitCode -ne 0) {
            # Respect cancellation: never start another installer after an error.
            throw "WinGet завершился с кодом $installExitCode. Проверьте интернет и сообщения выше, затем повторите запуск."
        }
    }
    else { Install-BasketPythonDirect }
}

function Repair-BasketEnvironment {
    $environment = Join-Path $script:BasketProjectRoot '.venv'
    if (-not (Test-Path -LiteralPath $environment)) { return }
    $healthy = Get-BasketPythonInfo (Join-Path $environment 'Scripts/python.exe') -RequireVenv
    if ($null -ne $healthy) { return }
    $backupName = '.venv-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
    Write-Host 'Старое окружение не работает на этом компьютере. Подготовлю новое.'
    Move-Item -LiteralPath $environment -Destination (Join-Path $script:BasketProjectRoot $backupName)
    Write-Host "Прежнее окружение сохранено в $backupName. Папка instance с данными сохраняется."
}

function Start-BasketApplication {
    param($PythonInfo)
    & $PythonInfo.path (Join-Path $script:BasketProjectRoot 'launcher.py') | Out-Host
    return $LASTEXITCODE
}

function Invoke-BasketSetup {
    param([switch]$InstallOnly)
    try {
        $basketPython = Find-BasketPython
        if ($null -eq $basketPython) {
            Write-Host 'Подходящий Python не найден. Нужна стабильная версия 3.12, 3.13 или 3.14.'
            Install-BasketPython
            # Re-read absolute paths; no terminal restart or PATH refresh needed.
            $basketPython = Find-BasketPython
            if ($null -eq $basketPython) {
                throw 'После установки Python пока не найден. Перезагрузите Windows, если установщик попросил, и повторите запуск.'
            }
        }
        Write-Host "Python $($basketPython.version) готов к работе."
        if ($InstallOnly) {
            Write-Host 'Теперь запустите START_WINDOWS.bat. Он установит библиотеки приложения и откроет сайт.'
            return 0
        }
        Repair-BasketEnvironment
        return (Start-BasketApplication $basketPython)
    }
    catch {
        Write-Host ''
        Write-Host "Не удалось подготовить приложение: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host 'Можно установить Python 3.14 вручную с https://www.python.org/downloads/windows/ и повторить запуск.'
        Write-Host 'Ваши продукты, магазины и цены в папке instance не удаляются.'
        return 1
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-BasketSetup -InstallOnly:$InstallOnly)
}
