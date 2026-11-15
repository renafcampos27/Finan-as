@echo off
setlocal

set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "INSTALLER=%~dp0INSTALAR_FINANCAS_PESSOAIS.ps1"

if not exist "%POWERSHELL%" (
    echo ERRO: O Windows PowerShell nao foi encontrado.
    echo Este pacote requer Windows 10 ou Windows 11 de 64 bits.
    pause
    exit /b 1
)

if not exist "%INSTALLER%" (
    echo ERRO: O pacote esta incompleto.
    echo Extrai novamente TODO o conteudo do ZIP antes de instalar.
    pause
    exit /b 1
)

"%POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%"
set "RESULT=%ERRORLEVEL%"

if not "%RESULT%"=="0" (
    echo.
    echo A instalacao nao foi concluida. Nenhum dado financeiro foi apagado.
    pause
)

exit /b %RESULT%
