# -*- coding: utf-8 -*-
"""
AVToolbox CLI —— 给 AI agent / 脚本调用的非交互接口。

设计原则（面向 agent，而非面向人）：
  1. 所有输出统一走 stdout 的单个 JSON envelope：
       {"ok": bool, "action": str, "data": {...}, "error": null|{"code","message","hint"}}
     stdout 只有这一份 JSON，绝不混入 ffmpeg 日志 —— 日志走 stderr。
  2. 退出码语义化，便于 agent 判断：
       0 成功 / 2 参数错误 / 3 源文件问题 / 4 ffmpeg 缺失 / 5 执行失败 / 6 输出已存在
  3. 每个动作都提供 --dry-run，只回命令不落盘，Agent 可先确认再执行。
  4. capabilities 动作把「本机到底能干什么」全量吐出来（含可用预设、目标格式、模板占位符），
     agent 无需读源码就能自我校准。
  5. 默认不覆盖已有输出文件（除非 --overwrite），避免 agent 误删用户数据。

用法示例：
    python avtool.py capabilities
    python avtool.py probe --input a.mp4
    python avtool.py merge --video v.mp4 --audio a.m4a --output out.mp4
    python avtool.py compress --input big.mp4 --preset x264_crf23
    python avtool.py convert --input a.mov --format mp4
    python avtool.py extract --input a.mp4 --mode audio_from_video --format mp3
    python avtool.py batch --input dir/ --op convert --format mkv --recursive --json-progress
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

import deps
import compressor as C
import converter as V
import batch as B

CREATE_NO_WINDOW = deps.CREATE_NO_WINDOW
FFMPEG = deps.FFMPEG
FFPROBE = deps.FFPROBE

CLI_VERSION = "1.0"
SCHEMA_VERSION = "1"

EXIT_OK = 0
EXIT_ARGS = 2
EXIT_INPUT = 3
EXIT_NO_FFMPEG = 4
EXIT_RUN = 5
EXIT_EXISTS = 6


# ============================================================ JSON 输出

class Failure(Exception):
    """带退出码的业务错误。"""

    def __init__(self, message, code=EXIT_RUN, hint=None, error_code="runtime_error"):
        super().__init__(message)
        self.message = message
        self.exit_code = code
        self.hint = hint
        self.error_code = error_code


def _emit(payload, exit_code=EXIT_OK):
    """把结果写到 stdout（唯一的 JSON），然后退出。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return exit_code


def ok(action, **data):
    return {"ok": True, "action": action, "schema": SCHEMA_VERSION,
            "data": data, "error": None}


def fail(action, exc):
    return {"ok": False, "action": action, "schema": SCHEMA_VERSION, "data": None,
            "error": {"code": exc.error_code, "message": exc.message, "hint": exc.hint}}


# ============================================================ 媒体探测

def probe_raw(path):
    """调 ffprobe 拿原始 JSON。失败返回 None。"""
    if not FFPROBE:
        return None
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL, timeout=120,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def media_info(path):
    """把 ffprobe 结果整理成 agent 友好的结构。"""
    raw = probe_raw(path)
    if not raw:
        return None
    fmt = raw.get("format", {}) or {}
    streams = []
    for s in raw.get("streams", []) or []:
        kind = s.get("codec_type")
        item = {
            "index": s.get("index"),
            "type": kind,
            "codec": s.get("codec_name"),
            "codec_long": s.get("codec_long_name"),
        }
        if kind == "video":
            item["width"] = s.get("width")
            item["height"] = s.get("height")
            item["fps"] = _parse_fps(s.get("avg_frame_rate") or s.get("r_frame_rate"))
            item["pix_fmt"] = s.get("pix_fmt")
            item["is_cover"] = bool((s.get("disposition") or {}).get("attached_pic"))
        elif kind == "audio":
            item["channels"] = s.get("channels")
            item["sample_rate"] = int(s.get("sample_rate") or 0)
            item["bit_rate"] = int(s.get("bit_rate") or 0)
        streams.append(item)

    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    has_video = any(s["type"] == "video" and not s.get("is_cover") for s in streams)
    has_audio = any(s["type"] == "audio" for s in streams)

    try:
        size = int(fmt.get("size") or os.path.getsize(path))
    except (TypeError, ValueError, OSError):
        size = 0

    return {
        "path": os.path.abspath(path),
        "name": os.path.basename(path),
        "size": size,
        "size_human": V.human_size(size),
        "duration": round(duration, 3),
        "duration_str": _fmt_dur(duration),
        "format": fmt.get("format_name"),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "has_video": has_video,
        "has_audio": has_audio,
        "video_streams": sum(1 for s in streams if s["type"] == "video" and not s.get("is_cover")),
        "audio_streams": sum(1 for s in streams if s["type"] == "audio"),
        "streams": streams,
        "tags": _pick_tags(fmt.get("tags") or {}),
    }


def _parse_fps(text):
    if not text or text in ("0/0", "N/A"):
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den = float(den)
            return round(float(num) / den, 3) if den else None
        return round(float(text), 3)
    except (ValueError, ZeroDivisionError):
        return None


def _fmt_dur(sec):
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _pick_tags(tags):
    keep = ("title", "artist", "album", "comment", "encoder", "creation_time")
    return {k: v for k, v in tags.items() if k.lower() in keep}


def require_file(path, what="输入文件"):
    if not path:
        raise Failure(f"缺少{what}路径", EXIT_ARGS, error_code="missing_argument")
    if not os.path.isfile(path):
        raise Failure(f"{what}不存在: {path}", EXIT_INPUT,
                      hint="确认路径是否含空格或需要引号", error_code="input_not_found")
    return os.path.abspath(path)


def require_ffmpeg():
    if not FFMPEG:
        raise Failure("未找到 ffmpeg", EXIT_NO_FFMPEG,
                      hint="确认 bin/ffmpeg.exe 存在，或把 ffmpeg 加入 PATH",
                      error_code="ffmpeg_not_found")


def resolve_output(src, output, ext, out_dir=None, overwrite=False, suffix="",
                   dry_run=False):
    """推导输出路径；默认拒绝覆盖已有文件。

    suffix: 自动推导文件名时追加的后缀，避免与源文件同名
            （例：压缩 big.mp4 默认产出 big_compressed.mkv，而不是又撞上 big.mp4）。
            只在未指定 --output 时生效。
    dry_run: dry-run 不落盘，因此不因文件已存在而中断 —— 那会让
            「先 dry-run 看命令、再实跑」这个推荐流程在第二次调用时直接失败。
            此时只在返回值里附带冲突信息（见下面的 _LAST_* 约定）。
    """
    if output:
        dst = os.path.abspath(output)
    else:
        base_dir = out_dir or os.path.dirname(os.path.abspath(src))
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(base_dir, stem + suffix + ext)
    if os.path.exists(dst) and not overwrite and not dry_run:
        raise Failure(f"输出文件已存在: {dst}", EXIT_EXISTS,
                      hint="加 --overwrite 覆盖，或用 --output / --out-dir 换位置",
                      error_code="output_exists")
    d = os.path.dirname(dst)
    if d and not os.path.isdir(d):
        raise Failure(f"输出目录不存在: {d}", EXIT_ARGS,
                      hint="先用 mkdir 建目录，或改 --output 指向已存在的目录",
                      error_code="output_dir_missing")
    return dst


def output_conflicts(dst, overwrite=False):
    """dry-run 时回报「若真跑会不会撞车」，供 agent 提前决策。"""
    if overwrite or not os.path.exists(dst):
        return None
    return {"code": "output_exists",
            "message": f"输出文件已存在: {dst}",
            "hint": "实跑前需加 --overwrite，或改用 --output / --out-dir"}


# ============================================================ 执行

def run_ffmpeg(cmd, quiet=False, timeout=None):
    """执行 ffmpeg。stdout/stderr 全部走 stderr，保证 stdout 只有最终 JSON。"""
    if not quiet:
        sys.stderr.write("[cmd] " + " ".join(_quote(c) for c in cmd) + "\n")
        sys.stderr.flush()
    t0 = time.time()
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise Failure("执行超时", EXIT_RUN, error_code="timeout")
    except OSError as e:
        raise Failure(f"无法执行 ffmpeg: {e}", EXIT_NO_FFMPEG, error_code="exec_failed")
    elapsed = round(time.time() - t0, 2)

    tail = ""
    if r.returncode != 0:
        lines = [l for l in (r.stderr or "").splitlines() if l.strip()]
        tail = "\n".join(lines[-12:])
        raise RuntimeError_Ffmpeg(tail, cmd, elapsed, r.returncode)
    return elapsed, (r.stderr or "").strip()


class RuntimeError_Ffmpeg(Failure):
    def __init__(self, ffmpeg_log, cmd, elapsed, rc):
        msg = _explain_ffmpeg(ffmpeg_log) or "ffmpeg 执行失败"
        super().__init__(msg, EXIT_RUN, hint=ffmpeg_log[-600:] if ffmpeg_log else None,
                         error_code="ffmpeg_error")
        self.ffmpeg_log = ffmpeg_log
        self.cmd = cmd
        self.elapsed = elapsed
        self.returncode = rc


def _explain_ffmpeg(log):
    """把常见 ffmpeg 报错翻译成 agent 能直接照做的提示。"""
    rules = [
        ("Could not write header", "目标容器装不下该流组合（如裸音频容器塞入视频轨），换容器或去掉多余流"),
        ("does not support", "目标格式不支持所选编码，换编码器或换容器"),
        ("Invalid argument", "参数不合法，检查编码器名/分辨率/时间点"),
        ("No such file or directory", "源文件路径有误"),
        ("Permission denied", "文件被占用或无写权限，关闭占用程序或换输出目录"),
        ("Output file #0 does not contain any stream", "没有选中任何流，检查 -map 与源是否有对应轨道"),
        ("Filtergraph", "滤镜与 -c copy 互斥，仅换容器时不能加滤镜"),
        ("unknown encoder", "本机 ffmpeg 没有该编码器，跑 capabilities 看可用列表"),
        ("height not divisible by 2", "缩放后高度为奇数，把 scale 的 h 写成 -2"),
    ]
    low = log.lower()
    for key, tip in rules:
        if key.lower() in low:
            first = [l for l in log.splitlines() if l.strip()]
            return f"{first[-1] if first else key} → {tip}"
    lines = [l for l in log.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def _quote(s):
    s = str(s)
    return f'"{s}"' if " " in s else s


def emit_result(action, dst, elapsed, src_size, quiet=False, extra=None):
    """构造执行成功后的 data（含体积对比）。"""
    size_out = os.path.getsize(dst) if os.path.isfile(dst) else 0
    ratio, delta = V.ratio_text(src_size, size_out) if src_size else ("—", "—")
    data = {
        "output": dst,
        "size_in": src_size,
        "size_out": size_out,
        "size_in_human": V.human_size(src_size) if src_size else None,
        "size_out_human": V.human_size(size_out),
        "ratio": ratio,
        "change": delta,
        "elapsed": elapsed,
    }
    if extra:
        data.update(extra)
    return ok(action, **data)


# ============================================================ 动作实现

def act_capabilities(args):
    """把本机能力 + 全部可用选项吐给 agent，让它能自我校准。"""
    require_ffmpeg()
    rep = deps.check_all()
    encoders = rep["encoders"]
    nvenc = rep["nvenc"]

    lossless = [{"key": k, "label": C.LOSSLESS_PRESETS[k]["label"],
                "container": C.LOSSLESS_PRESETS[k]["container"],
                "hw": C.LOSSLESS_PRESETS[k]["hw"],
                "note": C.LOSSLESS_PRESETS[k]["note"]}
               for k in C.available_lossless(encoders, nvenc)]
    comp = [{"key": k, "label": C.COMPRESS_PRESETS[k]["label"],
             "container": C.COMPRESS_PRESETS[k]["container"],
             "hw": C.COMPRESS_PRESETS[k]["hw"],
             "note": C.COMPRESS_PRESETS[k]["note"]}
            for k in C.available_compress(encoders, nvenc)]

    def _targets(table, kind):
        out = []
        for key, t in table.items():
            out.append({
                "key": key,
                "ext": t["ext"],
                "label": t["label"],
                "kind": kind,
                "default_video_codec": t.get("default_v"),
                "default_audio_codec": t.get("default_a"),
                "video_codecs": t.get("v", []),
                "audio_codecs": t.get("a", []),
                "note": t.get("note", ""),
                "multi_stream": bool(t.get("multi_stream")),
            })
        return out

    actions = {
        "capabilities": "查看本机 ffmpeg 能力、可用压缩方案、全部目标格式（agent 应先调这个）",
        "probe": "读取媒体文件信息（时长/分辨率/编码/流数/体积）",
        "merge": "把分离的视频流与音频流合成一个文件",
        "compress": "压缩视频：数学无损 或 高压缩比",
        "convert": "格式转换 / 仅换容器",
        "extract": "轨道提取：提音频 / 去音频 / 只换音频编码 / 仅换容器",
        "batch": "批量处理一或多组文件（对目录递归）",
    }
    examples = [
        "avtool.py capabilities",
        "avtool.py probe --input a.mp4",
        "avtool.py merge --video v.mp4 --audio a.m4a --output out.mp4 --dry-run",
        "avtool.py compress --input big.mp4 --preset x264_crf23 --out-dir ./out",
        "avtool.py convert --input a.mov --format mp4 --video-codec libx265 --crf 20",
        "avtool.py convert --input a.mp4 --format mkv --mode remux",
        "avtool.py extract --input a.mp4 --mode audio_from_video --format mp3",
        "avtool.py batch --input ./raw --op convert --format mkv --recursive --out-dir ./out",
    ]
    return ok(
        "capabilities",
        cli_version=CLI_VERSION,
        schema_version=SCHEMA_VERSION,
        ffmpeg=FFMPEG,
        ffmpeg_origin=rep["ffmpeg_origin"],
        ffprobe=FFPROBE,
        ffprobe_origin=rep["ffprobe_origin"],
        bin_ready=rep.get("bin_ready"),
        fetch_hint=rep.get("fetch_hint"),
        nvenc=nvenc,
        encoder_count=len(encoders),
        actions=actions,
        lossless_presets=lossless,
        compress_presets=comp,
        video_formats=_targets(V.VIDEO_TARGETS, "video"),
        audio_formats=_targets(V.AUDIO_TARGETS, "audio"),
        extract_modes=[{"key": k, "label": v["label"], "desc": v["desc"]}
                       for k, v in V.EXTRACT_MODES.items()],
        batch_ops={
            "convert": "格式转换（需 --format）",
            "remux": "仅换容器（需 --format）",
            "compress": "压缩（需 --preset）",
            "extract_audio": "提取音频（需 --format，音频格式）",
            "video_only": "只保留画面（需 --format）",
        },
        batch_template_placeholders={
            "{name}": "原文件名(不含扩展名)",
            "{ext}": "原扩展名(不含点)",
            "{index}": "序号(从1开始)",
            "{date}": "当天日期 YYYYMMDD",
            "{op}": "操作名 convert/compress/...",
        },
        exit_codes={
            "0": "成功",
            "2": "参数错误",
            "3": "源文件不存在或不可读",
            "4": "ffmpeg 缺失或无法执行",
            "5": "ffmpeg 执行失败",
            "6": "输出文件已存在（未加 --overwrite）",
        },
        examples=examples,
        notes=[
            "所有命令都支持 --dry-run，只输出将要执行的 ffmpeg 命令，不产生文件。",
            "stdout 恒为单个 JSON；ffmpeg 日志走 stderr，可用 --quiet 静音。",
            "默认不覆盖已有输出文件；需要覆盖请显式加 --overwrite。",
            "「仅换容器」前请先用 probe 看源编码，或直接跑 convert，兼容性不匹配会返回明确错误。",
            "无损压缩对已是有损编码的源通常不会变小，要瘦身请用高压缩比 preset。",
            "bin_ready 为 false 表示 bin/ 里没有内置 ffmpeg（仓库不含，因单文件 160MB 超平台限制）；"
            "此时用的是系统 ffmpeg。要恢复自带依赖，跑项目根目录的 fetch_ffmpeg.py。",
        ],
    )


def act_probe(args):
    path = require_file(args.input, "输入文件")
    require_ffmpeg()
    info = media_info(path)
    if not info:
        raise Failure(f"无法读取媒体信息（可能不是有效的音视频文件）: {path}",
                      EXIT_INPUT, hint="用 ffprobe 单独验一下，或换一个文件",
                      error_code="probe_failed")
    return ok("probe", **info)


def act_merge(args):
    require_ffmpeg()
    video = require_file(args.video, "视频源")
    audio = require_file(args.audio, "音频源") if args.audio else None

    A = _load_merge_build_cmd()
    start = _ts(args.start, "start")
    end = _ts(args.end, "end")
    if start is not None and end is not None and end <= start:
        raise Failure("--end 必须晚于 --start", EXIT_ARGS, error_code="bad_time_range")

    if args.mode == "copy" and (start or end or args.volume != 1.0 or args.audio_delay):
        if not args.force:
            raise Failure(
                "「仅换容器」模式下音量/音频延迟/时间裁剪无法生效（ffmpeg 的 -vf/-af 与 -c copy 互斥）",
                EXIT_ARGS,
                hint="改用 --mode transcode，或去掉 --volume/--audio-delay/--start/--end，"
                     "或加 --force 忽略这些参数继续",
                error_code="copy_mode_conflict")

    ext = os.path.splitext(args.output)[1] if args.output else (
        os.path.splitext(video)[1] or ".mkv")
    dst = resolve_output(video, args.output, ext, args.out_dir, args.overwrite,
                         dry_run=args.dry_run)

    cmd, desc = A.build_cmd(video, audio, dst, args.mode,
                            args.video_codec or "libx264",
                            args.audio_codec or "aac",
                            args.crf, args.preset_speed, args.audio_bitrate,
                            start, end, args.volume, args.shortest, args.audio_delay)

    if args.dry_run:
        return ok("merge", dry_run=True, command=cmd, command_str=" ".join(_quote(c) for c in cmd),
                  output=dst, video=video, audio=audio, mode=args.mode, description=desc,
                  conflict=output_conflicts(dst, args.overwrite))

    src_size = os.path.getsize(video)
    elapsed, _ = run_ffmpeg(cmd, args.quiet)
    return emit_result("merge", dst, elapsed, src_size, extra={"mode": args.mode, "description": desc})


_MERGE_NS = None


def _load_merge_build_cmd():
    """把 merge_av.py 里的 build_cmd 拿出来复用（GUI 与 CLI 共用同一套命令构造）。

    merge_av.py 顶层 import tkinter，无 GUI 环境下可能失败，所以做一个降级副本：
    直接解析源码取出 build_cmd 相关片段。这里采用更稳的做法——尝试 import，
    失败则从源码中提取函数体编译进独立命名空间。
    """
    global _MERGE_NS
    if _MERGE_NS is not None:
        return _MERGE_NS

    try:                                   # 首选：正常 import
        import merge_av as A
        _MERGE_NS = A
        return A
    except Exception:
        pass

    # 降级：把 merge_av.py 里 build_cmd 之前的所有"纯逻辑"片段抽出来执行。
    src = open(os.path.join(deps.APP_DIR, "merge_av.py"), encoding="utf-8").read()
    ns = {}
    head = src.split("# ---------------------------------------------------------------- GUI")[0]
    # 剥掉所有 import / tkinter 相关行，只保留常量与函数定义
    lines = []
    skip_block = None
    for ln in head.splitlines():
        if ln.strip().startswith(("import ", "from ")):
            continue
        if ln.strip().startswith("def run("):
            skip_block = "run"
            continue
        if skip_block == "run":
            if ln and not ln[0].isspace() and not ln.startswith("#"):
                skip_block = None
            else:
                continue
        lines.append(ln)
    code = "\n".join(lines)
    code = "import os, re, json, subprocess\n" + code
    code += "\nFFMPEG = r'%s'\n" % (FFMPEG or "")
    code += "\nFFPROBE = r'%s'\n" % (FFPROBE or "")
    code += "\nCREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0\n"
    exec(compile(code, "merge_av_head", "exec"), ns)
    _MERGE_NS = _NSProxy(ns)
    return _MERGE_NS


class _NSProxy:
    """把 dict 命名空间包装成对象属性访问。"""

    def __init__(self, ns):
        self.__dict__.update(ns)

    def __getattr__(self, item):
        raise Failure(f"合并模块缺少 {item}（merge_av.py 结构可能已变）", EXIT_RUN,
                      error_code="internal_error")


def _ts(text, name):
    """解析时间参数（支持 12 / 1:30 / 01:02:03.5）。"""
    if not text:
        return None
    t = str(text).strip()
    if not t:
        return None
    if not re.fullmatch(r"[\d:.]+", t):
        raise Failure(f"--{name} 时间格式无法识别: {text}", EXIT_ARGS,
                      hint="支持 12 / 1:30 / 01:02:03.5", error_code="bad_time")
    parts = t.split(":")
    if len(parts) > 3:
        raise Failure(f"--{name} 时间格式无法识别: {text}", EXIT_ARGS, error_code="bad_time")
    try:
        sec = 0.0
        for p in parts:
            sec = sec * 60 + float(p)
    except ValueError:
        raise Failure(f"--{name} 时间格式无法识别: {text}", EXIT_ARGS, error_code="bad_time")
    return sec


def act_compress(args):
    require_ffmpeg()
    src = require_file(args.input, "输入文件")
    encoders, nvenc = deps.ffmpeg_encoders(), deps.nvenc_available()

    all_presets = {}
    all_presets.update({k: ("lossless", v) for k, v in C.LOSSLESS_PRESETS.items()})
    all_presets.update({k: ("compress", v) for k, v in C.COMPRESS_PRESETS.items()})
    if args.preset not in all_presets:
        raise Failure(f"未知方案: {args.preset}", EXIT_ARGS,
                      hint="可用: " + ", ".join(sorted(all_presets)),
                      error_code="bad_preset")
    kind, preset = all_presets[args.preset]

    avail = (C.available_lossless(encoders, nvenc) if kind == "lossless"
             else C.available_compress(encoders, nvenc))
    if args.preset not in avail:
        raise Failure(f"方案 {args.preset} 在本机不可用（编码器缺失或显卡不支持）", EXIT_ARGS,
                      hint="可用: " + ", ".join(avail), error_code="preset_unavailable")

    info = media_info(src)
    if info and not info["has_video"]:
        raise Failure("源文件不含视频轨，压缩功能用不上（音频请走 convert/extract）",
                      EXIT_INPUT, error_code="no_video_stream")
    has_audio = bool(info["has_audio"]) if info else True

    start = _ts(args.start, "start")
    end = _ts(args.end, "end")
    if start is not None and end is not None and end <= start:
        raise Failure("--end 必须晚于 --start", EXIT_ARGS, error_code="bad_time_range")

    ext = preset["container"]
    dst = resolve_output(src, args.output, ext, args.out_dir, args.overwrite,
                         suffix="_compressed", dry_run=args.dry_run)

    if kind == "lossless":
        cmd = C.build_lossless_cmd(FFMPEG, src, dst, args.preset,
                                   audio_mode=args.audio, start=start, end=end,
                                   has_audio=has_audio)
    else:
        scale = _scale_arg(args.scale, info)
        cmd = C.build_compress_cmd(FFMPEG, src, dst, args.preset,
                                   audio_bitrate=args.audio_bitrate,
                                   start=start, end=end, scale=scale,
                                   preset=args.preset_speed, has_audio=has_audio)

    if args.dry_run:
        return ok("compress", dry_run=True, command=cmd,
                  command_str=" ".join(_quote(c) for c in cmd),
                  output=dst, input=src, preset=args.preset,
                  preset_label=preset["label"], kind=kind,
                  conflict=output_conflicts(dst, args.overwrite))

    src_size = os.path.getsize(src)
    elapsed, _ = run_ffmpeg(cmd, args.quiet)
    return emit_result("compress", dst, elapsed, src_size,
                       extra={"preset": args.preset, "preset_label": preset["label"], "kind": kind})


def _scale_arg(scale, info):
    """把 1080p / 720p / 1280x720 / -2 形式统一成 ffmpeg scale 表达式。"""
    if not scale:
        return None
    s = str(scale).strip().lower()
    named = {"1080p": "1920:-2", "720p": "1280:-2", "480p": "854:-2", "360p": "640:-2"}
    if s in named:
        return named[s]
    if re.fullmatch(r"\d+", s):
        return f"{s}:-2"
    if re.fullmatch(r"\d+x-?\d+", s):
        return s.replace("x", ":")
    raise Failure(f"--scale 无法识别: {scale}", EXIT_ARGS,
                  hint="支持 1080p/720p/480p/360p、宽度数字如 1280、或 1280x720",
                  error_code="bad_scale")


def act_convert(args):
    require_ffmpeg()
    src = require_file(args.input, "输入文件")
    info = media_info(src)
    if not info:
        raise Failure(f"无法读取媒体信息: {src}", EXIT_INPUT, error_code="probe_failed")

    # 同一个函数服务 convert 与 extract 两个子命令：
    #   convert 的 --mode   : convert / remux
    #   extract 的 --mode   : audio_from_video / video_only / remux / audio_only
    # 两者共用 remux，其余取值互不重叠，所以直接透传即可。
    # （曾经把 extract 的 audio_from_video 误写成 extract_audio，导致
    #   `extract --mode audio_from_video -f mp4` 静默走成整体转换。）
    mode = args.mode or "convert"
    if mode == "convert" and args.action == "extract":
        raise Failure("extract 必须指定 --mode（audio_from_video/video_only/remux/audio_only）",
                      EXIT_ARGS, hint="例如 --mode audio_from_video", error_code="missing_argument")
    return _do_convert(args, src, info, mode=mode)


def _do_convert(args, src, info, mode):
    # ---- 目标格式解析：
    # extract_audio 只接受音频格式；其余接受视频格式（audio_only 需多流容器）
    if mode == "audio_from_video":
        if not info["has_audio"]:
            raise Failure("源文件没有音轨，无法提取音频", EXIT_INPUT,
                          error_code="no_audio_stream")
        target = args.format or "mp3"
        if target not in V.AUDIO_TARGETS:
            raise Failure(f"--format {target} 不是音频格式", EXIT_ARGS,
                          hint="可用: " + ", ".join(V.AUDIO_TARGETS),
                          error_code="bad_format")
    elif mode == "video_only":
        target = args.format or "mp4"
        if target not in V.VIDEO_TARGETS:
            raise Failure(f"--format {target} 不是视频格式", EXIT_ARGS,
                          hint="可用: " + ", ".join(V.VIDEO_TARGETS),
                          error_code="bad_format")
    elif mode == "remux":
        target = args.format
        if not target or target not in V.ALL_TARGETS:
            raise Failure(f"--format 必填且必须是已知格式: {args.format}", EXIT_ARGS,
                          hint="可用: " + ", ".join(V.ALL_TARGETS),
                          error_code="bad_format")
        warns, must_re = V.check_compat(info, target, "remux")
        if warns and not args.force:
            raise Failure(warns[0], EXIT_INPUT,
                          hint="改用 --mode convert 重编码，或加 --force 强行尝试",
                          error_code="incompatible_container")
    else:
        target = args.format
        if not target or target not in V.ALL_TARGETS:
            raise Failure(f"--format 必填且必须是已知格式: {args.format}", EXIT_ARGS,
                          hint="可用: " + ", ".join(V.ALL_TARGETS),
                          error_code="bad_format")

    tgt = V.ALL_TARGETS[target]
    ext = tgt["ext"]
    # 未指定 --output 时加操作后缀，避免源是 a.mp4 目标也是 mp4 时撞名
    suffix = {"remux": "_remux", "audio_from_video": "_audio", "video_only": "_novideo"}.get(mode, "_converted")
    dst = resolve_output(src, args.output, ext, args.out_dir, args.overwrite,
                         suffix=suffix, dry_run=args.dry_run)

    has_audio = info["has_audio"] and tgt.get("default_a") != "none"

    if mode == "remux":
        cmd, err = V.build_extract_cmd(FFMPEG, src, dst, "remux", target)
    elif mode == "audio_from_video":
        cmd, err = V.build_extract_cmd(FFMPEG, src, dst, "audio_from_video", target,
                                       acodec=args.audio_codec, abitrate=args.audio_bitrate,
                                       has_audio=True)
    elif mode == "video_only":
        cmd, err = V.build_extract_cmd(FFMPEG, src, dst, "video_only", target,
                                       vcodec=args.video_codec, crf=args.crf,
                                       preset=args.preset_speed, has_audio=False)
    else:
        scale = _scale_arg(args.scale, info)
        cmd, err = V.build_convert_cmd(FFMPEG, src, dst, target,
                                       vcodec=args.video_codec, acodec=args.audio_codec,
                                       crf=args.crf, preset=args.preset_speed,
                                       abitrate=args.audio_bitrate, scale=scale,
                                       fps=args.fps, has_audio=has_audio)
    if err or not cmd:
        raise Failure(err or "无法构造转换命令", EXIT_ARGS, error_code="build_cmd_failed")

    if args.dry_run:
        return ok("convert", dry_run=True, command=cmd,
                  command_str=" ".join(_quote(c) for c in cmd),
                  output=dst, input=src, mode=mode, format=target,
                  target_label=tgt["label"],
                  conflict=output_conflicts(dst, args.overwrite))

    src_size = os.path.getsize(src)
    elapsed, _ = run_ffmpeg(cmd, args.quiet)
    return emit_result("convert", dst, elapsed, src_size,
                       extra={"mode": mode, "format": target, "target_label": tgt["label"]})


# ---- 批量 ---------------------------------------------------------------

BATCH_OP_FORMAT_KIND = {
    "convert": "any_video",
    "remux": "any_video",
    "video_only": "video",
    "extract_audio": "audio",
    "compress": None,
}


def act_batch(args):
    require_ffmpeg()
    srcs = []
    for p in (args.input or []):
        if not os.path.exists(p):
            raise Failure(f"输入路径不存在: {p}", EXIT_INPUT, error_code="input_not_found")
        srcs.append(p)
    if not srcs:
        raise Failure("--input 至少要给一个文件或目录", EXIT_ARGS,
                      error_code="missing_argument")

    exts = None
    if args.exts:
        exts = {e if e.startswith(".") else "." + e for e in
                (x.strip().lower() for x in args.exts.split(",")) if e}

    files = B.collect_files(srcs, recursive=args.recursive, exts=exts)
    if args.limit:
        files = files[:args.limit]
    if not files:
        return ok("batch", total=0, jobs=[], note="没有匹配到任何文件")

    jobs = [B.Job(f, args.op) for f in files]
    jobs, dup = B.dedupe(jobs)

    cmds = _prepare_batch_jobs(args, jobs)
    planned = [{"index": i, "input": j.src, "output": j.dst, "status": j.status,
                "message": j.message,
                "command_str": (" ".join(_quote(c) for c in cmds[id(j)])) if id(j) in cmds else None}
               for i, j in enumerate(jobs, 1)]

    if args.dry_run:
        return ok("batch", dry_run=True, op=args.op, total=len(jobs), duplicated=dup,
                  plan=planned, note="dry-run 只推导路径，不执行；去掉 --dry-run 即开始处理")

    started = time.time()
    results = []
    done = failed = skipped = 0
    for i, j in enumerate(jobs, 1):
        if j.status == "skipped":
            skipped += 1
            results.append({
                "index": i, "input": j.src, "output": j.dst, "status": "skipped",
                "message": j.message, "size_in": None, "size_out": None,
                "size_in_human": None, "size_out_human": None, "elapsed": 0,
            })
            continue
        if args.json_progress:
            sys.stderr.write(json.dumps({"progress": i, "total": len(jobs),
                                         "input": j.src, "output": j.dst},
                                        ensure_ascii=False) + "\n")
            sys.stderr.flush()
        t0 = time.time()
        j.size_in = os.path.getsize(j.src) if os.path.isfile(j.src) else 0
        try:
            run_ffmpeg(cmds[id(j)], quiet=args.quiet)
            j.status = "done"
            j.size_out = os.path.getsize(j.dst) if os.path.isfile(j.dst) else 0
            done += 1
        except Failure as e:
            j.status = "failed"
            j.message = e.message
            failed += 1
        results.append({
            "index": i, "input": j.src, "output": j.dst, "status": j.status,
            "message": j.message,
            "size_in": j.size_in, "size_out": j.size_out,
            "size_in_human": V.human_size(j.size_in) if j.size_in else None,
            "size_out_human": V.human_size(j.size_out) if j.size_out else None,
            "elapsed": round(time.time() - t0, 2),
        })

    total_in = sum(r["size_in"] or 0 for r in results)
    total_out = sum(r["size_out"] or 0 for r in results if r["status"] == "done")
    return ok("batch", op=args.op, total=len(jobs), duplicated=dup,
              done=done, failed=failed, skipped=skipped,
              size_in=total_in, size_out=total_out,
              size_in_human=V.human_size(total_in),
              size_out_human=V.human_size(total_out),
              elapsed=round(time.time() - started, 2),
              results=results)


def _prepare_batch_jobs(args, jobs):
    """为每个 job 推导输出路径并构造命令。失败的直接标 skipped。

    返回 {id(job): cmd} 映射。
    注意：batch.Job 用了 __slots__，不能往实例上挂额外属性（会 AttributeError），
    所以命令单独存在字典里。
    """
    cmds = {}
    op = args.op
    out_dir = os.path.abspath(args.out_dir) if args.out_dir else None
    if out_dir and not os.path.isdir(out_dir):
        raise Failure(f"输出目录不存在: {out_dir}", EXIT_ARGS,
                      hint="先建目录，或改 --out-dir", error_code="output_dir_missing")

    encoders, nvenc = deps.ffmpeg_encoders(), deps.nvenc_available()
    taken = set()
    template = args.template or "{name}_{op}"

    for idx, j in enumerate(jobs, 1):
        try:
            info = media_info(j.src)
            if not info:
                j.status = "skipped"
                j.message = "无法读取媒体信息"
                continue

            if op in ("convert", "remux", "video_only", "extract_audio"):
                target = args.format
                if op == "extract_audio":
                    if not info["has_audio"]:
                        j.status, j.message = "skipped", "源无音轨"
                        continue
                    target = target or "mp3"
                    if target not in V.AUDIO_TARGETS:
                        raise Failure(f"--format {target} 不是音频格式", EXIT_ARGS,
                                      error_code="bad_format")
                else:
                    target = target or "mp4"
                    table = V.VIDEO_TARGETS if op != "remux" else V.ALL_TARGETS
                    if target not in table:
                        raise Failure(f"--format {target} 不适用于 {op}", EXIT_ARGS,
                                      hint="可用: " + ", ".join(table), error_code="bad_format")
                if op == "remux":
                    warns, _ = V.check_compat(info, target, "remux")
                    if warns and not args.force:
                        j.status, j.message = "skipped", warns[0]
                        continue
                ext = V.ALL_TARGETS[target]["ext"]
                dst = B.ensure_unique(
                    B.make_output_path(j, out_dir, ext, template, idx, op), taken)
                has_audio = info["has_audio"] and V.ALL_TARGETS[target].get("default_a") != "none"
                if op == "convert":
                    cmd, err = V.build_convert_cmd(
                        FFMPEG, j.src, dst, target, vcodec=args.video_codec,
                        acodec=args.audio_codec, crf=args.crf,
                        preset=args.preset_speed, abitrate=args.audio_bitrate,
                        scale=_scale_arg(args.scale, info), fps=args.fps, has_audio=has_audio)
                elif op == "remux":
                    cmd, err = V.build_extract_cmd(FFMPEG, j.src, dst, "remux", target)
                elif op == "video_only":
                    cmd, err = V.build_extract_cmd(
                        FFMPEG, j.src, dst, "video_only", target,
                        vcodec=args.video_codec, crf=args.crf,
                        preset=args.preset_speed, has_audio=False)
                else:
                    cmd, err = V.build_extract_cmd(
                        FFMPEG, j.src, dst, "audio_from_video", target,
                        acodec=args.audio_codec, abitrate=args.audio_bitrate, has_audio=True)
                if err or not cmd:
                    j.status, j.message = "skipped", err or "无法构造命令"
                    continue

            elif op == "compress":
                if not info["has_video"]:
                    j.status, j.message = "skipped", "源无视频轨"
                    continue
                preset_sel = args.preset or "x264_crf23"
                is_lossless = preset_sel in C.LOSSLESS_PRESETS
                avail = (C.available_lossless(encoders, nvenc) if is_lossless
                         else C.available_compress(encoders, nvenc))
                if preset_sel not in avail:
                    raise Failure(f"方案 {preset_sel} 在本机不可用", EXIT_ARGS,
                                  hint="可用: " + ", ".join(avail),
                                  error_code="preset_unavailable")
                p = (C.LOSSLESS_PRESETS if is_lossless else C.COMPRESS_PRESETS)[preset_sel]
                ext = p["container"]
                dst = B.ensure_unique(
                    B.make_output_path(j, out_dir, ext, template, idx, op), taken)
                if is_lossless:
                    cmd = C.build_lossless_cmd(FFMPEG, j.src, dst, preset_sel,
                                               audio_mode=args.audio,
                                               has_audio=info["has_audio"])
                else:
                    cmd = C.build_compress_cmd(FFMPEG, j.src, dst, preset_sel,
                                               audio_bitrate=args.audio_bitrate,
                                               scale=_scale_arg(args.scale, info),
                                               preset=args.preset_speed,
                                               has_audio=info["has_audio"])
            else:
                raise Failure(f"未知批量操作: {op}", EXIT_ARGS,
                              hint="可用: " + ", ".join(BATCH_OP_FORMAT_KIND),
                              error_code="bad_op")

            j.dst = dst
            cmds[id(j)] = cmd
            j.status = "pending"
        except Failure as e:
            j.status, j.message = "skipped", e.message
    return cmds


ACTIONS = {
    "capabilities": act_capabilities,
    "probe": act_probe,
    "merge": act_merge,
    "compress": act_compress,
    "convert": act_convert,
    "extract": act_convert,
    "batch": act_batch,
}


# ============================================================ argparse

class _Parser(argparse.ArgumentParser):
    """把 argparse 的报错也变成 JSON envelope。

    默认的 argparse 在参数缺失/非法时会 print usage 到 stderr 并直接 sys.exit(2)，
    stdout 一个字节都没有 —— agent 拿到的就是空输出，无法判断出了什么事。
    这里覆写 error()，让它按统一格式吐 JSON 再以退出码 2 结束。
    """

    def error(self, message):
        payload = fail(self.prog, Failure(
            message, EXIT_ARGS,
            hint="跑 `avtool.py <action> --help` 看该动作的完整参数；"
                 "或跑 `avtool.py capabilities` 看全部可用值",
            error_code="bad_arguments"))
        _emit(payload, EXIT_ARGS)
        raise SystemExit(EXIT_ARGS)


def build_parser():
    p = _Parser(
        prog="avtool.py",
        description="音视频工具箱 CLI —— 面向 AI agent 的 JSON 接口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="先跑 `avtool.py capabilities` 查看本机可用能力与全部选项。")
    p.add_argument("--version", action="version", version=f"avtool {CLI_VERSION}")
    sub = p.add_subparsers(dest="action", metavar="<action>")

    # ---- capabilities
    sub.add_parser("capabilities", help="查看本机能力、可用方案、全部格式（agent 先调这个）")

    # ---- probe
    sp = sub.add_parser("probe", help="读取媒体文件信息")
    sp.add_argument("--input", "-i", required=True, help="媒体文件路径")
    sp.add_argument("--quiet", "-q", action="store_true", help="静音 ffmpeg 日志")

    # ---- merge
    sm = sub.add_parser("merge", help="把分离的视频流与音频流合成一个文件")
    sm.add_argument("--video", "-v", required=True, help="视频源")
    sm.add_argument("--audio", "-a", default=None, help="音频源（可省，此时保留视频自带音轨）")
    sm.add_argument("--output", "-o", default=None, help="输出路径（省则按 --out-dir 推导）")
    sm.add_argument("--out-dir", default=None, help="输出目录（与 --output 二选一）")
    sm.add_argument("--mode", choices=["copy", "transcode"], default="copy",
                    help="copy=无损封装(默认,秒级) / transcode=重编码")
    sm.add_argument("--video-codec", default="libx264", help="重编码时的视频编码器")
    sm.add_argument("--audio-codec", default="aac", help="重编码时的音频编码器")
    sm.add_argument("--crf", type=int, default=18, help="画质，越小越好（默认 18）")
    sm.add_argument("--preset", default="medium", help="x264/x265 preset（默认 medium）")
    sm.add_argument("--audio-bitrate", default="192k", help="音频码率（默认 192k）")
    sm.add_argument("--start", default=None, help="起始时间，如 90 / 1:30 / 00:01:30.5")
    sm.add_argument("--end", default=None, help="结束时间（同样格式）")
    sm.add_argument("--volume", type=float, default=1.0, help="音量倍数（仅重编码生效）")
    sm.add_argument("--audio-delay", type=float, default=0.0, help="音频整体延后秒数（仅重编码）")
    sm.add_argument("--no-shortest", dest="shortest", action="store_false", default=True,
                    help="不加 -shortest（默认会以最短流为准截断）")
    sm.add_argument("--overwrite", action="store_true", help="允许覆盖已有输出")
    sm.add_argument("--dry-run", action="store_true", help="只回命令不执行")
    sm.add_argument("--force", action="store_true", help="忽略 copy 模式的参数冲突警告")
    sm.add_argument("--quiet", "-q", action="store_true", help="静音 ffmpeg 日志")

    _add_common_encode_args(sm)

    # ---- compress
    sc = sub.add_parser("compress", help="压缩视频（数学无损 或 高压缩比）")
    sc.add_argument("--input", "-i", required=True, help="源视频")
    sc.add_argument("--preset", "-p", required=True,
                    help="方案 key；跑 capabilities 看列表，如 x264_crf23")
    sc.add_argument("--output", "-o", default=None, help="输出路径")
    sc.add_argument("--out-dir", default=None, help="输出目录")
    sc.add_argument("--start", default=None, help="起始时间")
    sc.add_argument("--end", default=None, help="结束时间")
    sc.add_argument("--quiet", "-q", action="store_true")
    sm2 = sc.add_mutually_exclusive_group()
    sm2.add_argument("--overwrite", action="store_true", help="允许覆盖")
    sc.add_argument("--dry-run", action="store_true", help="只回命令不执行")
    _add_common_encode_args(sc)

    # ---- convert / extract
    for name, helptext in (("convert", "格式转换 / 仅换容器"),):
        sx = sub.add_parser(name, help=helptext)
        sx.add_argument("--input", "-i", required=True, help="源文件")
        sx.add_argument("--format", "-f", default=None,
                        help="目标格式 key，如 mp4/mkv/webm/mp3/flac/mka")
        sx.add_argument("--mode", choices=["convert", "remux"], default="convert",
                        help="convert=重编码(默认) / remux=仅换容器")
        sx.add_argument("--output", "-o", default=None, help="输出路径")
        sx.add_argument("--out-dir", default=None, help="输出目录")
        sx.add_argument("--overwrite", action="store_true", help="允许覆盖")
        sx.add_argument("--dry-run", action="store_true", help="只回命令不执行")
        sx.add_argument("--force", action="store_true",
                        help="remux 遇到容器不兼容时仍强行尝试")
        sx.add_argument("--quiet", "-q", action="store_true")
        _add_common_encode_args(sx)

    se = sub.add_parser("extract", help="轨道提取：提音频 / 去音频 / 仅换容器")
    se.add_argument("--input", "-i", required=True, help="源文件")
    se.add_argument("--mode", required=True,
                    choices=["audio_from_video", "video_only", "remux", "audio_only"],
                    help="audio_from_video=提音频 / video_only=去音频 / remux=仅换容器")
    se.add_argument("--format", "-f", default=None, help="目标格式 key")
    se.add_argument("--output", "-o", default=None, help="输出路径")
    se.add_argument("--out-dir", default=None, help="输出目录")
    se.add_argument("--overwrite", action="store_true", help="允许覆盖")
    se.add_argument("--dry-run", action="store_true", help="只回命令不执行")
    se.add_argument("--force", action="store_true")
    se.add_argument("--quiet", "-q", action="store_true")
    _add_common_encode_args(se)

    # ---- batch
    sb = sub.add_parser("batch", help="批量处理文件或整个目录")
    sb.add_argument("--input", "-i", required=True, nargs="+",
                    help="一个或多个文件/目录")
    sb.add_argument("--op", required=True,
                    choices=list(BATCH_OP_FORMAT_KIND),
                    help="批量操作类型")
    sb.add_argument("--format", "-f", default=None, help="目标格式 key（compress 以外必填）")
    sb.add_argument("--preset", "-p", default=None, help="压缩方案 key（op=compress 必填）")
    sb.add_argument("--out-dir", default=None, help="输出目录（省则与源同目录）")
    sb.add_argument("--template", default="{name}_{op}",
                    help="命名模板，占位符 {name} {ext} {index} {date} {op}")
    sb.add_argument("--recursive", "-r", action="store_true", help="递归子目录")
    sb.add_argument("--exts", default=None, help="只处理这些扩展名，逗号分隔，如 mp4,mov")
    sb.add_argument("--limit", type=int, default=0, help="最多处理 N 个（0=不限）")
    sb.add_argument("--dry-run", action="store_true", help="只推导路径不执行")
    sb.add_argument("--force", action="store_true", help="忽略 remux 兼容性警告")
    sb.add_argument("--json-progress", action="store_true",
                    help="逐条进度以 JSON 行写 stderr，便于 agent 跟踪")
    sb.add_argument("--overwrite", action="store_true", help="允许覆盖（默认自动加序号避让）")
    sb.add_argument("--quiet", "-q", action="store_true")
    _add_common_encode_args(sb)

    return p


def _add_common_encode_args(sp):
    """给子命令补上编码/画质/音频相关的通用参数（重复调用安全）。"""
    existing = {a.dest for a in sp._actions}
    if "video_codec" not in existing:
        sp.add_argument("--video-codec", default=None, help="视频编码器（省则用目标默认）")
    if "audio_codec" not in existing:
        sp.add_argument("--audio-codec", default=None, help="音频编码器（省则用目标默认）")
    if "crf" not in existing:
        sp.add_argument("--crf", type=int, default=23, help="画质，越小越好（默认 23）")
    if "preset_speed" not in existing:
        sp.add_argument("--preset-speed", default="medium",
                        help="编码速度 preset：ultrafast..veryslow，或 NVENC 的 p1..p7")
    if "audio_bitrate" not in existing:
        sp.add_argument("--audio-bitrate", default="192k", help="音频码率（默认 192k）")
    if "scale" not in existing:
        sp.add_argument("--scale", default=None,
                        help="缩放，支持 1080p/720p/480p/360p 或 1280x720")
    if "fps" not in existing:
        sp.add_argument("--fps", type=float, default=None, help="目标帧率")
    if "audio" not in existing:
        sp.add_argument("--audio", default="lossless",
                        choices=["lossless", "copy", "aac", "none"],
                        help="压缩时音频处理：lossless(默认,flac)/copy/aac/none")


def normalize_args(args):
    """--video-codec 在部分子命令里是 --video-codec，另一些被 _add_common 动态补上。
    统一补齐缺失属性，避免 handler 里 hasattr 满天飞。"""
    defaults = {
        "input": None, "output": None, "out_dir": None, "format": None,
        "preset": None, "video_codec": None, "audio_codec": None,
        "crf": 23, "preset_speed": "medium", "audio_bitrate": "192k",
        "scale": None, "fps": None, "audio": "lossless",
        "recursive": False, "exts": None, "limit": 0, "template": "{name}_{op}",
        "json_progress": False, "overwrite": False, "dry_run": False,
        "force": False, "quiet": False, "start": None, "end": None,
        "mode": "convert", "op": None,
    }
    for k, v in defaults.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    return args


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # 无参数时给出 capabilities，方便 agent 直接探路
    if not argv:
        argv = ["capabilities"]

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # error() 已经吐过 JSON 了，这里只透传退出码
        return e.code if isinstance(e.code, int) else EXIT_ARGS

    action = args.action
    if not action:
        args = parser.parse_args(["capabilities"])
        action = "capabilities"

    handler = ACTIONS.get(action)
    if not handler:
        payload = fail(action or "unknown", Failure(
            f"未知动作: {action}", EXIT_ARGS, error_code="bad_action"))
        return _emit(payload, EXIT_ARGS)

    try:
        result = handler(normalize_args(args))
        return _emit(result, EXIT_OK)
    except Failure as e:
        return _emit(fail(action, e), e.exit_code)
    except KeyboardInterrupt:
        return _emit(fail(action, Failure("被用户中断", EXIT_RUN, error_code="interrupted")), EXIT_RUN)
    except Exception as e:                      # 兜底：绝不把 traceback 混进 stdout
        import traceback
        traceback.print_exc(file=sys.stderr)
        return _emit(fail(action, Failure(f"内部错误: {e}", EXIT_RUN,
                                          error_code="internal_error")), EXIT_RUN)


if __name__ == "__main__":
    sys.exit(main())
