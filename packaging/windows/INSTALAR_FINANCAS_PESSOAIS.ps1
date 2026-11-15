[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [switch]$NoShortcuts,
    [switch]$NoLaunch,
    [switch]$NonInteractive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath(
        [Environment]::ExpandEnvironmentVariables($Path)
    ).TrimEnd("\")
}

function Test-IsSameOrChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Parent
    )

    $candidatePath = (Get-FullPath $Candidate) + "\"
    $parentPath = (Get-FullPath $Parent) + "\"
    return $candidatePath.StartsWith(
        $parentPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Get-PackageFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    # Get-FileHash depends on module auto-loading. A Windows PowerShell child
    # can inherit a PowerShell 7 PSModulePath, so use the shared .NET primitive.
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [IO.File]::OpenRead($Path)
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace("-", "")
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}

function Read-PackageManifest {
    param([Parameter(Mandatory = $true)][string]$Root)

    $requiredFiles = @(
        "LiquidGlass.exe",
        "RELEASE_MANIFEST.json",
        "_internal\python314.dll",
        "_internal\VCRUNTIME140.dll",
        "_internal\VCRUNTIME140_1.dll",
        "_internal\sqlite3.dll",
        "_internal\_sqlite3.pyd",
        "_internal\PySide6\Qt6Core.dll",
        "_internal\PySide6\Qt6Gui.dll",
        "_internal\PySide6\Qt6Widgets.dll",
        "_internal\PySide6\Qt6Network.dll",
        "_internal\PySide6\Qt6Multimedia.dll",
        "_internal\PySide6\plugins\platforms\qwindows.dll",
        "_internal\cryptography\hazmat\bindings\_rust.pyd",
        "_internal\assets\style.qss"
    )
    foreach ($relativePath in $requiredFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $relativePath) -PathType Leaf)) {
            throw (
                "Pacote incompleto: falta " + $relativePath + ". " +
                "Extrai novamente TODO o ZIP."
            )
        }
    }

    $manifestPath = Join-Path $Root "RELEASE_MANIFEST.json"
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if (
        [string]$manifest.target.operating_system -ne "Windows" -or
        [int]$manifest.target.bits -ne 64 -or
        [bool]$manifest.target.requires_python_on_target
    ) {
        throw "O manifesto nao identifica um pacote Windows autocontido de 64 bits."
    }
    if (-not [bool]$manifest.self_test.passed) {
        throw "O pacote nao tem um auto-teste de construcao valido."
    }
    if (
        -not [bool]$manifest.installation.dependencies_embedded -or
        [bool]$manifest.installation.requires_admin
    ) {
        throw "O manifesto nao confirma uma instalacao autocontida sem administrador."
    }

    if ([string]$manifest.package_integrity.algorithm -ne "SHA-256") {
        throw "O manifesto nao inclui a verificacao integral SHA-256 esperada."
    }
    $integrityEntries = @($manifest.package_integrity.entries)
    if (
        $integrityEntries.Count -lt 1 -or
        $integrityEntries.Count -ne [int]$manifest.package_integrity.file_count
    ) {
        throw "A lista de integridade do pacote e invalida."
    }

    $rootPrefix = (Get-FullPath $Root) + "\"
    $listedPaths = New-Object `
        "System.Collections.Generic.HashSet[string]" `
        ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $integrityEntries) {
        $relativePath = ([string]$entry.path).Replace("/", "\")
        $segments = @($relativePath.Split("\"))
        if (
            [string]::IsNullOrWhiteSpace($relativePath) -or
            [System.IO.Path]::IsPathRooted($relativePath) -or
            $segments -contains "" -or
            $segments -contains "." -or
            $segments -contains ".." -or
            $relativePath.Equals(
                "RELEASE_MANIFEST.json",
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            -not $listedPaths.Add($relativePath)
        ) {
            throw "O manifesto contem um caminho de ficheiro invalido."
        }

        $filePath = Get-FullPath (Join-Path $Root $relativePath)
        if (
            -not $filePath.StartsWith(
                $rootPrefix,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            -not (Test-Path -LiteralPath $filePath -PathType Leaf)
        ) {
            throw "Pacote incompleto: falta $relativePath."
        }
        $fileInfo = Get-Item -LiteralPath $filePath
        if ([long]$fileInfo.Length -ne [long]$entry.size) {
            throw "O tamanho de $relativePath nao corresponde ao manifesto."
        }
        $actualFileHash = Get-PackageFileSha256 -Path $filePath
        if (-not $actualFileHash.Equals(
            [string]$entry.sha256,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "A verificacao SHA-256 falhou em $relativePath."
        }
    }

    $actualFiles = @(
        Get-ChildItem -LiteralPath $Root -Recurse -File |
            Where-Object {
                -not $_.FullName.Equals(
                    $manifestPath,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    if ($actualFiles.Count -ne $listedPaths.Count) {
        throw "O pacote contem ficheiros ausentes ou nao reconhecidos."
    }
    foreach ($actualFile in $actualFiles) {
        $relativePath = $actualFile.FullName.Substring($rootPrefix.Length)
        if (-not $listedPaths.Contains($relativePath)) {
            throw "O pacote contem um ficheiro nao reconhecido: $relativePath."
        }
    }

    $executablePath = Join-Path $Root "LiquidGlass.exe"
    $actualHash = Get-PackageFileSha256 -Path $executablePath
    $expectedHash = [string]$manifest.executable_sha256
    if (-not $actualHash.Equals(
        $expectedHash,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "A verificacao SHA-256 do executavel falhou. Nao o executes."
    }
    return $manifest
}

function New-AppShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$ShortcutPath,
        [Parameter(Mandatory = $true)][string]$ExecutablePath
    )

    $shortcutFolder = Split-Path -Parent $ShortcutPath
    New-Item -ItemType Directory -Path $shortcutFolder -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $ExecutablePath
    $shortcut.WorkingDirectory = Split-Path -Parent $ExecutablePath
    $shortcut.Description = "Financas Pessoais - Liquid Glass"
    $shortcut.IconLocation = $ExecutablePath + ",0"
    $shortcut.Save()
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "Este pacote so pode ser instalado no Windows."
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Este pacote requer Windows de 64 bits."
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "O Windows nao disponibilizou a pasta LOCALAPPDATA."
    }

    $sourceRoot = Get-FullPath (Split-Path -Parent $MyInvocation.MyCommand.Path)
    if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        $InstallRoot = Join-Path `
            $env:LOCALAPPDATA `
            "Programs\FinancasPessoaisLiquidGlass"
    }
    $InstallRoot = Get-FullPath $InstallRoot
    $installParent = Split-Path -Parent $InstallRoot
    $dataRoot = Get-FullPath (Join-Path $env:LOCALAPPDATA "FinancasPessoais")

    if ($InstallRoot -eq [System.IO.Path]::GetPathRoot($InstallRoot)) {
        throw "A raiz de um disco nao pode ser usada como destino."
    }
    if (Test-IsSameOrChildPath -Candidate $InstallRoot -Parent $dataRoot) {
        throw (
            "A pasta do programa tem de ficar separada dos dados financeiros. " +
            "Escolhe outro destino."
        )
    }
    if (
        $InstallRoot -ne $sourceRoot -and
        (Test-IsSameOrChildPath -Candidate $InstallRoot -Parent $sourceRoot)
    ) {
        throw "O destino nao pode ficar dentro da pasta temporaria extraida."
    }

    $sourceManifest = Read-PackageManifest $sourceRoot
    $version = [string]$sourceManifest.version
    Write-Host "Financas Pessoais - Liquid Glass v$version" -ForegroundColor Cyan
    Write-Host "Python, PySide6, Qt, SQLite e cryptography ja estao no pacote."
    Write-Host "Nao sera instalado nenhum pacote Python no Windows."
    Write-Host "Destino: $InstallRoot"

    $sameLocation = $InstallRoot.Equals(
        $sourceRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )
    if (-not $sameLocation) {
        if (Test-Path -LiteralPath $InstallRoot) {
            $running = @(Get-Process -Name "LiquidGlass" -ErrorAction SilentlyContinue)
            if ($running.Count -gt 0) {
                throw "Fecha primeiro todas as janelas da aplicacao Liquid Glass."
            }
            if (
                -not (Test-Path -LiteralPath (Join-Path $InstallRoot "LiquidGlass.exe")) -or
                -not (Test-Path -LiteralPath (Join-Path $InstallRoot "RELEASE_MANIFEST.json"))
            ) {
                throw (
                    "O destino ja existe mas nao e uma instalacao reconhecida. " +
                    "Nenhum ficheiro foi substituido."
                )
            }
        }

        New-Item -ItemType Directory -Path $installParent -Force | Out-Null
        $identity = [Guid]::NewGuid().ToString("N")
        $stagingRoot = $InstallRoot + ".installing-" + $identity
        $previousRoot = $InstallRoot + ".previous-" + $identity
        New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
        try {
            foreach ($item in Get-ChildItem -LiteralPath $sourceRoot -Force) {
                Copy-Item `
                    -LiteralPath $item.FullName `
                    -Destination $stagingRoot `
                    -Recurse `
                    -Force
            }
            Read-PackageManifest $stagingRoot | Out-Null

            $hadPrevious = Test-Path -LiteralPath $InstallRoot
            if ($hadPrevious) {
                Move-Item -LiteralPath $InstallRoot -Destination $previousRoot
            }
            try {
                Move-Item -LiteralPath $stagingRoot -Destination $InstallRoot
                Read-PackageManifest $InstallRoot | Out-Null
            } catch {
                if (Test-Path -LiteralPath $InstallRoot) {
                    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
                }
                if (Test-Path -LiteralPath $previousRoot) {
                    Move-Item -LiteralPath $previousRoot -Destination $InstallRoot
                }
                throw
            }
            if (Test-Path -LiteralPath $previousRoot) {
                Remove-Item -LiteralPath $previousRoot -Recurse -Force
            }
        } finally {
            if (Test-Path -LiteralPath $stagingRoot) {
                Remove-Item -LiteralPath $stagingRoot -Recurse -Force
            }
        }
    }

    $installedExecutable = Join-Path $InstallRoot "LiquidGlass.exe"
    $shortcutWarnings = @()
    if (-not $NoShortcuts) {
        $desktop = [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::DesktopDirectory
        )
        $programs = [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::Programs
        )
        foreach ($shortcutPath in @(
            (Join-Path $desktop "Financas Pessoais - Liquid Glass.lnk"),
            (Join-Path $programs "Financas Pessoais\Liquid Glass.lnk")
        )) {
            try {
                New-AppShortcut `
                    -ShortcutPath $shortcutPath `
                    -ExecutablePath $installedExecutable
            } catch {
                $shortcutWarnings += $shortcutPath
                Write-Warning "Nao foi possivel criar o atalho: $shortcutPath"
            }
        }
    }

    Write-Host "Instalacao verificada com sucesso." -ForegroundColor Green
    Write-Host "Os dados continuam separados em %LOCALAPPDATA%\FinancasPessoais."
    if ($shortcutWarnings.Count -gt 0) {
        Write-Host "A aplicacao ficou instalada em: $installedExecutable"
    }
    if (-not $NoLaunch) {
        Start-Process -FilePath $installedExecutable -WorkingDirectory $InstallRoot
    }
    exit 0
} catch {
    Write-Host ""
    Write-Host ("ERRO: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host "Nenhum dado financeiro foi apagado."
    if (-not $NonInteractive) {
        Read-Host "Prime Enter para fechar"
    }
    exit 1
}
