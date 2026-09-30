# -*- coding: utf-8 -*-
"""
格式转换模块 —— 容器互转、编码转换、音视频提取/剥离。

三类操作：
  1. 转换 (convert)   : 换容器 + 按需重编码
  2. 提取 (extract)   : 从已有文件里抽出音频 / 抽出纯视频 / 只换容器
  3. 音频输出 (audio) : 视频 → 纯音频文件

设计要点：
  - 容器与编码强绑定（如 mp4 装不了 flac、webm 只认 vp8/vp9/av1 + vorbis/opus），
    所以 TARGETS 里每个目标都显式声明它允许的编码，UI 只给出合法组合。
  - "stream copy" 是最快路径，只在目标容器不支持源编码时才要求重编码。
"""

# ---- 容器能力表 ----------------------------------------------------------
# container -> {ext, label, kind, vcodecs(允许的视频编码), acodecs, note}
# kind: 'video' 带视频 | 'audio' 仅音频

VIDEO_TARGETS = {
    "mp4": {
        "ext": ".mp4", "label": "MP4 (H.264/H.265 + AAC) — 最通用",
        "v": ["libx264", "libx265", "h264_nvenc", "hevc_nvenc", "mpeg4"],
        "a": ["aac", "libmp3lame", "ac3", "copy", "none"],
        "default_v": "libx264", "default_a": "aac",
        "note": "手机/网页/播放器兼容性最好",
    },
    "mkv": {
        "ext": ".mkv", "label": "MKV (Matroska) — 什么都能装",
        "v": ["libx264", "libx265", "libvpx-vp9", "av1_nvenc", "mpeg4", "ffv1", "copy", "none"],
        "a": ["aac", "libmp3lame", "libopus", "flac", "ac3", "copy", "none"],
        "default_v": "libx264", "default_a": "aac",
        "note": "万能容器，支持多音轨/字幕",
    },
    "mov": {
        "ext": ".mov", "label": "MOV (QuickTime)",
        "v": ["libx264", "libx265", "prores", "h264_nvenc", "hevc_nvenc", "copy", "none"],
        "a": ["aac", "pcm_s16le", "ac3", "copy", "none"],
        "default_v": "libx264", "default_a": "aac",
        "note": "苹果生态 / 剪辑软件",
    },
    "webm": {
        "ext": ".webm", "label": "WebM (VP9/AV1 + Opus) — 网页",
        "v": ["libvpx-vp9", "libaom-av1", "av1_nvenc"],
        "a": ["libopus", "libvorbis", "none"],
        "default_v": "libvpx-vp9", "default_a": "libopus",
        "note": "浏览器原生支持，开源免专利",
    },
    "avi": {
        "ext": ".avi", "label": "AVI (老设备/老播放器)",
        "v": ["mpeg4", "libx264", "mjpeg", "copy", "none"],
        "a": ["libmp3lame", "pcm_s16le", "ac3", "copy", "none"],
        "default_v": "mpeg4", "default_a": "libmp3lame",
        "note": "兼容老设备，不支持 H.265",
    },
    "flv": {
        "ext": ".flv", "label": "FLV (Flash/直播)",
        "v": ["libx264", "copy", "none"],
        "a": ["aac", "libmp3lame", "copy", "none"],
        "default_v": "libx264", "default_a": "aac",
        "note": "老式网页播放 / 推流",
    },
    "ts": {
        "ext": ".ts", "label": "MPEG-TS (电视录制/流)",
        "v": ["libx264", "libx265", "mpeg2video", "copy", "none"],
        "a": ["aac", "ac3", "libmp3lame", "copy", "none"],
        "default_v": "libx264", "default_a": "aac",
        "note": "广播电视、IPTV 录制",
    },
    "gif": {
        "ext": ".gif", "label": "GIF 动图（无声音）",
        "v": ["gif"], "a": ["none"],
        "default_v": "gif", "default_a": "none",
        "note": "无声动图，建议配合降低分辨率与帧率",
    },
    "m4v": {
        "ext": ".m4v", "label": "M4V (iTunes)",
        "v": ["libx264", "libx265", "copy", "none"],
        "a": ["aac", "ac3", "copy", "none"],
        "default_v": "libx264", "default_a": "aac",
        "note": "iTunes / 苹果设备",
    },
    "3gp": {
        "ext": ".3gp", "label": "3GP (老式手机)",
        "v": ["mpeg4", "libx264", "none"],
        "a": ["aac", "libmp3lame", "none"],
        "default_v": "mpeg4", "default_a": "aac",
        "note": "老式功能机",
    },
}

AUDIO_TARGETS = {
    "mp3": {
        "ext": ".mp3", "label": "MP3 (兼容性最好)",
        "a": ["libmp3lame", "copy"],
        "default_a": "libmp3lame",
        "note": "谁都能放",
    },
    "m4a": {
        "ext": ".m4a", "label": "M4A / AAC (苹果/安卓通用)",
        "a": ["aac", "copy"],
        "default_a": "aac",
        "note": "同码率音质优于 MP3",
    },
    "aac": {
        "ext": ".aac", "label": "AAC 裸流 (ADTS)",
        "a": ["aac", "copy"],
        "default_a": "aac",
        "note": "裸流，无容器",
    },
    "flac": {
        "ext": ".flac", "label": "FLAC (无损压缩)",
        "a": ["flac"],
        "default_a": "flac",
        "note": "无损，体积约为 WAV 一半",
    },
    "wav": {
        "ext": ".wav", "label": "WAV (PCM 无压缩)",
        "a": ["pcm_s16le", "pcm_s24le"],
        "default_a": "pcm_s16le",
        "note": "无压缩，体积很大，适合后期处理",
    },
    "opus": {
        "ext": ".opus", "label": "Opus (同码率音质最好)",
        "a": ["libopus"],
        "default_a": "libopus",
        "note": "现代语音/音乐编码",
    },
    "ogg": {
        "ext": ".ogg", "label": "OGG Vorbis",
        "a": ["libvorbis"],
        "default_a": "libvorbis",
        "note": "开源格式",
    },
    "ac3": {
        "ext": ".ac3", "label": "AC3 (杜比数字)",
        "a": ["ac3"],
        "default_a": "ac3",
        "note": "家庭影院/环绕声",
    },
    "mka": {
        "ext": ".mka", "label": "MKA (Matroska 音频容器)",
        "a": ["flac", "libopus", "aac", "libmp3lame", "copy"],
        "default_a": "flac",
        "note": "可装多音轨/字幕",
        "multi_stream": True,
    },
}

# 能同时容纳视频轨的音频容器（目前只有 Matroska 音频 MKA）。
# 其余都是裸流格式，塞视频轨会报 "Could not write header"。
MULTI_STREAM_AUDIO_TARGETS = {k for k, v in AUDIO_TARGETS.items() if v.get("multi_stream")}

ALL_TARGETS = {}
ALL_TARGETS.update(VIDEO_TARGETS)
ALL_TARGETS.update(AUDIO_TARGETS)

# 编码显示名
CODEC_LABEL = {
    "copy": "不重编码（直接拷贝·最快）",
    "none": "不含此轨（去掉）",
    "libx264": "H.264 (x264·软编)",
    "libx265": "H.265/HEVC (x265·软编)",
    "h264_nvenc": "H.264 (NVENC·显卡)",
    "hevc_nvenc": "H.265 (NVENC·显卡)",
    "av1_nvenc": "AV1 (NVENC·显卡)",
    "libvpx-vp9": "VP9 (libvpx)",
    "libaom-av1": "AV1 (libaom·慢)",
    "mpeg4": "MPEG-4 Part 2",
    "mpeg2video": "MPEG-2",
    "mjpeg": "MJPEG",
    "prores": "Apple ProRes",
    "gif": "GIF",
    "ffv1": "FFV1 (无损)",
    "aac": "AAC",
    "libmp3lame": "MP3 (LAME)",
    "libopus": "Opus",
    "libvorbis": "Vorbis",
    "flac": "FLAC (无损)",
    "ac3": "AC3",
    "pcm_s16le": "PCM 16-bit",
    "pcm_s24le": "PCM 24-bit",
}


def codec_label(c):
    return CODEC_LABEL.get(c, c)


# 编码器名（ffmpeg -encoders）→ 编解码器名（ffprobe codec_name）的映射。
# 容器能力表里存的是"编码器"，而 ffprobe 报的是"编解码器"，两套命名不可直接比。
ENCODER_TO_CODEC = {
    "libx264": "h264", "libx265": "hevc",
    "h264_nvenc": "h264", "hevc_nvenc": "hevc", "av1_nvenc": "av1",
    "libvpx-vp9": "vp9", "libaom-av1": "av1", "mpeg4": "mpeg4",
    "mpeg2video": "mpeg2video", "mjpeg": "mjpeg", "prores": "prores",
    "gif": "gif", "ffv1": "ffv1",
    "libmp3lame": "mp3", "libopus": "opus", "libvorbis": "vorbis",
    "pcm_s16le": "pcm_s16le", "pcm_s24le": "pcm_s24le",
    "aac": "aac", "flac": "flac", "ac3": "ac3",
}


def _codec_names(allowed):
    """把容器能力表里的编码器名集合，展开成可用于比对 ffprobe 的编解码器名集合。"""
    out = set()
    for a in allowed:
        out.add(a)
        if a in ENCODER_TO_CODEC:
            out.add(ENCODER_TO_CODEC[a])
    return out


# 需要显式 -map 才不自动附加额外流的容器（否则可能产出重复轨道）
NEEDS_STRICT_MAP = {"ts", "mpegts", "mpeg", "vob"}


# ---- 提取模式 -----------------------------------------------------------

EXTRACT_MODES = {
    "convert": {
        "label": "转换格式（完整转码）",
        "desc": "按目标格式重编码音视频，最通用的转换方式",
        "needs_audio": False,
    },
    "remux": {
        "label": "仅换容器（不重编码·秒级）",
        "desc": "原样搬运音视频流到新容器，画质零损失",
        "needs_audio": False,
    },
    "audio_from_video": {
        "label": "只提取音频（丢掉画面）",
        "desc": "把视频里的音轨导成 MP3/AAC/FLAC 等音频文件",
        "needs_audio": True,
    },
    "video_only": {
        "label": "只保留画面（去掉声音）",
        "desc": "丢掉音轨，输出无声视频",
        "needs_audio": False,
    },
    "audio_only": {
        "label": "仅转换音频格式（保留画面·换音频编码）",
        "desc": "画面原样拷贝不重编码，只把音轨换成别的格式（需容器支持，如 MKV/MKA）",
        "needs_audio": True,
    },
}


# ---- 命令构造 -----------------------------------------------------------

def _vcodec_args(vcodec, crf, preset, hw_preset="p5"):
    """返回视频编码参数列表。"""
    if vcodec in ("copy", "none"):
        return []
    args = ["-c:v", vcodec]
    if vcodec in ("libx264", "libx265"):
        args += ["-preset", preset, "-crf", str(crf)]
    elif vcodec in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
        args += ["-preset", hw_preset, "-cq", str(crf)]
    elif vcodec == "libvpx-vp9":
        args += ["-crf", str(crf), "-b:v", "0"]
    elif vcodec == "libaom-av1":
        args += ["-crf", str(crf), "-b:v", "0", "-cpu-used", "4"]
    elif vcodec == "prores":
        args += ["-profile:v", "3"]
    elif vcodec == "gif":
        args += ["-loop", "0"]
    if vcodec not in ("gif", "prores", "ffv1"):
        args += ["-pix_fmt", "yuv420p"]
    return args


def _acodec_args(acodec, abitrate):
    if acodec in ("copy", "none"):
        return []
    args = ["-c:a", acodec]
    if acodec in ("aac", "libmp3lame", "libopus", "libvorbis", "ac3"):
        args += ["-b:a", abitrate]
    return args


def build_convert_cmd(ffmpeg, src, dst, target,
                      vcodec=None, acodec=None, crf=23, preset="medium",
                      abitrate="192k", scale=None, fps=None, has_audio=True):
    """
    构造「转换」命令：换容器 + 按需重编码。
    target 为 ALL_TARGETS 的 key。
    """
    tgt = ALL_TARGETS.get(target)
    if not tgt:
        raise ValueError(f"未知目标格式: {target}")
    is_audio_only = target in AUDIO_TARGETS

    if vcodec is None:
        vcodec = tgt.get("default_v", "none")
    if acodec is None:
        acodec = tgt.get("default_a", "aac")

    cmd = [ffmpeg, "-hide_banner", "-y", "-i", src]

    if is_audio_only:
        # 纯音频目标：只取音轨。
        # 注意 .opus / .aac / .mp3 等裸流 muxer 只允许一条音频流，
        # 必须同时 -vn 去掉视频，否则会报 "Nothing was written into output file"。
        cmd += ["-vn", "-map", "0:a:0"]
        cmd += ["-c:a", acodec] if acodec != "copy" else ["-c:a", "copy"]
        if acodec in ("aac", "libmp3lame", "libopus", "libvorbis", "ac3"):
            cmd += ["-b:a", abitrate]
        if target in ("aac", "mp3", "ac3") and target != "mp3":
            cmd += ["-f", {"aac": "adts", "ac3": "ac3"}.get(target, target)]
        cmd += ["-map_metadata", "0", dst]
        return cmd, None

    if vcodec == "none":
        return None, "目标格式需要视频轨，但视频编码被设为「不含此轨」"
    # 用 -map 显式指定；MPEG-TS 等容器在显式映射下仍会附加默认流而产出重复轨道，
    # 所以这里再叠一层 -map 覆盖 + 去掉自动映射的写法（见 NEEDS_STRICT_MAP）。
    cmd += ["-map", "0:v:0"]
    if has_audio and acodec != "none":
        cmd += ["-map", "0:a:0"]
    cmd += _vcodec_args(vcodec, crf, preset)
    if has_audio and acodec != "none":
        cmd += _acodec_args(acodec, abitrate)

    vf = []
    if scale:
        vf.append(f"scale={scale}:flags=lanczos")
    if fps:
        vf.append(f"fps={fps}")
    if vf and vcodec != "copy":
        cmd += ["-vf", ",".join(vf)]

    cmd += ["-map_metadata", "0", "-map_chapters", "0"]
    if target in ("mp4", "mov", "m4v"):
        cmd += ["-movflags", "+faststart"]
    cmd += [dst]
    return cmd, None


def build_extract_cmd(ffmpeg, src, dst, mode, target=None,
                      vcodec=None, acodec=None, crf=23, preset="medium",
                      abitrate="192k", has_audio=True):
    """
    构造「提取」命令。
    mode: audio_from_video / video_only / remux / audio_only
    """
    if mode == "audio_from_video":
        if not has_audio:
            return None, "源文件没有音轨，无法提取音频"
        # 这里必须校验 target 是否真的是音频格式：GUI 的音频下拉框只放音频格式，
        # 但 CLI/agent 可能传 mp4 进来，静默 fallback 到 mp3 会让输出扩展名与
        # 实际编码不符（曾经真的产出过 mp4 容器、mp3 编码的错配文件）。
        if target is not None and target not in AUDIO_TARGETS:
            return None, f"--format {target} 不是音频格式，无法提取音频"
        at = target or "mp3"
        tgt = AUDIO_TARGETS[at]
        codec = acodec or tgt["default_a"]
        cmd = [ffmpeg, "-hide_banner", "-y", "-i", src, "-vn", "-map", "0:a:0"]
        cmd += ["-c:a", codec] if codec != "copy" else ["-c:a", "copy"]
        if codec in ("aac", "libmp3lame", "libopus", "libvorbis", "ac3"):
            cmd += ["-b:a", abitrate]
        if at in ("aac", "ac3"):
            cmd += ["-f", {"aac": "adts", "ac3": "ac3"}[at]]
        cmd += ["-map_metadata", "0", dst]
        return cmd, None

    if mode == "video_only":
        if target is None or target not in VIDEO_TARGETS:
            return None, "请选择视频目标格式"
        tgt = VIDEO_TARGETS[target]
        vc = vcodec or tgt["default_v"]
        if vc == "none":
            vc = tgt["default_v"]
        cmd = [ffmpeg, "-hide_banner", "-y", "-i", src, "-map", "0:v:0", "-an"]
        cmd += _vcodec_args(vc, crf, preset)
        cmd += ["-map_metadata", "0", "-map_chapters", "0"]
        if target in ("mp4", "mov", "m4v"):
            cmd += ["-movflags", "+faststart"]
        cmd += [dst]
        return cmd, None

    if mode == "remux":
        if target is None:
            return None, "请选择目标格式"
        cmd = [ffmpeg, "-hide_banner", "-y", "-i", src, "-map", "0", "-c", "copy"]
        cmd += ["-map_metadata", "0", "-map_chapters", "0"]
        if target in ("mp4", "mov", "m4v"):
            cmd += ["-movflags", "+faststart"]
        cmd += [dst]
        return cmd, None

    if mode == "audio_only":
        if not has_audio:
            return None, "源文件没有音轨"
        if target is None or target not in AUDIO_TARGETS:
            return None, "请选择音频目标格式"
        if target not in MULTI_STREAM_AUDIO_TARGETS:
            # 裸流音频格式（.opus/.mp3/.aac/.flac/.wav/.ogg/.ac3）无法容纳视频轨，
            # 硬塞会报 "Could not write header"。提示改用 MKA 或 MKV。
            return None, (f"{target.upper()} 是裸音频格式，无法同时保留视频轨。\n"
                          f"想保留画面请选 MKA 或 MKV 容器。")
        tgt = AUDIO_TARGETS[target]
        ac = acodec or tgt["default_a"]
        cmd = [ffmpeg, "-hide_banner", "-y", "-i", src,
               "-map", "0:v:0", "-map", "0:a:0"]
        # 视频原样拷贝（不重编码），只转音频
        cmd += ["-c:v", "copy"]
        cmd += ["-c:a", ac] if ac != "copy" else ["-c:a", "copy"]
        if ac in ("aac", "libmp3lame", "libopus", "libvorbis", "ac3"):
            cmd += ["-b:a", abitrate]
        cmd += ["-map_metadata", "0", dst]
        return cmd, None

    return None, f"未知提取模式: {mode}"


# ---- 兼容性检查 ---------------------------------------------------------

def check_compat(src_info, target, mode="convert"):
    """
    检查源能否装进目标格式。返回 (warnings:list, must_reencode:bool)。
    仅在「仅换容器」时才有硬性不兼容问题。

    注意：容器能力表里存的是**编码器名**（libx264），ffprobe 报的是**编解码器名**（h264），
    必须经 ENCODER_TO_CODEC 换名后再比，否则会把 MKV 误判成不支持 H.264。
    """
    warnings = []
    if not src_info or target is None:
        return warnings, False

    # 兼容两种调用方：
    #   GUI 传的是 ffprobe 原始结构（流字段是 codec_type）
    #   avtool CLI 传的是整理后的结构（流字段是 type，编码在 codec）
    # 只认一种会让另一侧的兼容检查静默失效（曾导致 remux→webm 不报警）。
    def _streams():
        for s in src_info.get("streams", []) or []:
            kind = s.get("codec_type") or s.get("type")
            codec = s.get("codec_name") or s.get("codec") or ""
            disp = s.get("disposition") or {}
            yield kind, codec, bool(disp.get("attached_pic"))

    v_codecs = [c for k, c, cover in _streams() if k == "video" and not cover]
    a_codecs = [c for k, c, _ in _streams() if k == "audio"]

    if target in VIDEO_TARGETS:
        tgt = VIDEO_TARGETS[target]
        allowed_v = _codec_names(tgt["v"])
        for c in v_codecs:
            if c and c not in allowed_v and tgt["v"]:
                warnings.append(f"目标容器 {target.upper()} 不兼容源视频编码 {c.upper()}，"
                                f"「仅换容器」会失败，请改用重编码")
                return warnings, True
        allowed_a = _codec_names(tgt["a"])
        for c in a_codecs:
            if c and c not in allowed_a and tgt["a"]:
                warnings.append(f"目标容器 {target.upper()} 不兼容源音频编码 {c.upper()}，"
                                f"「仅换容器」会失败，请改用重编码")
                return warnings, True
    elif target in AUDIO_TARGETS:
        tgt = AUDIO_TARGETS[target]
        allowed_a = _codec_names(tgt["a"])
        for c in a_codecs:
            if c and c not in allowed_a:
                warnings.append(f"目标格式 {target.upper()} 不兼容源音频编码 {c.upper()}")
                return warnings, True
    return warnings, False


def human_size(n):
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} PB"


def ratio_text(src_size, dst_size):
    if not src_size or not dst_size:
        return "—", "—"
    r = src_size / dst_size
    delta = (dst_size - src_size) / src_size
    if delta < -0.001:
        d = f"省 {abs(delta) * 100:.1f}%"
    elif delta > 0.001:
        d = f"增 {delta * 100:.1f}%"
    else:
        d = "基本持平"
    return f"{r:.2f}x", d
