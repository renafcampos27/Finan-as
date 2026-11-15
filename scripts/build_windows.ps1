param(
    [switch]$KeepWorkFolders,
    [string]$OutputDirectory = "."
)

$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath(
    (Join-Path -Path $PSScriptRoot -ChildPath "..")
)
Set-Location -LiteralPath $projectRoot

$packageOutput = [System.IO.Path]::GetFullPath(
    (Join-Path -Path $projectRoot -ChildPath $OutputDirectory)
)
# Keep build output under the checkout, where generated paths can be excluded
# explicitly. CI uses artifacts/release so checksum files never dirty source.
if (
    $packageOutput -ne $projectRoot -and
    -not $packageOutput.StartsWith(
        $projectRoot.TrimEnd("\") + "\",
        [System.StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "A pasta de distribuicao tem de ficar dentro do projeto."
}
New-Item -ItemType Directory -Path $packageOutput -Force | Out-Null

function Get-PinnedVersion {
    param(
        [Parameter(Mandatory = $true)][string]$RequirementsFile,
        [Parameter(Mandatory = $true)][string]$PackageName
    )

    $escapedName = [System.Text.RegularExpressions.Regex]::Escape($PackageName)
    $match = Select-String `
        -LiteralPath $RequirementsFile `
        -Pattern "^$escapedName==([0-9][0-9A-Za-z.-]*)$" |
        Select-Object -First 1
    if (-not $match) {
        throw "Dependencia sem versao fixa: $PackageName"
    }
    return $match.Matches[0].Groups[1].Value
}

function Remove-GeneratedPath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $target = [System.IO.Path]::GetFullPath(
        (Join-Path -Path $projectRoot -ChildPath $RelativePath)
    )
    $rootPrefix = $projectRoot.TrimEnd("\") + "\"
    if (-not $target.StartsWith(
        $rootPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Caminho gerado fora do projeto: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

$pythonExecutable = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$gitExecutable = (Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$runtimeProbe = @'
import json
import os
import platform
import struct
import sys
import sysconfig
from importlib.metadata import version
from pathlib import Path
from PySide6.QtCore import qVersion
from scripts.bundle_provenance import trusted_build_path

packages = {
    name: version(name)
    for name in (
        "PySide6",
        "PySide6_Addons",
        "PySide6_Essentials",
        "shiboken6",
        "cryptography",
        "argon2-cffi",
        "numpy",
        "cffi",
        "PyInstaller",
        "pytest",
        "ruff",
        "pip-audit",
    )
}
print(json.dumps({
    "python": platform.python_version(),
    "implementation": platform.python_implementation(),
    "architecture": platform.machine(),
    "bits": struct.calcsize("P") * 8,
    "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    "qt": qVersion(),
    "packages": packages,
    "isolated_venv": sys.prefix != sys.base_prefix and "include-system-site-packages = false" in (Path(sys.prefix) / "pyvenv.cfg").read_text(encoding="utf-8").lower(),
    "native_search_path": trusted_build_path(Path(sys.executable), Path(sys.base_prefix), Path(os.environ["SystemRoot"])),
}))
'@
$runtimeJson = $runtimeProbe | & $pythonExecutable -
if ($LASTEXITCODE -ne 0 -or -not $runtimeJson) {
    throw "Nao foi possivel validar o Python e as dependencias do projeto."
}
$runtimeInfo = $runtimeJson | ConvertFrom-Json
$env:PATH = [string]$runtimeInfo.native_search_path
$env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot "build\pyinstaller-cache"
foreach ($variable in @("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH", "QML_IMPORT_PATH")) {
    [Environment]::SetEnvironmentVariable($variable, $null, "Process")
}
if (-not [bool]$runtimeInfo.isolated_venv) {
    throw "A construcao requer um ambiente virtual isolado, sem pacotes globais."
}
$pythonVersion = [Version]$runtimeInfo.python
if (
    $pythonVersion.Major -ne 3 -or
    $pythonVersion.Minor -ne 14 -or
    $pythonVersion -lt [Version]"3.14.7"
) {
    throw "A construcao requer CPython 3.14.7 ou posterior da serie 3.14."
}
if ([int]$runtimeInfo.bits -ne 64) {
    throw "A distribuicao oficial requer Python e Windows de 64 bits."
}
if ([bool]$runtimeInfo.free_threaded) {
    throw "A distribuicao oficial requer CPython normal, nao free-threaded."
}

$expectedPySide = Get-PinnedVersion `
    -RequirementsFile "$projectRoot\requirements.txt" `
    -PackageName "PySide6"
$expectedCryptography = Get-PinnedVersion `
    -RequirementsFile "$projectRoot\requirements.txt" `
    -PackageName "cryptography"
$expectedPyInstaller = Get-PinnedVersion `
    -RequirementsFile "$projectRoot\requirements-dev.txt" `
    -PackageName "PyInstaller"
$expectedPytest = Get-PinnedVersion `
    -RequirementsFile "$projectRoot\requirements-dev.txt" `
    -PackageName "pytest"
$expectedRuff = Get-PinnedVersion `
    -RequirementsFile "$projectRoot\requirements-dev.txt" `
    -PackageName "ruff"
$expectedPipAudit = Get-PinnedVersion `
    -RequirementsFile "$projectRoot\requirements-dev.txt" `
    -PackageName "pip-audit"
if ([string]$runtimeInfo.packages.PySide6 -ne $expectedPySide) {
    throw "PySide6 instalado nao corresponde a requirements.txt."
}
if ([string]$runtimeInfo.packages.cryptography -ne $expectedCryptography) {
    throw "cryptography instalado nao corresponde a requirements.txt."
}
$expectedArgon2 = Get-PinnedVersion `
    -RequirementsFile "$projectRoot\requirements.txt" `
    -PackageName "argon2-cffi"
if ([string]$runtimeInfo.packages."argon2-cffi" -ne $expectedArgon2) {
    throw "argon2-cffi instalado nao corresponde a requirements.txt."
}
$expectedNumPy = Get-PinnedVersion `
    -RequirementsFile "$projectRoot\requirements.txt" `
    -PackageName "numpy"
if ([string]$runtimeInfo.packages.numpy -ne $expectedNumPy) {
    throw "NumPy instalado nao corresponde a requirements.txt."
}
if ([string]$runtimeInfo.packages.PyInstaller -ne $expectedPyInstaller) {
    throw "PyInstaller instalado nao corresponde a requirements-dev.txt."
}
if ([string]$runtimeInfo.packages.pytest -ne $expectedPytest) {
    throw "pytest instalado nao corresponde a requirements-dev.txt."
}
if ([string]$runtimeInfo.packages.ruff -ne $expectedRuff) {
    throw "Ruff instalado nao corresponde a requirements-dev.txt."
}
if ([string]$runtimeInfo.packages.'pip-audit' -ne $expectedPipAudit) {
    throw "pip-audit instalado nao corresponde a requirements-dev.txt."
}

$pipCheck = & $pythonExecutable -m pip check 2>&1
$realConflicts = $pipCheck | Where-Object { $_ -notmatch '^\s*cptr\s' }
if ($LASTEXITCODE -ne 0 -and $realConflicts) {
    throw "O ambiente Python tem dependencias incompativeis: $($realConflicts -join "`n")"
}
Write-Output ($pipCheck | Where-Object { $_ -notmatch '^\s*cptr\s' })


$versionLine = Select-String `
    -LiteralPath "$projectRoot\utils\version.py" `
    -Pattern '^APP_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$' |
    Select-Object -First 1
if (-not $versionLine) {
    throw "Nao foi possivel determinar a versao da aplicacao."
}
$appVersion = $versionLine.Matches[0].Groups[1].Value

# Package only a committed, tagged source snapshot. Recheck before publishing.
if (-not $gitExecutable) {
    $gitExecutable = (Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
}
$sourceCommit = & $gitExecutable rev-parse --verify HEAD
if ($LASTEXITCODE -ne 0) { throw "A construcao requer um repositorio Git." }
$sourceTree = & $gitExecutable rev-parse 'HEAD^{tree}'
if ($LASTEXITCODE -ne 0) { throw "Nao foi possivel identificar a arvore Git." }
$sourceTag = "v$appVersion"
$tagCommit = & $gitExecutable rev-parse --verify "refs/tags/$sourceTag^{commit}"
if ($LASTEXITCODE -ne 0 -or $tagCommit -ne $sourceCommit) {
    throw "A tag $sourceTag tem de identificar o commit atual antes da construcao."
}
function Assert-SourceSnapshot {
    $currentCommit = & $gitExecutable rev-parse --verify HEAD
    if ($LASTEXITCODE -ne 0 -or $currentCommit -ne $sourceCommit) {
        throw "A revisao Git mudou durante a construcao."
    }
    $sourceChanges = & $gitExecutable status --porcelain --untracked-files=normal
    if ($LASTEXITCODE -ne 0 -or $sourceChanges) {
        throw "A construcao requer uma arvore Git limpa, incluindo ficheiros novos."
    }
}
Assert-SourceSnapshot

# Retain existing distribution files until the replacement has passed all tests.
$sourceInventoryProbe = @'
import json
from pathlib import Path
from scripts.build_validation_candidate import source_inputs
from scripts.package_integrity import inventory_digest, sha256_file
root = Path.cwd()
entries = [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size, "sha256": sha256_file(p)} for p in source_inputs(root)]
print(json.dumps({"entries": entries, "inventory_sha256": inventory_digest(entries)}))
'@
$sourceInventoryJson = $sourceInventoryProbe | & $pythonExecutable -
if ($LASTEXITCODE -ne 0 -or -not $sourceInventoryJson) {
    throw "Nao foi possivel inventariar os ficheiros de origem."
}
$sourceInventory = $sourceInventoryJson | ConvertFrom-Json

$previousPackages = @(
    "LiquidGlass_App.zip", "LiquidGlass_App.sha256.txt",
    "LiquidGlass_App_v$appVersion.zip", "LiquidGlass_App_v$appVersion.sha256.txt"
)
$recoveryFolder = Join-Path $projectRoot (
    "artifacts\package-recovery\" + [Guid]::NewGuid().ToString("N")
)
foreach ($previousPackage in $previousPackages) {
    $previousPath = Join-Path $packageOutput $previousPackage
    if (Test-Path -LiteralPath $previousPath -PathType Leaf) {
        New-Item -ItemType Directory -Path $recoveryFolder -Force | Out-Null
        Copy-Item -LiteralPath $previousPath -Destination $recoveryFolder
    }
}
if (Test-Path -LiteralPath $recoveryFolder) {
    Write-Output "Pacotes anteriores preservados: $recoveryFolder"
}
Remove-GeneratedPath "build"
Remove-GeneratedPath "dist"

$versionParts = @($appVersion.Split('.') | ForEach-Object { [int]$_ })
$companyName = "Finan$([char]0x00E7)as Pessoais"
$productName = "$companyName $([char]0x2014) Liquid Glass"
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($($versionParts[0]), $($versionParts[1]), $($versionParts[2]), 0),
    prodvers=($($versionParts[0]), $($versionParts[1]), $($versionParts[2]), 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '$companyName'),
        StringStruct('FileDescription', '$productName'),
        StringStruct('FileVersion', '$appVersion'),
        StringStruct('InternalName', 'LiquidGlass'),
        StringStruct('OriginalFilename', 'LiquidGlass.exe'),
        StringStruct('ProductName', '$productName'),
        StringStruct('ProductVersion', '$appVersion')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
New-Item -ItemType Directory -Path "$projectRoot\build" -Force | Out-Null
Set-Content `
    -LiteralPath "$projectRoot\build\version_info.txt" `
    -Value $versionInfo `
    -Encoding UTF8

& $pythonExecutable -m PyInstaller --noconfirm --clean LiquidGlass.spec
if ($LASTEXITCODE -ne 0) {
    throw "A construcao do executavel falhou."
}

$applicationFolder = Join-Path $projectRoot "dist\LiquidGlass"
if (-not (Test-Path -LiteralPath "$applicationFolder\LiquidGlass.exe")) {
    throw "O executavel esperado nao foi criado."
}

$requiredBundleFiles = @(
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
    "_internal\_argon2_cffi_bindings\_ffi.pyd",
    "_internal\assets\style.qss"
)
foreach ($relativeFile in $requiredBundleFiles) {
    if (-not (Test-Path -LiteralPath "$applicationFolder\$relativeFile")) {
        throw "O pacote nao incluiu a dependencia obrigatoria: $relativeFile"
    }
}
if (-not (Get-ChildItem -LiteralPath "$applicationFolder\_internal\numpy" -Recurse -File -Filter "*_multiarray_umath*.pyd")) {
    throw "O pacote nao incluiu o motor nativo do NumPy."
}

function Assert-FunctionalSelfTest {
    param([Parameter(Mandatory = $true)]$Data)
    if ($Data.ok -ne $true -or $Data.frozen -ne $true -or [string]$Data.numpy -ne $expectedNumPy) {
        throw "O auto-teste nao confirmou o runtime congelado e NumPy esperado."
    }
    foreach ($operation in @("numpy_simulation", "numpy_health", "lgbk1_roundtrip", "lgbk2_roundtrip", "window_open_close")) {
        if ($Data.functional_checks.$operation.passed -ne $true) {
            throw "O auto-teste nao executou a operacao obrigatoria: $operation"
        }
    }
}

$temporaryPrefix = [System.IO.Path]::GetFullPath(
    [System.IO.Path]::GetTempPath()
).TrimEnd("\") + "\"
$selfTestFolder = [System.IO.Path]::GetFullPath(
    (Join-Path `
        -Path $temporaryPrefix `
        -ChildPath ("LiquidGlassBuildSelfTest_" + [Guid]::NewGuid().ToString("N")))
)
if (-not $selfTestFolder.StartsWith(
    $temporaryPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Diretorio temporario invalido para o auto-teste do pacote."
}
New-Item -ItemType Directory -Path $selfTestFolder -Force | Out-Null
$selfTestReport = Join-Path $selfTestFolder "package_self_test.json"
$previousStateDirectory = [Environment]::GetEnvironmentVariable(
    "FINANCAS_PESSOAIS_STATE_DIR",
    "Process"
)
$previousReportPath = [Environment]::GetEnvironmentVariable(
    "LIQUIDGLASS_SELF_TEST_REPORT",
    "Process"
)
$selfTestData = $null
$previousTestingFlag = $env:FINANCAS_PESSOAIS_TESTING
try {
    $env:FINANCAS_PESSOAIS_TESTING = "1"
    [Environment]::SetEnvironmentVariable(
        "FINANCAS_PESSOAIS_STATE_DIR",
        (Join-Path $selfTestFolder "profile"),
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "LIQUIDGLASS_SELF_TEST_REPORT",
        $selfTestReport,
        "Process"
    )
    $selfTestProcess = Start-Process `
        -FilePath "$applicationFolder\LiquidGlass.exe" `
        -ArgumentList "--package-self-test" `
        -WindowStyle Hidden `
        -PassThru
    if (-not $selfTestProcess.WaitForExit(90000)) {
        Stop-Process -Id $selfTestProcess.Id -Force
        throw "O auto-teste do executavel excedeu 90 segundos."
    }
    $selfTestProcess.Refresh()
    if ($selfTestProcess.ExitCode -ne 0) {
        throw "O auto-teste do executavel terminou com codigo $($selfTestProcess.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $selfTestReport)) {
        throw "O executavel nao criou o relatorio do auto-teste."
    }
    $selfTestData = Get-Content -LiteralPath $selfTestReport -Raw |
        ConvertFrom-Json
    if (-not $selfTestData.ok -or [int]$selfTestData.page_exports_loaded -lt 1) {
        throw "O relatorio do auto-teste do executavel e invalido."
    }
    Assert-FunctionalSelfTest -Data $selfTestData
    New-Item -ItemType Directory -Path "$applicationFolder\evidence" -Force | Out-Null
    Copy-Item -LiteralPath $selfTestReport -Destination "$applicationFolder\evidence\package_self_test.json"
} finally {
    [Environment]::SetEnvironmentVariable("FINANCAS_PESSOAIS_TESTING", $previousTestingFlag, "Process")
    [Environment]::SetEnvironmentVariable(
        "FINANCAS_PESSOAIS_STATE_DIR",
        $previousStateDirectory,
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "LIQUIDGLASS_SELF_TEST_REPORT",
        $previousReportPath,
        "Process"
    )
    if (Test-Path -LiteralPath $selfTestFolder) {
        Remove-Item -LiteralPath $selfTestFolder -Recurse -Force
    }
}

$certificateThumbprint = $env:LIQUIDGLASS_SIGN_CERT_THUMBPRINT
if ($certificateThumbprint) {
    $certificate = Get-Item `
        -LiteralPath "Cert:\CurrentUser\My\$certificateThumbprint" `
        -ErrorAction Stop
    $signature = Set-AuthenticodeSignature `
        -LiteralPath "$applicationFolder\LiquidGlass.exe" `
        -Certificate $certificate `
        -TimestampServer "http://timestamp.digicert.com" `
        -HashAlgorithm SHA256
    if ($signature.Status -ne "Valid") {
        throw "A assinatura do executavel falhou: $($signature.StatusMessage)"
    }
} else {
    Write-Warning (
        "Executavel sem assinatura: define LIQUIDGLASS_SIGN_CERT_THUMBPRINT " +
        "com um certificado de assinatura de codigo para distribuicao publica."
    )
}

Copy-Item -LiteralPath "$projectRoot\README.md" -Destination $applicationFolder
Copy-Item `
    -LiteralPath "$projectRoot\COMO_INSTALAR_NOUTRO_PC.txt" `
    -Destination $applicationFolder
$distributionLaunchers = @(
    "INSTALAR_FINANCAS_PESSOAIS.bat",
    "INSTALAR_FINANCAS_PESSOAIS.ps1",
    "APLICAR_ATUALIZACAO.ps1",
    "ABRIR_FINANCAS_PESSOAIS.bat"
)
foreach ($launcher in $distributionLaunchers) {
    $sourceLauncher = Join-Path "$projectRoot\packaging\windows" $launcher
    if (-not (Test-Path -LiteralPath $sourceLauncher -PathType Leaf)) {
        throw "Ficheiro de distribuicao em falta: $launcher"
    }
    Copy-Item -LiteralPath $sourceLauncher -Destination $applicationFolder
}

$exeHash = (Get-FileHash `
    -LiteralPath "$applicationFolder\LiquidGlass.exe" `
    -Algorithm SHA256).Hash
$manifest = [ordered]@{
    product = $productName
    version = $appVersion
    built_at = (Get-Date).ToString("o")
    source = [ordered]@{
        kind = "git_release_source"
        commit = [string]$sourceCommit
        tree = [string]$sourceTree
        tag = [string]$sourceTag
        dirty = $false
        snapshot_directory = $projectRoot
        entries = @($sourceInventory.entries)
        inventory_sha256 = [string]$sourceInventory.inventory_sha256
    }
    executable = "LiquidGlass.exe"
    executable_sha256 = $exeHash
    signed = ((Get-AuthenticodeSignature `
        -LiteralPath "$applicationFolder\LiquidGlass.exe").Status -eq "Valid")
    target = [ordered]@{
        operating_system = "Windows"
        architecture = [string]$runtimeInfo.architecture
        bits = [int]$runtimeInfo.bits
        bundle_mode = "one-folder"
        requires_python_on_target = $false
    }
    runtime = [ordered]@{
        python = [string]$runtimeInfo.python
        implementation = [string]$runtimeInfo.implementation
        free_threaded = [bool]$runtimeInfo.free_threaded
        pyside6 = [string]$runtimeInfo.packages.PySide6
        pyside6_addons = [string]$runtimeInfo.packages.PySide6_Addons
        pyside6_essentials = [string]$runtimeInfo.packages.PySide6_Essentials
        shiboken6 = [string]$runtimeInfo.packages.shiboken6
        qt = [string]$runtimeInfo.qt
        cryptography = [string]$runtimeInfo.packages.cryptography
        numpy = [string]$runtimeInfo.packages.numpy
        argon2 = [string]$runtimeInfo.packages.'argon2-cffi'
        cffi = [string]$runtimeInfo.packages.cffi
        pyinstaller = [string]$runtimeInfo.packages.PyInstaller
    }
    build_tools = [ordered]@{
        pyinstaller = [string]$runtimeInfo.packages.PyInstaller
        ruff = [string]$runtimeInfo.packages.ruff
        pytest = [string]$runtimeInfo.packages.pytest
        pip_audit = [string]$runtimeInfo.packages.'pip-audit'
        packaged_for_target = $false
    }
    self_test = [ordered]@{
        passed = [bool]$selfTestData.ok
        report = "evidence/package_self_test.json"
        database_quick_check = [string]$selfTestData.database_quick_check
        page_exports_loaded = [int]$selfTestData.page_exports_loaded
    }
    installation = [ordered]@{
        mode = "per-user"
        requires_admin = $false
        dependencies_embedded = $true
        default_directory = "%LOCALAPPDATA%\Programs\FinancasPessoaisLiquidGlass"
        installer = "INSTALAR_FINANCAS_PESSOAIS.bat"
        portable_launcher = "ABRIR_FINANCAS_PESSOAIS.bat"
        install_test = [ordered]@{
            passed = $false
            database_quick_check = ""
            page_exports_loaded = 0
        }
    }
}
$manifest | ConvertTo-Json -Depth 9 | Set-Content `
    -LiteralPath "$applicationFolder\RELEASE_MANIFEST.json" `
    -Encoding UTF8
Copy-Item `
    -LiteralPath "$projectRoot\RELEASE_NOTES.md" `
    -Destination $applicationFolder
Copy-Item `
    -LiteralPath "$projectRoot\RELEASE_NOTES_RESUME.md" `
    -Destination $applicationFolder
Copy-Item `
    -LiteralPath "$projectRoot\DATA_SAFETY.md" `
    -Destination $applicationFolder

$releaseManifestPath = Join-Path $applicationFolder "RELEASE_MANIFEST.json"
$packageEntries = @(
    Get-ChildItem -LiteralPath $applicationFolder -Recurse -File |
        Where-Object {
            -not $_.FullName.Equals(
                $releaseManifestPath,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        } |
        Sort-Object -Property FullName |
        ForEach-Object {
            $relativePath = $_.FullName.Substring($applicationFolder.Length + 1)
            [ordered]@{
                path = $relativePath.Replace("\", "/")
                size = [long]$_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            }
        }
)
$manifest.package_integrity = [ordered]@{
    algorithm = "SHA-256"
    file_count = [int]$packageEntries.Count
    entries = $packageEntries
}
$manifest | ConvertTo-Json -Depth 7 | Set-Content `
    -LiteralPath $releaseManifestPath `
    -Encoding UTF8

$installerTestFolder = [System.IO.Path]::GetFullPath(
    (Join-Path `
        -Path $temporaryPrefix `
        -ChildPath ("LiquidGlassInstallerTest_" + [Guid]::NewGuid().ToString("N")))
)
if (-not $installerTestFolder.StartsWith(
    $temporaryPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Diretorio temporario invalido para testar o instalador."
}
New-Item -ItemType Directory -Path $installerTestFolder -Force | Out-Null
$installedTestRoot = Join-Path $installerTestFolder "InstalledApplication"
$installedTestReport = Join-Path $installerTestFolder "installed_self_test.json"
$previousStateDirectory = [Environment]::GetEnvironmentVariable(
    "FINANCAS_PESSOAIS_STATE_DIR",
    "Process"
)
$previousReportPath = [Environment]::GetEnvironmentVariable(
    "LIQUIDGLASS_SELF_TEST_REPORT",
    "Process"
)
$installedTestData = $null
$previousTestingFlag = $env:FINANCAS_PESSOAIS_TESTING
try {
    $env:FINANCAS_PESSOAIS_TESTING = "1"
    $windowsPowerShell = Join-Path `
        $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    & $windowsPowerShell `
        -NoLogo `
        -NoProfile `
        -NonInteractive `
        -ExecutionPolicy Bypass `
        -File "$applicationFolder\INSTALAR_FINANCAS_PESSOAIS.ps1" `
        -InstallRoot $installedTestRoot `
        -NoShortcuts `
        -NoLaunch `
        -NonInteractive
    if ($LASTEXITCODE -ne 0) {
        throw "O instalador por utilizador falhou no ensaio isolado."
    }
    foreach ($relativeFile in @(
        "LiquidGlass.exe",
        "RELEASE_MANIFEST.json",
        "_internal\python314.dll",
        "_internal\PySide6\plugins\platforms\qwindows.dll"
    )) {
        if (-not (Test-Path -LiteralPath "$installedTestRoot\$relativeFile")) {
            throw "O instalador nao copiou o ficheiro obrigatorio: $relativeFile"
        }
    }

    [Environment]::SetEnvironmentVariable(
        "FINANCAS_PESSOAIS_STATE_DIR",
        (Join-Path $installerTestFolder "profile"),
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "LIQUIDGLASS_SELF_TEST_REPORT",
        $installedTestReport,
        "Process"
    )
    $installedTestProcess = Start-Process `
        -FilePath "$installedTestRoot\LiquidGlass.exe" `
        -ArgumentList "--package-self-test" `
        -WorkingDirectory $installedTestRoot `
        -WindowStyle Hidden `
        -PassThru
    if (-not $installedTestProcess.WaitForExit(90000)) {
        Stop-Process -Id $installedTestProcess.Id -Force
        throw "O auto-teste da copia instalada excedeu 90 segundos."
    }
    $installedTestProcess.Refresh()
    if ($installedTestProcess.ExitCode -ne 0) {
        throw (
            "O auto-teste da copia instalada terminou com codigo " +
            "$($installedTestProcess.ExitCode)."
        )
    }
    if (-not (Test-Path -LiteralPath $installedTestReport)) {
        throw "A copia instalada nao criou o relatorio do auto-teste."
    }
    $installedTestData = Get-Content -LiteralPath $installedTestReport -Raw |
        ConvertFrom-Json
    if (
        -not $installedTestData.ok -or
        [string]$installedTestData.database_quick_check -ne "ok" -or
        [int]$installedTestData.page_exports_loaded -lt 1
    ) {
        throw "O relatorio do auto-teste da copia instalada e invalido."
    }
    Assert-FunctionalSelfTest -Data $installedTestData
    $manifest.installation.install_test.passed = $true
    $manifest.installation.install_test.database_quick_check = `
        [string]$installedTestData.database_quick_check
    $manifest.installation.install_test.page_exports_loaded = `
        [int]$installedTestData.page_exports_loaded
} finally {
    [Environment]::SetEnvironmentVariable("FINANCAS_PESSOAIS_TESTING", $previousTestingFlag, "Process")
    [Environment]::SetEnvironmentVariable(
        "FINANCAS_PESSOAIS_STATE_DIR",
        $previousStateDirectory,
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "LIQUIDGLASS_SELF_TEST_REPORT",
        $previousReportPath,
        "Process"
    )
    if (Test-Path -LiteralPath $installerTestFolder) {
        Remove-Item -LiteralPath $installerTestFolder -Recurse -Force
    }
}
$manifest | ConvertTo-Json -Depth 7 | Set-Content `
    -LiteralPath "$applicationFolder\RELEASE_MANIFEST.json" `
    -Encoding UTF8

$privateFiles = Get-ChildItem -LiteralPath $applicationFolder -Recurse -File |
    Where-Object {
        $_.Extension -in ".sqlite", ".sqlite3", ".db", ".lgbak" -or
        $_.Extension -in ".key", ".pem", ".log" -or
        $_.Name -like ".env*" -or
        $_.FullName -match "[\\/](anexos|backups|logs)[\\/]"
    }
if ($privateFiles) {
    throw "A distribuicao contem ficheiros privados e nao sera empacotada."
}

Assert-SourceSnapshot
Compress-Archive `
    -Path $applicationFolder `
    -DestinationPath "$projectRoot\build\LiquidGlass_App_v$appVersion.zip" `
    -CompressionLevel Optimal
Move-Item -LiteralPath "$projectRoot\build\LiquidGlass_App_v$appVersion.zip" `
    -Destination "$packageOutput\LiquidGlass_App_v$appVersion.zip" -Force

$zip = Get-Item -LiteralPath "$packageOutput\LiquidGlass_App_v$appVersion.zip"
$zipHash = (Get-FileHash -LiteralPath $zip.FullName -Algorithm SHA256).Hash
"$zipHash  $($zip.Name)" | Set-Content `
    -LiteralPath "$packageOutput\LiquidGlass_App_v$appVersion.sha256.txt" `
    -Encoding ASCII
Copy-Item -LiteralPath $zip.FullName -Destination "$packageOutput\LiquidGlass_App.zip"
"$zipHash  LiquidGlass_App.zip" | Set-Content `
    -LiteralPath "$packageOutput\LiquidGlass_App.sha256.txt" `
    -Encoding ASCII

if (-not $KeepWorkFolders) {
    Remove-GeneratedPath "build"
    Remove-GeneratedPath "dist"
}
Write-Output (
    "Pacote criado: {0} ({1:N2} MB) - SHA-256 {2}" -f `
        $zip.FullName, ($zip.Length / 1MB), $zipHash
)
