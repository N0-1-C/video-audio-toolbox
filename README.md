# 音视频工具箱 v3.2

本地音视频处理工具，基于 FFmpeg + Python/tkinter。

> **GitHub**：https://github.com/N0-1-C/video-audio-toolbox
> **Gitee**（国内镜像）：https://gitee.com/cqhup/video-audio-toolbox

**两种用法**：人用 GUI（`启动.bat`），AI agent / 脚本用 CLI（`avtool.py`）。
两者共用同一套命令构造函数，行为一致。

## 克隆与首次运行

```bash
git clone https://github.com/N0-1-C/video-audio-toolbox.git
cd video-audio-toolbox
python fetch_ffmpeg.py     # 仅首次：下载 ffmpeg 到 bin/（约 190MB）
```

国内建议用 Gitee 镜像克隆（代码部分快得多）：

```bash
git clone https://gitee.com/cqhup/video-audio-toolbox.git
cd video-audio-toolbox
python fetch_ffmpeg.py                          # 默认从 GitHub Release 下载
python fetch_ffmpeg.py --source gyan            # GitHub 不通时换 gyan.dev 源
python fetch_ffmpeg.py --proxy http://127.0.0.1:7890   # 或指定代理
```

## 功能

| 标签页 | 功能 |
|---|---|
| 合并音视频 | 把分离的视频流与音频流合成一个文件（无损封装 / 重编码） |
| 无损压缩 | 数学无损重封装，或高压缩比瘦身 |
| 格式转换 | 19 种目标格式互转、仅换容器、轨道提取与剥离 |
| 批量处理 | 批量导入文件，按同一套参数排队加工 |

CLI 把以上能力全部暴露成 JSON 接口，另加 `probe`（媒体探测）与 `capabilities`（能力自省）。

## 快速开始

### 第 1 步：下载 ffmpeg（仅首次）

本仓库**不含** ffmpeg 二进制 —— 单个 160MB，超过 GitHub / Gitee 的单文件 100MB 限制。
克隆后跑一次：

```bat
python fetch_ffmpeg.py
:: 需要代理时
python fetch_ffmpeg.py --proxy http://127.0.0.1:17891
:: 只检查是否就绪
python fetch_ffmpeg.py --check
```

脚本会从 [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) 拉最新 GPL 静态构建，
解压 `ffmpeg.exe` / `ffprobe.exe` 到 `bin/`，并实跑校验。

> 已有 ffmpeg？也可以跳过下载 —— 程序会自动回退到系统 PATH 或 `C:\ffmpeg\bin`，
> 只是失去了「自带依赖」的便利。

### 第 2 步：使用

**GUI**：双击 **`启动.bat`**。

**CLI（给 agent / 脚本）**：

```bat
python avtool.py capabilities                    :: 先看本机能力与全部合法取值
python avtool.py probe --input a.mp4             :: 读媒体信息
python avtool.py merge -v v.mp4 -a a.m4a -o out.mp4
python avtool.py compress -i big.mp4 -p x264_crf23 --out-dir ./out
python avtool.py convert -i a.mov -f mp4
python avtool.py extract -i a.mp4 --mode audio_from_video -f mp3
python avtool.py batch -i ./raw --op convert -f mkv --recursive --out-dir ./out
```

每条命令都支持 `--dry-run`（只回 ffmpeg 命令不落盘）与 `--overwrite`（默认拒绝覆盖）。

**自检**：

```bat
python merge_av.py --check    :: 命令行环境自检（不开窗口）
python deps.py                :: 同上，单独跑依赖自检
```

也可以双击 **`环境自检.bat`**。

## 项目结构

```
video-audio-toolbox/
├── merge_av.py          主程序（GUI，四标签页）
├── avtool.py            CLI 接口（给 AI agent / 脚本调用）
├── fetch_ffmpeg.py      下载内置 ffmpeg/ffprobe（首次运行用）
├── deps.py              依赖解析与自检
├── compressor.py        压缩方案定义与命令构造
├── converter.py         格式转换目标表与命令构造
├── batch.py             批量任务模型与输出路径推导
├── bin/                 内置 FFmpeg（不进仓库，跑 fetch_ffmpeg.py 生成）
│   ├── ffmpeg.exe       含 x264/x265/NVENC/FFV1/opus/flac/libvpx/av1
│   └── ffprobe.exe      媒体信息探测
├── vendor/              （可选）纯 Python 第三方包，免 pip 安装
├── python/              （可选）便携版 Python，可让整包完全不依赖系统 Python
├── skill/av-toolbox/    给 AI agent 的 skill（可拷进 agent 的 skill 目录）
├── 启动.bat             GUI 入口
├── 环境自检.bat         自检
├── avtool.bat           CLI 入口
├── AGENT.md             给 AI agent 的接口契约文档
└── README.md
```

**Python 要求**：3.8+ 且安装时勾选了 tcl/tk（tkinter）。
若想连 Python 也免装，把便携版 Python 放进 `python/`（见下）。

### 依赖查找顺序

`bin/` 内置 → 系统 PATH → 常见安装位置（`C:\ffmpeg\bin` 等）。

内置的 `bin/ffmpeg.exe` 启动时会被实际执行一次做可用性校验，
若因架构不符或文件损坏跑不起来，会自动降级到系统版本并在日志里标注。

## 合并音视频

1. 选「视频源」和「音频源」
2. 点「自动识别同目录配对」可自动匹配同名文件（识别 `xxx_video.mp4` ↔ `xxx_audio.m4a` 之类后缀）
3. 选输出路径 → 「开始合并」

| 模式 | 说明 |
|---|---|
| 无损封装 `-c copy` | 不重编码，秒级完成，画质零损失（推荐） |
| 重编码 | 可调编码器 / CRF / preset / 音频码率，支持剪辑、音量、音频延迟 |

**已知限制**：无损封装模式下「音量」「音频延迟」「时间裁剪」无法生效
（FFmpeg 的 `-vf`/`-af` 与 `-c copy` 互斥）。程序会弹窗询问是改用重编码还是忽略这两项。

## 无损压缩

两类方案，实测数据（1080p30 合成素材）：

### 数学无损 —— 输出与源逐像素/逐样本完全一致

| 方案 | 压缩比 | 速度 | 适用 |
|---|---|---|---|
| x264 无损 (qp 0) | ~1.0x | 快 | 兼容性最好，通用 |
| x265 无损 | ~0.7x | 慢 | 追求小体积 |
| FFV1 归档级 | ~0.35x | 快 | 长期归档、带 CRC 校验 |
| NVENC h264 无损 | ~0.87x | 很快 | 显卡加速 |
| NVENC hevc 无损 | ~0.74x | 最快 | 显卡加速 + 更小 |

> **注意**：源文件本身已是有损压缩（如普通 MP4/H.264）时，
> 无损重封装**通常不会变小**，只能避免二次损失。想显著瘦身请用高压缩比。

### 高压缩比 —— 视觉无损（CRF/CQ 恒定质量，非数学无损）

| 方案 | 压缩比 | 速度 |
|---|---|---|
| H.264 CRF 18 / 23 / 28 | 2.7x / 4.2x / 8.6x | 快 |
| H.265 CRF 23 / 28 | 3.2x / 5.4x | 慢 |
| NVENC H.264 CQ 26 | 3.2x | 最快 |
| NVENC H.265 CQ 26 | 3.4x | 最快 |
| NVENC AV1 CQ 30 | 2.9x | 最快 |

可选同时降分辨率（1080p/720p/480p/360p）。

## 格式转换

### 一、转换 / 仅换容器

选中源文件后，先选**操作模式**，再选**目标格式**。程序会自动推荐编码组合，
也可手动改视频/音频编码器。

**19 种目标格式**

| 类型 | 格式 |
|---|---|
| 视频 (10) | MP4、MKV、MOV、WebM、AVI、FLV、TS、GIF、M4V、3GP |
| 音频 (9) | MP3、M4A、AAC、FLAC、WAV、OPUS、OGG、AC3、MKA |

**5 种操作模式**

| 模式 | 说明 |
|---|---|
| 转换格式 `convert` | 按目标容器重编码，可调 CRF / preset / 码率 / 分辨率 / 帧率 |
| 仅换容器 `remux` | `-c copy` 只改封装，不重编码、秒级完成 |
| 从视频提取音频 | 丢弃画面，输出音频文件 |
| 只保留画面 | 丢弃音轨，输出无声视频 |
| 只保留音频 | 丢弃音轨但保留视频轨（仅 MKA 这类多流容器可用） |

**兼容性预检**：选「仅换容器」时会检查源编码与目标容器是否匹配，
不匹配（如 H.264 → WebM）会即时警告并建议改用重编码。

> 裸音频容器（`.mp3` `.aac` `.opus` 等）只能装一条音频流，
> 塞视频轨会报 `Could not write header`，程序会提前拦截并提示。

### 二、编码器选择

| 视频编码 | 说明 |
|---|---|
| H.264 (x264) | 兼容性最好，软编 |
| H.265 (x265) | 同画质体积更小，软编较慢 |
| VP9 / AV1 (libvpx) | WebM 用 |
| MPEG-4 Part 2 | AVI / 3GP 用 |
| NVENC H.264 / H.265 / AV1 | 显卡硬编，最快 |

| 音频编码 | 说明 |
|---|---|
| AAC / MP3 / Opus / Vorbis / AC3 | 有损 |
| FLAC / PCM | 无损 |

## 批量处理

1. 「添加文件」或「添加文件夹」（可选递归子目录）
2. 选**操作模式**（转换 / 压缩 / 提取音频 / 仅换容器）与对应方案
3. 设定**输出目录**与**命名模板** → 开始批量

**命名模板占位符**

| 占位符 | 含义 | 示例 |
|---|---|---|
| `{name}` | 原文件名（不含扩展名） | `video.2024.final` |
| `{ext}` | 原扩展名（不含点） | `mp4` |
| `{index}` | 序号 | `1` |
| `{date}` | 当天日期 | `20260929` |
| `{op}` | 操作名 | `convert` / `compress` |

默认模板 `{name}_{op}`。程序自动补目标扩展名，并对重名文件追加序号防覆盖。

**列表操作**：点击表头排序（名称 / 大小 / 状态 / 时长）、重复导入自动去重、
单条或批量移除、实时显示条数 / 合计体积 / 待处理与成败统计。

## 让整包完全不依赖系统 Python（可选）

1. 下载 [Python embeddable package](https://www.python.org/downloads/windows/)（Windows x64）
2. 解压到本目录下的 `python\`
3. 从官方安装版的 `Lib\tkinter\` 和 `DLLs\` 里把 `tkinter`、`_tkinter.pyd`、
   `tcl86t.dll`、`tk86t.dll` 及 `tcl\` 目录拷进对应位置
4. `启动.bat` 会自动优先使用它

## 环境自检

启动时自动检查并在日志区打印报告。也可单独运行：

```bat
python merge_av.py --check
```

输出包含：Python 版本、内置 ffmpeg 路径与来源、必需/可选组件状态、NVENC 是否可用、
当前可用的压缩方案清单。

## CLI 接口（给 AI agent / 脚本）

完整契约见 **`AGENT.md`**。三个核心约定：

**1. stdout 永远是单个 JSON**

```json
{"ok": true, "action": "convert", "schema": "1",
 "data": {"output": "…", "size_out_human": "25.0 MB", "ratio": "4.20x"},
 "error": null}
```

ffmpeg 日志、进度全部走 stderr。`json.loads(stdout)` 一定成功。

**2. 退出码语义化**

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 参数错误 |
| 3 | 源文件不存在或不可读 |
| 4 | ffmpeg 缺失或无法执行 |
| 5 | ffmpeg 执行失败 |
| 6 | 输出文件已存在（未加 `--overwrite`） |

**3. 默认不覆盖任何文件**，撞车直接返回退出码 6。

### 动作一览

| 动作 | 用途 |
|---|---|
| `capabilities` | 本机能力、可用方案、全部合法取值（**agent 应先调这个**） |
| `probe` | 读媒体信息（时长/分辨率/编码/流数/体积） |
| `merge` | 合并分离的音视频 |
| `compress` | 压缩（数学无损 或 高压缩比） |
| `convert` | 格式转换 / 仅换容器 |
| `extract` | 轨道提取（提音频 / 去音频 / 仅换容器） |
| `batch` | 批量处理文件或整个目录 |

### 给 agent 的推荐流程

```
capabilities → probe → <动作> --dry-run → <动作> --quiet
```

`--dry-run` 返回将要执行的 ffmpeg 命令（`command` 数组 + `command_str` 字符串），
确认无误后再实跑。dry-run 不写盘，目标已存在时不会失败，而是用 `data.conflict`
字段告诉你「真跑会不会撞车」。批量任务加 `--json-progress`，进度按行写 stderr，便于长任务汇报。

## 给 AI agent 的 skill

`skill/av-toolbox/` 是一个可直接装进 AI agent 的 skill，让 agent 知道**怎么找到并用对**这个工具箱。

```
skill/av-toolbox/
├── SKILL.md       给 agent 的操作手册（动作、参数、错误码表）
├── README.md      安装与排障
└── scripts/
    ├── locate.py                            定位工具箱 + 环境自检
    └── example_merge_and_compress.py        agent 调用示例（可直接跑）
```

安装：把 `skill/av-toolbox/` 拷进 agent 的 skill 目录

```bash
cp -r skill/av-toolbox ~/.workbuddy/skills/          # 用户级
cp -r skill/av-toolbox <项目>/.workbuddy/skills/      # 项目级
```

验证：

```bash
python skill/av-toolbox/scripts/locate.py --json
```

成功返回 `"ok": true` 加工具箱路径、Python 解释器、ffmpeg 来源、NVENC 可用性，
以及本机**实际可用**的 preset 与格式清单（agent 据此自我校准，无需读源码）。

> skill 与工具箱是分开的两份东西：skill 只有几十 KB，**不含** ffmpeg；
> 工具箱才是几百 MB 的处理本体。
