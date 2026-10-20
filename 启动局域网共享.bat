@echo off
chcp 65001 >nul
title 智慧武大数学平台 · 局域网共享模式
cd /d "%~dp0"

echo ============================================
echo   智慧武大数学平台 · 局域网共享模式
echo ============================================
echo.
echo 正在启动服务器...
echo.

python scripts\serve-lan.py

if errorlevel 1 (
    echo.
    echo 启动失败：请确认电脑已安装 Python 并加入 PATH。
    echo 可前往 https://www.python.org/downloads/ 安装。
    echo.
)
pause
