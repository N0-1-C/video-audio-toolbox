# -*- coding: utf-8 -*-
"""
音视频工具箱 (AV Toolbox)
  1. 合并   —— 把分离的视频流 + 音频流合成一个文件
  2. 无损压缩 —— 数学无损重封装，或高压缩比瘦身

依赖全部自带：
  - bin/ffmpeg.exe, bin/ffprobe.exe   （随项目分发，无需装 PATH）
  - vendor/*.py                       （纯 Python 第三方包，免 pip）
  仅需系统装有 Python 3.8+（含 tkinter）。

运行: python merge_av.py
自检: python deps.py
"""

import json
import os
import re
import subprocess
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import deps
import compressor as C
import converter as V
import batch as B

APP_TITLE = "音视频工具箱"
APP_VERSION = "3.1"
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".m4v", ".wmv", ".mpg", ".mpeg", ".3gp", ".rmvb", ".vob")
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".ac3", ".dts", ".ape", ".amr")

CREATE_NO_WINDOW = deps.CREATE_NO_WINDOW
FFMPEG = deps.FFMPEG
FFPROBE = deps.FFPROBE

# 能力缓存在首次使用时填充，避免启动时卡在 NVENC 探测
_CAPS = {"encoders": None, "nvenc": None}


def capabilities():
    """惰性获取 ffmpeg 能力（编码器集合 + NVENC 是否可用）。"""
    if _CAPS["encoders"] is None:
        _CAPS["encoders"] = deps.ffmpeg_encoders()
        _CAPS["nvenc"] = deps.nvenc_available()
    return _CAPS["encoders"], _CAPS["nvenc"]


def run(cmd, on_line=None):
    """执行命令，逐行回调输出。返回 (returncode, 全部输出)。"""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    lines = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        if on_line:
            on_line(line)
    proc.wait()
    return proc.returncode, "\n".join(lines)


def probe(path):
    """读取媒体文件信息。返回 dict，失败返回 None。"""
    if not FFPROBE:
        return None
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        rc, out = run(cmd)
    except Exception:
        return None
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def summarize(info):
    """把 ffprobe 结果压成简短描述。"""
    if not info:
        return None
    dur = 0.0
    try:
        dur = float(info.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        dur = 0.0
    v = a = 0
    vdesc, adesc = [], []
    for s in info.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video":
            v += 1
            if not s.get("disposition", {}).get("attached_pic"):
                wh = ""
                if s.get("width") and s.get("height"):
                    wh = f'{s["width"]}x{s["height"]}'
                vdesc.append(" ".join(x for x in (wh, s.get("codec_name", "").upper()) if x))
        elif kind == "audio":
            a += 1
            ch = s.get("channels")
            chs = {1: "单声道", 2: "立体声"}.get(ch, f"{ch}声道" if ch else "")
            adesc.append(" ".join(x for x in (s.get("codec_name", "").upper(), chs) if x))
    m, s = divmod(int(dur), 60)
    h, m = divmod(m, 60)
    dur_s = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return {
        "duration": dur,
        "duration_str": dur_s,
        "vcount": v,
        "acount": a,
        "vdesc": " / ".join(vdesc) or "无",
        "adesc": " / ".join(adesc) or "无",
    }


# ---------------------------------------------------------------- 时间轴解析

def parse_ts(text):
    """支持 12 / 1:30 / 01:02:03.5 / 00:00:05.250，返回秒(float)。空则 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"[\d:.]+", text):
        raise ValueError(f"时间格式无法识别: {text}")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"时间格式无法识别: {text}")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"时间格式无法识别: {text}")
    sec = 0.0
    for v in vals:
        sec = sec * 60 + v
    return sec


def fmt_ts(sec):
    """秒 -> HH:MM:SS.mmm"""
    if sec is None:
        return "—"
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_dur(sec):
    if sec is None:
        return "未知"
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m}m{s}s" if h else f"{m}m{s}s"


VIDEO_CONTAINERS = {"mp4": ["-movflags", "+faststart"], "mkv": [], "mov": ["-movflags", "+faststart"],
                    "avi": [], "flv": [], "webm": [], "ts": []}
COPYABLE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "flac", "opus", "vorbis"}
COPYABLE_VIDEO = {"h264", "hevc", "mpeg4", "vp8", "vp9", "av1", "mpeg2video", "theora"}


def build_cmd(video, audio, output, mode, vcodec, acodec, crf, preset, audio_bitrate,
              start, end, volume, shortest, audio_delay):
    """
    mode: 'copy'(无损封装) | 'transcode'(重编码)
    audio_delay: 音频整体延后秒数（用 adelay 实现，仅重编码模式生效）
    返回 (cmd_list, 说明文本)
    """
    cmd = [FFMPEG, "-hide_banner", "-y"]

    # 输入 0: 视频源。用 -ss 放在 -i 前做快速定位（关键帧对齐，copy 模式下避免重编码）
    if start is not None and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", video]

    has_audio = bool(audio and os.path.isfile(audio))
    audio_idx = 1 if has_audio else 0
    if has_audio:
        cmd += ["-i", audio]

    # ---- 流选择：视频取输入0的最佳视频流；音频优先取输入1，没有则退回输入0自带音频
    cmd += ["-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", f"{audio_idx}:a:0"]
    elif mode != "copy":
        cmd += ["-map", "0:a?"]

    # ---- 编码参数
    if mode == "copy":
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", vcodec]
        if vcodec in ("libx264", "libx265"):
            cmd += ["-preset", preset, "-crf", str(crf)]
        elif vcodec == "libvpx-vp9":
            cmd += ["-crf", str(crf), "-b:v", "0"]
        cmd += ["-pix_fmt", "yuv420p"]
        if has_audio:
            cmd += ["-c:a", acodec]
            if acodec in ("aac", "libmp3lame", "libopus"):
                cmd += ["-b:a", audio_bitrate]

    # ---- 滤镜只在重编码时可用；-c copy 与 -vf/-af 互斥（ffmpeg 会直接报错）
    if mode != "copy":
        vf = []
        if end is not None:
            # 这里 -ss 已在输入端生效，故 trim 的 end 使用相对于起点的时长
            vf.append(f"trim=end={max(0.0, end - (start or 0.0)):.3f}")
        vf += ["setpts=PTS-STARTPTS"]
        cmd += ["-vf", ",".join(vf)]

        af = []
        if abs(audio_delay) > 1e-6:
            ms = int(round(audio_delay * 1000))
            af.append(f"adelay={ms}|{ms}")
        if abs(volume - 1.0) > 1e-6:
            af.append(f"volume={volume}")
        af.append(f"atrim=end={max(0.0, end - (start or 0.0)):.3f},asetpts=PTS-STARTPTS"
                  if end is not None else "asetpts=PTS-STARTPTS")
        cmd += ["-af", ",".join(af) if af else "anull"]
    else:
        # copy 模式下音量/延迟/终点裁剪无法实现，上层已提示用户
        pass

    if shortest:
        cmd += ["-shortest"]

    ext = os.path.splitext(output)[1].lstrip(".").lower()
    cmd += VIDEO_CONTAINERS.get(ext, [])
    cmd += [output]

    if mode == "copy":
        desc = "无损封装 (-c copy)"
    else:
        bits = [f"v:{vcodec}"]
        if vcodec in ("libx264", "libx265"):
            bits.append(f"crf{crf}")
            bits.append(preset)
        bits.append(f"a:{acodec}")
        desc = "重编码 " + " ".join(bits)
    return cmd, desc


# ---------------------------------------------------------------- GUI

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_TITLE} v{APP_VERSION}")
        root.geometry("900x800")
        root.minsize(820, 700)

        # ---- 合并标签页状态
        self.video_path = tk.StringVar()
        self.audio_path = tk.StringVar()
        self.out_path = tk.StringVar()
        self.mode = tk.StringVar(value="copy")
        self.vcodec = tk.StringVar(value="libx264")
        self.acodec = tk.StringVar(value="aac")
        self.crf = tk.IntVar(value=18)
        self.preset = tk.StringVar(value="medium")
        self.audio_bitrate = tk.StringVar(value="192k")
        self.shortest = tk.BooleanVar(value=True)
        self.start_time = tk.StringVar()
        self.end_time = tk.StringVar()
        self.a_offset = tk.StringVar()
        self.volume = tk.DoubleVar(value=1.0)
        self.status = tk.StringVar(value="就绪")
        self.v_info = tk.StringVar(value="未选择视频")
        self.a_info = tk.StringVar(value="未选择音频")

        # ---- 压缩标签页状态
        self.c_in = tk.StringVar()
        self.c_out = tk.StringVar()
        self.c_kind = tk.StringVar(value="lossless")   # lossless | compress
        self.c_lossless = tk.StringVar()
        self.c_compress = tk.StringVar()
        self.c_audio = tk.StringVar(value="lossless")
        self.c_scale = tk.StringVar(value="保持原分辨率")
        self.c_preset = tk.StringVar(value="medium")
        self.c_info = tk.StringVar(value="未选择文件")
        self.c_before = None   # (size, duration, desc)
        self._c_has_audio = True

        self.proc = None
        self.stop_flag = False
        self.msg_q = queue.Queue()
        self._busy = False

        # ---- 转换标签页状态
        self.x_in = tk.StringVar()
        self.x_out = tk.StringVar()
        self.x_mode = tk.StringVar(value="convert")
        self.x_target = tk.StringVar()
        self.x_vcodec = tk.StringVar()
        self.x_acodec = tk.StringVar()
        self.x_crf = tk.IntVar(value=23)
        self.x_preset = tk.StringVar(value="medium")
        self.x_abitrate = tk.StringVar(value="192k")
        self.x_scale = tk.StringVar(value="保持原分辨率")
        self.x_fps = tk.StringVar(value="保持原帧率")
        self.x_info = tk.StringVar(value="未选择文件")
        self._x_has_audio = True
        self._x_src_info = None

        # ---- 批量标签页状态
        self.b_jobs = []                    # list[batch.Job]
        self.b_op = tk.StringVar(value="convert")
        self.b_target = tk.StringVar()
        self.b_scale = tk.StringVar(value="保持原分辨率")
        self.b_crf = tk.IntVar(value=23)
        self.b_preset = tk.StringVar(value="medium")
        self.b_kind = tk.StringVar(value="compress")     # 批量压缩：lossless|compress
        self.b_lossless = tk.StringVar()
        self.b_compress = tk.StringVar()
        self.b_outdir = tk.StringVar()
        self.b_template = tk.StringVar(value="{name}_{op}")
        self.b_summary = tk.StringVar(value="尚无任务")
        self.b_recursive = tk.BooleanVar(value=True)
        self._b_taken = set()
        self._b_running = False

        self._build_ui()
        self._poll_queue()
        self._check_env()
        self._refresh_compress_presets()
        self._refresh_convert_options()

    # ---------------- UI 搭建
    def _build_ui(self):
        root = self.root
        root.columnconfigure(0, weight=1)

        self.nb = ttk.Notebook(root)
        self.nb.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 4))
        root.rowconfigure(0, weight=1)

        self.tab_merge = ttk.Frame(self.nb)
        self.tab_comp = ttk.Frame(self.nb)
        self.tab_conv = ttk.Frame(self.nb)
        self.tab_batch = ttk.Frame(self.nb)
        self.nb.add(self.tab_merge, text="  合并音视频  ")
        self.nb.add(self.tab_comp, text="  无损压缩  ")
        self.nb.add(self.tab_conv, text="  格式转换  ")
        self.nb.add(self.tab_batch, text="  批量处理  ")

        self._build_merge_tab(self.tab_merge)
        self._build_compress_tab(self.tab_comp)
        self._build_convert_tab(self.tab_conv)
        self._build_batch_tab(self.tab_batch)

        # ---- 共享的进度 / 日志区
        bottom = ttk.Frame(root)
        bottom.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        bottom.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=0)

        act = ttk.Frame(bottom)
        act.grid(row=0, column=0, sticky="ew")
        act.columnconfigure(0, weight=1)
        self.pb = ttk.Progressbar(act, mode="determinate", maximum=100)
        self.pb.grid(row=0, column=0, sticky="ew")
        self.btn_run = ttk.Button(act, text="开始", command=self.dispatch_run, width=12)
        self.btn_run.grid(row=0, column=1, padx=6)
        self.btn_stop = ttk.Button(act, text="停止", command=self.stop, width=8, state="disabled")
        self.btn_stop.grid(row=0, column=2)

        self.lbl_status = ttk.Label(bottom, textvariable=self.status, font=("Consolas", 9))
        self.lbl_status.grid(row=1, column=0, sticky="w", pady=(4, 0))

        logf = ttk.LabelFrame(bottom, text=" 日志 ")
        logf.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        bottom.rowconfigure(2, weight=1)
        self.log = tk.Text(logf, height=11, wrap="none", bg="#1e1e1e", fg="#d4d4d4",
                           insertbackground="#d4d4d4", font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=6)
        self.log.configure(yscrollcommand=sb.set, state="disabled")

        self.nb.bind("<<NotebookTabChanged>>", lambda _: self._sync_run_button())

    # ---------------- 合并页
    def _build_merge_tab(self, root):
        pad = {"padx": 8, "pady": 4}
        root.columnconfigure(0, weight=1)

        files = ttk.LabelFrame(root, text=" 输入 / 输出 ")
        files.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        files.columnconfigure(1, weight=1)

        ttk.Label(files, text="视频源").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.video_path).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(files, text="浏览…", width=8, command=self.pick_video).grid(row=0, column=2, **pad)
        ttk.Label(files, textvariable=self.v_info, foreground="#7a7a7a").grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(files, text="音频源").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.audio_path).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(files, text="浏览…", width=8, command=self.pick_audio).grid(row=2, column=2, **pad)
        ttk.Label(files, textvariable=self.a_info, foreground="#7a7a7a").grid(row=3, column=1, sticky="w", padx=8)

        ttk.Label(files, text="输出文件").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.out_path).grid(row=4, column=1, sticky="ew", **pad)
        ttk.Button(files, text="另存为…", width=8, command=self.pick_output).grid(row=4, column=2, **pad)

        btns = ttk.Frame(files)
        btns.grid(row=5, column=1, sticky="w", padx=8, pady=(0, 4))
        ttk.Button(btns, text="自动识别同目录配对", command=self.auto_pair).pack(side="left")
        ttk.Button(btns, text="清空", command=self.clear_all).pack(side="left", padx=6)

        opts = ttk.LabelFrame(root, text=" 合并参数 ")
        opts.grid(row=1, column=0, sticky="ew", padx=10, pady=6)
        opts.columnconfigure(3, weight=1)

        ttk.Label(opts, text="模式").grid(row=0, column=0, sticky="w", **pad)
        mf = ttk.Frame(opts)
        mf.grid(row=0, column=1, columnspan=3, sticky="w", **pad)
        ttk.Radiobutton(mf, text="无损封装（不重编码，快，推荐）", value="copy",
                        variable=self.mode, command=self._on_mode).pack(side="left")
        ttk.Radiobutton(mf, text="重编码", value="transcode",
                        variable=self.mode, command=self._on_mode).pack(side="left", padx=14)

        ttk.Label(opts, text="视频编码").grid(row=1, column=0, sticky="w", **pad)
        self.cb_vcodec = ttk.Combobox(opts, textvariable=self.vcodec, width=14, state="readonly",
                                      values=["libx264", "libx265", "libvpx-vp9", "mpeg4"])
        self.cb_vcodec.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(opts, text="CRF").grid(row=1, column=2, sticky="e", **pad)
        self.sp_crf = ttk.Spinbox(opts, from_=0, to=51, textvariable=self.crf, width=6)
        self.sp_crf.grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(opts, text="音频编码").grid(row=2, column=0, sticky="w", **pad)
        self.cb_acodec = ttk.Combobox(opts, textvariable=self.acodec, width=14, state="readonly",
                                      values=["aac", "libmp3lame", "libopus", "flac", "copy"])
        self.cb_acodec.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(opts, text="码率").grid(row=2, column=2, sticky="e", **pad)
        self.cb_abr = ttk.Combobox(opts, textvariable=self.audio_bitrate, width=6, state="readonly",
                                   values=["96k", "128k", "192k", "256k", "320k"])
        self.cb_abr.grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(opts, text="preset").grid(row=3, column=0, sticky="w", **pad)
        self.cb_preset = ttk.Combobox(opts, textvariable=self.preset, width=14, state="readonly",
                                      values=["ultrafast", "veryfast", "fast", "medium", "slow", "veryslow"])
        self.cb_preset.grid(row=3, column=1, sticky="w", **pad)
        ttk.Checkbutton(opts, text="以较短流为准 (-shortest)", variable=self.shortest).grid(
            row=3, column=2, columnspan=2, sticky="w", **pad)

        ttk.Label(opts, text="音频延迟").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(opts, textvariable=self.a_offset, width=10).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(opts, text="秒（音频整体延后为正，用于对口型；仅重编码模式生效）",
                  foreground="#7a7a7a").grid(row=4, column=2, columnspan=2, sticky="w", padx=8)

        ttk.Label(opts, text="音量").grid(row=5, column=0, sticky="w", **pad)
        vol_f = ttk.Frame(opts)
        vol_f.grid(row=5, column=1, columnspan=3, sticky="ew", **pad)
        vol_f.columnconfigure(0, weight=1)
        self.scale_vol = ttk.Scale(vol_f, from_=0.0, to=3.0, variable=self.volume, orient="horizontal",
                                   command=lambda _: self.lbl_vol.config(text=f"{self.volume.get():.2f}x"))
        self.scale_vol.grid(row=0, column=0, sticky="ew")
        self.lbl_vol = ttk.Label(vol_f, text="1.00x", width=7)
        self.lbl_vol.grid(row=0, column=1, padx=6)
        ttk.Button(vol_f, text="重置", width=6, command=self.reset_vol).grid(row=0, column=2)

        clip = ttk.LabelFrame(root, text=" 剪辑（可选，留空为完整合并） ")
        clip.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        ttk.Label(clip, text="起点").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(clip, textvariable=self.start_time, width=14).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(clip, text="终点").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(clip, textvariable=self.end_time, width=14).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(clip, text="格式: 12 或 1:30 或 00:01:02.5", foreground="#7a7a7a").grid(
            row=0, column=4, sticky="w", padx=8)
        ttk.Button(clip, text="从视频信息填入终点", command=self.fill_end_from_video).grid(
            row=0, column=5, sticky="w", **pad)

        self._on_mode()

    # ---------------- 压缩页
    def _build_compress_tab(self, root):
        pad = {"padx": 8, "pady": 4}
        root.columnconfigure(0, weight=1)

        files = ttk.LabelFrame(root, text=" 输入 / 输出 ")
        files.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        files.columnconfigure(1, weight=1)

        ttk.Label(files, text="源文件").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.c_in).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(files, text="浏览…", width=8, command=self.pick_c_input).grid(row=0, column=2, **pad)
        ttk.Label(files, textvariable=self.c_info, foreground="#7a7a7a",
                  justify="left").grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(files, text="输出文件").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.c_out).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(files, text="另存为…", width=8, command=self.pick_c_output).grid(row=2, column=2, **pad)

        btns = ttk.Frame(files)
        btns.grid(row=3, column=1, sticky="w", padx=8, pady=(0, 4))
        ttk.Button(btns, text="清空", command=self.clear_c).pack(side="left")

        # --- 类别选择
        kind = ttk.LabelFrame(root, text=" 压缩方式 ")
        kind.grid(row=1, column=0, sticky="ew", padx=10, pady=6)
        kind.columnconfigure(1, weight=1)
        kf = ttk.Frame(kind)
        kf.grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        ttk.Radiobutton(kf, text="数学无损（逐像素完全一致）", value="lossless",
                        variable=self.c_kind, command=self._on_c_kind).pack(side="left")
        ttk.Radiobutton(kf, text="高压缩比（视觉无损，体积大幅变小）", value="compress",
                        variable=self.c_kind, command=self._on_c_kind).pack(side="left", padx=18)

        # --- 无损方案
        self.frm_lossless = ttk.LabelFrame(root, text=" 无损方案 ")
        self.frm_lossless.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        self.frm_lossless.columnconfigure(1, weight=1)
        ttk.Label(self.frm_lossless, text="方案").grid(row=0, column=0, sticky="w", **pad)
        self.cb_lossless = ttk.Combobox(self.frm_lossless, textvariable=self.c_lossless,
                                        state="readonly", width=34)
        self.cb_lossless.grid(row=0, column=1, sticky="w", **pad)
        self.cb_lossless.bind("<<ComboboxSelected>>", lambda _: self._update_c_hint())
        self.lbl_lossless_note = ttk.Label(self.frm_lossless, text="", foreground="#7a7a7a")
        self.lbl_lossless_note.grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(self.frm_lossless, text="音频").grid(row=2, column=0, sticky="w", **pad)
        af = ttk.Frame(self.frm_lossless)
        af.grid(row=2, column=1, sticky="w", **pad)
        for txt, val in (("FLAC 无损", "lossless"), ("原样拷贝", "copy"), ("AAC 128k（有损·更小）", "aac")):
            ttk.Radiobutton(af, text=txt, value=val, variable=self.c_audio).pack(side="left", padx=(0, 12))

        ttk.Label(self.frm_lossless, text="", foreground="#7a7a7a").grid(row=3, column=0)
        ttk.Label(self.frm_lossless,
                  text="提示：源文件本身已是有损压缩时，无损重封装通常不会变小，\n"
                       "      只能避免二次损失。要显著瘦身请用「高压缩比」。",
                  foreground="#8a8a8a", justify="left").grid(row=3, column=1, sticky="w", padx=8)

        # --- 高压缩方案
        self.frm_compress = ttk.LabelFrame(root, text=" 高压缩比方案 ")
        self.frm_compress.grid(row=3, column=0, sticky="ew", padx=10, pady=6)
        self.frm_compress.columnconfigure(1, weight=1)
        ttk.Label(self.frm_compress, text="方案").grid(row=0, column=0, sticky="w", **pad)
        self.cb_compress = ttk.Combobox(self.frm_compress, textvariable=self.c_compress,
                                        state="readonly", width=34)
        self.cb_compress.grid(row=0, column=1, sticky="w", **pad)
        self.cb_compress.bind("<<ComboboxSelected>>", lambda _: self._update_c_hint())
        self.lbl_compress_note = ttk.Label(self.frm_compress, text="", foreground="#7a7a7a")
        self.lbl_compress_note.grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(self.frm_compress, text="分辨率").grid(row=2, column=0, sticky="w", **pad)
        self.cb_scale = ttk.Combobox(self.frm_compress, textvariable=self.c_scale,
                                     state="readonly", width=22,
                                     values=["保持原分辨率", "1080p (1920x1080)", "720p (1280x720)",
                                             "480p (854x480)", "360p (640x360)"])
        self.cb_scale.grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(self.frm_compress, text="速度 preset").grid(row=3, column=0, sticky="w", **pad)
        self.cb_cpreset = ttk.Combobox(self.frm_compress, textvariable=self.c_preset,
                                       state="readonly", width=22,
                                       values=["ultrafast", "veryfast", "fast", "medium", "slow"])
        self.cb_cpreset.grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(self.frm_compress, text="", foreground="#7a7a7a").grid(row=4, column=0)
        ttk.Label(self.frm_compress,
                  text="说明：高压缩比是「视觉无损」（CRF/CQ 恒定质量），画质有极小损失、\n"
                       "      不能保证逐像素一致；适合网盘/分享/存档瘦身。",
                  foreground="#8a8a8a", justify="left").grid(row=4, column=1, sticky="w", padx=8)

    # ---------------- 转换页
    def _build_convert_tab(self, root):
        pad = {"padx": 8, "pady": 4}
        root.columnconfigure(0, weight=1)

        files = ttk.LabelFrame(root, text=" 输入 / 输出 ")
        files.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        files.columnconfigure(1, weight=1)

        ttk.Label(files, text="源文件").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.x_in).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(files, text="浏览…", width=8, command=self.pick_x_input).grid(row=0, column=2, **pad)
        ttk.Label(files, textvariable=self.x_info, foreground="#7a7a7a",
                  justify="left").grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(files, text="输出文件").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.x_out).grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(files, text="另存为…", width=8, command=self.pick_x_output).grid(row=2, column=2, **pad)
        bf = ttk.Frame(files)
        bf.grid(row=3, column=1, sticky="w", padx=8, pady=(0, 4))
        ttk.Button(bf, text="清空", command=self.clear_x).pack(side="left")

        # --- 模式
        mode = ttk.LabelFrame(root, text=" 操作类型 ")
        mode.grid(row=1, column=0, sticky="ew", padx=10, pady=6)
        mode.columnconfigure(1, weight=1)
        mf = ttk.Frame(mode)
        mf.grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        for key, spec in V.EXTRACT_MODES.items():
            ttk.Radiobutton(mf, text=spec["label"], value=key,
                            variable=self.x_mode, command=self._on_x_mode).pack(side="left", padx=(0, 14))
        self.lbl_x_mode = ttk.Label(mode, text="", foreground="#7a7a7a")
        self.lbl_x_mode.grid(row=1, column=1, sticky="w", padx=8)

        # --- 目标格式
        fmt = ttk.LabelFrame(root, text=" 目标格式 ")
        fmt.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        fmt.columnconfigure(1, weight=1)
        ttk.Label(fmt, text="格式").grid(row=0, column=0, sticky="w", **pad)
        self.cb_x_target = ttk.Combobox(fmt, textvariable=self.x_target, state="readonly", width=44)
        self.cb_x_target.grid(row=0, column=1, sticky="w", **pad)
        self.cb_x_target.bind("<<ComboboxSelected>>", lambda _: self._on_x_target())
        self.lbl_x_note = ttk.Label(fmt, text="", foreground="#7a7a7a")
        self.lbl_x_note.grid(row=1, column=1, sticky="w", padx=8)

        # --- 编码参数
        enc = ttk.LabelFrame(root, text=" 编码参数 ")
        enc.grid(row=3, column=0, sticky="ew", padx=10, pady=6)
        enc.columnconfigure(1, weight=1)

        ttk.Label(enc, text="视频编码").grid(row=0, column=0, sticky="w", **pad)
        self.cb_x_vcodec = ttk.Combobox(enc, textvariable=self.x_vcodec, state="readonly", width=26)
        self.cb_x_vcodec.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(enc, text="质量 CRF/CQ (越小越好)").grid(row=0, column=2, sticky="e", **pad)
        self.sp_x_crf = ttk.Spinbox(enc, from_=0, to=51, textvariable=self.x_crf, width=6)
        self.sp_x_crf.grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(enc, text="音频编码").grid(row=1, column=0, sticky="w", **pad)
        self.cb_x_acodec = ttk.Combobox(enc, textvariable=self.x_acodec, state="readonly", width=26)
        self.cb_x_acodec.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(enc, text="码率").grid(row=1, column=2, sticky="e", **pad)
        self.cb_x_abr = ttk.Combobox(enc, textvariable=self.x_abitrate, state="readonly", width=8,
                                     values=["96k", "128k", "192k", "256k", "320k"])
        self.cb_x_abr.grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(enc, text="分辨率").grid(row=2, column=0, sticky="w", **pad)
        self.cb_x_scale = ttk.Combobox(enc, textvariable=self.x_scale, state="readonly", width=26,
                                       values=["保持原分辨率", "2160p (3840x2160)", "1080p (1920x1080)",
                                               "720p (1280x720)", "480p (854x480)", "360p (640x360)"])
        self.cb_x_scale.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(enc, text="帧率").grid(row=2, column=2, sticky="e", **pad)
        self.cb_x_fps = ttk.Combobox(enc, textvariable=self.x_fps, state="readonly", width=8,
                                     values=["保持原帧率", "60", "30", "25", "24", "15", "10"])
        self.cb_x_fps.grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(enc, text="preset").grid(row=3, column=0, sticky="w", **pad)
        self.cb_x_preset = ttk.Combobox(enc, textvariable=self.x_preset, state="readonly", width=26,
                                        values=["ultrafast", "veryfast", "fast", "medium", "slow", "veryslow"])
        self.cb_x_preset.grid(row=3, column=1, sticky="w", **pad)

        self.lbl_x_warn = ttk.Label(root, text="", foreground="#d9a441", justify="left")
        self.lbl_x_warn.grid(row=4, column=0, sticky="w", padx=18)

    # ---------------- 转换页逻辑
    def _on_x_mode(self):
        """按操作类型决定哪些控件可用。"""
        mode = self.x_mode.get()
        spec = V.EXTRACT_MODES.get(mode, {})
        self.lbl_x_mode.configure(text=spec.get("desc", ""))
        audio_out = mode in ("audio_from_video",)
        # 「仅换容器」不需要编码参数
        copy_only = mode == "remux"
        if copy_only:
            for w in (self.cb_x_vcodec, self.cb_x_acodec, self.sp_x_crf,
                      self.cb_x_preset, self.cb_x_scale, self.cb_x_fps):
                w.configure(state="disabled")
            self.cb_x_abr.configure(state="disabled")
        else:
            self.sp_x_crf.configure(state="normal")
            self.cb_x_preset.configure(state="readonly")
            self.cb_x_scale.configure(state="readonly")
            self.cb_x_fps.configure(state="readonly")
            self.cb_x_vcodec.configure(state="disabled" if audio_out else "readonly")
            self.cb_x_acodec.configure(state="readonly")
            self.cb_x_abr.configure(state="readonly")
        self._refresh_convert_options()
        self._sync_run_button()

    def _on_x_target(self):
        self._apply_target_codecs()
        self._update_x_compat()

    def _apply_target_codecs(self):
        """按目标格式刷新可选编解码器，并选合理默认值。"""
        key = self._selected_x_target()
        tgt = V.ALL_TARGETS.get(key)
        if not tgt:
            return
        vlist = tgt.get("v")
        alist = tgt.get("a")
        if vlist:
            labels = [V.codec_label(c) for c in vlist]
            self.cb_x_vcodec.configure(values=labels)
            cur_v = self._x_key_from_label(self.x_vcodec.get(), vlist)
            default = tgt.get("default_v", vlist[0])
            self.x_vcodec.set(V.codec_label(cur_v if cur_v in vlist else default))
        else:
            self.cb_x_vcodec.configure(values=[V.codec_label("none")])
            self.x_vcodec.set(V.codec_label("none"))

        if alist:
            labels = [V.codec_label(c) for c in alist]
            self.cb_x_acodec.configure(values=labels)
            cur_a = self._x_key_from_label(self.x_acodec.get(), alist)
            default = tgt.get("default_a", alist[0])
            self.x_acodec.set(V.codec_label(cur_a if cur_a in alist else default))
        else:
            self.cb_x_acodec.configure(values=[V.codec_label("none")])
            self.x_acodec.set(V.codec_label("none"))
        self.lbl_x_note.configure(text=tgt.get("note", ""))

    @staticmethod
    def _x_key_from_label(label, allowed):
        for c in allowed:
            if V.codec_label(c) == label:
                return c
        return None

    def _selected_x_target(self):
        label = self.x_target.get()
        for k, t in V.ALL_TARGETS.items():
            if t["label"] == label:
                return k
        return None

    def _x_codec_value(self, var, allowed):
        return self._x_key_from_label(var.get(), allowed)

    def _refresh_convert_options(self):
        """按当前操作类型给出合法的目标格式列表。"""
        mode = self.x_mode.get()
        if mode == "audio_from_video":
            keys = list(V.AUDIO_TARGETS)
        elif mode in ("video_only", "remux"):
            keys = list(V.VIDEO_TARGETS)
        elif mode == "audio_only":
            # 只有能装视频轨的音频容器可用（MKA），或视频容器
            keys = list(V.MULTI_STREAM_AUDIO_TARGETS) + list(V.VIDEO_TARGETS)
        else:
            keys = list(V.ALL_TARGETS)
        self._x_target_keys = keys
        labels = [V.ALL_TARGETS[k]["label"] for k in keys]
        self.cb_x_target.configure(values=labels)
        if self.x_target.get() not in labels:
            self.x_target.set(labels[0] if labels else "")
        self._apply_target_codecs()
        self._update_x_compat()

    def _update_x_compat(self):
        """「仅换容器」时检查容器兼容性并给出警告。"""
        warn = ""
        mode = self.x_mode.get()
        key = self._selected_x_target()
        if mode == "remux" and self._x_src_info and key:
            msgs, must = V.check_compat(self._x_src_info, key, mode)
            if msgs:
                warn = "⚠ " + msgs[0]
        self.lbl_x_warn.configure(text=warn)
        return warn

    def pick_x_input(self):
        p = filedialog.askopenfilename(
            title="选择源文件",
            filetypes=[("媒体文件", " ".join("*" + e for e in VIDEO_EXT + AUDIO_EXT)),
                       ("所有文件", "*.*")])
        if p:
            self.x_in.set(p)
            self._load_x_info()

    def _load_x_info(self):
        p = self.x_in.get()
        if not p or not os.path.isfile(p):
            return
        info = probe(p)
        s = summarize(info)
        self._x_src_info = info
        self._x_has_audio = bool(s and s["acount"] > 0)
        size = os.path.getsize(p)
        if s:
            self.x_info.set(f'大小 {C.human_size(size)} ｜ 时长 {s["duration_str"]} ｜ '
                            f'{s["vcount"]} 视频流: {s["vdesc"]} ｜ {s["acount"]} 音频流: {s["adesc"]}')
            self.log_write(f"[探测] {os.path.basename(p)} → {C.human_size(size)}, "
                           f"时长 {s['duration_str']}, {s['vdesc']}, {s['adesc']}")
        else:
            self.x_info.set(f"大小 {C.human_size(size)}（无法解析媒体信息）")
        if not self._x_has_audio:
            self.log_write("[提示] 源文件无音轨，涉及音频的操作会失败。")
        self._auto_x_output()
        self._update_x_compat()

    def _auto_x_output(self):
        src = self.x_in.get()
        if not src:
            return
        key = self._selected_x_target()
        ext = V.ALL_TARGETS[key]["ext"] if key else ".mp4"
        self.x_out.set(os.path.splitext(src)[0] + "_out" + ext)

    def pick_x_output(self):
        key = self._selected_x_target()
        ext = V.ALL_TARGETS[key]["ext"] if key else ".mp4"
        base = os.path.splitext(os.path.basename(self.x_in.get() or "output"))[0]
        init = self.x_out.get() or os.path.join(os.path.dirname(self.x_in.get() or "."),
                                                base + "_out" + ext)
        p = filedialog.asksaveasfilename(
            title="保存为", initialfile=os.path.basename(init),
            initialdir=os.path.dirname(init) or ".", defaultextension=ext,
            filetypes=[("所有文件", "*.*")])
        if p:
            self.x_out.set(p)

    def clear_x(self):
        for v in (self.x_in, self.x_out, self.x_info):
            v.set("")
        self.x_info.set("未选择文件")
        self._x_src_info = None
        self.lbl_x_warn.configure(text="")
        self.log_write("[清空] 已重置转换输入")
        self._sync_run_button()

    # ---------------- 批量页
    def _build_batch_tab(self, root):
        pad = {"padx": 6, "pady": 3}
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # --- 导入
        imp = ttk.LabelFrame(root, text=" 1. 导入文件 ")
        imp.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        bf = ttk.Frame(imp)
        bf.grid(row=0, column=0, sticky="w", **pad)
        ttk.Button(bf, text="添加文件…", command=self.b_add_files).pack(side="left")
        ttk.Button(bf, text="添加文件夹…", command=self.b_add_folder).pack(side="left", padx=6)
        ttk.Button(bf, text="移除选中", command=self.b_remove_selected).pack(side="left")
        ttk.Button(bf, text="清空列表", command=self.b_clear).pack(side="left", padx=6)
        ttk.Checkbutton(bf, text="文件夹递归子目录", variable=self.b_recursive).pack(side="left", padx=10)
        ttk.Label(imp, textvariable=self.b_summary, foreground="#7a7a7a").grid(
            row=1, column=0, sticky="w", padx=8)

        # --- 列表
        lf = ttk.LabelFrame(root, text=" 2. 任务列表（点击列头排序，双击打开所在目录） ")
        lf.grid(row=1, column=0, sticky="nsew", padx=10, pady=6)
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)

        cols = ("idx", "name", "size", "status")
        self.tv = ttk.Treeview(lf, columns=cols, show="headings", selectmode="extended")
        for c, txt, w, anchor in (("idx", "#", 46, "center"),
                                  ("name", "文件名", 420, "w"),
                                  ("size", "大小", 90, "e"),
                                  ("status", "状态", 150, "w")):
            self.tv.heading(c, text=txt, command=lambda _c=c: self._b_sort(_c))
            self.tv.column(c, width=w, anchor=anchor, stretch=(c == "name"))
        self.tv.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(lf, orient="vertical", command=self.tv.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.tv.configure(yscrollcommand=vsb.set)
        self.tv.bind("<Double-1>", self._b_open_dir)
        self._b_sort_col = None
        self._b_sort_desc = False

        # --- 操作
        op = ttk.LabelFrame(root, text=" 3. 批量操作 ")
        op.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        op.columnconfigure(1, weight=1)

        ttk.Label(op, text="操作").grid(row=0, column=0, sticky="w", **pad)
        of = ttk.Frame(op)
        of.grid(row=0, column=1, columnspan=3, sticky="w", **pad)
        for val, txt in (("convert", "格式转换"), ("compress", "压缩"),
                         ("extract_audio", "提取音频"), ("remux", "仅换容器")):
            ttk.Radiobutton(of, text=txt, value=val, variable=self.b_op,
                            command=self._on_b_op).pack(side="left", padx=(0, 14))

        ttk.Label(op, text="目标格式").grid(row=1, column=0, sticky="w", **pad)
        self.cb_b_target = ttk.Combobox(op, textvariable=self.b_target, state="readonly", width=42)
        self.cb_b_target.grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(op, text="压缩方案").grid(row=2, column=0, sticky="w", **pad)
        self.cb_b_plan = ttk.Combobox(op, state="readonly", width=42)
        self.cb_b_plan.grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(op, text="分辨率").grid(row=3, column=0, sticky="w", **pad)
        self.cb_b_scale = ttk.Combobox(op, textvariable=self.b_scale, state="readonly", width=20,
                                       values=["保持原分辨率", "2160p (3840x2160)", "1080p (1920x1080)",
                                               "720p (1280x720)", "480p (854x480)", "360p (640x360)"])
        self.cb_b_scale.grid(row=3, column=1, sticky="w", **pad)

        # --- 输出
        out = ttk.LabelFrame(root, text=" 4. 输出设置 ")
        out.grid(row=3, column=0, sticky="ew", padx=10, pady=(6, 10))
        out.columnconfigure(1, weight=1)

        ttk.Label(out, text="输出目录").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(out, textvariable=self.b_outdir).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(out, text="选择…", width=8, command=self.b_pick_outdir).grid(row=0, column=2, **pad)
        ttk.Button(out, text="同源目录", width=9, command=lambda: self.b_outdir.set("")).grid(row=0, column=3, **pad)

        ttk.Label(out, text="命名模板").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(out, textvariable=self.b_template).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Label(out, text="可用: {name} {ext} {index} {date} {op}", foreground="#7a7a7a").grid(
            row=1, column=2, columnspan=2, sticky="w", padx=8)

    # ---------------- 批量页逻辑
    def _b_sort(self, col):
        if self._b_sort_col == col:
            self._b_sort_desc = not self._b_sort_desc
        else:
            self._b_sort_col, self._b_sort_desc = col, False

        # 注意：不能用 self.b_jobs.index(j) 当排序键——sort() 会在排序过程中
        # 就地改动同一个列表，list.index() 随之失效（ValueError: not in list）。
        # 用 enumerate 的原始下标做键，既稳定又避免回调期间访问被改动的列表。
        if col == "idx" and not self._b_sort_desc:
            # 恢复原始顺序
            self.b_jobs.sort(key=lambda j: j._order)
        else:
            keymap = {
                "idx": lambda t: t[0],
                "name": lambda t: t[1].name.lower(),
                "size": lambda t: (t[1].size_in if t[1].size_in is not None else -1),
                "status": lambda t: t[1].status,
            }
            keyfn = keymap.get(col, keymap["idx"])
            ordered = sorted(enumerate(self.b_jobs), key=keyfn, reverse=self._b_sort_desc)
            self.b_jobs = [j for _, j in ordered]
        self._b_refresh_list()

    def _b_open_dir(self, event=None):
        sel = self.tv.selection()
        if not sel:
            return
        idx = self.tv.index(sel[0])
        if 0 <= idx < len(self.b_jobs):
            p = self.b_jobs[idx].src
            if os.path.exists(p):
                try:
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
                except OSError:
                    os.startfile(os.path.dirname(p))

    def b_add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择要批量处理的文件",
            filetypes=[("媒体文件", " ".join("*" + e for e in VIDEO_EXT + AUDIO_EXT)),
                       ("所有文件", "*.*")])
        if paths:
            self._b_import(list(paths))

    def b_add_folder(self):
        d = filedialog.askdirectory(title="选择文件夹（自动收集里面的媒体文件）")
        if d:
            self._b_import([d])

    def _b_import(self, paths):
        exts = set(VIDEO_EXT) | set(AUDIO_EXT)
        found = B.collect_files(paths, recursive=self.b_recursive.get(), exts=exts)
        if not found:
            messagebox.showinfo(APP_TITLE, "没找到可处理的媒体文件。\n"
                                           "支持: " + ", ".join(sorted(exts)))
            return
        new = [B.Job(p) for p in found]
        merged, dup = B.dedupe(self.b_jobs + new)
        added = len(merged) - len(self.b_jobs)
        self.b_jobs = merged
        self._b_refresh_list()
        self.log_write(f"[批量] 新增 {added} 个文件"
                       + (f"，跳过 {dup} 个重复" if dup else "")
                       + f"（列表共 {len(self.b_jobs)} 项）")
        self._sync_run_button()

    def b_remove_selected(self):
        sel = self.tv.selection()
        if not sel:
            return
        idxs = sorted((self.tv.index(i) for i in sel), reverse=True)
        for i in idxs:
            if 0 <= i < len(self.b_jobs):
                del self.b_jobs[i]
        self._b_refresh_list()
        self._sync_run_button()

    def b_clear(self):
        if self.b_jobs and not messagebox.askyesno(APP_TITLE, f"清空全部 {len(self.b_jobs)} 个任务？"):
            return
        self.b_jobs = []
        self._b_refresh_list()
        self._sync_run_button()

    def b_pick_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.b_outdir.set(d)

    def _b_selected_plan(self):
        """返回当前批量设置对应的 (方案key, 说明)。"""
        op = self.b_op.get()
        if op == "convert":
            return self._selected_b_target(), None
        if op == "remux":
            return self._selected_b_target(), None
        if op == "extract_audio":
            return self._selected_b_target(), None
        if op == "compress":
            label = self.cb_b_plan.get()
            for k in self._ll_keys:
                if C.LOSSLESS_PRESETS[k]["label"] == label:
                    return k, "lossless"
            for k in self._cp_keys:
                if C.COMPRESS_PRESETS[k]["label"] == label:
                    return k, "compress"
        return None, None

    def _selected_b_target(self):
        label = self.b_target.get()
        for k, t in V.ALL_TARGETS.items():
            if t["label"] == label:
                return k
        return None

    def _on_b_op(self):
        op = self.b_op.get()
        # 目标格式下拉：按操作类型给候选
        if op in ("convert",):
            keys = list(V.ALL_TARGETS)
        elif op in ("remux", "compress"):
            keys = list(V.VIDEO_TARGETS)
        elif op == "extract_audio":
            keys = list(V.AUDIO_TARGETS)
        else:
            keys = list(V.ALL_TARGETS)
        self._b_target_keys = keys
        labels = [V.ALL_TARGETS[k]["label"] for k in keys]
        self.cb_b_target.configure(values=labels)
        if self.b_target.get() not in labels:
            self.b_target.set(labels[0] if labels else "")

        # 压缩方案下拉
        if op == "compress":
            plans = ([C.LOSSLESS_PRESETS[k]["label"] for k in self._ll_keys]
                     + [C.COMPRESS_PRESETS[k]["label"] for k in self._cp_keys])
            self.cb_b_plan.configure(values=plans)
            if self.cb_b_plan.get() not in plans:
                pref = "H.264 CRF 23（推荐·均衡）"
                self.cb_b_plan.set(pref if pref in plans else (plans[0] if plans else ""))
            self.cb_b_plan.configure(state="readonly")
            self.cb_b_target.configure(state="disabled")
            self.cb_b_scale.configure(state="readonly")
        else:
            self.cb_b_plan.set("")
            self.cb_b_plan.configure(state="disabled")
            self.cb_b_target.configure(state="readonly")
            self.cb_b_scale.configure(state="disabled" if op == "extract_audio" else "readonly")

        # 分辨率：提取音频/仅换容器用不到
        self.cb_b_scale.configure(state="disabled" if op in ("extract_audio", "remux") else "readonly")

    def _b_refresh_list(self):
        self.tv.delete(*self.tv.get_children())
        for i, j in enumerate(self.b_jobs, 1):
            size = C.human_size(j.size_in) if j.size_in else "—"
            st = B.STATUS_TEXT.get(j.status, j.status)
            if j.message:
                st += f" · {j.message}"
            self.tv.insert("", "end", iid=str(i - 1), values=(i, j.name, size, st))
        s = B.summary(self.b_jobs)
        total = sum(j.size_in or 0 for j in self.b_jobs)
        self.b_summary.set(f"共 {len(self.b_jobs)} 项 ｜ 合计 {C.human_size(total) if total else '—'} ｜ "
                           f"待处理 {s['pending']}  完成 {s['done']}  失败 {s['failed']}  跳过 {s['skipped']}")

    def _b_fill_sizes(self):
        """给还没有大小的任务补上文件大小（惰性，避免导入时卡）。"""
        for j in self.b_jobs:
            if j.size_in is None:
                try:
                    j.size_in = os.path.getsize(j.src)
                except OSError:
                    j.size_in = 0
        self._b_refresh_list()

    # ---------------- 环境自检
    def log_write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _check_env(self):
        rep = deps.check_all()
        self._report = rep
        self.log_write("=" * 66)
        self.log_write(f"{APP_TITLE} v{APP_VERSION}")
        self.log_write("=" * 66)
        for line in deps.format_report(rep).splitlines():
            self.log_write(line)
        if rep["missing_required"]:
            self.status.set("✗ 缺少必需组件")
            self.btn_run.configure(state="disabled")
            self.root.after(300, self._show_env_problem)
        else:
            self.status.set("就绪")

    def _show_env_problem(self):
        rep = self._report
        msg = "检测到必需组件缺失，程序无法正常工作：\n\n"
        for mod, desc in rep["missing_required"]:
            msg += f"  • {mod} —— {desc}\n"
        msg += "\n解决方案：安装 Python 3.8+ 时勾选「tcl/tk」组件。"
        messagebox.showerror(APP_TITLE, msg)

    def _on_mode(self):
        editing = self.mode.get() == "transcode"
        state = "readonly" if editing else "disabled"
        for w in (self.cb_vcodec, self.cb_acodec, self.cb_abr, self.cb_preset):
            w.configure(state=state)
        self.sp_crf.configure(state="normal" if editing else "disabled")

    # ---------------- 压缩页辅助
    def _on_c_kind(self):
        lossless = self.c_kind.get() == "lossless"
        if lossless:
            self.frm_compress.grid_remove()
            self.frm_lossless.grid()
        else:
            self.frm_lossless.grid_remove()
            self.frm_compress.grid()
        self._update_c_hint()
        self._sync_run_button()

    def _refresh_compress_presets(self):
        enc, nv = capabilities()
        ll = C.available_lossless(enc, nv)
        cp = C.available_compress(enc, nv)
        self._ll_keys = ll
        self._cp_keys = cp
        self.cb_lossless.configure(values=[C.LOSSLESS_PRESETS[k]["label"] for k in ll])
        self.cb_compress.configure(values=[C.COMPRESS_PRESETS[k]["label"] for k in cp])
        if ll and self.c_lossless.get() not in [C.LOSSLESS_PRESETS[k]["label"] for k in ll]:
            self.c_lossless.set(C.LOSSLESS_PRESETS[ll[0]]["label"])
        pref = "x264_crf23" if "x264_crf23" in cp else (cp[0] if cp else None)
        if cp and self.c_compress.get() not in [C.COMPRESS_PRESETS[k]["label"] for k in cp]:
            self.c_compress.set(C.COMPRESS_PRESETS[pref]["label"])
        self._on_c_kind()
        self._on_b_op()
        self.log_write(f"[能力] 无损方案 {len(ll)} 个，高压缩方案 {len(cp)} 个，"
                       f"NVENC {'可用' if nv else '不可用'}")

    def _selected_key(self, kind):
        """把下拉框的显示名还原成方案 key。"""
        if kind == "lossless":
            label = self.c_lossless.get()
            for k in self._ll_keys:
                if C.LOSSLESS_PRESETS[k]["label"] == label:
                    return k
        else:
            label = self.c_compress.get()
            for k in self._cp_keys:
                if C.COMPRESS_PRESETS[k]["label"] == label:
                    return k
        return None

    def _update_c_hint(self):
        if self.c_kind.get() == "lossless":
            k = self._selected_key("lossless")
            p = C.get_lossless(k) if k else None
            self.lbl_lossless_note.configure(text=p["note"] if p else "")
        else:
            k = self._selected_key("compress")
            p = C.get_compress(k) if k else None
            txt = p["note"] if p else ""
            self.lbl_compress_note.configure(text=txt)

    def pick_c_input(self):
        p = filedialog.askopenfilename(
            title="选择要压缩的文件",
            filetypes=[("视频文件", " ".join("*" + e for e in VIDEO_EXT)),
                       ("所有文件", "*.*")])
        if p:
            self.c_in.set(p)
            self._load_c_info()
            self._auto_c_output()

    def pick_c_output(self):
        k = self._selected_key("lossless") if self.c_kind.get() == "lossless" else self._selected_key("compress")
        if self.c_kind.get() == "lossless":
            ext = C.LOSSLESS_PRESETS[k]["container"] if k else ".mkv"
        else:
            ext = C.COMPRESS_PRESETS[k]["container"] if k else ".mp4"
        base = os.path.splitext(os.path.basename(self.c_in.get() or "output"))[0]
        init = self.c_out.get() or (os.path.join(os.path.dirname(self.c_in.get() or "."),
                                                 base + "_out" + ext))
        p = filedialog.asksaveasfilename(
            title="保存为", initialfile=os.path.basename(init),
            initialdir=os.path.dirname(init) or ".", defaultextension=ext,
            filetypes=[("MKV", "*.mkv"), ("MP4", "*.mp4"), ("所有文件", "*.*")])
        if p:
            self.c_out.set(p)

    def _load_c_info(self):
        p = self.c_in.get()
        if not p or not os.path.isfile(p):
            return
        s = summarize(probe(p))
        size = os.path.getsize(p)
        self.c_before = size
        self._c_has_audio = bool(s and s["acount"] > 0)
        if s:
            self.c_info.set(f'大小 {C.human_size(size)} ｜ 时长 {s["duration_str"]} ｜ '
                            f'{s["vcount"]} 视频流: {s["vdesc"]} ｜ {s["acount"]} 音频流: {s["adesc"]}')
            self.log_write(f"[探测] {os.path.basename(p)} → {C.human_size(size)}, "
                           f"时长 {s['duration_str']}, {s['vdesc']}, {s['adesc']}")
        else:
            self.c_info.set(f"大小 {C.human_size(size)}（无法解析媒体信息）")
            self.log_write(f"[探测] {os.path.basename(p)} → {C.human_size(size)}（无法解析媒体信息）")
        if not self._c_has_audio:
            self.log_write("[提示] 源文件未检测到音频流，将只处理视频。")
        self._auto_c_output()
        self._sync_run_button()

    def _auto_c_output(self):
        src = self.c_in.get()
        if not src:
            return
        if self.c_kind.get() == "lossless":
            k = self._selected_key("lossless")
            ext = C.LOSSLESS_PRESETS[k]["container"] if k else ".mkv"
        else:
            k = self._selected_key("compress")
            ext = C.COMPRESS_PRESETS[k]["container"] if k else ".mp4"
        stem = os.path.splitext(src)[0]
        self.c_out.set(stem + "_out" + ext)

    def clear_c(self):
        for v in (self.c_in, self.c_out, self.c_info):
            v.set("")
        self.c_info.set("未选择文件")
        self.c_before = None
        self.log_write("[清空] 已重置压缩输入")
        self._sync_run_button()

    # ---------------- 按钮同步
    def _sync_run_button(self):
        if self._busy or self._b_running:
            return
        if not FFMPEG:
            self.btn_run.configure(state="disabled")
            return
        idx = self.nb.index("current")
        if idx == 3:
            n = len(self.b_jobs)
            self.btn_run.configure(text=f"开始批量 ({n})" if n else "开始批量",
                                   state="normal" if n else "disabled")
            return
        labels = {0: "开始合并", 1: "开始压缩", 2: "开始转换"}
        self.btn_run.configure(text=labels.get(idx, "开始"), state="normal")

    def dispatch_run(self):
        idx = self.nb.index("current")
        if idx == 0:
            self.start()
        elif idx == 1:
            self.start_compress()
        elif idx == 2:
            self.start_convert()
        else:
            self.start_batch()

    # ---------------- 转换执行
    def start_convert(self):
        src = self.x_in.get().strip()
        dst = self.x_out.get().strip()
        mode = self.x_mode.get()
        key = self._selected_x_target()

        if not os.path.isfile(src):
            messagebox.showerror(APP_TITLE, "请选择有效的源文件。")
            return
        if not dst:
            messagebox.showerror(APP_TITLE, "请指定输出文件路径。")
            return
        if os.path.abspath(dst) == os.path.abspath(src):
            messagebox.showerror(APP_TITLE, "输出文件不能覆盖源文件。")
            return
        if not key:
            messagebox.showerror(APP_TITLE, "请选择目标格式。")
            return
        if os.path.exists(dst) and not messagebox.askyesno(APP_TITLE, "输出文件已存在，覆盖？"):
            return

        tgt = V.ALL_TARGETS[key]
        scale_map = self._scale_map()
        scale = scale_map.get(self.x_scale.get())
        fps = None if self.x_fps.get() == "保持原帧率" else self.x_fps.get()

        if mode == "convert":
            vc = self._x_codec_value(self.x_vcodec, tgt.get("v", [])) or tgt.get("default_v")
            ac = self._x_codec_value(self.x_acodec, tgt.get("a", [])) or tgt.get("default_a")
            cmd, err = V.build_convert_cmd(
                FFMPEG, src, dst, key, vcodec=vc, acodec=ac,
                crf=self.x_crf.get(), preset=self.x_preset.get(),
                abitrate=self.x_abitrate.get(), scale=scale, fps=fps,
                has_audio=self._x_has_audio)
        else:
            ac = self._x_codec_value(self.x_acodec, tgt.get("a", []))
            vc = self._x_codec_value(self.x_vcodec, tgt.get("v", []))
            cmd, err = V.build_extract_cmd(
                FFMPEG, src, dst, mode, target=key, vcodec=vc, acodec=ac,
                crf=self.x_crf.get(), preset=self.x_preset.get(),
                abitrate=self.x_abitrate.get(), has_audio=self._x_has_audio)

        if err:
            messagebox.showerror(APP_TITLE, err)
            self.log_write(f"[阻止] {err}")
            return

        desc = f"{V.EXTRACT_MODES.get(mode, {}).get('label', mode)} → {tgt['label']}"
        src_size = os.path.getsize(src)
        self.log_write("")
        self.log_write("=" * 66)
        self.log_write(f"[开始] {desc}")
        self.log_write(f"[输入] {src}  ({C.human_size(src_size)})")
        self.log_write(f"[输出] {dst}")
        self.log_write("[命令] " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

        self._busy = True
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.stop_flag = False
        self.pb.configure(value=0)
        self.status.set("转换中…")
        threading.Thread(target=self._job_worker,
                         args=(cmd, src, dst, src_size, desc, "转换"), daemon=True).start()

    @staticmethod
    def _scale_map():
        return {
            "2160p (3840x2160)": "3840:-2",
            "1080p (1920x1080)": "1920:-2",
            "720p (1280x720)": "1280:-2",
            "480p (854x480)": "854:-2",
            "360p (640x360)": "640:-2",
            "保持原分辨率": None,
        }

    # ---------------- 批量执行
    def start_batch(self):
        if not self.b_jobs:
            messagebox.showinfo(APP_TITLE, "任务列表为空。")
            return
        todo = [j for j in self.b_jobs if j.status in ("pending", "failed", "skipped")]
        if not todo:
            if not messagebox.askyesno(APP_TITLE, "所有任务都已处理过，全部重跑一遍？"):
                return
            todo = list(self.b_jobs)
            for j in todo:
                j.status, j.message, j.dst, j.size_out = "pending", "", None, None

        op = self.b_op.get()
        op_label = {"convert": "转换", "compress": "压缩",
                    "extract_audio": "提取音频", "remux": "仅换容器"}.get(op, op)
        if op == "compress":
            key, kind = self._selected_b_plan()
            if not key:
                messagebox.showerror(APP_TITLE, "请选择压缩方案。")
                return
        else:
            key = self._selected_b_target()
            if not key:
                messagebox.showerror(APP_TITLE, "请选择目标格式。")
                return

        outdir = self.b_outdir.get().strip()
        if outdir and not os.path.isdir(outdir):
            try:
                os.makedirs(outdir, exist_ok=True)
            except OSError as e:
                messagebox.showerror(APP_TITLE, f"无法创建输出目录：{e}")
                return

        tem = self.b_template.get().strip() or "{name}_{op}"
        self.log_write("")
        self.log_write("=" * 66)
        self.log_write(f"[批量] 操作={op_label}  共 {len(todo)} 项  输出目录={outdir or '（与源文件同目录）'}")
        self.log_write(f"[批量] 命名模板={tem}")

        self._b_running = True
        self._busy = True
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.stop_flag = False
        self.pb.configure(value=0)
        self.status.set("批量处理中…")
        threading.Thread(target=self._batch_worker,
                         args=(todo, op, key, outdir, tem), daemon=True).start()

    def _selected_b_plan(self):
        label = self.cb_b_plan.get()
        for k in self._ll_keys:
            if C.LOSSLESS_PRESETS[k]["label"] == label:
                return k, "lossless"
        for k in self._cp_keys:
            if C.COMPRESS_PRESETS[k]["label"] == label:
                return k, "compress"
        return None, None

    def _batch_worker(self, todo, op, key, outdir, template):
        q = self.msg_q
        scale_map = self._scale_map()
        scale = scale_map.get(self.b_scale.get())
        taken = set()
        n = len(todo)
        ok = fail = 0
        totals = [0, 0]

        for i, job in enumerate(todo, 1):
            if self.stop_flag:
                job.status = "skipped"
                job.message = "已停止"
                q.put(("batch_row", None))
                continue

            job.status = "running"
            job.message = ""
            q.put(("batch_row", None))
            q.put(("status", f"批量处理中… ({i}/{n}) {job.name}"))

            try:
                src_size = os.path.getsize(job.src)
            except OSError:
                src_size = 0
            job.size_in = src_size

            info = probe(job.src)
            has_audio = bool(info and any(s.get("codec_type") == "audio"
                                          for s in info.get("streams", [])))

            # 决定输出路径与扩展名
            if op == "compress":
                kind = "lossless" if key in C.LOSSLESS_PRESETS else "compress"
                ext = (C.LOSSLESS_PRESETS if kind == "lossless" else C.COMPRESS_PRESETS)[key]["container"]
                op_name = "compress"
            else:
                tgt = V.ALL_TARGETS[key]
                ext = tgt["ext"]
                op_name = {"convert": "convert", "extract_audio": "audio",
                           "remux": "remux"}.get(op, op)
            try:
                dst = B.make_output_path(job, outdir, ext, template, i, op_name)
                dst = B.ensure_unique(dst, taken)
            except Exception as e:
                job.status, job.message = "failed", f"路径生成失败: {e}"
                fail += 1
                q.put(("batch_row", None))
                continue
            job.dst = dst

            # 构造命令
            err = None
            try:
                if op == "compress":
                    kind = "lossless" if key in C.LOSSLESS_PRESETS else "compress"
                    if kind == "lossless":
                        cmd = C.build_lossless_cmd(FFMPEG, job.src, dst, key,
                                                   audio_mode="lossless", has_audio=has_audio)
                    else:
                        cmd = C.build_compress_cmd(FFMPEG, job.src, dst, key,
                                                   preset=self.b_preset.get(),
                                                   scale=scale, has_audio=has_audio)
                elif op == "remux":
                    cmd, err = V.build_extract_cmd(FFMPEG, job.src, dst, "remux", target=key)
                elif op == "extract_audio":
                    if not has_audio:
                        err = "源文件无音轨"
                    else:
                        cmd, err = V.build_extract_cmd(FFMPEG, job.src, dst, "audio_from_video",
                                                       target=key, abitrate="192k",
                                                       has_audio=has_audio)
                else:  # convert
                    cmd, err = V.build_convert_cmd(FFMPEG, job.src, dst, key,
                                                   crf=23, preset="medium",
                                                   scale=scale, has_audio=has_audio)
            except Exception as e:
                err = f"命令构造失败: {e}"

            if err or not cmd:
                job.status, job.message = "failed", err or "无法构造命令"
                fail += 1
                q.put(("log", f"[失败] {job.name}: {job.message}"))
                q.put(("batch_row", None))
                continue

            # 执行
            rc, tail = self._run_one(cmd, job)
            if self.stop_flag:
                job.status, job.message = "skipped", "已停止"
            elif rc == 0 and os.path.exists(dst):
                try:
                    job.size_out = os.path.getsize(dst)
                except OSError:
                    job.size_out = 0
                job.status = "done"
                ratio, delta = C.ratio_text(src_size, job.size_out)
                job.message = f"{C.human_size(src_size)}→{C.human_size(job.size_out)} {delta}"
                ok += 1
                totals[0] += src_size
                totals[1] += job.size_out
                q.put(("log", f"[完成] ({i}/{n}) {job.name}  {job.message}"))
            else:
                job.status = "failed"
                job.message = f"ffmpeg 返回码 {rc}"
                fail += 1
                q.put(("log", f"[失败] ({i}/{n}) {job.name}  {job.message}"))
                for l in tail[-6:]:
                    q.put(("log", "    " + l))

            q.put(("batch_row", None))
            q.put(("progress", i / n * 100))

        # 汇总
        r, d = C.ratio_text(totals[0], totals[1])
        q.put(("log", "-" * 66))
        q.put(("log", f"[批量结束] 成功 {ok}  失败 {fail}"
                      + (f"  合计 {C.human_size(totals[0])} → {C.human_size(totals[1])} ({d})"
                         if totals[0] else "")))
        q.put(("status", f"批量结束：成功 {ok}，失败 {fail}"))
        q.put(("batch_done", None))

    def _run_one(self, cmd, job):
        """执行单条 ffmpeg 命令，返回 (returncode, 末尾输出行)。"""
        tail = []
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, universal_newlines=True,
                                    encoding="utf-8", errors="replace",
                                    creationflags=CREATE_NO_WINDOW)
            self.proc = proc
            for line in proc.stdout:
                if self.stop_flag:
                    proc.terminate()
                    break
                line = line.rstrip("\n")
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
            proc.wait()
            return proc.returncode, tail
        except Exception as e:
            tail.append(f"[异常] {e}")
            return -1, tail
        finally:
            self.proc = None

    # ---------------- 压缩执行
    def start_compress(self):
        src = self.c_in.get().strip()
        dst = self.c_out.get().strip()
        if not os.path.isfile(src):
            messagebox.showerror(APP_TITLE, "请选择有效的源文件。")
            return
        if not dst:
            messagebox.showerror(APP_TITLE, "请指定输出文件路径。")
            return
        if os.path.abspath(dst) == os.path.abspath(src):
            messagebox.showerror(APP_TITLE, "输出文件不能覆盖源文件。")
            return
        if os.path.exists(dst) and not messagebox.askyesno(APP_TITLE, "输出文件已存在，覆盖？"):
            return

        kind = self.c_kind.get()
        key = self._selected_key(kind)
        if not key:
            messagebox.showerror(APP_TITLE, "请选择压缩方案。")
            return

        if kind == "lossless":
            cmd = C.build_lossless_cmd(FFMPEG, src, dst, key, audio_mode=self.c_audio.get(),
                                       has_audio=self._c_has_audio)
            desc = C.LOSSLESS_PRESETS[key]["label"]
        else:
            scale_map = {
                "1080p (1920x1080)": "1920:-2",
                "720p (1280x720)": "1280:-2",
                "480p (854x480)": "854:-2",
                "360p (640x360)": "640:-2",
                "保持原分辨率": None,
            }
            scale = scale_map.get(self.c_scale.get())
            cmd = C.build_compress_cmd(FFMPEG, src, dst, key,
                                       preset=self.c_preset.get(), scale=scale,
                                       has_audio=self._c_has_audio)
            desc = C.COMPRESS_PRESETS[key]["label"]

        src_size = os.path.getsize(src)
        self.log_write("")
        self.log_write("=" * 66)
        self.log_write(f"[开始] 压缩方案: {desc}")
        self.log_write(f"[输入] {src}  ({C.human_size(src_size)})")
        self.log_write(f"[输出] {dst}")
        self.log_write("[命令] " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

        self._busy = True
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.stop_flag = False
        self.pb.configure(value=0)
        self.status.set("压缩中…")
        threading.Thread(target=self._job_worker,
                         args=(cmd, src, dst, src_size, desc, "压缩"), daemon=True).start()

    def _job_worker(self, cmd, src, dst, src_size, desc, verb):
        """
        单任务执行器（压缩 / 转换共用）。
        逐行读取 ffmpeg 输出，解析 time= 推进度，结束时汇报体积变化。
        """
        q = self.msg_q
        duration = None
        try:
            s = summarize(probe(src))
            if s and s["duration"]:
                duration = s["duration"]
        except Exception:
            pass

        time_re = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")
        rc = 1
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, universal_newlines=True,
                                    encoding="utf-8", errors="replace",
                                    creationflags=CREATE_NO_WINDOW)
            self.proc = proc
            tail = []
            for line in proc.stdout:
                if self.stop_flag:
                    proc.terminate()
                    break
                line = line.rstrip("\n")
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
                m = time_re.search(line)
                if m and duration:
                    cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    pct = max(0.0, min(100.0, cur / duration * 100))
                    q.put(("progress", pct))
                    q.put(("status", f"{verb}中… {pct:.1f}%  ({fmt_ts(cur)} / {fmt_ts(duration)})"))
                elif line.strip() and not line.startswith(("frame=", "size=", "  ")):
                    q.put(("log", line))
            proc.wait()
            rc = proc.returncode
            if self.stop_flag:
                q.put(("log", "[已停止] 用户中断"))
                q.put(("status", "已停止"))
            elif rc == 0 and os.path.exists(dst):
                dst_size = os.path.getsize(dst)
                ratio, delta = C.ratio_text(src_size, dst_size)
                s2 = summarize(probe(dst))
                q.put(("log", "-" * 66))
                q.put(("log", f"[完成] {desc}"))
                q.put(("log", f"[大小] {C.human_size(src_size)} → {C.human_size(dst_size)}"
                              f"   压缩比 {ratio}   {delta}"))
                if s2:
                    q.put(("log", f"[结果] 时长 {s2['duration_str']} ｜ {s2['vdesc']} ｜ {s2['adesc']}"))
                q.put(("log", f"[输出] {dst}"))
                q.put(("status", f"✓ {verb}完成  {C.human_size(src_size)} → "
                                 f"{C.human_size(dst_size)}  ({delta})"))
            else:
                q.put(("log", f"[失败] ffmpeg 返回码 {rc}"))
                for l in tail[-12:]:
                    q.put(("log", "  " + l))
                q.put(("status", f"✗ {verb}失败，详见日志"))
        except Exception as e:
            q.put(("log", f"[异常] {e}"))
            q.put(("status", "✗ 出错"))
        finally:
            self.proc = None
            q.put(("done", rc))

    def reset_vol(self):
        self.volume.set(1.0)
        self.lbl_vol.config(text="1.00x")

    def pick_video(self):
        p = filedialog.askopenfilename(title="选择视频文件",
                                       filetypes=[("视频文件", " ".join("*" + e for e in VIDEO_EXT)),
                                                  ("所有文件", "*.*")])
        if p:
            self.video_path.set(p)
            self._load_video_info()
            self._auto_output()

    def pick_audio(self):
        p = filedialog.askopenfilename(title="选择音频文件",
                                       filetypes=[("音频文件", " ".join("*" + e for e in AUDIO_EXT)),
                                                  ("所有文件", "*.*")])
        if p:
            self.audio_path.set(p)
            self._load_audio_info()

    def pick_output(self):
        init = self.out_path.get() or os.path.splitext(self.video_path.get())[0] + "_merged.mp4"
        p = filedialog.asksaveasfilename(title="保存为", initialfile=os.path.basename(init),
                                         initialdir=os.path.dirname(init) or ".",
                                         defaultextension=".mp4",
                                         filetypes=[("MP4", "*.mp4"), ("MKV", "*.mkv"), ("MOV", "*.mov"),
                                                    ("所有文件", "*.*")])
        if p:
            self.out_path.set(p)

    def _load_video_info(self):
        p = self.video_path.get()
        if not p or not os.path.isfile(p):
            return
        info = probe(p)
        s = summarize(info)
        if s:
            self.v_info.set(f'时长 {s["duration_str"]} ｜ {s["vcount"]} 视频流: {s["vdesc"]} ｜ {s["acount"]} 音频流: {s["adesc"]}')
            self.log_write(f"[探测] 视频 {os.path.basename(p)} → 时长 {s['duration_str']}, 视频流 {s['vdesc']}, 音频流 {s['adesc']}")
        else:
            self.v_info.set("无法解析媒体信息")
        self._auto_output()

    def _load_audio_info(self):
        p = self.audio_path.get()
        if not p or not os.path.isfile(p):
            return
        s = summarize(probe(p))
        if s:
            self.a_info.set(f'时长 {s["duration_str"]} ｜ {s["acount"]} 音频流: {s["adesc"]}')
            self.log_write(f"[探测] 音频 {os.path.basename(p)} → 时长 {s['duration_str']}, {s['adesc']}")

    def _auto_output(self):
        if self.out_path.get():
            return
        v = self.video_path.get()
        if v:
            self.out_path.set(os.path.splitext(v)[0] + "_merged.mp4")

    def auto_pair(self):
        """在视频同目录里找同名/近似名的音频文件。"""
        v = self.video_path.get()
        if not v or not os.path.isfile(v):
            messagebox.showinfo(APP_TITLE, "先选择视频文件。")
            return
        d = os.path.dirname(v)
        stem = os.path.splitext(os.path.basename(v))[0]
        # 去掉常见的 _video / .video 后缀再匹配
        base = re.sub(r"[._-]?(video|visual|画面|视频)$", "", stem, flags=re.I)

        cands = []
        for name in os.listdir(d):
            if os.path.splitext(name)[1].lower() not in AUDIO_EXT:
                continue
            a_base = re.sub(r"[._-]?(audio|sound|音轨|音频)$", "", os.path.splitext(name)[0], flags=re.I)
            if a_base.lower() == base.lower() or a_base.lower() == stem.lower():
                cands.append(os.path.join(d, name))

        if cands:
            self.audio_path.set(cands[0])
            self._load_audio_info()
            self.log_write(f"[配对] 自动选中音频: {os.path.basename(cands[0])}")
        else:
            messagebox.showinfo(APP_TITLE, f"同目录下没找到与「{stem}」匹配的音频文件。\n请手动选择。")

    def fill_end_from_video(self):
        s = summarize(probe(self.video_path.get()))
        if s and s["duration"]:
            self.end_time.set(f"{s['duration']:.3f}")
            self.log_write(f"[剪辑] 终点填为视频时长 {s['duration']:.3f}s")

    def clear_all(self):
        for var in (self.video_path, self.audio_path, self.out_path, self.start_time,
                    self.end_time, self.a_offset):
            var.set("")
        self.v_info.set("未选择视频")
        self.a_info.set("未选择音频")
        self.reset_vol()
        self.log_write("[清空] 已重置输入")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "log":
                    self.log_write(payload)
                elif kind == "progress":
                    self.pb.configure(value=payload)
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "done":
                    self.proc = None
                    self._busy = False
                    self.btn_stop.configure(state="disabled")
                    self._sync_run_button()
                    self.pb.configure(value=100 if payload == 0 else 0)
                elif kind == "batch_row":
                    self._b_refresh_list()
                elif kind == "batch_done":
                    self.proc = None
                    self._busy = False
                    self._b_running = False
                    self.btn_stop.configure(state="disabled")
                    self._sync_run_button()
                    self._b_refresh_list()
                    self.pb.configure(value=100)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    # ---------------- 执行
    def start(self):
        video = self.video_path.get().strip()
        audio = self.audio_path.get().strip()
        output = self.out_path.get().strip()

        if not os.path.isfile(video):
            messagebox.showerror(APP_TITLE, "请选择有效的视频文件。")
            return
        if not audio or not os.path.isfile(audio):
            messagebox.showerror(APP_TITLE, "请选择有效的音频文件。")
            return
        if not output:
            messagebox.showerror(APP_TITLE, "请指定输出文件路径。")
            return
        if os.path.abspath(output) in (os.path.abspath(video), os.path.abspath(audio)):
            messagebox.showerror(APP_TITLE, "输出文件不能覆盖输入文件。")
            return
        if os.path.exists(output) and not messagebox.askyesno(APP_TITLE, "输出文件已存在，覆盖？"):
            return

        try:
            start = parse_ts(self.start_time.get())
            end = parse_ts(self.end_time.get())
            a_off = parse_ts(self.a_offset.get()) or 0.0
        except ValueError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        if start is not None and end is not None and end <= start:
            messagebox.showerror(APP_TITLE, "终点必须大于起点。")
            return

        if abs(a_off) > 1e-6 and self.mode.get() == "copy":
            if not messagebox.askyesno(APP_TITLE,
                                       "无损封装模式无法实现「音频延迟」和「音量调节」。\n"
                                       "继续将忽略这两项设置。\n\n是否改为「重编码」模式？\n"
                                       "（是=改用重编码；否=忽略这两项继续）"):
                a_off = 0.0
                self.volume.set(1.0)
                self.lbl_vol.config(text="1.00x")

        _, desc = build_cmd(video, audio, output, self.mode.get(), self.vcodec.get(),
                            self.acodec.get(), self.crf.get(), self.preset.get(),
                            self.audio_bitrate.get(), start, end, self.volume.get(),
                            self.shortest.get(), a_off)
        self.log_write("")
        self.log_write("=" * 70)
        self.log_write(f"[开始] 模式: {desc}")
        self.log_write(f"[输入] 视频 {video}")
        self.log_write(f"[输入] 音频 {audio}")
        self.log_write(f"[输出] {output}")
        if start is not None or end is not None:
            self.log_write(f"[剪辑] {fmt_ts(start)} → {fmt_ts(end)}")
        if abs(a_off) > 1e-6:
            self.log_write(f"[对齐] 音频延迟 {a_off:+.3f}s")
        if abs(self.volume.get() - 1.0) > 1e-6:
            self.log_write(f"[音量] {self.volume.get():.2f}x")

        self._busy = True
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.stop_flag = False
        self.pb.configure(value=0)
        self.status.set("合并中…")

        t = threading.Thread(target=self._worker, args=(video, audio, output, start, end, a_off), daemon=True)
        t.start()

    def _worker(self, video, audio, output, start, end, a_off):
        q = self.msg_q
        duration = None
        try:
            info = summarize(probe(video))
            if info and info["duration"]:
                duration = info["duration"]
                if start is not None:
                    duration -= start
                if end is not None:
                    duration = min(duration, end - (start or 0.0))
        except Exception:
            pass

        cmd, _ = build_cmd(video, audio, output, self.mode.get(), self.vcodec.get(),
                           self.acodec.get(), self.crf.get(), self.preset.get(),
                           self.audio_bitrate.get(), start, end, self.volume.get(),
                           self.shortest.get(), a_off)
        q.put(("log", "[命令] " + " ".join(f'"{c}"' if " " in c else c for c in cmd)))

        time_re = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")
        rc = 1
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, universal_newlines=True,
                                    encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
            self.proc = proc
            tail = []
            for line in proc.stdout:
                if self.stop_flag:
                    proc.terminate()
                    break
                line = line.rstrip("\n")
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
                # 进度条行刷屏，转成进度
                m = time_re.search(line)
                if m and duration:
                    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                    cur = h * 3600 + mi * 60 + s
                    pct = max(0.0, min(100.0, cur / duration * 100))
                    q.put(("progress", pct))
                    q.put(("status", f"合并中… {pct:.1f}%  ({fmt_ts(cur)} / {fmt_ts(duration)})"))
                elif line.strip() and not line.startswith(("frame=", "size=", "  ")):
                    q.put(("log", line))
            proc.wait()
            rc = proc.returncode
            if self.stop_flag:
                q.put(("log", "[已停止] 用户中断"))
                q.put(("status", "已停止"))
            elif rc == 0:
                try:
                    size = os.path.getsize(output) / 1024 / 1024
                    s = summarize(probe(output))
                    dur_txt = s["duration_str"] if s else "?"
                    q.put(("log", f"[完成] {output}"))
                    q.put(("log", f"[完成] 大小 {size:.1f} MB, 时长 {dur_txt}"))
                except OSError:
                    q.put(("log", "[完成] 输出已生成"))
                q.put(("status", "✓ 合并完成"))
            else:
                q.put(("log", "[失败] ffmpeg 返回码 " + str(rc)))
                for l in tail[-12:]:
                    q.put(("log", "  " + l))
                q.put(("status", "✗ 合并失败，详见日志"))
        except Exception as e:
            q.put(("log", f"[异常] {e}"))
            q.put(("status", "✗ 出错"))
        finally:
            self.proc = None
            q.put(("done", rc))

    def stop(self):
        self.stop_flag = True
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
        # 批量模式：标记剩余未跑的任务为停止
        if self._b_running:
            for j in self.b_jobs:
                if j.status == "pending":
                    j.status = "skipped"
                    j.message = "已停止"
        self.status.set("正在停止…")


def main():
    # 支持 --check / -c 无 GUI 自检
    if len(sys.argv) > 1 and sys.argv[1] in ("--check", "-c", "--selftest"):
        import deps as _d
        rep = _d.check_all()
        print(_d.format_report(rep))
        enc = rep["encoders"]
        nv = rep["nvenc"]
        print()
        print("可用无损压缩方案:")
        for k in C.available_lossless(enc, nv):
            print("  -", C.LOSSLESS_PRESETS[k]["label"])
        print("可用高压缩比方案:")
        for k in C.available_compress(enc, nv):
            print("  -", C.COMPRESS_PRESETS[k]["label"])
        print()
        if rep["missing_required"]:
            print("自检未通过：存在必需组件缺失。")
            return 1
        if not rep["ffmpeg"]:
            print("自检未通过：未找到 ffmpeg。请确认 bin/ 目录完整。")
            return 1
        print("自检通过。")
        return 0

    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
