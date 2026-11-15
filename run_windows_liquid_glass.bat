@echo off
setlocal
cd /d "%~dp0"
set "QT_OPENGL=software"
set "QT_QUICK_BACKEND=software"
set "APP=%~dp0financas_liquid_glass_launcher.pyw"

where pyw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pyw.exe -3.14 "%APP%"
    exit /b 0
)

where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%APP%"
    exit /b 0
)

where python.exe >nul 2>nul
if %errorlevel%==0 (
    start "" /min python.exe "%APP%"
    exit /b 0
)

echo Nao encontrei Python no PATH.
echo Instala Python ou confirma que a opcao "Add Python to PATH" esta activa.
echo.
echo Tambem podes testar com:
echo py -3.14 -m pip install -r requirements.txt
pause
exit /b 1
