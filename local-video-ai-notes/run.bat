@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk:replace"
set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"

echo ============================================
echo   本地视频 -> AI 富笔记  (输出到 output\, 见 index.html)
echo   无声视频/纯录屏: 请先在 启动大模型.bat 选 模型1(35B视觉)
echo   支持直接拖入 字幕/逐字稿 .txt (纯文本模式, 跳过抽帧转写)
echo ============================================

set "IN=%~1"
if not defined IN (
    set /p "IN=请把视频文件/文件夹/字幕txt拖到这里, 或粘贴完整路径后回车: "
)
if not defined IN (
    echo 未收到路径, 退出。
    pause
    exit /b 1
)
set "IN=%IN:"=%"
if not exist "%IN%" (
    echo 路径不存在: "%IN%"
    pause
    exit /b 1
)

cd /d "%~dp0"
set "EXT=%~x1"
if /i "%EXT%"==".txt" (
    echo [文本模式] 自动判断: 路径清单(队列) 或 字幕/逐字稿
    ".venv\Scripts\python.exe" make_notes.py "%IN%"
) else (
    ".venv\Scripts\python.exe" make_notes.py "%IN%"
)

echo.
echo ========== 全部完成, 按任意键退出 ==========
pause >nul
