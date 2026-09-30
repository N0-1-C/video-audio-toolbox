# -*- coding: utf-8 -*-
"""
下载内置 ffmpeg / ffprobe 到 bin/ 目录。

仓库里**不含**这两个二进制 —— 它们是静态链接的完整构建，单个 145MB，
超过 GitHub / Gitee 单文件 100MB 的硬限制，无法随仓库分发。
克隆本仓库后跑一次本脚本即可补齐，之后所有功能照常使用。

用法：
    python fetch_ffmpeg.py                    # 下载到 bin/
    python fetch_ffmpeg.py --check            # 只检查现有二进制是否可用
    python fetch_ffmpeg.py --proxy http://127.0.0.1:17891
    python fetch_ffmpeg.py --source gyan      # 换备用下载源
    python fetch_ffmpeg.py --force            # 强制重新下载

下载源：
    btb  BtbN/FFmpeg-Builds（默认，GitHub Release，GPL 静态构建）
         https://github.com/BtbN/FFmpeg-Builds/releases/latest
    gyan gyan.dev（备用，无代理也能访问的镜像）
         https://www.gyan.dev/ffmpeg/builds/

只用标准库，不需要 pip 安装任何东西。
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(APP_DIR, "bin")

SOURCES = {
    "btbn": {
        "url": ("https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
                "ffmpeg-master-latest-win64-gpl.zip"),
        "label": "BtbN/FFmpeg-Builds (GitHub Release, GPL 静态构建)",
        # 解压后二进制所在的子目录前缀（各级目录名里包含这段）
        "inner": "bin",
        "min_zip_mb": 40,       # 低于此值视为下载不完整
    },
    "gyan": {
        "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "label": "gyan.dev (essentials 静态构建)",
        "inner": "bin",
        "min_zip_mb": 20,
    },
}

WANT = ("ffmpeg.exe", "ffprobe.exe")
UA = "av-toolbox-fetch/1.0"


# ------------------------------------------------------------------ 检查

def probe_exe(path):
    """实跑 -version 确认可执行。"""
    if not os.path.isfile(path):
        return False
    try:
        r = subprocess.run([path, "-hide_banner", "-version"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=20,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def status():
    """返回 (是否就绪, 每项状态列表)。"""
    rows = []
    all_ok = True
    for name in WANT:
        p = os.path.join(BIN_DIR, name)
        exists = os.path.isfile(p)
        size_mb = os.path.getsize(p) / 1024 / 1024 if exists else 0
        runs = probe_exe(p) if exists else False
        all_ok = all_ok and runs
        if not exists:
            state = "缺失"
        elif not runs:
            state = "存在但无法运行"
        else:
            state = "可用"
        rows.append((name, state, size_mb))
    return all_ok, rows


def print_status():
    ok, rows = status()
    print(f"bin/ 目录: {BIN_DIR}")
    for name, state, size_mb in rows:
        mark = "OK " if state == "可用" else "-- "
        size = f"{size_mb:.1f} MB" if size_mb else "-"
        print(f"  [{mark}] {name:12} {state:14} {size}")
    return ok


# ------------------------------------------------------------------ 下载

def download(url, dst, proxy=None):
    """带进度显示的下载。返回实际字节数。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 显式读环境变量，避免系统代理干扰
        handlers.append(urllib.request.ProxyHandler())
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA})

    try:
        with opener.open(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            chunk = 1024 * 256
            with open(dst, "wb") as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if total:
                        pct = done * 100 / total
                        bar = "#" * int(pct / 2.5)
                        sys.stdout.write(f"\r  [{bar:<40}] {pct:5.1f}%  "
                                         f"{done / 1048576:.1f}/{total / 1048576:.1f} MB")
                    else:
                        sys.stdout.write(f"\r  {done / 1048576:.1f} MB")
                    sys.stdout.flush()
            sys.stdout.write("\n")
            return done
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.reason} —— {url}")
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"网络错误: {e.reason}\n"
            f"  目标: {url}\n"
            f"  若处于需要代理的网络环境，加上 --proxy http://127.0.0.1:17891")


def extract_zip(zip_path, out_dir):
    """从 zip 里提取 ffmpeg.exe / ffprobe.exe，扁平放到 out_dir。返回提取的文件名列表。"""
    got = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename).lower()
            if base not in WANT:
                continue
            # 只取 .../bin/xxx.exe 这种，跳过 doc/ 下的同名文件
            parts = info.filename.replace("\\", "/").split("/")
            if len(parts) >= 2 and parts[-2].lower() != "bin":
                continue
            target = os.path.join(out_dir, os.path.basename(info.filename))
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            got.append(os.path.basename(info.filename))
    return got


def fetch(source_key, proxy=None, force=False):
    src = SOURCES[source_key]
    print(f"下载源: {src['label']}")
    print(f"URL   : {src['url']}")
    if proxy:
        print(f"代理  : {proxy}")
    print()

    os.makedirs(BIN_DIR, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="avtoolbox_ffmpeg_")
    zip_path = os.path.join(tmpdir, "ffmpeg.zip")
    try:
        size = download(src["url"], zip_path, proxy)
        mb = size / 1048576
        if mb < src["min_zip_mb"]:
            raise RuntimeError(
                f"下载文件只有 {mb:.1f} MB，小于预期的 {src['min_zip_mb']} MB，"
                f"可能是错误页面而非完整包。请换 --source 或检查网络。")
        print(f"  已下载 {mb:.1f} MB")

        print("解压中…")
        got = extract_zip(zip_path, BIN_DIR)
        if not got:
            raise RuntimeError("压缩包里没找到 ffmpeg.exe / ffprobe.exe，下载源可能已变更")
        for name in got:
            p = os.path.join(BIN_DIR, name)
            print(f"  {name:12} {os.path.getsize(p) / 1048576:.1f} MB")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(
        description="下载内置 ffmpeg/ffprobe 到 bin/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="克隆仓库后跑一次本脚本即可，之后所有功能照常使用。")
    ap.add_argument("--check", action="store_true", help="只检查现有二进制，不下载")
    ap.add_argument("--force", action="store_true", help="已存在也强制重新下载")
    ap.add_argument("--source", choices=sorted(SOURCES), default="btbn",
                    help="下载源（默认 btbn）")
    ap.add_argument("--proxy", default=os.environ.get("AVTOOLBOX_PROXY"),
                    help="下载用的 HTTP 代理，如 http://127.0.0.1:17891")
    args = ap.parse_args()

    print("=" * 62)
    print("  音视频工具箱 —— 内置 ffmpeg 获取")
    print("=" * 62)
    ok = print_status()
    print()

    if args.check:
        if ok:
            print("检查通过：内置二进制可用。")
            return 0
        print("未就绪。跑 `python fetch_ffmpeg.py` 下载。")
        return 1

    if ok and not args.force:
        print("已就绪，无需下载。要强制重下请加 --force。")
        return 0

    try:
        fetch(args.source, args.proxy, args.force)
    except RuntimeError as e:
        print(f"\n下载失败：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130

    print()
    if print_status():
        print("\n完成。现在可以直接用了：")
        print("  双击 启动.bat          打开图形界面")
        print("  python avtool.py --help   命令行接口")
        return 0
    print("\n下载完成但二进制仍无法运行，可能是杀软拦截或架构不符。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
