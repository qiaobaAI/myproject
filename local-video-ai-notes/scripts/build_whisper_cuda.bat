@echo off
REM Build whisper.cpp with CUDA support (Windows).
REM Prereq: VS2022 Build Tools + CMake + NVIDIA CUDA Toolkit (13.x ok).
REM Usage: clone whisper.cpp into tools\whispercpp first, then run this.
setlocal
set "PF86=%ProgramFiles(x86)%"
set "ROOT=%~dp0.."
set "SRC=%ROOT%\tools\whispercpp"
if not exist "%SRC%\CMakeLists.txt" (
  echo [ERR] whisper.cpp source not found at %SRC%
  echo       git clone --depth 1 https://github.com/ggml-org/whisper.cpp "%SRC%"
  goto :end
)
set "VSWHERE=%PF86%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" ( echo [ERR] vswhere not found. Install VS2022 Build Tools. & goto :end )
"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath > "%TEMP%\vs_path.txt"
set /p VSPATH=<"%TEMP%\vs_path.txt"
if not defined VSPATH ( echo [ERR] MSVC C++ toolset not found. & goto :end )
call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
if errorlevel 1 ( echo [ERR] vcvars64 failed. & goto :end )
cd /d "%SRC%"
echo [*] configure ...
cmake -B build -S . -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120 -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF
if errorlevel 1 ( echo [ERR] configure failed. & goto :end )
echo [*] build whisper-cli ...
cmake --build build --target whisper-cli
if errorlevel 1 ( echo [ERR] build failed. & goto :end )
echo [DONE] %SRC%\build\bin\whisper-cli.exe
:end
pause
