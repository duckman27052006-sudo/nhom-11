@echo off
chcp 65001 >nul
setlocal
title Tat TFTP app
cd /d "%~dp0"

rem Dung moi tien trinh TFTP cua app va cho den khi cong 8080 + 6969 trong.
rem Dung duoc ca khi app bi treo, khong dong duoc cua so.

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo  Dang tat TFTP app va giai phong cong 8080 / 6969...
echo.
"%PY%" -m tftp.tftp_run --stop
echo.
pause
exit /b 0
