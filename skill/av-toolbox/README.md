# av-toolbox skill —— 安装与使用

给 AI agent 用的 skill，让它可以调用本机的**音视频工具箱**完成音视频处理。

## 这个 skill 依赖什么

它本身**不含**音视频处理逻辑，只负责**找到并用对**那个工具箱。
工具箱是「自带 ffmpeg + Python」的项目（根目录含 `avtool.py`）。

**Skill 与工具箱是分开的两份东西**：

| | 内容 | 说明 |
|---|---|---|
| 工具箱 | 音视频处理本体（ffmpeg + GUI + CLI） | 约 320MB（含 ffmpeg），**不在本 skill 目录内** |
| 本 skill | `SKILL.md` + 调用脚本 | 几十 KB，只描述「怎么调」 |

所以 skill 目录里**不放** ffmpeg 和工具箱代码 —— 那是项目根目录的职责。

> **ffmpeg 不随仓库分发**：单个二进制 160MB，超过 GitHub / Gitee 的单文件 100MB 限制。
> 克隆工具箱后跑一次 `python fetch_ffmpeg.py` 补齐（详见工具箱的 README）。

## 安装

### 方式一：项目内（推荐，随项目分发）

本项目已经放好了，无需额外操作：

```
project/                          ← 工具箱根目录
├── avtool.py                     ← CLI 接口
├── fetch_ffmpeg.py               ← 下载 bin/ 里的 ffmpeg（首次运行必做）
├── merge_av.py / bin/ ...        ← 工具箱本体
└── skill/
    └── av-toolbox/               ← 本 skill
        ├── SKILL.md
        └── scripts/
            ├── locate.py                             ← 定位工具箱 + 环境自检
            └── example_merge_and_compress.py         ← 调用示例
```

把 `skill/av-toolbox/` 整个目录拷到 agent 的 skill 目录即可：

```bash
# 用户级（所有项目可用）
cp -r skill/av-toolbox ~/.workbuddy/skills/

# 项目级（仅当前项目）
cp -r skill/av-toolbox <该项目>/.workbuddy/skills/
```

### 方式二：独立使用

工具箱在别处时，先把 skill 装进 agent，再让 skill 自己去定位：

```bash
cp -r skill/av-toolbox ~/.workbuddy/skills/
# 然后告诉 skill 工具箱在哪（任选其一）
export AVTOOLBOX_HOME="D:/AVToolbox"
# 或直接说明路径，skill 会用搜索的方式找到 avtool.py
```

## 验证安装

```bash
python ~/.workbuddy/skills/av-toolbox/scripts/locate.py --json
```

成功时输出 `"ok": true` 加工具箱路径、Python 解释器、ffmpeg 来源、NVENC 可用性，
以及本机**实际可用**的 preset 与格式清单。失败时会给出 `hint` 说明该设哪个环境变量。

## 脚本说明

### `scripts/locate.py` —— 定位与自检

```bash
python locate.py            # 人类可读
python locate.py --json     # agent 首选，结构化输出
python locate.py --check    # 附带完整自检报告
```

定位顺序：`$AVTOOLBOX_HOME` → 从脚本位置向上 3 层（适用于本 skill 在项目内的情况）
→ 常见路径（`C:\Users\pc\Desktop\test\project`、`C:\AVToolbox`、`D:\AVToolbox`、`~/AVToolbox`）。

退出码：`0` 成功 / `3` 找不到工具箱 / `4` 找到了但跑不起来。

### `scripts/example_merge_and_compress.py` —— 调用示例

```bash
python example_merge_and_compress.py <video> <audio> <out_dir>
```

演示了 agent 该有的完整调用姿势：

1. `capabilities` —— 会话开始先拿本机能力（**可用 preset 从这里来，不硬编码**）
2. `probe` —— 处理前看清源有什么轨道
3. `merge --dry-run` —— 先拿 ffmpeg 命令确认，并检查 `data.conflict`（真跑会不会撞车）
4. `merge` —— 实跑
5. `compress` —— 目标是瘦身所以用**高压缩比**而非无损

脚本是幂等的：重复跑不会因「输出已存在」而中断。

## 关键约定（也是 SKILL.md 里的铁律）

1. **不手搓 ffmpeg 命令**，一律走 `avtool.py`。它已封装流映射、容器兼容校验、防覆盖、报错翻译。
2. **先 `capabilities` 再动手**，可用 preset / 格式随机器变化。
3. **改文件前先 `--dry-run`**。dry-run 不写盘，因此即使目标已存在也**不会失败**，
   而是用 `data.conflict` 字段报告冲突。
4. **不默认加 `--overwrite`**，那是保护用户数据。
5. **`compress` 前先判断源是否已有损**。目标是「减小体积」时用高压缩比 preset；
   无损对已是有损编码的源只会**变大**。

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `locate.py` 返回 `ok: false`，退出码 3 | 找不到工具箱 | 设 `AVTOOLBOX_HOME` 指向含 `avtool.py` 的目录 |
| `capabilities` 里 `ffmpeg_origin` 是 `"系统"` | bin/ 里没有内置二进制（仓库不含） | 跑 `python fetch_ffmpeg.py` 下载，或忽略（用系统版本也能干活） |
| 退出码 4 + `ffmpeg: null` | 工具箱在但 ffmpeg 跑不起来 | 跑 `python fetch_ffmpeg.py --check` 看是缺失还是无法运行 |
| `ModuleNotFoundError: tkinter` | 用了托管 Python | CLI 用系统 Python；GUI 必须用系统 Python（托管版不带 tkinter） |
| `preset_unavailable` | 本机缺该编码器或无 NVENC | 从 `capabilities.compress_presets` 里换一个 |
| `output_exists`（退出码 6） | 目标已存在 | 加 `--overwrite`，或换 `--output` |
| `incompatible_container` | remux 目标容器装不下源编码 | 改 `--mode convert`，或加 `--force`（多数会失败） |

## 相关文档

- **`SKILL.md`** —— 给 agent 的操作手册（动作、参数、错误码）
- 工具箱根目录 **`AGENT.md`** —— CLI 完整接口契约
- 工具箱根目录 **`README.md`** —— 面向用户的功能说明
