# -*- coding: utf-8 -*-
"""
av-toolbox skill 调用示例：拿到一个「分离的视频 + 音频」，合并后压一版小的。

这就是 agent 该有的调用姿势 —— 全程走 avtool.py，不手搓 ffmpeg。
直接把 LOCATE 换成你自己项目里的路径即可。

    python example_merge_and_compress.py <video> <audio> <out_dir>
"""

import json
import os
import subprocess
import sys

# ---- 1. 定位工具箱（实际用 skill 时，这一步交给 scripts/locate.py 自动完成）
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
ROOT = os.path.abspath(os.path.join(SKILL_DIR, "..", ".."))
PY = r"C:\Users\pc\AppData\Local\Programs\Python\Python313\python.exe"
AVTOOL = os.path.join(ROOT, "avtool.py")


def av(*args, quiet=True):
    """调 avtool，返回解析后的 JSON dict。stdout 恒为单个 JSON。

    失败时若 stdout 不是 JSON（例如解释器都跑不起来），返回一个合成错误 dict，
    保持调用方接口一致 —— 免得每处都要判元组。
    """
    cmd = [PY, AVTOOL] + list(args)
    if quiet and args and args[0] != "capabilities":
        cmd.append("--quiet")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=3600)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        tail = (r.stderr or r.stdout or "无输出")[-600:]
        return {"ok": False,
                "error": {"code": "not_json",
                          "message": "avtool 未返回合法 JSON（可能解释器或路径有问题）",
                          "hint": tail}}


def must(d, what):
    """断言成功，否则打印错误并退出。"""
    if d and d.get("ok"):
        return d["data"]
    err = (d or {}).get("error") or {}
    print(f"✗ {what} 失败: [{err.get('code')}] {err.get('message')}")
    if err.get("hint"):
        print(f"  提示: {err['hint']}")
    sys.exit(1)


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    video, audio, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out_dir, exist_ok=True)

    # ---- 2. 会话开始必调：拿本机能力（可用 preset 从这来，别硬编码）
    caps = must(av("capabilities", quiet=False), "capabilities")
    print(f"工具箱: {ROOT}")
    print(f"ffmpeg: {caps['ffmpeg_origin']}  |  NVENC: {caps['nvenc']}")
    # capabilities 返回的是对象数组，取 key 才是能直接传给 --preset 的值
    compress_keys = [p["key"] for p in caps["compress_presets"]]

    # ---- 3. 处理前先 probe，看清源有什么
    vinfo = must(av("probe", "-i", video), "probe 视频")
    ainfo = must(av("probe", "-i", audio), "probe 音频")
    print(f"视频: {vinfo['duration_str']} {vinfo['size_human']} "
          f"音轨={vinfo['has_audio']}")
    print(f"音频: {ainfo['duration_str']} {ainfo['size_human']}")

    # ---- 4. 改文件前先 dry-run，确认命令无误
    #      dry-run 不写盘，所以即使目标已存在也不会失败；
    #      它会用 data.conflict 字段告诉你「真跑会不会撞车」。
    merged = os.path.join(out_dir, "merged.mp4")
    plan = must(av("merge", "-v", video, "-a", audio, "-o", merged, "--dry-run"),
                "merge dry-run")
    print("\n将执行:\n  " + plan["command_str"] + "\n")

    # ---- 5. 实跑。若目标已存在，dry-run 已经提醒过，这里显式带上 --overwrite
    extra = ["--overwrite"] if plan.get("conflict") else []
    if plan.get("conflict"):
        print(f"注意: {plan['conflict']['message']} → 本次将覆盖")
    r = must(av("merge", "-v", video, "-a", audio, "-o", merged, *extra), "merge")
    print(f"✓ 合并完成: {r['output']}  {r['size_out_human']}  ({r['elapsed']}s)")

    # ---- 6. 压缩：源是原始视频，这里目的是瘦身 → 用高压缩比，不用无损
    #      注意不同 preset 的容器可能相同（crf23 与 crf28 都是 .mp4），
    #      自动推导会撞名，所以显式给 --output；脚本自身幂等，带上 --overwrite。
    for preset in ("x264_crf23", "x264_crf28"):
        if preset not in compress_keys:
            continue
        d = must(av("compress", "-i", merged, "-p", preset,
                    "-o", os.path.join(out_dir, f"small_{preset}.mp4"),
                    "--overwrite"),
                 f"compress {preset}")
        print(f"✓ 压缩 {preset}: {d['size_out_human']}  压缩比 {d['ratio']}  {d['change']}")

    print(f"\n完成。产物在: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
