@echo off
setlocal
cd /d "%~dp0"
set "QT_OPENGL=software"
set "QT_QUICK_BACKEND=software"

echo A iniciar Financas Pessoais - Liquid Glass em modo debug...
echo.

where py.exe >nul 2>nul
if %errorlevel%==0 (
    py -3.14 "%~dp0main.py"
    goto end
)

where python.exe >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0main.py"
    goto end
)

echo Nao encontrei Python no PATH.

:end
echo.
echo Se apareceu um erro acima, copia-o e envia-mo.
pause
