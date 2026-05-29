@echo off
REM ============================================================
REM  WhatToWear - launch a local static HTTP server + open browser
REM  Needed because browsers block fetch() of local JSON over file://
REM  For deployment: upload index.html + data/ to any static host.
REM ============================================================
setlocal
cd /d "%~dp0"

set PORT=8765
set URL=http://localhost:%PORT%/index.html

REM 后台开浏览器（等 0.5 秒让服务器先起来）
start "" /min cmd /c "timeout /t 1 /nobreak >nul && start "" "%URL%""

echo.
echo  ==============================================================
echo   WhatToWear 本地服务器
echo   浏览器会自动打开： %URL%
echo   不要关闭这个窗口，关掉就停止服务。
echo   按 Ctrl+C 退出。
echo  ==============================================================
echo.

python server.py
