@echo off
chcp 65001 >nul
setlocal
title TFTP Console
cd /d "%~dp0"

rem --- Chon python: uu tien .venv, khong co thi dung python tren PATH ---
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem --- Kiem tra PySide6, tu cai neu thieu ---
"%PY%" -c "import PySide6" >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [!] Chua cai PySide6 - dang cai dat...
  "%PY%" -m pip install PySide6
  if errorlevel 1 (
    echo.
    echo  [LOI] Khong cai duoc PySide6. Thu chay thu cong:
    echo        %PY% -m pip install PySide6
    echo.
    pause
    exit /b 1
  )
)

rem --- Mo giao dien desktop ---
"%PY%" -m src.tftp_gui
if errorlevel 1 (
  echo.
  echo  [LOI] GUI dung voi ma %errorlevel%. Xem thong bao loi ben tren.
  echo.
  pause
)
