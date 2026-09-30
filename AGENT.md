# AGENT.md —— 给 AI agent 的调用说明

`avtool.py` 是这个工具箱的**非交互接口**，专门给 AI agent / 脚本 / 自动化流程调用。
GUI（`merge_av.py`）和 CLI 共用同一套命令构造函数，行为完全一致。

```
python avtool.py <action> [options]
```

---

## 一、必须先知道的三条契约

### 1. stdout 永远是单个 JSON，没有例外

```json
{
  "ok": true,
  "action": "convert",
  "schema": "1",
  "data": { "...": "..." },
  "error": null
}
```

失败时：

```json
{
  "ok": false,
  "action": "convert",
  "schema": "1",
  "data": null,
  "error": {
    "code": "incompatible_container",
    "message": "目标容器 WEBM 不兼容源视频编码 H264，「仅换容器」会失败，请改用重编码",
    "hint": "改用 --mode convert 重编码，或加 --force 强行尝试"
  }
}
```

**ffmpeg 的日志、进度、报错全部走 stderr。** 直接 `json.loads(stdout)` 一定成功。

### 2. 退出码语义化

| 码 | 含义 | agent 该怎么反应 |
|---|---|---|
| 0 | 成功 | 读 `data` |
| 2 | 参数错误 | 看 `error.hint` 修正参数，别重试同样的调用 |
| 3 | 源文件不存在/不可读 | 检查路径 |
| 4 | ffmpeg 缺失或无法执行 | 环境问题，别重试 |
| 5 | ffmpeg 执行失败 | 看 `error.message` 与 `error.hint`（含 ffmpeg 原始日志尾部） |
| 6 | 输出文件已存在 | 加 `--overwrite` 或换 `--output` |

### 3. 默认不覆盖任何已有文件

不加 `--overwrite` 时，输出路径撞车会直接返回退出码 6，**绝不静默覆盖用户数据**。

---

## 二、推荐调用流程

```
1. capabilities          ← 每次会话开始先调一次，拿到本机能力与全部合法取值
2. probe --input <file>  ← 处理前先看清源有什么轨道
3. <动作> --dry-run      ← 先拿命令确认，再实跑
4. <动作> --quiet        ← 实跑
```

### dry-run 不会因「输出已存在」而失败

这是刻意的设计：dry-run 不写盘，所以即使目标文件已存在也会正常返回 `ok: true`，
并把冲突信息放在 `data.conflict` 里（无冲突时为 `null`）：

```json
{"ok": true, "data": {
   "dry_run": true,
   "command_str": "…",
   "output": "…\\out.mp4",
   "conflict": {"code": "output_exists",
                "message": "输出文件已存在: …\\out.mp4",
                "hint": "实跑前需加 --overwrite，或改用 --output / --out-dir"}}}
```

**agent 应当这样用**：先 dry-run 读 `conflict`，若不为 `null` 就在实跑时补 `--overwrite`
（前提是用户确实允许覆盖），否则改 `--output`。这样「先 dry-run 再实跑」在重复执行时
不会因为撞车而中断 —— 之前 dry-run 会直接返回退出码 6，导致推荐流程第二次调用就失败。

### capabilities 返回什么

```json
{
  "ffmpeg": "…\\bin\\ffmpeg.exe",
  "nvenc": true,
  "lossless_presets":  [ {"key":"x264_qp0","label":"…","container":".mkv","hw":false}, … ],
  "compress_presets":  [ {"key":"x264_crf23","label":"…"}, … ],
  "video_formats":     [ {"key":"mp4","ext":".mp4","video_codecs":["libx264",…]}, … ],
  "audio_formats":     [ … ],
  "extract_modes":     [ {"key":"audio_from_video","label":"…"}, … ],
  "batch_ops":         { "convert": "…", "remux": "…", … },
  "batch_template_placeholders": { "{name}": "…", … },
  "exit_codes":        { "0": "成功", … },
  "examples":          [ "avtool.py …", … ]
}
```

**所有 key 都必须从 capabilities 里取，不要硬编码** —— 方案可用性取决于本机 ffmpeg 编解码器与
显卡（NVENC），不同机器结果不同。

---

## 三、动作详解

### probe —— 读媒体信息

```bash
avtool.py probe --input a.mp4
```

`data` 关键字段：`duration`、`size`、`format`、`has_video`、`has_audio`、
`video_streams`、`audio_streams`、`streams[]`（每项含 `type`/`codec`/`width`/`height`/`fps`/`channels`）。

> `streams[].type` 是 `"video"`/`"audio"`；判封面图看 `is_cover`。

### merge —— 合并分离的音视频

```bash
avtool.py merge --video v.mp4 --audio a.m4a --output out.mp4
avtool.py merge --video v.mp4 --audio a.m4a --mode transcode --crf 18 --start 1:30 --end 2:00
avtool.py merge -v v.mp4 -o out.mp4          # 不给音频源 = 只保留视频自带音轨
```

| 参数 | 说明 |
|---|---|
| `--mode copy` | 默认。无损封装，秒级，画质零损失 |
| `--mode transcode` | 重编码，可用 `--crf` / `--volume` / `--start` / `--end` / `--audio-delay` |
| `--start` `--end` | `90` / `1:30` / `00:01:30.5` 都行 |
| `--volume` | 音量倍数，仅 transcode 生效 |
| `--audio-delay` | 音频整体延后秒数，仅 transcode 生效 |
| `--no-shortest` | 默认加 `-shortest`（以最短流截断） |

**注意**：`--mode copy` 下 `--volume`/`--audio-delay`/`--start`/`--end` 互斥
（ffmpeg 的 `-vf`/`-af` 与 `-c copy` 不能共存），程序会返回 `copy_mode_conflict`，
要么改 `--mode transcode`，要么加 `--force` 明确表示忽略这些参数。

### compress —— 压缩视频

```bash
avtool.py compress --input big.mp4 --preset x264_crf23 --out-dir ./out
avtool.py compress -i big.mp4 -p x264_qp0 --output lossless.mkv
avtool.py compress -i big.mp4 -p x264_crf28 --scale 720p
```

| 类别 | preset 前缀 | 说明 |
|---|---|---|
| 数学无损 | `x264_qp0` / `x265_lossless` / `ffv1` / `nvenc_*_lossless` | 逐像素与源完全一致 |
| 高压缩比 | `x264_crf*` / `x265_crf*` / `nvenc_*_cq*` | 视觉无损，体积显著变小 |

**关键认知**：无损压缩对**已经是有损编码的源**（普通 MP4/H.264）通常**不会变小**，
甚至变大（qp0 约 +5%，FFV1 约 +27%），因为信息量已被压掉。要瘦身必须用高压缩比 preset。

其他参数：`--audio lossless|copy|aac|none`、`--scale`、`--start/--end`、`--preset-speed`。

### convert —— 格式转换 / 仅换容器

```bash
avtool.py convert --input a.mov --format mp4
avtool.py convert -i a.mp4 -f mkv --mode remux              # 秒级，零损失
avtool.py convert -i a.mov -f mp4 --video-codec libx265 --crf 20 --scale 1080p
```

| `--mode` | 说明 |
|---|---|
| `convert` | 默认。按目标容器重编码 |
| `remux` | 仅换容器，`-c copy`，不重编码 |

`remux` 前会做容器兼容性预检，不兼容（如 H.264 → WebM）直接返回
`incompatible_container`，不会白跑一遍再失败。加 `--force` 可强行尝试。

### extract —— 轨道提取

```bash
avtool.py extract --input a.mp4 --mode audio_from_video --format mp3
avtool.py extract -i a.mp4 --mode video_only -f mp4
avtool.py extract -i a.mp4 --mode remux -f mkv
```

| `--mode` | 说明 | `--format` 必填? |
|---|---|---|
| `audio_from_video` | 丢掉画面，导出音频 | 音频格式（默认 mp3） |
| `video_only` | 丢掉音轨，输出无声视频 | 视频格式（默认 mp4） |
| `remux` | 仅换容器 | 必填 |
| `audio_only` | 保留画面、只换音频编码 | 仅 MKA/MKV 这类多流容器 |

**限制**：`.mp3` `.aac` `.opus` `.flac` 等裸音频容器只能装一条流，不能同时保留视频轨。
`audio_from_video` 传视频格式会被直接拦下（`bad_format`），不会产出扩展名与编码不符的错配文件。

### batch —— 批量处理

```bash
# 先看计划
avtool.py batch -i ./raw --op convert -f mkv --recursive --out-dir ./out --dry-run
# 再执行
avtool.py batch -i ./raw ./more --op compress -p x264_crf28 --out-dir ./out --json-progress
```

| `--op` | 需要 | 说明 |
|---|---|---|
| `convert` | `--format` | 格式转换 |
| `remux` | `--format` | 仅换容器（不兼容的自动跳过） |
| `compress` | `--preset` | 压缩 |
| `extract_audio` | `--format`（音频） | 提音频（无音轨的自动跳过） |
| `video_only` | `--format`（视频） | 去音频 |

**命名模板** `--template`（默认 `{name}_{op}`）：

| 占位符 | 含义 |
|---|---|
| `{name}` | 原文件名（不含扩展名） |
| `{ext}` | 原扩展名（不含点） |
| `{index}` | 序号，从 1 开始 |
| `{date}` | 当天日期 `YYYYMMDD` |
| `{op}` | 操作名 |

其他：`--recursive/-r` 递归子目录、`--exts mp4,mov` 只处理指定扩展名、
`--limit N` 限量、`--json-progress` 逐条进度写 stderr。

**批量结果**：

```json
{
  "total": 12, "done": 10, "failed": 1, "skipped": 1,
  "size_in": 104857600, "size_out": 26214400,
  "size_in_human": "100.0 MB", "size_out_human": "25.0 MB",
  "elapsed": 42.1,
  "results": [
    {"index":1,"input":"…","output":"…","status":"done",
     "size_in":…,"size_out":…,"size_in_human":"…","size_out_human":"…","elapsed":3.2}
  ]
}
```

`status` 取值：`done` / `failed`（`message` 含原因）/ `skipped`（不满足前提，如无音轨、容器不兼容）。

---

## 四、错误码速查

| `error.code` | 触发条件 | agent 应对 |
|---|---|---|
| `bad_arguments` | 缺参数 / 取值非法 | 按 `hint` 修参数 |
| `bad_action` | 未知动作 | 跑 capabilities 看 `actions` |
| `input_not_found` | 源文件不存在 | 核对路径 |
| `probe_failed` | 不是有效媒体文件 | 换文件 |
| `output_exists` | 输出已存在且未加 `--overwrite` | 加 `--overwrite` 或换路径 |
| `output_dir_missing` | 输出目录不存在 | 先建目录 |
| `bad_format` | 格式名不在表里 / 类型不匹配（如提音频传 mp4） | 查 capabilities |
| `bad_preset` | 压缩方案名不存在 | 查 capabilities |
| `preset_unavailable` | 方案存在但本机跑不了（缺编码器/无显卡） | 换方案 |
| `no_video_stream` / `no_audio_stream` | 源缺对应轨道 | 换文件或换操作 |
| `incompatible_container` | remux 目标容器装不下源编码 | 改 `--mode convert` 或 `--force` |
| `copy_mode_conflict` | copy 模式配了滤镜类参数 | 改 transcode 或 `--force` |
| `bad_time` / `bad_time_range` | 时间点格式错 / end ≤ start | 修正 |
| `bad_scale` | 缩放写法不认识 | 用 `720p` 或 `1280x720` |
| `ffmpeg_error` | ffmpeg 执行失败 | 看 `hint` 里原始日志，多数附了中文解释 |
| `ffmpeg_not_found` | 找不到 ffmpeg | 环境问题 |
| `timeout` / `interrupted` | 超时 / 被中断 | 视情况重试 |
| `internal_error` | 程序自身异常 | 报告 bug |

---

## 五、给 agent 的行为建议

1. **先 `capabilities`，再动手。** 本机有哪些编码器、能不能用 NVENC 是动态的，别假设。
2. **改文件前先 `--dry-run`。** 拿到 `command_str` 确认意图，再实跑。
3. **不要用 `--overwrite` 当默认值。** 只在用户明确要求覆盖时加。
4. **批量任务用 `--json-progress`，** 长任务可以按行读 stderr 汇报进度。
5. **`compress` 前先判断源是否已有损。** 若用户目标是「减小体积」，
   直接推荐高压缩比 preset（`x264_crf23` 起），别推无损 —— 无损对已有损源只会变大。
6. **失败的 job 不要盲目重试。** `error.code` 是 `bad_arguments`/`bad_format` 这类
   参数类错误时，重试同样调用必然再失败；先改参数。
7. **路径含空格没关系**，但自己拼命令行时记得加引号；用 `command`（数组）字段最安全。

---

## 六、schema 版本

当前 `schema: "1"`。字段只增不改；若未来有破坏性变更，`schema` 会升到 `"2"`
并在 capabilities 的 `notes` 里说明。
