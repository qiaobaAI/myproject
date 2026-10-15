# 附录：本地大模型运行环境（作者实测 + 通用选型）

> 本文档做两件事：① 总结作者**实际**使用的本地推理环境（供参考"视频笔记背后的大模型怎么跑起来的"）；② 给出**通用选型指南**，让你按自己的显卡/系统选对 llama.cpp 构建、模型量化、以及（24GB+ 用户专属的）双 server 方案。
>
> 你**不一定**要用和作者完全相同的配置——只要有一个 OpenAI 兼容的本地服务监听 `http://localhost:8000/v1` 即可（见主 README 第三节）。

---

## 一、作者实测硬件（RTX 5070 12GB 档）

| 部件 | 规格 |
|---|---|
| GPU | NVIDIA GeForce **RTX 5070** / **12GB** GDDR7（Blackwell，sm_120） |
| 驱动 / CUDA | 驱动 610.47，支持 **CUDA 13.3**（⚠️ 系统只有 `cublas64_13.dll`，没有 `cublas64_12.dll`） |
| CPU | AMD Ryzen 9 9900X（12 核 24 线程） |
| 内存 | **47GB** RAM |
| 系统 | Windows 11 |

> **关键约束**：12GB 显存放不下两个大模型同驻，所以 30B（笔记）和 35B（视觉 OCR）只能**二选一加载**（除非你用 24GB+ 显卡，见第四节双 server）。

---

## 二、llama.cpp 构建怎么选（按你的显卡/系统）

llama.cpp 官方 Release 提供**多种后端构建**，下载错版本会直接报错或跑不了 GPU。下表照抄：

| 你的环境 | 该下的构建（GitHub Release 命名特征） |
|---|---|
| **NVIDIA 显卡 + CUDA 12.x 驱动** | `llama-bXXXX-bin-win-cuda-12.x-x64`（如 `cuda-12.4`） |
| **NVIDIA 显卡 + CUDA 13.x 驱动**（50 系新卡 610+） | `llama-bXXXX-bin-win-cuda-13.x-x64`（作者用的 `cuda-13.3`） |
| **NVIDIA 显卡 + Linux** | 同上，选 `linux-cuda-*` 对应版本 |
| **AMD 显卡（RX 7000 等）** | `llama-bXXXX-bin-*-rocm-*` 或 `hipblas` 构建 |
| **Intel Arc 显卡** | `llama-bXXXX-bin-*-sycl-*` 构建 |
| **Apple Silicon Mac** | `llama-bXXXX-bin-macos-metal` 构建（统一内存，跑大模型极友好） |
| **纯 CPU / 无独显** | `llama-bXXXX-bin-*-noavx*` 或通用 CPU 构建 |

> ⚠️ 作者机是 **CUDA 13.3 构建**，只因 RTX 5070 正好是 13.3 驱动。**你该下和你驱动匹配的 CUDA 版本**，不是照抄 13.3。
> 也正因为作者是 CUDA 13.3，而 faster-whisper 的 ctranslate2 **要 CUDA 12 的 `cublas64_12.dll`**，两套东西各用各的 CUDA 版本，互不干扰——这就是为什么视频笔记的 Whisper 转写默认走 CPU（详见主 README 坑 1）。**若你系统装了 CUDA 12，Whisper 反而能 `--device cuda` 加速。**

---

## 三、模型量化与显存（通用选型，不绑定作者）

| 你的显存 | 笔记模型（推荐） | 无声视频 OCR 模型 | 能否同驻 |
|---|---|---|---|
| 6–8 GB | Qwen3-8B-Instruct Q4_K_M（~5GB） | Qwen2.5-VL-7B Q4_K_M（~5GB） | 否 |
| 12 GB | Qwen3-30B-A3B **Q4_K_M**（18.6GB，用 `--cpu-moe` 把专家落内存） | Qwen3.6-35B-A3B **Q2_K_P**（14GB，GPU 为主、少量 offload 内存） | 否 |
| 16 GB | 同上（更多层能上 GPU） | 同上 | 否 |
| 24 GB+ | 可全 GPU 跑 30B | 可全 GPU 跑 35B | **可以（双 server）** |
| 无独显 | Qwen3-8B（CPU）或 30B-cpu（很慢） | 同左 | — |

> **"显存放不下"不等于"跑不了"**：llama.cpp 会自动把放不进显存的层 offload 到内存续跑。所以作者 12GB 卡能跑 18.6GB 的 30B——只是超出显存的部分走内存，速度介于纯 GPU/纯 CPU 之间。**这要求内存够大**（30B `--cpu-moe` 约需 20GB 空闲内存，作者 47GB 绰绰有余；16GB 内存用户会吃紧）。

### 作者实际用的模型文件（`models/` 子目录）
```
models/
├─ Qwen3-30B-A3B-Q4_K_M\
│   └─ Qwen3-30B-A3B-Q4_K_M.gguf        # 18.6GB，纯文本，做"笔记总结"用
├─ Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q2_K_P.gguf   # 14GB，带视觉
└─ mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf  # 视觉多模态投影文件
```

| 模型 | 量化 | 体积 | 用途 | 加载方式 |
|---|---|---|---|---|
| **Qwen3-30B-A3B** | Q4_K_M | 18.6GB | 笔记总结（默认） | `--cpu-moe`：专家走内存 + 注意力上 GPU |
| **Qwen3.6-35B-A3B** | Q2_K_P | 14GB | 视觉 OCR（无声视频） | GPU 为主 + 少量 offload 内存 + `--mmproj` 加载视觉投影 |

> 35B 的 14GB 权重**无法全装进 12GB 显存**（作者实测是"GPU 放 12GB 左右、其余落内存"），所以"全 GPU"是错误说法——在此修正。只有 24GB+ 显卡才能让它真正全 GPU 流畅。

---

## 四、大模型怎么下载（30B / 35B 都是十几 GB）

国内直连 HuggingFace 常超时/被墙，用镜像：

- **魔搭 ModelScope**：搜模型名直接下，国内快。
- **hf-mirror.com**（推荐，命令行最顺手）：
  ```bash
  pip install -U "huggingface_hub[cli]"
  set HF_ENDPOINT=https://hf-mirror.com      # macOS/Linux: export HF_ENDPOINT=https://hf-mirror.com
  huggingface-cli download <HF用户名>/<模型名> --local-dir models/<模型名>
  ```
  例如 35B 视觉：`huggingface-cli download Qwen/Qwen3-VL-... `（具体仓库名以你选的模型为准）。
- 大文件建议用 **迅雷 / aria2c** 多线程拉（浏览器单线程易断）。

---

## 五、启动器 `启动大模型.bat`（作者自写的菜单）

作者把复杂度封装成交互菜单 bat，**双击先选模型，再选模式**：

```
STEP 1 : Select Model
  [1] Qwen3.6-35B-A3B  (Q2_K, vision ON,  14GB, GPU为主+内存offload)   ← 无声视频OCR用
  [2] Qwen3-30B-A3B    (Q4_K_M, text only, 18.6GB, cpu-moe)            ← 笔记默认

主菜单:
  [1] Web Chat Mode   (浏览器UI，含视觉，推荐)
  [2] CLI Chat Mode
  [3] Benchmark
  [4] Settings        (调 GPU层数/上下文/线程)
  [5] Change Model
  [6] Help
  [7] Exit
```

它会启动：
```bat
llama-server.exe -m "<模型.gguf>" [--mmproj "<mmproj>"] ^
  -ngl 99 -c 16384 -t 12 -b 512 -ub 512 ^
  --cpu-moe ^   (仅 30B)
  --host 0.0.0.0 --port 8000
```

启动后监听 `http://localhost:8000/v1`（OpenAI 兼容），视频笔记工具默认就调这个端点。

### 关键参数含义
- `-ngl 99`：尽可能把层放 GPU（99 表示全放，受显存限制实际会部分回退到内存）。
- `-c 16384`：上下文窗口 16K，足够长视频转写稿。
- `-t 12`：CPU 线程数（对齐 12 物理核；你若核数不同改成你的核数）。
- `-b 512 / -ub 512`：批处理大小。
- `--cpu-moe`：30B 的 MoE 专家卸载到内存（显存不够时的关键参数）。

---

## 六、24GB+ 用户专属：双 server 方案（彻底解决"二选一"）

如果你有 24GB 以上显存（3090/4090/5090），可以**同时起两个 llama-server**：一个跑 30B（笔记），一个跑 35B（OCR），互不干扰，无声视频自动 OCR 无需切换模型：

```bat
:: 终端1：笔记模型（8000）
llama-server.exe -m models/Qwen3-30B-A3B-Q4_K_M/Qwen3-30B-A3B-Q4_K_M.gguf -ngl 99 -c 16384 -t 12 --port 8000

:: 终端2：视觉模型（8001）
llama-server.exe -m models/Qwen3.6-35B-A3B-...-Q2_K_P.gguf --mmproj models/mmproj-...-f16.gguf -ngl 99 -c 16384 -t 12 --port 8001
```

然后跑笔记时把视觉 OCR 指到 8001：
```bash
.venv\Scripts\python.exe make_notes.py "D:/录屏/无旁白.mp4" --vision ^
  --vision-url http://localhost:8001/v1 --vision-model <你的35B文件名>
```

---

## 七、给 clone 者的建议

1. **不想完全照搬**：只要有任意 OpenAI 兼容本地服务（llama.cpp / ollama / vllm）跑在 `localhost:8000`，改 `make_notes.py` 里的 `LLM_BASE_URL` / `LLM_MODEL` 即可。
2. **想完全复刻作者效果**：按第二节下对应 CUDA 版本的 llama.cpp 构建；下 Qwen3-30B-A3B Q4_K_M（笔记）+ Qwen3.6-35B-A3B Q2_K_P + mmproj（视觉）；仿照上面的启动命令起服务。
3. **显存 < 12GB**：用更小的量化（如 Q3_K_M / Q2_K）或纯 CPU 跑，速度慢但能跑（见第三节表）。
4. **mac / Linux**：不用 bat，直接命令行起 `llama-server` 即可，其余流程一致；Linux/macOS 桌面用户也可用本仓库附带的 `run.sh`。
