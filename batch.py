# -*- coding: utf-8 -*-
"""
批量任务模块 —— 文件列表管理 + 输出路径推导 + 队列执行。

任务模型 Job：
    src       源文件绝对路径
    status    pending / running / done / failed / skipped
    message   状态说明或错误摘要
    dst       实际输出路径（执行时确定）

命名模板占位符：
    {name}   原文件名（不含扩展名）
    {ext}    原扩展名（不含点）
    {index}  序号（从 1 开始）
    {date}   当前日期 YYYYMMDD
    {op}     操作名（merge/compress/convert/...）
"""

import os
import re
import time


class Job:
    __slots__ = ("src", "status", "message", "dst", "size_in", "size_out", "op", "_order")

    _counter = 0

    def __init__(self, src, op="convert"):
        self.src = src
        self.op = op
        self.status = "pending"     # pending/running/done/failed/skipped
        self.message = ""
        self.dst = None
        self.size_in = None
        self.size_out = None
        # 导入顺序，用于「按原始顺序」排序时的稳定键
        Job._counter += 1
        self._order = Job._counter

    @property
    def name(self):
        return os.path.basename(self.src)

    @property
    def stem(self):
        return os.path.splitext(self.name)[0]

    @property
    def ext(self):
        return os.path.splitext(self.name)[1].lstrip(".")

    def __repr__(self):
        return f"<Job {self.name} {self.status}>"


STATUS_TEXT = {
    "pending": "待处理",
    "running": "处理中",
    "done": "✓ 完成",
    "failed": "✗ 失败",
    "skipped": "⊘ 跳过",
}


def render_template(template, job, index, op_name="out"):
    """
    渲染输出文件名模板。template 是**文件名**（不含目录），可含占位符。
    自动补上源扩展名（若模板没写扩展名则由调用方决定）。
    """
    if not template:
        template = "{name}_{op}"
    mapping = {
        "name": job.stem,
        "ext": job.ext,
        "index": str(index),
        "date": time.strftime("%Y%m%d"),
        "op": op_name,
    }
    out = template
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", v)
    # 清掉占位符和非法文件名字符
    out = re.sub(r"\{[^}]*\}", "", out)
    out = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", out).strip(" .")
    return out or f"{job.stem}_{op_name}"


def make_output_path(job, out_dir, ext, template="{name}_{op}", index=1, op_name="out"):
    """
    推导输出路径。out_dir 为空则与原文件同目录。
    ext 形如 '.mp4'。

    注意：不能用 os.path.splitext 判断「模板里是否已写了扩展名」——
    文件名可能含多个点（"video.2024.final_convert"），splitext 会把
    ".final_convert" 当成扩展名，导致真正的 .mp4 被漏掉。
    这里只认「末尾是 . + 1~5 位字母数字」才算显式扩展名。
    """
    d = out_dir or os.path.dirname(job.src)
    stem = render_template(template, job, index, op_name)
    if not re.search(r"\.[A-Za-z0-9]{1,5}$", stem):
        stem += ext
    return os.path.join(d, stem)


def ensure_unique(path, taken=None):
    """
    避免覆盖：若路径已存在或在 taken 集合里，追加 _1/_2…

    注意：不能用 os.path.splitext 定位插号位置——文件名里可能含多个点
    （如 "my.movie.2024_convert.mp4"），splitext 会以最后一个点为准，
    但若模板产出 "my.movie.2024_convert"（无扩展名）就会把 ".2024_convert"
    误判为扩展名，插号变成 "my.movie_1.2024_convert"。这里只认
    「最后一个点之后是合理扩展名（1-5 位字母数字）」的情况。
    """
    taken = taken if taken is not None else set()
    if not os.path.exists(path) and path not in taken:
        taken.add(path)
        return path

    d, name = os.path.split(path)
    m = re.search(r"\.([A-Za-z0-9]{1,5})$", name)
    if m:
        stem, ext = name[: m.start()], name[m.start():]
    else:
        stem, ext = name, ""

    i = 1
    while True:
        cand = os.path.join(d, f"{stem}_{i}{ext}")
        if not os.path.exists(cand) and cand not in taken:
            taken.add(cand)
            return cand
        i += 1


def collect_files(paths, recursive=False, exts=None):
    """
    把「文件或目录」列表展开成文件列表。
    exts: 允许的扩展名集合（小写，含点）；None 表示全部。
    """
    out = []
    for p in paths:
        if os.path.isfile(p):
            if _ext_ok(p, exts):
                out.append(os.path.abspath(p))
        elif os.path.isdir(p):
            if recursive:
                for root, _, files in os.walk(p):
                    for f in sorted(files):
                        fp = os.path.join(root, f)
                        if _ext_ok(fp, exts):
                            out.append(os.path.abspath(fp))
            else:
                for f in sorted(os.listdir(p)):
                    fp = os.path.join(p, f)
                    if os.path.isfile(fp) and _ext_ok(fp, exts):
                        out.append(os.path.abspath(fp))
    return out


def _ext_ok(path, exts):
    if not exts:
        return True
    return os.path.splitext(path)[1].lower() in exts


def dedupe(jobs):
    """按源路径去重，保留首次出现的顺序。返回 (结果列表, 去掉的重复数)。"""
    seen = set()
    out = []
    dup = 0
    for j in jobs:
        key = os.path.normcase(os.path.abspath(j.src))
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        out.append(j)
    return out, dup


def summary(jobs):
    """统计各状态数量。"""
    s = {"pending": 0, "running": 0, "done": 0, "failed": 0, "skipped": 0}
    for j in jobs:
        s[j.status] = s.get(j.status, 0) + 1
    return s
