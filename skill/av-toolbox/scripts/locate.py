# -*- coding: utf-8 -*-
"""
av-toolbox skill 的定位与自检脚本。

用途：让 AI agent 一条命令拿到「工具箱在哪、能不能跑、本机有哪些能力」。
不依赖任何第三方包，只用标准库 + 工具箱自带的 deps.py。

用法：
    python locate.py            # 定位工具箱 + 环境摘要
    python locate.py --json     # 同上的 JSON 形式（agent 首选）
    python locate.py --check    # 附带完整自检报告（等价于 merge_av.py --check）
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 候选位置：环境变量 > 相对本脚本向上找 > 常见路径
CANDIDATES = [
    os.environ.get("AVTOOLBOX_HOME"),
    # skill 若被拷进项目内（<root>/skill/av-toolbox/scripts/），向上 3 层即项目根
    os.path.abspath(os.path.join(HERE, "..", "..", "..")),
    r"C:\Users\pc\Desktop\test\project",
    r"C:\AVToolbox",
    r"D:\AVToolbox",
    os.path.join(os.path.expanduser("~"), "AVToolbox"),
]

PY_CANDIDATES = [
    r"C:\Users\pc\AppData\Local\Programs\Python\Python313\python.exe",
    sys.executable,
]


def find_root():
    """返回第一个含 avtool.py 的目录。"""
    for c in CANDIDATES:
        if c and os.path.isfile(os.path.join(c, "avtool.py")):
            return os.path.abspath(c)
    return None


def find_python(root):
    """优先用工具箱自带的便携 Python，其次系统 Python。"""
    portable = os.path.join(root, "python", "python.exe")
    if os.path.isfile(portable):
        return portable
    for p in PY_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return "python"


def run_capabilities(root, py):
    """调 avtool.py capabilities，返回解析后的 dict。"""
    try:
        r = subprocess.run([py, os.path.join(root, "avtool.py"), "capabilities"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180,
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"调用失败: {e}"
    try:
        return json.loads(r.stdout), None
    except json.JSONDecodeError:
        return None, (r.stderr or r.stdout or "无输出")[-500:]


def main():
    as_json = "--json" in sys.argv
    do_check = "--check" in sys.argv

    root = find_root()
    if not root:
        out = {
            "ok": False,
            "error": "找不到音视频工具箱（没找到含 avtool.py 的目录）",
            "hint": "把工具箱路径写进环境变量 AVTOOLBOX_HOME，或告诉我它装在哪",
            "searched": [c for c in CANDIDATES if c],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 3

    py = find_python(root)
    caps, err = run_capabilities(root, py)

    result = {
        "ok": caps is not None and caps.get("ok", False),
        "root": root,
        "python": py,
        "avtool": os.path.join(root, "avtool.py"),
        "portable_python": os.path.isfile(os.path.join(root, "python", "python.exe")),
        "gui_entry": os.path.join(root, "启动.bat"),
        "cli_entry": os.path.join(root, "avtool.bat"),
        "agent_doc": os.path.join(root, "AGENT.md"),
        "example_cmd": f'"{py}" "{os.path.join(root, "avtool.py")}" capabilities',
    }
    if err:
        result["error"] = err
    if caps:
        d = caps.get("data", {}) or {}
        result["ffmpeg"] = d.get("ffmpeg")
        result["ffmpeg_origin"] = d.get("ffmpeg_origin")
        result["nvenc"] = d.get("nvenc")
        result["lossless_presets"] = [p["key"] for p in d.get("lossless_presets", [])]
        result["compress_presets"] = [p["key"] for p in d.get("compress_presets", [])]
        result["video_formats"] = [t["key"] for t in d.get("video_formats", [])]
        result["audio_formats"] = [t["key"] for t in d.get("audio_formats", [])]
        result["extract_modes"] = [m["key"] for m in d.get("extract_modes", [])]
        result["batch_ops"] = list((d.get("batch_ops") or {}).keys())

    if do_check:
        try:
            r = subprocess.run([py, os.path.join(root, "merge_av.py"), "--check"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180,
                               creationflags=0x08000000 if os.name == "nt" else 0)
            result["self_check"] = r.stdout.strip().splitlines()
            result["self_check_ok"] = (r.returncode == 0)
        except (OSError, subprocess.SubprocessError) as e:
            result["self_check_ok"] = False
            result["self_check"] = [f"失败: {e}"]

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 4


if __name__ == "__main__":
    sys.exit(main())
