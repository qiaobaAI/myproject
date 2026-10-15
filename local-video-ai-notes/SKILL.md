---
name: video-notes-builder
description: 帮用户在本地从零搭建"视频 → AI 富笔记"工具（本地视频转结构化图文学习笔记，超越百度网盘 AI 笔记）。当用户想搭建本地视频笔记生成器、把课程/教程/会议录像转成带图+原理类比+代码的笔记、或复刻 video-notes 项目时使用。触发词：本地视频笔记、视频转笔记、超越百度网盘笔记、video-notes、把录像变成学习文档。
---

# 视频 → AI 富笔记 搭建技能（video-notes）

本技能让你替用户在**纯本地**搭出一个"视频进、富笔记出"的工具：左视频 + 右结构化笔记（分节讲解 + 生活类比讲底层原理 + 代码说明/代码框 + 知识小结表 + 自测题 + 关键帧截图，点时间戳跳视频）。全程不出本机，不依赖百度/云服务。

> **核心价值**：用户已验证效果优于百度网盘 AI 笔记；本技能把"怎么搭 + 哪些坑必踩"完整固化，照做即可复刻，无需从零试错。

---

## 一、架构（流水线）

```
视频
 ├─ ffmpeg 抽关键帧 ──────────► 截图/  (HTML内嵌 + 视觉OCR用)
 ├─ faster-whisper(本地CPU)听音轨 ─► 逐字稿.txt(带时间戳)
 ├─ [可选] 视觉模型(35B)看帧OCR ─► 画面识别.txt (无声/纯录屏救命)
 └─ 本地 llama-server(OpenAI兼容) 总结 ─► 结构化JSON ─► 组装
        └─ index.html(默认) + 可选 md/docx/pptx/思维导图
```

三个本地环节，互不依赖云：**ffmpeg**（独立二进制）、**faster-whisper**（本地模型）、**本地大模型服务**（任意 OpenAI 兼容，默认 `http://localhost:8000/v1`）。

---

## 二、交付物（仓库应含）

| 文件 | 说明 | 是否入库 |
|---|---|---|
| `make_notes.py` | 核心流水线（~1100 行，纯标准库 + 惰性导入第三方） | ✅ 必须 |
| `run.bat` | Windows 启动器，**必须 GBK+CRLF**（见命门2） | ✅ 必须 |
| `run.sh` | macOS/Linux 启动器（**必须 LF**，拖拽/命令行） | ✅ 必须 |
| `requirements.txt` | faster-whisper / python-docx / python-pptx / requests | ✅ 必须 |
| `README.md` | 完整搭建 + 八个坑 | ✅ 必须 |
| `SKILL.md` | 本文件 | ✅ 必须 |
| `docs/llama-setup.md` | 作者真实大模型环境总结（llama.cpp CUDA13.3 构建/启动器/模型） | ✅ 建议带 |
| `skills/story-taught-tech-new/` | 知识讲解 skill（笔记"类比讲原理"的能力来源） | ✅ 建议带 |
| `.gitignore` | 排除大文件 | ✅ 必须 |
| `tools/ffmpeg/` | ffmpeg 二进制(400MB+) | ❌ 忽略，用户自行下载 |
| `models/` | whisper 模型(200MB+) | ❌ 忽略，用户自行下载 |
| `.venv/` `output/` `*.log` | 环境/产物/日志 | ❌ 忽略 |

> **不要往 git 里塞大文件**：ffmpeg、whisper 模型、.venv、output 都靠 README 指引用户本地准备。

---

## 三、搭建 SOP（照做）

> ⚠️ **搭建前必读**：先看 README 第三节「硬件与模型选型」——按显卡显存（6-8/12/16/24GB/无独显）选对模型与量化，否则会直接 OOM 或跑不起来。显存 <12GB 别硬上 30B/35B，用 Qwen3-8B 档。

1. **Python 隔离环境**：**用 3.11 / 3.12**（3.13 偏新，ctranslate2 可能无 wheel）；`python -m venv .venv` → 激活 → `pip install -r requirements.txt`。
2. **ffmpeg**：下载解压到 `tools/ffmpeg/bin/ffmpeg.exe`（或 macOS/Linux 的 `ffmpeg`），或加入系统 PATH。
3. **whisper 模型**：在 `models/` 下用 huggingface-cli 走镜像下放（**别直连 HF**，见命门3）：
   ```bash
   pip install -U "huggingface_hub[cli]"
   set HF_ENDPOINT=https://hf-mirror.com     # macOS/Linux: export HF_ENDPOINT=https://hf-mirror.com
   huggingface-cli download Systran/faster-whisper-base --local-dir models/faster-whisper-base
   ```
4. **本地大模型**：起一个 OpenAI 兼容服务监听 `localhost:8000`（llama.cpp/ollama 均可）。笔记用 30B 级文本模型；无声视频 OCR 需带视觉的 35B。大模型文件用魔搭/hf-mirror 下（见 README 3.6 / docs/llama-setup.md 第四节）。
5. **运行**：Windows 拖视频到 `run.bat`；macOS/Linux 用 `./run.sh` 或直接 `python make_notes.py "<视频或文件夹>"`。加 `--reuse` 调提示词秒重跑；`--vision` 强制视觉 OCR；`--all-formats` 补四种格式。

---

## 四、关键参数（写进 argparse）

- `--out <根目录>`：总输出根（默认 `output/`）；**别传完整 `_notes` 路径**，否则套两层目录。
- `--reuse`：复用逐字稿/截图/画面识别，跳过转写。
- `--vision`：强制视觉 OCR。
- `--frame-interval 15`：抽帧间隔（默认 30）。
- `--mirror-from "D:\baiduyunpan"`：输出目录从指定层级起镜像。
- `--all-formats`：额外生成 md/docx/pptx/思维导图。
- `--device cuda`：Whisper 强走 GPU（**新手禁用**，见命门1）。
- `--llm-base / --llm-model / --vision-url / --vision-model`：接 ollama 或独立视觉端点。

---

## 五、🔥 命门（必踩，提前处理）

**命门1 — Whisper 的 GPU 加速取决于系统 CUDA 版本**：ctranslate2 预编译版要 `cublas64_12.dll`。若系统只有 CUDA 13（50 系新卡 610+ 驱动，只有 `cublas64_13.dll`）→ 缺文件 → 必须 CPU（默认即 CPU，加线程超时兜底）。若系统装了 CUDA 12（有 `v12.x` 目录）→ 用 `--device cuda` 即可 GPU 加速。别照抄"必须 CPU"——有 CUDA 12 的用户不加这参数就是浪费显卡。
**命门2 — bat 必须 GBK+CRLF**：cmd 默认 936(GBK)。UTF-8→中文乱码"不是内部或外部命令"；GBK 但 LF 行尾→命令从词中断（echo→'cho'）。生成用二进制 `("\r\n".join(lines)).encode("gbk")`，绝不用 `\n` 文本拼接。对应的 `run.sh` 则必须 LF，否则 macOS/Linux 报 "bad interpreter"。
**命门3 — HF 大文件被代理截断**：直连下载模型变 0 字节/半截。whisper 模型用 `huggingface-cli download ... --local-dir` 并设 `HF_ENDPOINT=https://hf-mirror.com`；大模型(30B/35B)用魔搭或 hf-mirror + 迅雷/aria2c 拉，别用浏览器直连。脚本优先读本地 `models/`。
**命门4 — 跨盘符**：视频 D:、输出 F: 时 `os.path.relpath` 抛错；`video_src_for_html()` 跨盘回退 `file://` 绝对路径。
**命门5 — 路径带引号/`file://`**：用户从资源管理器复制路径常带 `"..."` 或 `file:///` 前缀；入口 `clean_input_path()` 去掉。
**命门6 — Office 占用**：docx/pptx 被打开时 `PermissionError`；`_save_office()` 捕获后另存 `xxx_new.ext`。
**命门7 — max_tokens 太小截断笔记**：旧值 2048 不够，放至 7000（本机上下文 16384 撑得住）。
**命门8 — 视觉与笔记共端点但显存二选一（24GB+ 可破局）**：12/16GB 显存放不下 30B 和 35B 同驻，无声视频 OCR 前先加载带视觉的 35B。但 **24GB+ 用户（3090/4090/5090）可起两个 llama-server**（8000 跑 30B、8001 跑 35B），用 `--vision-url http://localhost:8001/v1 --vision-model <35B名>` 让 OCR 走独立端点，无需切换模型。详见 docs/llama-setup.md 第六节。

---

## 六、提示词要点（决定笔记质量）

- 每个 section 的 point 不是短语，是 1-3 句完整讲解（是什么/为什么/怎么做），复用转写里的具体类名/方法名。
- 每节必须有 `deepdive`：用**生活类比**讲底层原理（先类比后点破技术对应），对标"story-taught-tech"风格。本仓库已附 `skills/story-taught-tech-new/` 完整 skill，可直接让 AI 助手加载它来产出统一的类比讲解质量。
- 每节若有代码，给 `code.desc`（必填，解释意图/步骤/输入输出）+ `code.snippet`（还原真实代码）。
- 产出 `summary_table`（知识点/核心内容/考点易混点/难度★）与 `quiz`。
- frame 只能从给定截图清单挑，禁止编造文件名。

---

## 七、验证清单

- [ ] 单视频拖拽 → `output/<镜像路径>/<名>_notes/index.html` 生成，内嵌截图可跳视频。
- [ ] 文件夹拖拽 → 递归查找、按源路径镜像、已存在则跳过（断点续跑）。
- [ ] 带引号路径粘贴 → `clean_input_path` 清洗后正常运行不闪退。
- [ ] 无声视频 + 35B 视觉 → `画面识别.txt` 生成、笔记含画面文字。
- [ ] `--reuse` 跳过转写秒重跑。

---

## 八、给用户的话

搭好后告诉用户：**Windows 双击 `run.bat` 拖视频即用；macOS/Linux 用 `./run.sh` 或直接 `python make_notes.py`**；调提示词改 `make_notes.py` 的 system prompt；无声视频先加载视觉模型（或 24GB+ 用双 server）。硬件选型与所有坑已在 README 第三节与第六节详述。
