[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [Parameter(Mandatory = $true)][string]$InstallRoot,
    [Parameter(Mandatory = $true)][ValidateRange(1, 2147483647)][int]$ParentProcessId,
    [Parameter(Mandatory = $true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$ExpectedVersion,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedManifestSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedInstallerSha256,
    [switch]$NoLaunch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$manifestStream = $null
$installerStream = $null

function Assert-NotReparsePoint {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        $attributes = (Get-Item -LiteralPath $Path -Force).Attributes
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "A atualizacao nao aceita ligacoes de pastas ou ficheiros no destino temporario."
        }
    }
}

function Assert-StreamHash {
    param([IO.FileStream]$Stream, [string]$Expected)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $actual = [BitConverter]::ToString($hasher.ComputeHash($Stream)).Replace('-', '')
        if (-not $actual.Equals($Expected, [StringComparison]::OrdinalIgnoreCase)) {
            throw "O pacote temporario foi alterado depois da descarga."
        }
        $Stream.Position = 0
    } finally {
        $hasher.Dispose()
    }
}

try {
    # Only the standard per-user installation is eligible for in-app replacement.
    # The parent has already completed its normal protected database shutdown.
    $expectedRoot = [IO.Path]::GetFullPath(
        (Join-Path $env:LOCALAPPDATA "Programs\FinancasPessoaisLiquidGlass")
    ).TrimEnd('\')
    $targetRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
    $sourceRoot = [IO.Path]::GetFullPath($PackageRoot).TrimEnd('\')
    $workRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path)).TrimEnd('\')
    $expectedSource = Join-Path $workRoot "extracted\LiquidGlass"
    $sentinel = Join-Path $workRoot ".update-workdir"
    if (
        -not (Split-Path -Leaf $workRoot).StartsWith('financas-update-', [StringComparison]::Ordinal) -or
        -not $sourceRoot.Equals($expectedSource, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $sentinel -PathType Leaf) -or
        (Get-Content -LiteralPath $sentinel -Raw) -ne 'FinancasPessoais'
    ) {
        throw "A pasta temporaria de atualizacao nao e reconhecida."
    }
    if (-not $targetRoot.Equals($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "O destino nao corresponde a instalacao gerida da aplicacao."
    }
    if ($targetRoot.Equals($sourceRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "O pacote temporario nao pode coincidir com a instalacao."
    }
    $manifestPath = Join-Path $sourceRoot "RELEASE_MANIFEST.json"
    $installer = Join-Path $sourceRoot "INSTALAR_FINANCAS_PESSOAIS.ps1"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "O pacote nao inclui o instalador."
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while (Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue) {
        if ([DateTime]::UtcNow -gt $deadline) {
            throw "A aplicacao nao fechou. A atualizacao foi cancelada."
        }
        Start-Sleep -Milliseconds 500
    }
    foreach ($checkedPath in @(
        $workRoot, (Split-Path -Parent $sourceRoot), $sourceRoot,
        (Split-Path -Parent $targetRoot), $targetRoot, $manifestPath, $installer
    )) {
        Assert-NotReparsePoint $checkedPath
    }
    # Pin the trusted inventory and the executable installer from the verified ZIP.
    # Read-only shared handles deny mutation/deletion until the installer exits.
    $manifestStream = [IO.File]::Open($manifestPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $installerStream = [IO.File]::Open($installer, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    Assert-StreamHash $manifestStream $ExpectedManifestSha256
    Assert-StreamHash $installerStream $ExpectedInstallerSha256
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ([string]$manifest.version -ne $ExpectedVersion) {
        throw "A versao do pacote de atualizacao mudou."
    }
    # A separate PowerShell process preserves the installer's exit code contract.
    # Argument-array invocation does not evaluate paths as PowerShell commands.
    $installerArguments = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $installer, '-InstallRoot', $targetRoot, '-NoShortcuts', '-NonInteractive'
    )
    if ($NoLaunch) { $installerArguments += '-NoLaunch' }
    & "$PSHOME\powershell.exe" @installerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "O instalador recusou a atualizacao. Consulta o registo update.log."
    }
    exit 0
} catch {
    Write-Error -Message ("Atualizacao cancelada: " + $_.Exception.Message) -ErrorAction Continue
    exit 1
} finally {
    if ($null -ne $manifestStream) { $manifestStream.Dispose() }
    if ($null -ne $installerStream) { $installerStream.Dispose() }
}
