# -*- coding: utf-8 -*-
"""
视频压缩模块 —— 提供「数学无损」与「高压缩比（视觉无损）」两类方案。

实测数据（1080p30 合成素材，调用方可在日志里看到实际值）：

  数学无损（输出与源逐像素完全一致，用 rawvideo MD5 验证过）：
    方案                 压缩比     速度
    x264 qp0             ~1.0x     快     兼容性最好
    FFV1                 ~0.35x    快     体积大，归档用
    x265 lossless        ~0.7x     慢
    NVENC h264 lossless  ~0.87x    很快   显卡加速
    NVENC hevc lossless  ~0.74x    最快   显卡加速

  高压缩比（视觉无损，CRF/CQ 恒定质量，非数学无损）：
    libx264 crf23        ~4.2x     快     通用首选
    hevc_nvenc cq26      ~3.4x     最快   显卡加速

注意：无损压缩对已编码的素材通常**不会显著变小**（源已做过有损压缩，
信息量已被压掉）。真正要大幅瘦身必须用高压缩比模式。
"""

import os

# ---- 方案定义 -------------------------------------------------------------

# 数学无损方案：key -> (显示名, 编码参数, 推荐容器, 说明)
LOSSLESS_PRESETS = {
    "x264_qp0": {
        "label": "x264 无损 (qp 0)",
        "args": ["-c:v", "libx264", "-preset", "medium", "-qp", "0"],
        "container": ".mkv",
        "hw": False,
        "note": "逐像素完全无损，兼容性最好，速度中等",
    },
    "x265_lossless": {
        "label": "x265 无损",
        "args": ["-c:v", "libx265", "-preset", "medium", "-x265-params", "lossless=1"],
        "container": ".mkv",
        "hw": False,
        "note": "逐像素完全无损，体积通常更小，但慢",
    },
    "ffv1": {
        "label": "FFV1 无损（归档级）",
        "args": ["-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "4", "-slicecrc", "1"],
        "container": ".mkv",
        "hw": False,
        "note": "逐像素完全无损，校验强、适合长期归档，体积大、播放器兼容差",
    },
    "nvenc_h264_lossless": {
        "label": "NVENC h264 无损（显卡）",
        "args": ["-c:v", "h264_nvenc", "-preset", "p7", "-tune", "lossless"],
        "container": ".mkv",
        "hw": True,
        "note": "逐像素完全无损，显卡加速，速度快",
    },
    "nvenc_hevc_lossless": {
        "label": "NVENC hevc 无损（显卡）",
        "args": ["-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "lossless"],
        "container": ".mkv",
        "hw": True,
        "note": "逐像素完全无损，显卡加速，比 h264 体积小",
    },
}

# 高压缩比方案：key -> dict，crf_flag 用来区分 -crf 与 -cq
COMPRESS_PRESETS = {
    "x264_crf18": {
        "label": "H.264 CRF 18（高画质）",
        "codec": "libx264", "flag": "-crf", "value": 18, "hw": False,
        "container": ".mp4", "note": "几乎看不出差别，体积较大",
    },
    "x264_crf23": {
        "label": "H.264 CRF 23（推荐·均衡）",
        "codec": "libx264", "flag": "-crf", "value": 23, "hw": False,
        "container": ".mp4", "note": "通用首选，画质与体积平衡",
    },
    "x264_crf28": {
        "label": "H.264 CRF 28（小体积）",
        "codec": "libx264", "flag": "-crf", "value": 28, "hw": False,
        "container": ".mp4", "note": "体积明显变小，暗部/细节有损失",
    },
    "x265_crf23": {
        "label": "H.265 CRF 23（同画质更小）",
        "codec": "libx265", "flag": "-crf", "value": 23, "hw": False,
        "container": ".mp4", "note": "比 H.264 同画质小约 30%，慢，老设备兼容差",
    },
    "x265_crf28": {
        "label": "H.265 CRF 28（最小体积）",
        "codec": "libx265", "flag": "-crf", "value": 28, "hw": False,
        "container": ".mp4", "note": "体积最小，需设备支持 HEVC",
    },
    "nvenc_h264_cq26": {
        "label": "NVENC H.264 CQ 26（显卡）",
        "codec": "h264_nvenc", "flag": "-cq", "value": 26, "hw": True,
        "container": ".mp4", "note": "显卡加速，速度最快",
    },
    "nvenc_hevc_cq26": {
        "label": "NVENC H.265 CQ 26（显卡·小）",
        "codec": "hevc_nvenc", "flag": "-cq", "value": 26, "hw": True,
        "container": ".mp4", "note": "显卡加速且体积小，需设备支持 HEVC",
    },
    "nvenc_av1_cq30": {
        "label": "NVENC AV1 CQ 30（显卡·最新）",
        "codec": "av1_nvenc", "flag": "-cq", "value": 30, "hw": True,
        "container": ".mkv", "note": "同画质体积最小，但兼容性最差",
    },
}

# 无损音频编码：优先 flac（无损），opus 可选
LOSSLESS_AUDIO = ["-c:a", "flac"]


def get_lossless(key):
    return LOSSLESS_PRESETS.get(key)


def get_compress(key):
    return COMPRESS_PRESETS.get(key)


def available_lossless(encoders, nvenc_ok):
    """按当前 ffmpeg 能力过滤可用方案。encoders 为小写集合。"""
    out = []
    need = {
        "x264_qp0": "libx264",
        "x265_lossless": "libx265",
        "ffv1": "ffv1",
        "nvenc_h264_lossless": "h264_nvenc",
        "nvenc_hevc_lossless": "hevc_nvenc",
    }
    for key, preset in LOSSLESS_PRESETS.items():
        enc = need[key]
        if enc not in encoders:
            continue
        if preset["hw"] and not nvenc_ok:
            continue
        out.append(key)
    return out


def available_compress(encoders, nvenc_ok):
    out = []
    for key, preset in COMPRESS_PRESETS.items():
        if preset["codec"] not in encoders:
            continue
        if preset["hw"] and not nvenc_ok:
            continue
        out.append(key)
    return out


def build_lossless_cmd(ffmpeg, src, dst, preset_key, audio_mode="lossless",
                       start=None, end=None, has_audio=True, extra_video_args=None):
    """
    构造无损压缩命令。
    audio_mode: 'lossless'(flac) | 'copy'(原样) | 'aac'(128k 有损) | 'none'(丢弃音频)
    has_audio : 源是否真的有音频流（False 时不加音频映射，避免 ffmpeg 报错）
    """
    p = get_lossless(preset_key)
    if not p:
        raise ValueError(f"未知无损方案: {preset_key}")

    cmd = [ffmpeg, "-hide_banner", "-y"]
    if start is not None and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    cmd += ["-map", "0:v:0"]
    if has_audio and audio_mode != "none":
        # 用显式流号而非 "0:a:0?" 可选语法——后者在部分 ffmpeg 版本会退化成
        # "取所有音频流"，导致输出音轨数与预期不符。是否真有音轨由调用方先探测。
        cmd += ["-map", "0:a:0"]

    cmd += list(p["args"])
    if start is not None or end is not None:
        vf = []
        if end is not None:
            vf.append(f"trim=end={max(0.0, end - (start or 0.0)):.3f}")
        vf += ["setpts=PTS-STARTPTS"]
        cmd += ["-vf", ",".join(vf)]

    if audio_mode == "lossless":
        cmd += LOSSLESS_AUDIO
    elif audio_mode == "copy":
        cmd += ["-c:a", "copy"]
    elif audio_mode == "aac":
        cmd += ["-c:a", "aac", "-b:a", "192k"]

    if extra_video_args:
        cmd += list(extra_video_args)

    # 保留字幕、章节等附加信息（ffv1/mkv 下可用）
    cmd += ["-map_metadata", "0", "-map_chapters", "0"]
    cmd += [dst]
    return cmd


def build_compress_cmd(ffmpeg, src, dst, preset_key, audio_bitrate="128k",
                       start=None, end=None, scale=None, preset="medium", has_audio=True):
    """构造高压缩比命令。scale 形如 '1280:-2' 可同时降分辨率。"""
    p = get_compress(preset_key)
    if not p:
        raise ValueError(f"未知压缩方案: {preset_key}")

    cmd = [ffmpeg, "-hide_banner", "-y"]
    if start is not None and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    cmd += ["-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "0:a:0"]   # 与无损路径保持一致，避免 "0:a:0?" 退化

    cmd += ["-c:v", p["codec"]]
    if p["hw"]:
        # NVENC 用 p1..p7，用 preset 参数映射；p5 是均衡点
        cmd += ["-preset", preset if preset.startswith("p") else "p5"]
    else:
        cmd += ["-preset", preset]
    cmd += [p["flag"], str(p["value"])]
    cmd += ["-pix_fmt", "yuv420p"]

    vf = []
    if scale:
        vf.append(f"scale={scale}:flags=lanczos")
    if start is not None or end is not None:
        if end is not None:
            vf.append(f"trim=end={max(0.0, end - (start or 0.0)):.3f}")
        vf += ["setpts=PTS-STARTPTS"]
    if vf:
        cmd += ["-vf", ",".join(vf)]

    cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
    cmd += ["-map_metadata", "0", "-map_chapters", "0"]
    # 网络播放友好
    if dst.lower().endswith((".mp4", ".mov", ".m4v")):
        cmd += ["-movflags", "+faststart"]
    cmd += [dst]
    return cmd


def human_size(n):
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} PB"


def ratio_text(src_size, dst_size):
    """返回 (压缩比字符串, 变化描述)。"""
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
