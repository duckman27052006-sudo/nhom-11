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
echo  TFTP server se tu khoi dong tai 127.0.0.1:6969 trong GUI.
echo  Doi trang thai "Server dang chay" truoc khi gui file tren web.
echo.
echo  Tat app: dong cua so GUI, hoac Ctrl+C tai cua so nay.
echo  Ca hai cong 8080 va 6969 se duoc nha.
echo.

rem --- Chay GUI + web gateway nhu mot khoi (xem tftp/tftp_run.py) ---
rem Mot console duy nhat: dong console / Ctrl+C / dong GUI deu dung ca hai
rem va nha ca cong 8080 lan 6969.
"%PY%" -m tftp.tftp_run --host "%LAN_HOST%" --allowed-network "%LAN_CIDR%" --root files --data-dir web-data
if errorlevel 2 goto launch_error
exit /b 0

:launch_error
echo.
echo  [LOI] Khong the khoi dong dich vu. Hay kiem tra dependency va thong bao loi.
echo.
pause
exit /b 1
