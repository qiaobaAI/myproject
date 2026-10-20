# Whisper GPU 转写（whisper.cpp + CUDA）

本文说明本工具**如何把语音转写从 CPU 搬到 GPU**，以及为什么不用 `faster-whisper` 的 GPU 模式。

---

## 1. 为什么要换 whisper.cpp

本工具的默认转写引擎原本是 `faster-whisper`，它底层是 `ctranslate2`。

**问题**：`ctranslate2` 的预编译 wheel 写死了要加载 **CUDA 12 的 `cublas64_12.dll`**。
而 50 系新卡（RTX 50xx，驱动 610+）默认装的是 **CUDA 13**，只有 `cublas64_13.dll`。
两者版本对不上 → `--device cuda` 直接报错或卡死 → 只能退回 CPU 硬扛。

**解法**：改用 [whisper.cpp](https://github.com/ggml-org/whisper.cpp)。
它自带 ggml 的 CUDA kernel，**只依赖 CUDA 运行库本身，不挑 cublas 版本**，
因此在 CUDA 12 / 13 上都能跑 GPU。

> 结论：**CUDA 12 用户**两条路都能走；**CUDA 13 用户（尤其 50 系）**只能走 whisper.cpp。
> 本项目两种都支持：**优先 whisper.cpp GPU，失败自动回退 faster-whisper CPU**。

---

## 2. 实测速度（作者 RTX 5070 12GB）

| 模型 | 74.77 秒音频耗时 | 实时率 | 2 小时视频预估 |
|---|---|---|---|
| `ggml-base`（141MB） | 1.43 秒 | ~52x | ~2.5 分钟 |
| `ggml-small`（465MB） | 2.25 秒 | ~33x | ~4 分钟 |

对比：同一份音频用 CPU（`faster-whisper base`，12 核）约 10~25 秒，即 **5~15 倍差距**。

> 默认用 **small**：技术课程里大量英文类名/API，base 易出现同音错字
> （如 "Spring Boot"→"Spring佛陈"、"技术栈"→"技术站"）。small 准得多，33x 也足够快。
> 追求极限速度可切回 base。

---

## 3. 前置条件

| 组件 | 说明 |
|---|---|
| NVIDIA 显卡 + 驱动 | 支持 CUDA 即可（12/13 都行） |
| CUDA Toolkit | 需包含 `nvcc`。13.x 可用 |
| Visual Studio 2022 Build Tools | 勾选「使用 C++ 的桌面开发」（含 MSVC + Windows SDK） |
| CMake | 3.20+ |
| Git | 用于克隆 whisper.cpp |

> Windows 上用 [vswhere](https://github.com/microsoft/vswhere) 自动定位 VS，无需手动配环境变量。

---

## 4. 构建步骤（Windows）

```bat
REM 1) 把 whisper.cpp 克隆到工具的 tools/whispercpp（该目录被 .gitignore 忽略，不进库）
git clone --depth 1 https://github.com/ggml-org/whisper.cpp tools\whispercpp

REM 2) 编译（脚本会自己调 vcvars64.bat 配好 MSVC 环境）
scripts\build_whisper_cuda.bat
```

成功标志：生成 `tools\whispercpp\build\bin\whisper-cli.exe`

> ⚠️ **产物在 `build\bin\`，不是 `build\` 根目录**。这是 whisper.cpp 的布局。

### 4.1 显卡架构（`CMAKE_CUDA_ARCHITECTURES`）

| 系列 | arch |
|---|---|
| RTX 20xx (Turing) | 75 |
| RTX 30xx (Ampere) | 86 |
| RTX 40xx (Ada) | 89 |
| **RTX 50xx (Blackwell)** | **120** |

脚本默认 **120**（适配 50 系）。其它卡请改脚本里的 `-DCMAKE_CUDA_ARCHITECTURES`。

### 4.2 Linux / macOS

```bash
git clone --depth 1 https://github.com/ggml-org/whisper.cpp tools/whispercpp
CUDA_ARCH=120 scripts/build_whisper_cuda.sh    # Linux；macOS 自动用 Metal
```

---

## 5. 下载 ggml 模型

whisper.cpp 用 `.bin` 格式（不是 faster-whisper 的目录格式）。放到 `tools/whispercpp/models/`：

```bash
# 走 hf-mirror 镜像，避免直连 HuggingFace 被截断
curl -L -o tools/whispercpp/models/ggml-small.bin \
  https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-small.bin

# 可选：base（更快）
curl -L -o tools/whispercpp/models/ggml-base.bin \
  https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
```

也可用官方脚本：`tools/whispercpp/models/download-ggml-model.cmd small`

---

## 6. 验证 GPU 是否真的生效

```bash
tools/whispercpp/build/bin/whisper-cli.exe -m tools/whispercpp/models/ggml-small.bin \
  -f 你的音频.wav -l zh
```

看到下面两行就说明走的是 GPU：

```
ggml_cuda_init: found 1 CUDA devices (Total VRAM: 12226 MiB):
  Device 0: NVIDIA GeForce RTX 5070, compute capability 12.0 ...
whisper_backend_init_gpu: using CUDA0 backend
```

---

## 7. 集成说明（无需你手动调用）

`make_notes.py` 的 `transcribe()` 会自动：

1. 找到 `tools/whispercpp/build/bin/whisper-cli.exe` 和 `models/ggml-*.bin`；
2. 用 ffmpeg 把视频抽成 16k 单声道 wav（whisper-cli 原生不认 mp4）；
3. 调 whisper-cli 出 SRT，解析成与 faster-whisper 相同的 `segs` 结构（下游零改动）；
4. **任何环节失败 → 自动回退 faster-whisper CPU**，不会让整个任务崩。

模型可通过参数或环境变量指定：

```bash
# 临时换模型
python make_notes.py "视频.mp4" --whisper-cpp-model tools/whispercpp/models/ggml-base.bin

# 强制走 CPU（不用 GPU）
python make_notes.py "视频.mp4" --device cpu
```

---

## 8. 常见问题

### 8.1 `whisper-cli.exe` 一闪而过 / exit code 127

**缺 CUDA 运行时 DLL**。`ggml-cuda.dll` 依赖 `cublas64_13.dll` / `cudart64_13.dll` / `cublasLt64_13.dll`。

这些 DLL 在 **`CUDA\v13.3\bin\x64`**，**不在 `bin`**。两种解法：

- 把 `cublas64_13.dll`、`cublasLt64_13.dll`、`cudart64_13.dll` 从
  `CUDA\v13.3\bin\x64` 复制到 `tools\whispercpp\build\bin\`（和 exe 放一起）；
- 或确保 `CUDA\v13.3\bin\x64` 在系统 PATH 里。

> 本工具的 `make_notes.py` 已自动把 `bin\x64` 加进子进程 PATH，通常无需手动处理。

### 8.2 编译很慢（几十分钟）

`NMake Makefiles` 生成器是**单线程**的，而 ggml-cuda 有几十个重模板 `.cu` 文件。
改用 **Ninja** 并加并行：

```bash
cmake -B build -S . -G "Ninja" ... && cmake --build build --target whisper-cli --parallel
```

（`scripts/build_whisper_cuda.sh` 已默认用 Ninja。）

### 8.3 满屏 `warning C4819`

只是代码页告警（源码 UTF-8、cmd 936），**无害**，可忽略。

### 8.4 为什么不直接提交编译产物 / 模型

体积太大（`ggml-cuda.dll` 56MB、模型数百 MB），且需匹配各人显卡架构。
`.gitignore` 已排除 `tools/`，请按本文自行编译。

---

## 9. 踩坑记录（贡献者参考）

- **Git Bash 下手工设 MSVC 环境极坑**：`INCLUDE`/`LIB` 必须是 `C:\` 反斜杠绝对路径（原生 `link.exe` 不认 `/c/`）；
  但 `PATH` 里找 `nmake` 需要 forward-slash、找 `rc.exe` 需要反斜杠 →
  最省事的是**直接用 `vcvars64.bat`**（`scripts/build_whisper_cuda.bat` 就是这么做）。
- `-DCMAKE_RC_COMPILER` / `-DCMAKE_MT` 的值若含反斜杠，cmake 会报 `Invalid character escape '\P'` →
  这些 `-D` 传参值要用**正斜杠**。
- `subprocess.run(..., text=True)` 在中文 Windows 上默认按 GBK 解码，而 whisper-cli 输出 UTF-8 中文 →
  线程抛 `UnicodeDecodeError`（不影响结果，但刷红）→ 必须显式 `encoding="utf-8", errors="replace"`。
