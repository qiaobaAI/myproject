#!/usr/bin/env bash
# Build whisper.cpp with GPU support (Linux / macOS).
# Prereq: cmake, a C/C++ compiler, and CUDA toolkit (Linux) or Xcode CLT (macOS).
# Usage: clone whisper.cpp into tools/whispercpp first, then run this script.
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/tools/whispercpp"

if [ ! -f "$SRC/CMakeLists.txt" ]; then
  echo "[ERR] whisper.cpp source not found at $SRC"
  echo "      git clone --depth 1 https://github.com/ggml-org/whisper.cpp \"$SRC\""
  exit 1
fi

cd "$SRC"

case "$(uname -s)" in
  Darwin)
    BACKEND="-DGGML_METAL=ON"
    ARCH_ARG=""
    echo "[*] macOS: using Metal backend"
    ;;
  Linux)
    BACKEND="-DGGML_CUDA=ON"
    # 按你的显卡改架构：RTX 30=86, RTX 40=89, RTX 50=120
    ARCH_ARG="-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH:-120}"
    echo "[*] Linux: using CUDA backend (arch=${CUDA_ARCH:-120}，可用 CUDA_ARCH 覆盖)"
    ;;
  *)
    echo "[ERR] unsupported OS: $(uname -s)"
    exit 1
    ;;
esac

echo "[*] configure ..."
cmake -B build -S . -G "Ninja" -DCMAKE_BUILD_TYPE=Release \
  $BACKEND $ARCH_ARG \
  -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF

echo "[*] build whisper-cli ..."
cmake --build build --target whisper-cli --parallel

echo "[DONE] $SRC/build/bin/whisper-cli"
