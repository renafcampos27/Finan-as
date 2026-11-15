@echo off
setlocal

set "APP_ROOT=%~dp0"
set "APP_EXE=%APP_ROOT%LiquidGlass.exe"

if not exist "%APP_EXE%" goto incomplete
if not exist "%APP_ROOT%_internal\python314.dll" goto incomplete
if not exist "%APP_ROOT%_internal\PySide6\Qt6Core.dll" goto incomplete
if not exist "%APP_ROOT%_internal\PySide6\plugins\platforms\qwindows.dll" goto incomplete

start "Financas Pessoais" "%APP_EXE%"
exit /b 0

:incomplete
echo ERRO: O pacote esta incompleto ou ainda esta dentro do ZIP.
echo Extrai TODO o conteudo e mantem a pasta _internal junto do executavel.
pause
exit /b 1
