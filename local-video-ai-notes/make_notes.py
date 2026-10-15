#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地视频 -> AI 富笔记生成器
流水线: 抽关键帧(ffmpeg) -> 本地 Whisper 转写(带时间戳) -> 本地 llama-server 总结 -> 富笔记包
输出: 笔记.md(图文+时间戳+导图) / 逐字稿.txt / 笔记.docx / 笔记.pptx / 思维导图.mm / index.html(视频联动播放器)

依赖(隔离 venv): faster-whisper, python-docx, python-pptx, requests
大模型: 复用你本地 llama-server (OpenAI 兼容, 默认 http://localhost:8000/v1)
"""
import argparse
import base64
import html as _html
import json
import os
import re
import sys
import subprocess
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(SCRIPT_DIR, "tools", "ffmpeg", "bin", "ffmpeg.exe")
FFPROBE = os.path.join(SCRIPT_DIR, "tools", "ffmpeg", "bin", "ffprobe.exe")
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"
if not os.path.exists(FFPROBE):
    FFPROBE = "ffprobe"

WHISPER_MODEL_DEFAULT = "base"
WHISPER_MODELS_DIR = os.path.join(SCRIPT_DIR, "models")  # 本地预置的 whisper 模型目录
LLM_BASE_URL = "http://localhost:8000/v1"
LLM_MODEL = "Qwen3-30B-A3B-Q4_K_M"
LLM_MAX_TOKENS = 7000  # 放开输出长度上限(含 deepdive/code 扩展内容); 本机上下文16384, 7000级安全
FRAME_INTERVAL = 30  # 秒：每隔多少秒抽一帧
CHUNK_TOKENS = 5000  # 转写稿超过此估算 token 数则分块总结

# 视觉 OCR：默认复用笔记模型端点。
# 说明：本机有两个本地模型——
#   模型1 = Qwen3.6-35B-A3B(Q2_K_P，带 mmproj 视觉)，模型2 = Qwen3-30B-A3B(Q4_K_M，纯文本)。
# 若笔记端点加载的是 35B 视觉模型，则同一端点既能 OCR 又能写笔记(无声视频最优解)；
# 若加载的是 30B(纯文本)，视觉调用会失败并优雅降级(转写为空则笔记为空)。
VISION_BASE_URL = "http://localhost:8000/v1"
VISION_MODEL = "Qwen3-30B-A3B-Q4_K_M"  # 视觉能力取决于实际加载的模型；单模型服务下通常可忽略此字段
VISION_MAX_FRAMES = 8  # 视觉OCR最多抽样帧数(控制token成本)
VISION_SILENT_CHARS = 30  # 转写稿字数低于此值视为"无声/极短旁白"，自动触发视觉OCR


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def run(cmd):
    print("  $", " ".join(cmd) if isinstance(cmd, list) else cmd)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")


def fmt_ts(sec):
    sec = int(round(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def clean_input_path(raw):
    """清洗用户输入：去掉首尾空白、成对引号，以及浏览器复制路径时可能带上的 file:// 前缀。"""
    s = (raw or "").strip()
    if s.lower().startswith("file:///"):
        s = s[8:]
    elif s.lower().startswith("file://"):
        s = s[7:]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s.strip()


def mirror_rel(path, mirror_from=None):
    """计算 path 在 output 下应镜像的相对目录。

    - 默认：去掉盘符后的完整路径（保留从驱动器根开始的完整层级），
      例如 D:\\baiduyunpan\\course\\a.mp4 -> baiduyunpan/course
    - 若给定 mirror_from：只取相对于该目录的相对路径
      （mirror_from 不是祖先时自动回退到去盘符完整路径）。
    """
    p = os.path.abspath(path)
    if mirror_from:
        mf = os.path.abspath(clean_input_path(mirror_from))
        try:
            rel = os.path.relpath(p, mf)
            if rel != "." and not rel.startswith(".."):
                return rel
        except ValueError:
            pass
    drive, tail = os.path.splitdrive(p)
    return tail.lstrip("/\\")


def parse_ts(ts):
    """'01:52' / '1:02:33' -> 秒；无法解析返回 None。"""
    if ts is None:
        return None
    nums = re.findall(r"\d+", str(ts))
    if not nums:
        return None
    nums = [int(x) for x in nums]
    if len(nums) >= 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0]


def pick_frame_for_ts(ts, manifest):
    """按时间戳就近匹配一帧截图(比让模型瞎猜文件名可靠)。"""
    sec = parse_ts(ts)
    if sec is None or not manifest:
        return None
    return min(manifest, key=lambda m: abs(m["ts"] - sec))["file"]


# ----------------------------------------------------------------------------
# 1. 视频元数据 + 抽帧
# ----------------------------------------------------------------------------
def get_meta(video):
    r = run([FFPROBE, "-v", "error", "-show_entries",
             "format=duration:stream=width,height,r_frame_rate",
             "-of", "json", video])
    try:
        d = json.loads(r.stdout)
        dur = float(d["format"]["duration"])
        st = next(s for s in d["streams"] if s.get("width"))
        w, h = int(st["width"]), int(st["height"])
        return dur, w, h
    except Exception:
        return 0.0, 0, 0


def extract_frames(video, out_dir, interval):
    os.makedirs(out_dir, exist_ok=True)
    # 缩到宽 960，省空间；每 interval 秒一帧
    vf = f"fps=1/{interval},scale='min(960,iw)':-2"
    pat = os.path.join(out_dir, "frame_%06d.jpg")
    run([FFMPEG, "-y", "-i", video, "-vf", vf, "-q:v", "3", pat])
    return scan_frames(out_dir, interval)


def scan_frames(out_dir, interval):
    """从已存在的截图目录重建帧清单(供 --reuse 复用)。"""
    files = sorted([f for f in os.listdir(out_dir)
                    if f.startswith("frame_") and f.endswith(".jpg")]) if os.path.isdir(out_dir) else []
    return [{"file": f, "ts": i * interval, "label": fmt_ts(i * interval)}
            for i, f in enumerate(files)]


# ----------------------------------------------------------------------------
# 2. Whisper 本地转写
# ----------------------------------------------------------------------------
def resolve_whisper_model(model):
    """把模型名解析成本地路径：优先用预先下好的 models/faster-whisper-<name>，
    避免本机联网去 HuggingFace 下载（国内易卡/失败）。"""
    if os.path.isdir(model):
        return model
    local = os.path.join(WHISPER_MODELS_DIR, f"faster-whisper-{model}")
    if os.path.isdir(local) and os.path.exists(os.path.join(local, "model.bin")):
        print(f"  [Whisper] 使用本地预置模型: {local}")
        return local
    return model  # 回退成模型名，让 faster-whisper 自己去下载


# Whisper 模型缓存：跨视频复用，避免 9 个文件反复加载 9 次
_WHISPER_MODELS = {}


def _load_whisper(model, device):
    key = (model, device)
    if key not in _WHISPER_MODELS:
        from faster_whisper import WhisperModel
        # CPU 上用 int8（快而省）；GPU 上用 float16
        ct = "int8" if device == "cpu" else "float16"
        print(f"  [Whisper] 加载模型 {os.path.basename(str(model))} (device={device}) ...")
        _WHISPER_MODELS[key] = WhisperModel(model, device=device, compute_type=ct)
    return _WHISPER_MODELS[key]


def _transcribe_try(video, model, device, timeout):
    """单设备尝试转写；用线程+超时防止 CUDA 卡死拖垮整批。"""
    import queue, threading
    res = queue.Queue()

    def worker():
        try:
            wm = _load_whisper(model, device)
            out, lang, prob = [], None, 0.0
            segs, info = wm.transcribe(video, beam_size=5, language="zh")
            for s in segs:
                out.append({"start": s.start, "end": s.end, "text": s.text.strip()})
            res.put(("ok", out, info.language, info.language_probability))
        except Exception as e:
            res.put(("err", repr(e)))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise RuntimeError(f"{device} 模式 {timeout}s 内无响应（疑似 CUDA 卡死），已中断本次尝试")
    status, *rest = res.get()
    if status == "err":
        raise RuntimeError(rest[0])
    out, lang, prob = rest
    print(f"  [Whisper] 实际设备: {device} | 语言: {lang}, 概率 {prob:.2f}")
    return out


def transcribe(video, model, device="cpu", cuda_timeout=120):
    model = resolve_whisper_model(model)
    # 默认 CPU（最稳）：faster-whisper 底层的 ctranslate2 预编译版要 CUDA 12 的
    # cublas64_12.dll；若系统只有 CUDA 13（如 50 系新卡 610+ 驱动）则缺此文件，
    # CUDA 模式会报错或卡死。但**若用户系统装了 CUDA 12**，则 --device cuda 可正常加速。
    # 因此默认走 CPU，仅当用户显式 --device cuda 才试 GPU，失败/超时自动回退 CPU。
    if device == "cuda":
        try:
            return _transcribe_try(video, model, "cuda", cuda_timeout)
        except Exception as e:
            print(f"  [Whisper] CUDA 失败({e})，回退 CPU ...")
            device = "cpu"
    return _transcribe_try(video, model, "cpu", cuda_timeout)


def build_transcript(segs):
    lines = []
    for s in segs:
        lines.append(f"[{fmt_ts(s['start'])}] {s['text']}")
    return "\n".join(lines)


def parse_transcript(text):
    """把逐字稿.txt 反解析成 segs(供 --reuse)。"""
    segs = []
    for line in text.splitlines():
        m = re.match(r"\[([\d:]+)\]\s*(.*)", line.strip())
        if m:
            segs.append({"start": parse_ts(m.group(1)) or 0, "end": 0, "text": m.group(2)})
    return segs


# ----------------------------------------------------------------------------
# 3. 调用本地大模型 (OpenAI 兼容)
# ----------------------------------------------------------------------------
def call_llm(base_url, api_key, model, system, user, timeout=900, max_tokens=LLM_MAX_TOKENS):
    import requests
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    r = requests.post(url, headers={"Authorization": f"Bearer {api_key}"},
                     json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def extract_json(text):
    # 容忍模型多余的说明文字，抓取第一个 {...}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def call_vision(base_url, api_key, model, system, text, images,
                timeout=600, max_tokens=2000):
    """多模态调用：把若干图片(已编码为 data URI)与文本一起发给视觉模型。"""
    import requests
    url = base_url.rstrip("/") + "/chat/completions"
    content = [{"type": "text", "text": text}]
    for img in images:
        content.append({"type": "image_url", "image_url": {"url": img}})
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    r = requests.post(url, headers={"Authorization": f"Bearer {api_key}"},
                     json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def vision_ocr(frames_dir, manifest, base_url, api_key, model,
               cache_path=None, max_frames=VISION_MAX_FRAMES, timeout=600):
    """用本地视觉模型识别关键帧画面文字/代码/图表，返回整合文本。

    均匀抽样最多 max_frames 帧，base64 编码后发给视觉模型；
    识别结果写入 cache_path(供 --reuse 复用)。返回 str，空串表示未识别/失败。
    """
    if not manifest:
        return ""
    n = len(manifest)
    if n <= max_frames:
        picks = manifest
    else:
        idxs = [int(round(i * (n - 1) / (max_frames - 1))) for i in range(max_frames)]
        picks = [manifest[i] for i in idxs]
    imgs, labels = [], []
    for m in picks:
        fp = os.path.join(frames_dir, m["file"])
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            imgs.append(f"data:image/jpeg;base64,{b64}")
            labels.append(f"{m['label']}({m['file']})")
        except Exception:
            continue
    if not imgs:
        return ""
    sys_p = (
        "你是视频画面分析助手。下面是一段教学/技术视频的若干关键帧截图(按时间顺序)。"
        "请识别每帧画面上的文字信息：包括幻灯片/PPT 文字、屏幕上的代码(尽量保留关键类名/方法名/语法)、"
        "公式、图表标题、终端输出等；并简述该帧主题。"
        "按帧顺序用『帧1 / 帧2 ...』分段输出，保留关键术语与代码片段(不要改写代码)。"
        "若某帧无明显文字请说明画面主题。只输出识别结果，不要评论。"
    )
    user_p = ("以下是各帧(标注了时间戳与文件名)：\n" + "\n".join(labels) +
              "\n请逐帧识别并输出画面文字与主题。")
    try:
        out = call_vision(base_url, api_key, model, sys_p, user_p, imgs,
                         timeout=timeout, max_tokens=2500)
    except Exception as e:
        print(f"  [!] 视觉OCR失败(可能当前模型不支持视觉): {e}")
        return ""
    out = (out or "").strip()
    if cache_path:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(out)
        except Exception:
            pass
    return out



def llm_summary(chunk_text, manifest_str, base_url, api_key, model,
                is_final=False, max_tokens=LLM_MAX_TOKENS, vision_context=""):
    sys_p = (
        "你是资深技术讲师，擅长把视频内容整理成『可以直接复习』的深度学习笔记"
        "(风格对标优质付费课程笔记：讲解详细、有具体类名方法名、有易混点提示)。"
        "根据转写稿和截图清单，产出结构化、有信息量的中文笔记。"
        "只输出一个 JSON 对象，不要任何解释。字段格式:\n"
        "{\n"
        '  "title": "整份笔记标题",\n'
        '  "intro": "2-4句导读: 这个视频讲什么、解决什么问题、学完能掌握什么",\n'
    '  "sections": [\n'
    '    {"heading": "小节标题(要具体到知识点, 不要笼统)",\n'
    '     "ts": "mm:ss 该节起始时间",\n'
    '     "frame": "从截图清单里选与该节最贴切的截图文件名, 或 null",\n'
    '     "points": ["要点1(1-3句完整讲解)", "要点2", ...],\n'
    '     "note": "该节的易混点/面试考点提示, 没有则空串",\n'
    '     "deepdive": "底层原理/延伸知识(2-5句). 在视频已讲内容之上合理扩展, 解释背后的机制; 用生活类比讲清(朴素日常事物如小区物业/快递/排队/做菜), 先讲类比再点破对应技术点; 类比讲\'什么感觉\', 机制讲\'为什么\'; 不编造术语, 不确定标注\'拓展思考\'",\n'
    '     "code": [{"desc": "代码说明: 这段在做什么/关键步骤/输入/输出/设计意图(帮助看不懂代码的人理解)", "lang": "java", "snippet": "尽量还原视频真实代码或伪代码; 无代码可写 null 或留空"}]\n'
    '    }\n'
    '  ],\n'
    '  "summary_table": [\n'
    '    {"topic": "知识点", "content": "核心内容(1-2句)", "exam": "考点/易混点", "level": "★~★★★★"}\n'
    '  ],\n'
    '  "quiz": [{"q": "题目", "a": "答案"}]\n'
    "}\n"
    "硬性要求:\n"
    "1) 每个 point 不是短语，而是 1-3 句完整讲解，讲清『是什么/为什么/怎么做』，"
    "尽量复用转写稿里的具体类名、方法名、参数、版本、结论；\n"
    "2) sections 按逻辑细分，一共至少 4 节；每节 3-6 个 points，信息量要足，"
    "让人不看视频也能复习到位；\n"
    "3) summary_table 至少 4 行，覆盖本段核心知识点，level 用 ★ 表示难度；\n"
    "4) quiz 出 3 道有区分度的题；\n"
    "5) frame 只能从给定截图清单里挑文件名，不要编造；\n"
    "6) 每节必须有 deepdive：在视频已讲内容之上做合理延伸，解释底层原理/本质机制；"
    "必须用『生活类比』方式讲(朴素可感知的日常事物)，先类比后点破技术对应；类比负责『是什么感觉』，机制负责『为什么』，二者缺一不可；"
    "不要编造术语，拿不准就写明是拓展思考；\n"
    "7) 每节若有代码实现，用 code 给出 desc(必填，解释代码意图/关键步骤/输入输出/设计动机) + snippet(尽量还原视频真实代码片段或伪代码)；"
    "无代码则该节 code 给空数组 [] 或 null；desc 的核心作用是让看不懂代码的人也能理解其逻辑。\n"
    "8) 若提供了【画面视觉识别内容】，必须结合其中的屏幕文字/代码/图表来丰富笔记；"
    "尤其当转写稿为空或极稀疏时，画面内容就是主要信息来源，请据此重建章节、要点与代码；不要忽略它。"
    )
    if is_final:
        sys_p += "\n这是全文综述：sections 为各章总览，quiz 出 3 道综合题。"
    user_p = f"【截图清单(时间戳 / 文件名)】\n{manifest_str}\n\n【转写稿(带时间戳)】\n{chunk_text}"
    if vision_context:
        user_p += (
            "\n\n【画面视觉识别内容(模型对关键帧的OCR/描述，请务必结合其中的屏幕文字/代码补充笔记；"
            "若转写稿为空或稀疏，画面内容即主要信息来源)】\n" + vision_context
        )
    content = call_llm(base_url, api_key, model, sys_p, user_p, max_tokens=max_tokens)
    return extract_json(content)


def llm_mindmap(title, outline_text, base_url, api_key, model):
    sys_p = ("根据章节大纲生成 Mermaid mindmap 的节点文本(不含 ``` 包裹)。"
             "格式: 每个一级节点一行以 '* ' 开头，子节点缩进两个空格继续 '* '。"
             "只输出节点文本，不要解释。")
    user_p = f"标题: {title}\n大纲:\n{outline_text}"
    return call_llm(base_url, api_key, model, sys_p, user_p, timeout=300, max_tokens=1500)


# ----------------------------------------------------------------------------
# 4. 组装输出
# ----------------------------------------------------------------------------
def _norm_points(s):
    """points 兼容字符串与 {text:..} 两种形态。"""
    out = []
    for p in s.get("points", []) or []:
        if isinstance(p, dict):
            t = p.get("text") or p.get("point") or ""
            if t:
                out.append(str(t))
        elif p is not None:
            out.append(str(p))
    return out


def make_markdown(title, intro, sections, summary_table, quiz, mindmap_text,
                  rel_frames_dir="截图"):
    md = []
    md.append(f"# {title}\n")
    md.append("> 由本地视频 + 本地大模型生成 · 全程不出本机\n")
    if intro:
        md.append(f"> **导读**：{intro}\n")
    # 目录
    md.append("## 目录\n")
    for i, s in enumerate(sections, 1):
        md.append(f"{i}. {s.get('heading','小节')}")
    md.append("")
    for i, s in enumerate(sections, 1):
        md.append(f"## {i}. {s.get('heading','小节')}")
        ts = s.get("ts", "")
        if ts:
            md.append(f"*⏱️ {ts}*\n")
        for p in _norm_points(s):
            md.append(f"- {p}")
        note = s.get("note")
        if note:
            md.append(f"\n> 🔎 **考点/易混点**：{note}")
        fr = s.get("frame")
        if fr and str(fr).lower() != "null":
            for f in [x.strip() for x in str(fr).split(",") if x.strip()]:
                md.append(f"\n![{s.get('heading','')}]({rel_frames_dir}/{f})\n")
        md.append("")
    # 知识小结表
    if summary_table:
        md.append("## 📊 知识小结\n")
        md.append("| 知识点 | 核心内容 | 考点/易混点 | 难度 |")
        md.append("| --- | --- | --- | --- |")
        for r in summary_table:
            md.append(f"| {r.get('topic','')} | {r.get('content','')} | "
                      f"{r.get('exam','')} | {r.get('level','')} |")
        md.append("")
    # 思维导图
    md.append("## 🧠 思维导图\n")
    md.append("```mermaid\nmindmap\n  root((%s))\n%s\n```\n" % (title, mindmap_text))
    # 自测题
    md.append("## ❓ 自测题\n")
    for qa in quiz:
        md.append(f"**Q: {qa.get('q','')}**")
        md.append(f"A: {qa.get('a','')}\n")
    return "\n".join(md)


def make_docx(path, title, intro, sections, summary_table, quiz, frames_dir):
    from docx import Document
    from docx.shared import Inches
    doc = Document()
    doc.add_heading(title, 0)
    if intro:
        p = doc.add_paragraph()
        p.add_run(intro).italic = True
    for i, s in enumerate(sections, 1):
        doc.add_heading(f"{i}. {s.get('heading','小节')}", level=1)
        if s.get("ts"):
            p = doc.add_paragraph()
            p.add_run(f"⏱️ {s['ts']}").italic = True
        for pt in _norm_points(s):
            doc.add_paragraph(pt, style="List Bullet")
        if s.get("note"):
            p = doc.add_paragraph()
            p.add_run("🔎 考点/易混点：" + str(s["note"])).italic = True
        fr = s.get("frame")
        if fr and str(fr).lower() != "null":
            for f in [x.strip() for x in str(fr).split(",") if x.strip()]:
                fp = os.path.join(frames_dir, f)
                if os.path.exists(fp):
                    try:
                        doc.add_picture(fp, width=Inches(5.5))
                    except Exception:
                        pass
    if summary_table:
        doc.add_heading("知识小结", level=1)
        try:
            t = doc.add_table(rows=1, cols=4)
            t.style = "Table Grid"
            hdr = t.rows[0].cells
            for j, h in enumerate(["知识点", "核心内容", "考点/易混点", "难度"]):
                hdr[j].text = h
            for r in summary_table:
                c = t.add_row().cells
                c[0].text = str(r.get("topic", ""))
                c[1].text = str(r.get("content", ""))
                c[2].text = str(r.get("exam", ""))
                c[3].text = str(r.get("level", ""))
        except Exception as e:
            print("  [!] docx 表格失败:", e)
    if quiz:
        doc.add_heading("自测题", level=1)
        for qa in quiz:
            doc.add_paragraph(f"Q: {qa.get('q','')}")
            doc.add_paragraph(f"A: {qa.get('a','')}")
    doc.save(path)


def make_pptx(path, title, intro, sections, summary_table, frames_dir):
    from pptx import Presentation
    from pptx.util import Inches, Pt
    prs = Presentation()
    # 封面
    s0 = prs.slides.add_slide(prs.slide_layouts[0])
    s0.shapes.title.text = title
    try:
        s0.placeholders[1].text = intro or "本地视频 · 本地大模型生成"
    except Exception:
        pass
    for i, s in enumerate(sections, 1):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = f"{i}. {s.get('heading','小节')}"
        body = slide.placeholders[1]
        tf = body.text_frame
        tf.text = ""
        pts = _norm_points(s)
        for k, pt in enumerate(pts):
            p = tf.add_paragraph()
            p.text = "• " + pt
            p.font.size = Pt(14)
        fr = s.get("frame")
        if fr and str(fr).lower() != "null":
            f = [x.strip() for x in str(fr).split(",") if x.strip()][0]
            fp = os.path.join(frames_dir, f)
            if os.path.exists(fp):
                try:
                    slide.shapes.add_picture(fp, Inches(6.8), Inches(1.6), height=Inches(2.6))
                except Exception:
                    pass
    if summary_table:
        slide = prs.slides.add_slide(prs.slide_layouts[5] if len(prs.slide_layouts) > 5
                                     else prs.slide_layouts[1])
        slide.shapes.title.text = "知识小结"
        rows = len(summary_table) + 1
        try:
            tbl = slide.shapes.add_table(rows, 4, Inches(0.4), Inches(1.3),
                                         Inches(9.2), Inches(0.4 * rows + 0.4)).table
            for j, h in enumerate(["知识点", "核心内容", "考点/易混点", "难度"]):
                tbl.cell(0, j).text = h
            for i, r in enumerate(summary_table, 1):
                tbl.cell(i, 0).text = str(r.get("topic", ""))
                tbl.cell(i, 1).text = str(r.get("content", ""))
                tbl.cell(i, 2).text = str(r.get("exam", ""))
                tbl.cell(i, 3).text = str(r.get("level", ""))
        except Exception:
            pass
    prs.save(path)


def make_mindmap_mm(path, title, mindmap_text):
    # FreeMind .mm (XMind 可直接打开)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<map version="1.0.1">',
             f'  <node TEXT="{title}">']
    for line in (mindmap_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"\*\s*(.+)", line)
        if m:
            lines.append(f'    <node TEXT="{m.group(1)}"/>')
    lines.append("  </node>\n</map>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def video_src_for_html(video, out):
    """返回 HTML <video> 可用的 src：
    同盘符用相对路径(可随文件夹移动)，跨盘符(C/D/E...)回退到 file:// 绝对路径。"""
    try:
        rel = os.path.relpath(video, out).replace("\\", "/")
        return rel
    except ValueError:
        # 跨盘符：os.path.relpath 会抛 ValueError，改用 file:// 绝对路径
        return "file:///" + video.replace("\\", "/")


HTML_CSS = """
:root{--bg:#f7f8fa;--fg:#1f2328;--muted:#6b7280;--accent:#1a73e8;--line:#e6e8eb}
*{box-sizing:border-box}
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;display:flex;height:100vh;color:var(--fg);background:var(--bg)}
#left{flex:1;min-width:0;background:#000;display:flex;align-items:center;justify-content:center}
#left video{max-width:100%;max-height:100%}
#right{width:50%;min-width:380px;overflow:auto;padding:24px 28px 70px;background:#fff}
#right h1{font-size:22px;margin:0 0 6px}
.meta{color:var(--muted);font-size:12px;margin-bottom:14px}
.intro{background:#f0f6ff;border-left:3px solid var(--accent);padding:10px 12px;border-radius:6px;font-size:14px;line-height:1.75;margin-bottom:16px}
.toc{background:#fafbfc;border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin-bottom:18px;font-size:14px}
.toc b{display:block;margin-bottom:6px;color:var(--muted);font-size:12px;letter-spacing:.5px}
.toc a{display:block;color:var(--accent);text-decoration:none;padding:3px 0}
.toc a:hover{text-decoration:underline}
.sec{border-bottom:1px solid var(--line);padding:14px 0 18px}
.sec h2{font-size:17px;margin:0 0 8px;scroll-margin-top:12px}
.ts{color:var(--accent);cursor:pointer;font-size:12px;font-weight:400;background:#eef4ff;border-radius:10px;padding:2px 9px;white-space:nowrap}
.ts:hover{background:#dde9ff}
.sec ul{margin:6px 0;padding-left:20px}
.sec li{line-height:1.85;margin:5px 0;font-size:14px}
.note{background:#fff8e6;border-left:3px solid #f0b400;padding:8px 11px;border-radius:6px;font-size:13px;color:#7a5b00;margin:9px 0;line-height:1.7}
.shot{width:100%;border:1px solid var(--line);border-radius:8px;margin:11px 0 4px;cursor:pointer;display:block}
.shot:hover{box-shadow:0 3px 14px rgba(0,0,0,.16)}
table.kt{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0 22px}
table.kt th,table.kt td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top;line-height:1.6}
table.kt th{background:#f2f4f7;font-weight:600}
table.kt tr:nth-child(even) td{background:#fafbfc}
.lv{color:#e8a33d;white-space:nowrap;text-align:center;letter-spacing:1px}
.quiz{background:#f7f9fc;border-radius:8px;padding:11px 13px;margin:9px 0;font-size:14px}
.quiz .q{font-weight:600;margin:0 0 5px}
.quiz .a{margin:0;color:#374151;line-height:1.7}
.deep{background:#f3fbf6;border-left:3px solid #2e9e5b;padding:10px 12px;border-radius:6px;font-size:13.5px;color:#1d4d33;margin:10px 0;line-height:1.75}
.code-desc{background:#eef2ff;border-left:3px solid #5b6bff;padding:8px 11px;border-radius:6px;font-size:13px;color:#2a2f66;margin:10px 0 4px;line-height:1.7}
pre.code{background:#0f172a;color:#e2e8f0;border-radius:8px;padding:12px 14px;overflow:auto;font-size:12.5px;line-height:1.6;margin:4px 0 10px}
pre.code code{font-family:"SFMono-Regular",Consolas,"Courier New",monospace;white-space:pre}
h2.blk{font-size:17px;margin:22px 0 8px}
"""

HTML_JS = """
document.querySelectorAll('[data-ts]').forEach(function(e){
  var s=e.getAttribute('data-ts'); if(!s) return;
  e.onclick=function(){
    var p=s.split(':').map(Number);
    var t = p.length>2 ? (p[0]*3600+p[1]*60+p[2]) : (p[0]*60+(p[1]||0));
    var v=document.getElementById('v'); v.currentTime=t; v.play();
  };
});
document.querySelectorAll('.toc a').forEach(function(a){
  a.onclick=function(ev){ev.preventDefault();
    var el=document.querySelector(a.getAttribute('href'));
    if(el) el.scrollIntoView({behavior:'smooth',block:'start'});
  };
});
"""


def make_html(path, video_rel, title, intro, sections, summary_table, quiz,
              vision_used=False):
    def esc(s):
        return _html.escape(str(s if s is not None else ""))
    parts = []
    parts.append('<!doctype html><html lang="zh"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    parts.append(f'<title>{esc(title)}</title><style>{HTML_CSS}</style></head><body>')
    parts.append(f'<div id="left"><video id="v" controls src="{esc(video_rel)}"></video></div>')
    parts.append('<div id="right">')
    parts.append(f'<h1>{esc(title)}</h1>')
    meta = "🎬 本地视频 · 本地大模型生成 · 全程不出本机 · 点时间戳或截图可跳转视频"
    if vision_used:
        meta += " · 🔎 已视觉识别画面文字(无声/纯录屏可用)"
    parts.append(f'<div class="meta">{meta}</div>')
    if intro:
        parts.append(f'<div class="intro">💡 <b>导读：</b>{esc(intro)}</div>')
    if len(sections) > 1:
        toc = "".join(f'<a href="#sec{i}">{i}. {esc(s.get("heading",""))}</a>'
                      for i, s in enumerate(sections, 1))
        parts.append(f'<div class="toc"><b>目录</b>{toc}</div>')
    for i, s in enumerate(sections, 1):
        ts = s.get("ts", "")
        head = f'{i}. {esc(s.get("heading","小节"))}'
        if ts:
            head += f' <span class="ts" data-ts="{esc(ts)}">⏱️ {esc(ts)}</span>'
        parts.append(f'<div class="sec" id="sec{i}"><h2>{head}</h2><ul>')
        for p in _norm_points(s):
            parts.append(f"<li>{esc(p)}</li>")
        parts.append("</ul>")
        if s.get("note"):
            parts.append(f'<div class="note">🔎 <b>考点/易混点：</b>{esc(s["note"])}</div>')
        dd = s.get("deepdive")
        if dd and str(dd).lower() != "null":
            parts.append(f'<div class="deep">🔍 <b>底层原理 / 延伸：</b>{esc(dd)}</div>')
        codes = s.get("code") or []
        if isinstance(codes, dict):
            codes = [codes]
        for c in codes:
            if not isinstance(c, dict):
                continue
            cdesc = c.get("desc")
            if cdesc and str(cdesc).lower() != "null":
                parts.append(f'<div class="code-desc">📝 <b>代码说明：</b>{esc(cdesc)}</div>')
            csnip = c.get("snippet")
            if csnip and str(csnip).lower() not in ("null", "none", ""):
                lang = esc(c.get("lang", ""))
                parts.append(f'<pre class="code"><code data-lang="{lang}">{esc(csnip)}</code></pre>')
        fr = s.get("frame")
        if fr and str(fr).lower() != "null":
            for f in [x.strip() for x in str(fr).split(",") if x.strip()]:
                parts.append(f'<img class="shot" loading="lazy" src="截图/{esc(f)}" '
                             f'data-ts="{esc(ts)}" alt="{esc(s.get("heading",""))}">')
        parts.append("</div>")
    if summary_table:
        parts.append('<h2 class="blk">📊 知识小结</h2><table class="kt"><thead><tr>'
                     '<th>知识点</th><th>核心内容</th><th>考点/易混点</th><th>难度</th>'
                     '</tr></thead><tbody>')
        for r in summary_table:
            parts.append(f'<tr><td>{esc(r.get("topic",""))}</td>'
                         f'<td>{esc(r.get("content",""))}</td>'
                         f'<td>{esc(r.get("exam",""))}</td>'
                         f'<td class="lv">{esc(r.get("level",""))}</td></tr>')
        parts.append("</tbody></table>")
    if quiz:
        parts.append('<h2 class="blk">❓ 自测题</h2>')
        for qa in quiz:
            parts.append(f'<div class="quiz"><p class="q">Q：{esc(qa.get("q",""))}</p>'
                         f'<p class="a">A：{esc(qa.get("a",""))}</p></div>')
    parts.append("</div>")
    parts.append(f"<script>{HTML_JS}</script></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))


# ----------------------------------------------------------------------------
# 视频文件发现
# ----------------------------------------------------------------------------
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
              ".ts", ".mpeg", ".mpg", ".3gp", ".rmvb", ".mts", ".m2ts", ".vob"}

# 批量安全阈值（仅用于提示/确认，不限制功能本身）
# - 找到的视频数超过此值：提示先 --dry-run 预览清单，避免误抓软件自带视频/长时间空转
DRYRUN_SUGGEST_THRESHOLD = 50
# - 超过此值：强制要求 y/N 确认后才开始，防止手滑把整个大目录一次性跑光
BATCH_CONFIRM_THRESHOLD = 300


def find_videos(folder, recursive=True, exts=None):
    exts = {e.lower() if e.startswith(".") else "." + e.lower()
            for e in (exts or VIDEO_EXTS)}
    res = []
    if recursive:
        walker = os.walk(folder)
    else:
        walker = [(folder, [], [f for f in os.listdir(folder)
                                if os.path.isfile(os.path.join(folder, f))])]
    for root, _, files in walker:
        for f in files:
            if os.path.splitext(f)[1].lower() in exts:
                res.append(os.path.join(root, f))
    return sorted(res)


# ----------------------------------------------------------------------------
# 主流程（单视频）
# ----------------------------------------------------------------------------
def process_one(video, out, args):
    """处理单个视频，产物输出到 out 目录。异常向上抛，由调用方处理批量容错。"""
    os.makedirs(out, exist_ok=True)
    frames_dir = os.path.join(out, "截图")
    base = os.path.splitext(os.path.basename(video))[0]
    print(f"[*] 输出目录: {out}")

    transcript_path = os.path.join(out, "逐字稿.txt")
    reuse = getattr(args, "reuse", False) and os.path.exists(transcript_path)

    if reuse:
        # ---- 复用已有转写稿与截图，跳过抽帧/转写(方便调提示词时快速重跑) ----
        print("[1-2/4] --reuse: 复用已有逐字稿与截图，跳过抽帧 / 转写")
        with open(transcript_path, encoding="utf-8") as f:
            transcript = f.read()
        segs = parse_transcript(transcript)
        manifest = scan_frames(frames_dir, args.frame_interval)
        dur, w, h = 0.0, 0, 0
        print(f"      复用 {len(segs)} 段转写, {len(manifest)} 张截图")
    else:
        # 1. 元数据 + 抽帧
        print("[1/4] 抽取关键帧 ...")
        dur, w, h = get_meta(video)
        print(f"      时长 {fmt_ts(dur)}  分辨率 {w}x{h}")
        manifest = extract_frames(video, frames_dir, args.frame_interval)
        print(f"      抽取 {len(manifest)} 帧 (每 {args.frame_interval}s)")
        # 2. 转写
        print("[2/4] Whisper 本地转写 ...")
        segs = transcribe(video, args.whisper_model, args.device)
        transcript = build_transcript(segs)
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript)
        print(f"      转写 {len(segs)} 段, 字数 ~{len(transcript)}")

    manifest_str = "\n".join(f"{m['label']}  {m['file']}" for m in manifest)

    # 2.5 视觉 OCR：无声/极短旁白自动触发；或 --vision 强制开启。
    # 作用：让视觉模型(35B)「看」关键帧，OCR 出屏幕上的代码/PPT/文字，
    # 让无声视频(纯录屏/纯PPT)也能生成文本笔记。结果缓存到 画面识别.txt 供 --reuse 复用。
    vision_context = ""
    vision_used = False
    silent = len(transcript.strip()) < VISION_SILENT_CHARS
    vision_cache = os.path.join(out, "画面识别.txt")
    if not args.no_llm and (args.vision or silent):
        if getattr(args, "reuse", False) and os.path.exists(vision_cache):
            print("[*] --reuse: 复用已有画面识别结果")
            vision_context = open(vision_cache, encoding="utf-8").read().strip()
            vision_used = bool(vision_context)
        else:
            print("[*] 视觉OCR：识别关键帧画面文字/代码 ...")
            vision_context = vision_ocr(frames_dir, manifest, args.vision_url,
                                        args.api_key, args.vision_model,
                                        cache_path=vision_cache)
            vision_used = bool(vision_context.strip())
            if not vision_used:
                print("      无可用视觉内容(模型不支持视觉或画面无文字)；"
                      "若视频确实无声，请在 启动大模型.bat 选 模型1(35B视觉) 再跑")

    # 3. 本地大模型总结
    sections, quiz, summary_table = [], [], []
    mindmap_text, intro, title = "", "", base
    if not args.no_llm:
        print("[3/4] 本地大模型总结 (llama-server) ...")
        est_tokens = len(transcript) // 2  # 中文粗估
        chunks = []
        if est_tokens > CHUNK_TOKENS:
            buf, cnt = [], 0
            for s in segs:
                line = f"[{fmt_ts(s['start'])}] {s['text']}\n"
                if cnt + len(line) // 2 > CHUNK_TOKENS and buf:
                    chunks.append("\n".join(buf)); buf, cnt = [], 0
                buf.append(line); cnt += len(line) // 2
            if buf:
                chunks.append("\n".join(buf))
            print(f"      转写较长, 分 {len(chunks)} 块总结")
        else:
            chunks = [transcript]

        failed = False
        for idx, ch in enumerate(chunks):
            try:
                j = llm_summary(ch, manifest_str, args.llm_base, args.api_key,
                                args.llm_model, is_final=(len(chunks) == 1),
                                max_tokens=args.max_tokens,
                                vision_context=vision_context)
                if j and "sections" in j:
                    if j.get("title") and (idx == 0 or not title or title == base):
                        title = j["title"]
                    if j.get("intro") and not intro:
                        intro = j["intro"]
                    sections.extend(j.get("sections", []) or [])
                    quiz.extend(j.get("quiz", []) or [])
                    summary_table.extend(j.get("summary_table", []) or [])
                    print(f"      第 {idx+1}/{len(chunks)} 块: +{len(j.get('sections',[]) or [])} 节")
                else:
                    print(f"      [!] 第 {idx+1} 块返回解析失败, 跳过")
            except Exception as e:
                print("  [!] 大模型调用失败:", e)
                print("      请确认 启动大模型.bat 已选模型并 [1] 起服务 (localhost:8000)")
                failed = True
                break

        # 截图按时间戳自动兜底(模型没给或给错时)
        for s in sections:
            fr = s.get("frame")
            if not fr or str(fr).lower() in ("null", "none", ""):
                s["frame"] = pick_frame_for_ts(s.get("ts"), manifest)

        if not failed and sections:
            try:
                outline = "\n".join(f"{i}. {s.get('heading','')}"
                                    for i, s in enumerate(sections, 1))
                mindmap_text = llm_mindmap(title, outline, args.llm_base,
                                           args.api_key, args.llm_model)
            except Exception as e:
                print("  [!] 导图生成失败:", e)
                mindmap_text = ""

    if not sections:  # LLM 不可用时的降级包
        print("  [i] 未生成 LLM 总结, 输出降级版(纯转写+截图)")
        sections = [{"heading": "全文转写",
                     "points": [t for t in transcript.split("\n")[:40]],
                     "frame": pick_frame_for_ts("00:00", manifest), "ts": "00:00", "note": ""}]
        mindmap_text = ""

    if not mindmap_text:
        mindmap_text = "* 内容\n  * " + (sections[0].get("heading", base) if sections else base)

    # 4. 组装
    print("[4/4] 组装笔记包 ...")
    # HTML 是核心交付物，始终生成（左视频 + 右图文笔记，含 deepdive/code）
    try:
        make_html(os.path.join(out, "index.html"),
                  video_src_for_html(video, out), title, intro, sections,
                  summary_table, quiz, vision_used=vision_used)
        print("  [OK] index.html (核心交付)")
    except Exception as e:
        print("  [!] html 失败:", e)

    # 其余格式(docx/md/pptx/mm)默认不再生成；加 --all-formats 才补。
    # 用户反馈 HTML 已满足需要，少生成产物 => 更快、目录更干净。
    if getattr(args, "all_formats", False):
        md = make_markdown(title, intro, sections, summary_table, quiz, mindmap_text)
        with open(os.path.join(out, "笔记.md"), "w", encoding="utf-8") as f:
            f.write(md)
        def _save_office(fname, fn):
            """产物被 Office 占用时自动另存为 xxx_new.ext，绝不静默丢文件。"""
            p = os.path.join(out, fname)
            try:
                fn(p)
                return
            except PermissionError:
                root, ext = os.path.splitext(fname)
                alt_name = root + "_new" + ext
                try:
                    fn(os.path.join(out, alt_name))
                    print(f"  [i] {fname} 正被占用(可能已在 Word/PPT 中打开)，已另存为 {alt_name}")
                except Exception as e:
                    print(f"  [!] {fname} 失败:", e)
            except Exception as e:
                print(f"  [!] {fname} 失败:", e)

        _save_office("笔记.docx", lambda p: make_docx(
            p, title, intro, sections, summary_table, quiz, frames_dir))
        _save_office("笔记.pptx", lambda p: make_pptx(
            p, title, intro, sections, summary_table, frames_dir))
        try:
            make_mindmap_mm(os.path.join(out, "思维导图.mm"), title, mindmap_text)
        except Exception as e:
            print("  [!] mm 失败:", e)
        print("  [i] --all-formats: 已额外生成 笔记.md / 笔记.docx / 笔记.pptx / 思维导图.mm")

    print("  [OK] 产物:")
    keep = ["index.html", "逐字稿.txt"]
    if getattr(args, "all_formats", False):
        keep += ["笔记.md", "笔记.docx", "笔记.pptx", "思维导图.mm"]
    for f in keep:
        p = os.path.join(out, f)
        if os.path.exists(p):
            print(f"   - {p}")


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------
class _Tee:
    """同时写控制台与日志文件（文件用 UTF-8 保真中文）。"""
    def __init__(self, console, logf):
        self.console = console
        self.logf = logf

    def write(self, s):
        try:
            self.console.write(s)
        except Exception:
            pass
        try:
            self.logf.write(s)
        except Exception:
            pass

    def flush(self):
        try:
            self.console.flush()
        except Exception:
            pass
        try:
            self.logf.flush()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="本地视频 -> AI 富笔记（支持文件夹批量）")
    ap.add_argument("video", help="单个视频文件 或 视频所在文件夹")
    ap.add_argument("--out", default=None, help="输出目录（文件夹模式时作为根目录，保留子目录结构）")
    ap.add_argument("--whisper-model", default=WHISPER_MODEL_DEFAULT)
    ap.add_argument("--device", default="cpu", help="whisper 转写设备: cpu / cuda。默认 cpu 最稳；若你的系统装了 CUDA 12(toolkit v12.x) 可改 cuda 加速")
    ap.add_argument("--llm-base", default=LLM_BASE_URL)
    ap.add_argument("--llm-model", default=LLM_MODEL)
    ap.add_argument("--api-key", default="local")
    ap.add_argument("--frame-interval", type=int, default=FRAME_INTERVAL)
    ap.add_argument("--max-tokens", type=int, default=LLM_MAX_TOKENS,
                    help="单次大模型输出上限(调大=笔记更详细, 本机上下文16384内建议<=6000)")
    ap.add_argument("--no-llm", action="store_true", help="跳过本地大模型(仅转写+截图)")
    ap.add_argument("--no-recursive", action="store_true", help="文件夹模式不递归子目录")
    ap.add_argument("--reuse", action="store_true",
                    help="复用输出目录里已有的逐字稿.txt与截图, 跳过抽帧/转写(调提示词时快速重跑)")
    ap.add_argument("--ext", default=None, help="自定义视频后缀(逗号分隔), 如 mp4,mkv")
    ap.add_argument("--force", action="store_true", help="强制重跑(忽略已存在的笔记)")
    ap.add_argument("--dry-run", action="store_true",
                    help="仅扫描并列出将要处理的视频清单+数量，不调用 Whisper/LLM（大文件夹先用它预览，避免误抓/长时间运行）")
    ap.add_argument("--mirror-from", default=None,
                    help="镜像起点：输出目录只保留从该路径之后的层级(如 'D:\\videos\\java-course')；不传则镜像完整路径(去盘符)")
    ap.add_argument("--all-formats", action="store_true",
                    help="默认只生成 index.html(核心交付)；加此参数才额外生成 笔记.md/笔记.docx/笔记.pptx/思维导图.mm")
    ap.add_argument("--vision", action="store_true",
                    help="强制启用视觉OCR(识别关键帧画面文字/代码)。默认仅在『转写稿为空/极短』时自动启用，适合无声/纯录屏/纯PPT视频")
    ap.add_argument("--vision-url", default=VISION_BASE_URL,
                    help="视觉模型端点(默认复用笔记端点 localhost:8000；需加载支持视觉的模型如35B才能OCR)")
    ap.add_argument("--vision-model", default=VISION_MODEL,
                    help="视觉模型名(单模型服务下通常可忽略)")
    args = ap.parse_args()

    out_root = os.path.abspath(args.out) if args.out else os.path.join(SCRIPT_DIR, "output")

    # ---- 日志文件：每次运行在 out_root/ 下生成 run_时间戳.log ----
    log_dir = out_root
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "run_" + time.strftime("%Y%m%d_%H%M%S") + ".log")
    try:
        _logf = open(log_path, "w", encoding="utf-8", buffering=1)
        sys.stdout = _Tee(sys.stdout, _logf)
        sys.stderr = _Tee(sys.stderr, _logf)
        print(f"[*] 日志文件: {log_path}")
    except Exception as e:
        print(f"[警告] 日志文件创建失败(仅影响记录, 不影响运行): {e}")

    args.video = clean_input_path(args.video)
    src = os.path.abspath(args.video)
    if not os.path.exists(src):
        print("文件/文件夹不存在:", src); sys.exit(1)

    # ---- 输出目录：镜像输入路径（去盘符 / 或 --mirror-from 指定起点）----
    src_rel = mirror_rel(src, args.mirror_from)
    print(f"[*] 输出根目录: {out_root}")

    # ---- 单文件模式：镜像视频所在父目录 ----
    if os.path.isfile(src):
        base = os.path.splitext(os.path.basename(src))[0]
        dir_rel = mirror_rel(os.path.dirname(src), args.mirror_from)
        out = os.path.join(out_root, dir_rel, base + "_notes")
        if args.dry_run:
            sz = os.path.getsize(src)
            print(f"[dry-run] 单文件模式")
            print(f"  将处理: {src}")
            print(f"  文件大小: {sz/1024/1024:.1f} MB")
            print(f"  输出目录: {out}")
            print(f"[dry-run] 未做任何处理，退出。")
            return
        if dir_rel:
            print(f"[*] 镜像子路径: {dir_rel}")
        else:
            print(f"[*] 镜像模式: 完整路径(去盘符)")
        try:
            process_one(src, out, args)
        except Exception as e:
            print("\n[!] 处理失败:", e); sys.exit(1)
        return

    # ---- 文件夹批量模式：镜像输入文件夹本身 ----
    src_rel = mirror_rel(src, args.mirror_from)
    if src_rel:
        print(f"[*] 镜像子路径: {src_rel}")
    else:
        print(f"[*] 镜像模式: 完整路径(去盘符)")

    # ---- 文件夹批量模式 ----
    videos = find_videos(src, recursive=not args.no_recursive,
                         exts=args.ext.split(",") if args.ext else None)
    if not videos:
        print("在文件夹中未找到视频文件:", src); sys.exit(1)
    print(f"[*] 在 {src} 找到 {len(videos)} 个视频（递归={not args.no_recursive}）")

    # ---- 仅预览：列出清单后退出，不调用 Whisper/LLM ----
    if args.dry_run:
        print(f"\n[dry-run] 将处理以下 {len(videos)} 个视频（不调用 Whisper/LLM）：")
        for i, v in enumerate(videos, 1):
            print(f"  {i:4}. {v}")
        print(f"\n[dry-run] 共 {len(videos)} 个视频，未做任何处理，退出。")
        return

    # ---- 安全提示：超过阈值才建议 --dry-run（小文件夹不打扰）----
    if len(videos) > DRYRUN_SUGGEST_THRESHOLD:
        print(f"\n⚠️ 找到 {len(videos)} 个视频，超过建议预览阈值({DRYRUN_SUGGEST_THRESHOLD})。")
        print("   建议先加 --dry-run 预览清单，确认无误后再正式运行"
              "（避免误抓软件自带视频/长时间空转）。")

    # ---- 超大目录强制确认 ----
    if len(videos) > BATCH_CONFIRM_THRESHOLD:
        if sys.stdin.isatty():
            ans = input(f"\n找到 {len(videos)} 个视频，确认要全部处理？(y/N) ").strip().lower()
            if ans not in ("y", "yes", "是"):
                print("已取消。"); sys.exit(0)
        else:
            print(f"\n⚠️ 非交互环境且视频数={len(videos)}>={BATCH_CONFIRM_THRESHOLD}，"
                  f"为安全起见请显式加 --dry-run 或确认后再运行。")

    ok, fail = 0, []
    for i, v in enumerate(videos, 1):
        base = os.path.splitext(os.path.basename(v))[0]
        rel = os.path.dirname(os.path.relpath(v, src))
        # 最终输出 = 输出根 / 输入文件夹镜像子路径 / 内部相对路径 / 视频名_notes
        out = os.path.join(out_root, src_rel, rel, base + "_notes")
        # 跳过标记用 index.html（默认核心交付，任何模式都会生成）；--all-formats 也含它
        if not args.force and not args.reuse and os.path.exists(os.path.join(out, "index.html")):
            print(f"\n[{i}/{len(videos)}] 跳过(已存在笔记): {v}")
            ok += 1
            continue
        print(f"\n[{i}/{len(videos)}] 处理: {v}")
        try:
            process_one(v, out, args)
            ok += 1
        except SystemExit:
            fail.append(v)
        except Exception as e:
            fail.append(v)
            print(f"  [!!] 失败: {v} -> {e}")

    print(f"\n[OK] 批量完成：成功 {ok}/{len(videos)}")
    print(f"[*] 本次日志: {log_path}")
    if fail:
        print(f"  失败 {len(fail)} 个：")
        for f in fail:
            print(f"    - {f}")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\n[时间] 总耗时 {time.time()-t0:.1f}s")
