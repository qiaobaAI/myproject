@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk:replace"
set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"

echo ============================================
echo   ������Ƶ -^> AI ���ʼ�  (����� output\, �� index.html)
echo   ������Ƶ/��¼��: ������ ������ģ��.bat ѡ ģ��1(35B�Ӿ�)
echo ============================================

set "IN=%~1"
if not defined IN (
    set /p "IN=��������Ƶ���ļ���, ��ճ������·����س�: "
)
if not defined IN (
    echo δ����·��, �˳���
    pause
    exit /b 1
)
set "IN=%IN:"=%"
if not exist "%IN%" (
    echo ·��������: "%IN%"
    pause
    exit /b 1
)

cd /d "%~dp0"
set "EXT=%~x1"
if /i "%EXT%"==".txt" (
    echo [队列模式] 读取路径列表文件: %IN%
    ".venv\Scripts\python.exe" make_notes.py --queue "%IN%"
) else (
    ".venv\Scripts\python.exe" make_notes.py "%IN%"
)

echo.
echo ========== ȫ�����, ��������˳� ==========
pause >nul
