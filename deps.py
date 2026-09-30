# -*- coding: utf-8 -*-
"""
依赖解析器 — 自带全部运行时依赖，零安装启动。

解析顺序（就近优先）：
  1. 项目内置   <项目根>/bin/            ffmpeg.exe / ffprobe.exe
  2. 项目内置   <项目根>/vendor/         纯 Python 第三方包（免 pip，可随项目分发）
  3. 系统 PATH  shutil.which 兜底
  4. 常见安装位置（仅 ffmpeg）

设计约束：
  - 只依赖标准库本身 + tkinter，第三方包一律可选、缺失即降级。
  - 不修改系统环境、不写注册表、不装全局包。
"""

import os
import sys
import shutil
import subprocess
import importlib.util
import importlib.machinery

if getattr(sys, "frozen", False):                     # PyInstaller 打包后
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

BIN_DIR = os.path.join(APP_DIR, "bin")
VENDOR_DIR = os.path.join(APP_DIR, "vendor")

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


# ------------------------------------------------------------------ vendor 注入

def _install_vendor_path():
    """把项目 vendor/ 目录挂到 sys.path 最前，实现'免 pip 装包'。"""
    if not os.path.isdir(VENDOR_DIR):
        return
    if VENDOR_DIR not in sys.path:
        sys.path.insert(0, VENDOR_DIR)


_install_vendor_path()


def has_module(name):
    """判断模块是否可用（不需要 import，避免副作用）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ------------------------------------------------------------------ 二进制解析

def _probe_exe(path):
    """确认可执行文件真的能跑起来（防止架构不符 / 缺 DLL）。"""
    if not path or not os.path.isfile(path):
        return False
    try:
        r = subprocess.run([path, "-hide_banner", "-version"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_FFMPEG_FALLBACK_DIRS = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ProgramData\chocolatey\bin",
    os.path.join(os.path.expanduser("~"), "scoop", "shims"),
    "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin",
]


def _resolve_tool(stem):
    """按 内置 → PATH → 常见目录 顺序解析一个工具，返回 (路径, 来源)。"""
    # 1) 项目内置
    local = os.path.join(BIN_DIR, stem + EXE)
    if os.path.isfile(local):
        return local, "内置"

    # 2) 系统 PATH
    found = shutil.which(stem)
    if found:
        return found, "系统"

    # 3) 常见安装位置
    for d in _FFMPEG_FALLBACK_DIRS:
        cand = os.path.join(d, stem + EXE)
        if os.path.isfile(cand):
            return cand, "系统"

    return None, "未找到"


def _resolve_tool_verified(stem):
    """解析并验证可执行文件可用；内置不可用时自动降级到系统。"""
    path, origin = _resolve_tool(stem)
    if path and origin == "内置" and _probe_exe(path):
        return path, origin
    if path and origin == "内置":
        # 内置的跑不起来（架构/损坏），降级
        for d in _FFMPEG_FALLBACK_DIRS:
            cand = os.path.join(d, stem + EXE)
            if _probe_exe(cand):
                return cand, "系统(内置不可用)"
        found = shutil.which(stem)
        if found and _probe_exe(found):
            return found, "系统(内置不可用)"
        return path, "内置(无法运行)"
    if path and _probe_exe(path):
        return path, origin
    return (path, origin) if path else (None, "未找到")


FFMPEG, FFMPEG_ORIGIN = _resolve_tool_verified("ffmpeg")
FFPROBE, FFPROBE_ORIGIN = _resolve_tool_verified("ffprobe")


# ------------------------------------------------------------------ 能力探测

def ffmpeg_encoders():
    """返回可用编码器集合（小写）。失败返回空集合。"""
    if not FFMPEG:
        return set()
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return set()
    out = set()
    for line in r.stdout.splitlines():
        # 形如 " V....D libx264   H.264 ..."
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            out.add(parts[1].lower())
    return out


def ffmpeg_has(encoder):
    return encoder.lower() in ffmpeg_encoders()


def nvenc_available():
    """粗判 NVENC 是否可用（有编码器 + 能真正初始化一帧）。

    注意：测试尺寸必须 >= 145x49（NVENC 硬性下限），用 128x128 会被拒绝，
    导致误判为'不可用'。这里用 256x256 并显式给 rate/pix_fmt。
    """
    if not FFMPEG or not ffmpeg_has("h264_nvenc"):
        return False
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=black:s=256x256:r=25:d=0.2",
             "-c:v", "h264_nvenc", "-pix_fmt", "yuv420p", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ------------------------------------------------------------------ 自检汇总

# 可选第三方包：缺失只降级，不阻塞
OPTIONAL_MODULES = {
    "PIL": "图片拖放预览 / 截图（可选）",
    "psutil": "进程资源监控（可选）",
}

# 必需：标准库自带，这里只做存在性确认
REQUIRED_MODULES = {
    "tkinter": "图形界面（Python 自带，若缺失需重装 Python 并勾选 tcl/tk）",
    "json": "配置读写",
    "subprocess": "调用 ffmpeg",
    "threading": "后台任务",
}


def bin_ready():
    """bin/ 里是否已有可用的内置二进制。

    仓库不携带 ffmpeg（单文件 145MB，超 GitHub/Gitee 的 100MB 限制），
    克隆后需要跑一次 fetch_ffmpeg.py 补齐。这里用来区分「用户没下载」
    和「环境真的坏了」，给出不同的提示。
    """
    for name in ("ffmpeg" + EXE, "ffprobe" + EXE):
        p = os.path.join(BIN_DIR, name)
        if not os.path.isfile(p) or os.path.getsize(p) < 1024 * 1024:
            return False
    return True


FETCH_HINT = ("bin/ 里没有内置 ffmpeg —— 本仓库不含它（单个 145MB，超过平台\n"
              "  单文件限制）。跑一次下面这条命令即可自动下载，之后功能照常：\n"
              "      python fetch_ffmpeg.py\n"
              "  需要代理时：python fetch_ffmpeg.py --proxy http://127.0.0.1:17891")


def check_all():
    """返回完整自检报告 dict。"""
    report = {
        "app_dir": APP_DIR,
        "bin_dir": BIN_DIR,
        "vendor_dir": VENDOR_DIR,
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "ffmpeg": FFMPEG,
        "ffmpeg_origin": FFMPEG_ORIGIN,
        "ffprobe": FFPROBE,
        "ffprobe_origin": FFPROBE_ORIGIN,
        "missing_required": [],
        "missing_optional": [],
        "vendor_present": os.path.isdir(VENDOR_DIR),
        "bin_ready": bin_ready(),
        "fetch_hint": None,
        "nvenc": None,
    }
    # 内置二进制缺失且系统也没有 ffmpeg → 明确告诉用户去下载，而不是含糊的"未找到"
    if not report["bin_ready"] and not FFMPEG:
        report["fetch_hint"] = FETCH_HINT
    elif not report["bin_ready"]:
        report["fetch_hint"] = ("bin/ 里没有内置 ffmpeg（当前用的是系统版本）。"
                                "想恢复自带依赖，跑：python fetch_ffmpeg.py")
    for mod, desc in REQUIRED_MODULES.items():
        if not has_module(mod):
            report["missing_required"].append((mod, desc))
    for mod, desc in OPTIONAL_MODULES.items():
        if not has_module(mod):
            report["missing_optional"].append((mod, desc))

    if FFMPEG:
        report["encoders"] = ffmpeg_encoders()
        report["nvenc"] = nvenc_available()
    else:
        report["encoders"] = set()
        report["nvenc"] = False
    return report


def format_report(rep):
    """把报告格式化成可读文本（用于日志 / 无 GUI 模式）。"""
    L = []
    a = L.append
    a(f"程序目录   : {rep['app_dir']}")
    a(f"Python     : {rep['python']}  ({rep['python_exe']})")
    a(f"内置 bin/  : {'就绪 ✓' if rep.get('bin_ready') else '未就绪（缺 ffmpeg/ffprobe）'}")
    a(f"内置 vendor/: {'存在' if rep['vendor_present'] else '不存在'}")
    a("")
    for name, key, okey in (("ffmpeg", "ffmpeg", "ffmpeg_origin"),
                            ("ffprobe", "ffprobe", "ffprobe_origin")):
        p = rep[key]
        if p:
            size = os.path.getsize(p) / 1024 / 1024 if os.path.isfile(p) else 0
            a(f"{name:10} : [{rep[okey]}] {p}" + (f"  ({size:.1f} MB)" if size else ""))
        else:
            a(f"{name:10} : ✗ 未找到")
    a("")
    if rep.get("fetch_hint"):
        a("提示：")
        for line in rep["fetch_hint"].splitlines():
            a(f"  {line}")
        a("")
    if rep["missing_required"]:
        a("必需组件缺失：")
        for mod, desc in rep["missing_required"]:
            a(f"  ✗ {mod:12} {desc}")
    else:
        a("必需组件   : 全部就绪 ✓")
    if rep["missing_optional"]:
        a("可选组件缺失（不影响使用）：")
        for mod, desc in rep["missing_optional"]:
            a(f"  - {mod:12} {desc}")
    if rep.get("nvenc"):
        a("硬件加速   : NVENC 可用 ✓")
    elif rep.get("ffmpeg"):
        a("硬件加速   : NVENC 不可用（将使用 CPU 软编）")
    return "\n".join(L)


def main():
    rep = check_all()
    print(format_report(rep))
    print()
    if rep["missing_required"]:
        print("自检未通过：存在必需组件缺失。")
        return 1
    if not rep["ffmpeg"]:
        print("自检未通过：未找到 ffmpeg。")
        if rep.get("fetch_hint"):
            print()
            print(FETCH_HINT)
        return 1
    if not rep.get("bin_ready") and rep.get("fetch_hint"):
        print("自检通过（用的是系统 ffmpeg）。")
        print(rep["fetch_hint"])
        return 0
    print("自检通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
