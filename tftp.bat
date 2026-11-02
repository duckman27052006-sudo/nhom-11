@echo off
chcp 65001 >nul
setlocal
title TFTP Console
cd /d "%~dp0"

rem --- Chon python: uu tien .venv, khong co thi dung python tren PATH ---
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem --- Kiem tra dependency GUI va web, tu cai extras neu thieu ---
"%PY%" -c "import PySide6, flask, waitress" >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [!] Thieu dependency GUI/web - dang cai dat...
  "%PY%" -m pip install -e ".[gui,web]"
  if errorlevel 1 (
    echo.
    echo  [LOI] Khong cai duoc dependency GUI/web. Thu chay thu cong:
    echo        %PY% -m pip install -e ".[gui,web]"
    echo.
    pause
    exit /b 1
  )
)

rem --- Tim IPv4 va subnet cua card mang co default route ---
set "LAN_HOST="
set "LAN_CIDR="
for /f "usebackq tokens=1,2 delims=/" %%A in (`powershell -NoProfile -Command "$r=Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1; if ($r) { $a=Get-NetIPAddress -InterfaceIndex $r.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -notlike '127.*' } | Select-Object -First 1; if ($a) { Write-Output ($a.IPAddress + '/' + $a.PrefixLength) } }"`) do (
  set "LAN_HOST=%%A"
  set "LAN_CIDR=%%A/%%B"
)

if not defined LAN_HOST (
  echo.
  echo  [LOI] Khong tim thay IPv4 LAN. Hay ket noi Wi-Fi/Ethernet roi thu lai.
  echo.
  pause
  exit /b 1
)

echo.
echo  IP LAN:       %LAN_HOST%
echo  Web upload:   http://%LAN_HOST%:8080
echo  Trang GitHub: https://duckman27052006-sudo.github.io/nhom-11/
echo.
echo  Hay bam "Khoi dong server" trong GUI TFTP.
echo  Giu ca hai cua so dich vu dang chay de nhan file.
echo.

rem --- Khoi dong GUI TFTP va web gateway trong hai cua so rieng ---
start "TFTP Console" "%PY%" -m src.tftp_gui
if errorlevel 1 goto launch_error
start "TFTP Web Upload" "%PY%" -m src.web_upload --host "%LAN_HOST%" --port 8080 --allowed-network "%LAN_CIDR%" --tftp-server 127.0.0.1:6969
if errorlevel 1 goto launch_error
echo  Hay giu cac cua so TFTP Console va TFTP Web Upload dang chay.
echo.
pause
exit /b 0

:launch_error
echo.
echo  [LOI] Khong the khoi dong dich vu. Hay kiem tra dependency va thong bao loi.
echo.
pause
exit /b 1
