---
name: av-toolbox
description: 用本机自带的音视频工具箱（FFmpeg + Python，零依赖可分发）完成音视频处理：合并分离的音视频、无损压缩、格式转换、仅换容器、提取/剥离音视频轨、批量处理整个目录。工具箱自带 ffmpeg 与 GUI，并通过 avtool.py 暴露面向 agent 的 JSON 接口。触发词：合并音视频、音视频合并、视频加音频、无损压缩视频、视频压缩、格式转换、换容器、remux、提取音频、去掉音频、批量转码、batch transcode、avtool、音视频工具箱。凡是用户要求处理本地音视频文件（mp4/mkv/mov/webm/avi/mp3/flac 等）的增删改转，都应使用本 skill 而不是自己手搓 ffmpeg 命令。
agent_created: true
---

# av-toolbox

调用本机的**音视频工具箱**。该工具箱是一个自带全部运行时依赖（内置 ffmpeg/ffprobe、
无第三方 Python 包）的可分发项目，`avtool.py` 提供面向 AI agent 的 JSON 接口。

**核心原则：不要自己手搓 ffmpeg 命令，一律走 `avtool.py`。** 它已经封装好了：
流映射、容器与编码的合法组合校验、`-c copy` 与滤镜互斥的处理、输出防覆盖、
ffmpeg 报错的中文翻译。手搓命令极易踩 `-map` 丢轨、容器装不下编码、扩展名与编码错配这些坑。

---

## 一、先定位工具箱

工具箱位置**不固定**，按以下顺序找，找到第一个含 `avtool.py` 的目录即为根目录：

```bash
# 1. 环境变量（最优先，用户可显式指定）
echo "$AVTOOLBOX_HOME"

# 2. 常见位置
ls "/c/Users/pc/Desktop/test/project/avtool.py" 2>/dev/null      # 默认开发位置
ls "/d/AVToolbox/avtool.py" 2>/dev/null
ls "/c/AVToolbox/avtool.py" 2>/dev/null
```

找不到就用文件搜索（`avtool.py` 或 `merge_av.py`），别再往下猜路径。

### 确定 Python 解释器

**不要用托管 Python**（`~/.workbuddy/binaries/python/`）——它不带 tkinter，
虽然 CLI 大部分动作不用 tkinter，但为一致性统一用系统 Python：

```bash
PY="C:/Users/pc/AppData/Local/Programs/Python/Python313/python.exe"
```

若系统 Python 不在该路径，用 `python`。工具箱的 `avtool.bat` 也能直接跑（会自动挑解释器）。

**本项目已固化命令**（推荐直接用这个变量）：

```bash
AV="C:/Users/pc/AppData/Local/Programs/Python/Python313/python.exe <项目根>/avtool.py"
```

### 确认内置 ffmpeg 就绪（克隆后必做一次）

仓库**不含** ffmpeg 二进制（单个 160MB，超 GitHub/Gitee 单文件限制）。
若 `bin/ffmpeg.exe` 不存在，先跑一次下载 —— **这是 agent 可以自己做的一步，不要丢给用户**：

```bash
"$PY" <项目根>/fetch_ffmpeg.py            # 约 190MB，下载后自动解压到 bin/
"$PY" <项目根>/fetch_ffmpeg.py --check    # 只检查，不下载
```

需要走代理时（本机 local-proxy 默认端口 17891）：

```bash
node "C:/Users/pc/.workbuddy/skills/local-proxy/scripts/proxy.mjs" run \
  "C:/Users/pc/AppData/Local/Programs/Python/Python313/python.exe <项目根>/fetch_ffmpeg.py"
```

判断是否就绪的最快方式：跑 `avtool.py capabilities`，`data.ffmpeg` 非空且
`data.ffmpeg_origin` 为 `"内置"` 即正常。若为 `"系统"`，说明用的是系统 ffmpeg，
功能可用但失去了自带依赖的便利。

---

## 二、铁律

1. **先 `capabilities`，再动手。** 本机有哪些编码器、能不能用 NVENC 是动态的，
   可用 preset / 格式列表必须从 `capabilities` 现取，**不要硬编码、不要凭记忆**。
2. **改文件前先 `--dry-run`。** 拿到 `command_str` 确认意图，再实跑。
3. **不要默认加 `--overwrite`。** 只在用户明确要求覆盖时加。默认行为是拒绝覆盖
   （退出码 6），这是在保护用户数据，不要绕过它。
4. **`compress` 前先判断源是否已有损。** 若用户目标是「减小体积」，直接推荐高压缩比
   preset（`x264_crf23` 起），**不要推无损** —— 无损对已是有损编码的源只会变大
   （qp0 约 +5%，FFV1 约 +27%）。这是最常见的误用。
5. **参数类错误不要重试。** `error.code` 是 `bad_arguments` / `bad_format` / `bad_preset`
   时，重试同样调用必然再失败，先按 `error.hint` 改参数。
6. **路径含空格要加引号**；拼命令行时优先用 `data.command`（数组），别用 `command_str` 字符串。

---

## 三、调用契约

### stdout 恒为单个 JSON

```json
{"ok": true, "action": "convert", "schema": "1",
 "data": {"output": "…", "size_out_human": "25.0 MB", "ratio": "4.20x"},
 "error": null}
```

失败时：

```json
{"ok": false, "action": "convert", "schema": "1", "data": null,
 "error": {"code": "incompatible_container",
           "message": "目标容器 WEBM 不兼容源视频编码 H264，「仅换容器」会失败，请改用重编码",
           "hint": "改用 --mode convert 重编码，或加 --force 强行尝试"}}
```

ffmpeg 日志、进度**全部走 stderr**。`json.loads(stdout)` 一定成功。

### 退出码

| 码 | 含义 | 应对 |
|---|---|---|
| 0 | 成功 | 读 `data` |
| 2 | 参数错误 | 按 `error.hint` 修参数，别重试 |
| 3 | 源文件不存在/不可读 | 核对路径 |
| 4 | ffmpeg 缺失或无法执行 | 环境问题，别重试 |
| 5 | ffmpeg 执行失败 | 看 `error.message` + `error.hint`（含 ffmpeg 原始日志） |
| 6 | 输出文件已存在 | 加 `--overwrite` 或换 `--output` |

### 推荐调用流程

```
capabilities → probe → <动作> --dry-run → <动作> --quiet
```

---

## 四、动作速查

### capabilities —— 会话开始必调

```bash
$AV capabilities
```

返回本机 ffmpeg 路径、`nvenc` 是否可用、`lossless_presets`、`compress_presets`、
`video_formats`、`audio_formats`、`extract_modes`、`batch_ops`、
`batch_template_placeholders`、`exit_codes`、`examples`。

**所有 key 都从这里取。**

### probe —— 处理前先看清源

```bash
$AV probe --input a.mp4 --quiet
```

关键字段：`duration`、`size_human`、`has_video`、`has_audio`、`streams[]`
（`type` / `codec` / `width` / `height` / `fps` / `channels` / `is_cover`）。

> 判断「源有没有音轨」看 `has_audio`；判封面图看 `streams[].is_cover`。

### merge —— 合并分离的音视频

```bash
$AV merge --video v.mp4 --audio a.m4a --output out.mp4 --quiet
$AV merge -v v.mp4 -a a.m4a --mode transcode --crf 18 --start 1:30 --end 2:00 --quiet
$AV merge -v v.mp4 -o out.mp4 --quiet          # 不给音频源 = 保留视频自带音轨
```

| 参数 | 说明 |
|---|---|
| `--mode copy` | 默认。无损封装，秒级，画质零损失 |
| `--mode transcode` | 重编码，可用 `--crf` / `--volume` / `--start` / `--end` / `--audio-delay` |
| `--start` `--end` | `90` / `1:30` / `00:01:30.5` 都行 |
| `--volume` | 音量倍数，**仅 transcode 生效** |
| `--audio-delay` | 音频整体延后秒数，**仅 transcode 生效** |
| `--no-shortest` | 默认加 `-shortest`（以最短流截断） |

> `--mode copy` 下 `--volume`/`--audio-delay`/`--start`/`--end` 会返回
> `copy_mode_conflict`（ffmpeg 的 `-vf`/`-af` 与 `-c copy` 不能共存）。
> 要么改 `--mode transcode`，要么加 `--force` 明确表示忽略这些参数。

### compress —— 压缩视频

```bash
$AV compress --input big.mp4 --preset x264_crf23 --out-dir ./out --quiet
$AV compress -i big.mp4 -p x264_qp0 --output lossless.mkv --quiet
$AV compress -i big.mp4 -p x264_crf28 --scale 720p --quiet
```

| 类别 | preset | 说明 |
|---|---|---|
| 数学无损 | `x264_qp0` / `x265_lossless` / `ffv1` / `nvenc_h264_lossless` / `nvenc_hevc_lossless` | 逐像素与源完全一致 |
| 高压缩比 | `x264_crf18/23/28` / `x265_crf23/28` / `nvenc_h264_cq26` / `nvenc_hevc_cq26` / `nvenc_av1_cq30` | 视觉无损，体积显著变小 |

其他：`--audio lossless|copy|aac|none`、`--scale`、`--start/--end`、`--preset-speed`。

> **无损对已有损源不会变小**（可能变大）。要瘦身用高压缩比。
> 实测参考（1080p30 合成素材，实际值随素材变化）：
> x264 crf23 ≈ 4.2x 省 76%；crf28 ≈ 8.6x；x265 crf23 比 x264 同画质再小约 30%；NVENC CQ26 ≈ 3.2x。

### convert —— 格式转换 / 仅换容器

```bash
$AV convert --input a.mov --format mp4 --quiet
$AV convert -i a.mp4 -f mkv --mode remux --quiet                      # 秒级，零损失
$AV convert -i a.mov -f mp4 --video-codec libx265 --crf 20 --scale 1080p --quiet
```

- 视频目标（10）：`mp4` `mkv` `mov` `webm` `avi` `flv` `ts` `gif` `m4v` `3gp`
- 音频目标（9）：`mp3` `m4a` `aac` `flac` `wav` `opus` `ogg` `ac3` `mka`
- `--mode convert`（默认，重编码）/ `--mode remux`（仅换容器）

> `remux` 前会做容器兼容性预检，不兼容（如 H.264 → WebM）直接返回
> `incompatible_container`，不会白跑一遍才失败。`--force` 可强行尝试（通常会失败）。

### extract —— 轨道提取

```bash
$AV extract --input a.mp4 --mode audio_from_video --format mp3 --quiet
$AV extract -i a.mp4 --mode video_only -f mp4 --quiet
$AV extract -i a.mp4 --mode remux -f mkv --quiet
```

| `--mode` | 说明 | `--format` |
|---|---|---|
| `audio_from_video` | 丢掉画面，导出音频 | 音频格式（默认 mp3） |
| `video_only` | 丢掉音轨，输出无声视频 | 视频格式（默认 mp4） |
| `remux` | 仅换容器 | 必填 |
| `audio_only` | 保留画面、只换音频编码 | 仅 MKA/MKV 等多流容器 |

> `.mp3` `.aac` `.opus` `.flac` 等裸音频容器只能装一条流，不能同时保留视频轨。
> `audio_from_video` 传视频格式会被直接拦下（`bad_format`），不会产出扩展名与编码不符的错配文件。

### batch —— 批量处理

```bash
# 先看计划
$AV batch -i ./raw --op convert -f mkv --recursive --out-dir ./out --dry-run
# 再执行
$AV batch -i ./raw ./more --op compress -p x264_crf28 --out-dir ./out --json-progress
```

| `--op` | 需要 | 说明 |
|---|---|---|
| `convert` | `--format` | 格式转换 |
| `remux` | `--format` | 仅换容器（不兼容的自动跳过） |
| `compress` | `--preset` | 压缩 |
| `extract_audio` | `--format`（音频） | 提音频（无音轨的自动跳过） |
| `video_only` | `--format`（视频） | 去音频 |

**命名模板** `--template`（默认 `{name}_{op}`）：
`{name}` 原名 / `{ext}` 原扩展名 / `{index}` 序号 / `{date}` YYYYMMDD / `{op}` 操作名。

其他：`--recursive/-r`、`--exts mp4,mov`、`--limit N`、`--json-progress`（逐条进度写 stderr）。

批量结果含 `total` / `done` / `failed` / `skipped` / `size_in` / `size_out` / `elapsed`
和 `results[]`（每项 `status` 为 `done`/`failed`/`skipped`，`message` 说明原因）。

---

## 五、错误码速查

| `error.code` | 触发 | 应对 |
|---|---|---|
| `bad_arguments` | 缺参数 / 取值非法 | 按 `hint` 修 |
| `bad_action` | 未知动作 | 跑 capabilities |
| `input_not_found` | 源文件不存在 | 核对路径 |
| `probe_failed` | 不是有效媒体文件 | 换文件 |
| `output_exists` | 输出已存在 | 加 `--overwrite` 或换路径 |
| `output_dir_missing` | 输出目录不存在 | 先建目录 |
| `bad_format` | 格式名不存在 / 类型不匹配 | 查 capabilities |
| `bad_preset` | 压缩方案名不存在 | 查 capabilities |
| `preset_unavailable` | 方案存在但本机跑不了（缺编码器/无显卡） | 换方案 |
| `no_video_stream` / `no_audio_stream` | 源缺对应轨道 | 换文件或换操作 |
| `incompatible_container` | remux 目标容器装不下源编码 | 改 `--mode convert` 或 `--force` |
| `copy_mode_conflict` | copy 模式配了滤镜类参数 | 改 transcode 或 `--force` |
| `bad_time` / `bad_time_range` | 时间格式错 / end ≤ start | 修正 |
| `bad_scale` | 缩放写法不认识 | 用 `720p` 或 `1280x720` |
| `ffmpeg_error` | ffmpeg 执行失败 | 看 `hint` 里的原始日志（多数附中文解释） |
| `ffmpeg_not_found` | 找不到 ffmpeg | 环境问题 |
| `internal_error` | 程序自身异常 | 报告 bug |

---

## 六、GUI 与脚本模式

用户要交互操作、或任务复杂到需要可视化确认时，让用户双击项目根目录的
**`启动.bat`**（4 个标签页：合并音视频 / 无损压缩 / 格式转换 / 批量处理）。

环境自检：双击 **`环境自检.bat`**，或跑 `merge_av.py --check`。

> `merge_av.py` 是 GUI 主程序，`avtool.py` 是 CLI。两者共用同一套命令构造函数
> （`compressor.py` / `converter.py` / `batch.py`），行为一致，不存在两套逻辑。

---

## 七、别忘了

- **改文件前先 probe。** 不知道源有没有音轨就调 `extract_audio`，会白跑。
- **批量任务先 `--dry-run`。** 输出路径推导错了会污染整个目录。
- **长批量任务用 `--json-progress`。** 按行读 stderr 汇报进度，比等到最后才知道好。
- 完整接口契约见项目根目录的 **`AGENT.md`**；用户侧说明见 **`README.md`**。
