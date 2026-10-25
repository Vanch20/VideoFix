# VideoFix (vfix) - 视频智能修复、高效压缩、多分卷合并、自动去黑边与智能字幕提取工具

`vfix` 是一款集**深度损坏修复**、**硬件加速高压缩比转码**、**多分卷无损秒级合并**、**智能去除黑边与短剧切片**以及**智能语音识别字幕提取 (ASR)** 于一身的全能视频处理工具。

专为 NAS 服务器、ARM 嵌入式 Linux 盒子（如 RK3588/极光盒子）、macOS Apple Silicon 设备及常规 PC 设计打造。

---

## 🌟 核心特性

1. **深度抢救修复**：专治各种损坏/未下载完全视频（如 `.part`、`.tmp`、元数据丢失、高坏帧率文件），自动突破 FFmpeg 限制修复封装与音视频同步。
2. **极速硬件加速**：
   - macOS：全自动调用 Apple Silicon GPU（`hevc_videotoolbox` / `h264_videotoolbox`）。
   - Linux / ARM：原生支持 Rockchip 瑞芯微 RK3588/RK3568 VPU 硬件编解码加速（`hevc_rkmpp` / `h264_rkmpp`）及通用多核并行加速（`libx264` / `libx265`），视频体积立减 30% ~ 70%。
3. **多分卷秒级合并 (`-M`)**：
   - 支持多 CD 分卷（如 `CD1.avi` `CD2.avi` 或 `Part1.mp4` `Part2.mp4`）极速无损流复制合并。
   - **0 重编码、0 损画质、秒级完成**；合并成功后可选自动清理原分卷 (`-d`)。
4. **智能自动去黑边 (`-c` / `-a`)**：
   - 自动检测并剔除画面上下/左右无用黑边，强力过滤弹幕、字幕跑马灯与边角广告干扰。
   - 支持指定比例锁定（如短剧 `-a 9:16`、宽银幕电影 `-a 2.39:1`）。
5. **智能语音字幕提取 (`vsub` / `vfix -S`)**：
   - 双引擎自适应硬件加速：macOS 优先利用 Apple Silicon GPU/ANE 极速转录 (`mlx-whisper`)；Linux/NAS 采用成熟高效的 `faster-whisper` (CTranslate2)。
   - 智能 Silero-VAD 静音切片过滤（防静音段模型幻觉重复）与标点分句行宽限制（防字幕过长溢出屏幕）。
   - 默认输出同名外挂 `.srt` 字幕，亦支持导出 `.vtt`、`.txt`、`.json`，提供 `--lang-suffix` 语言后缀及 `--embed` 软封装混流回视频容器。
6. **智能防体积膨胀**：
   - 智能分析源片码率，若原片码率已在合理精简区间，自动转为极速重封装，严防转码后体积不降反增。
7. **后台脱机运行与任务守护 (`-B` / `--status` / `--stop`)**：
   - 支持一键转入系统后台脱机运行（双重 `fork` + `setsid` 守护模式），即刻释放终端，关闭终端窗口或断开 SSH 远程连接绝不中断任务。
   - 支持一键状态查询（`--status`），随时调取任务 PID、已运行时长与最新实时日志动态。
   - 支持一键安全终止（`--stop`），向进程组发送优雅中断信号，确保未转码完成的源文件严格受保护绝不误删，自动清理临时缓存。
8. **全自动零配置日志审计 (100% 自动生成)**：
   - 默认开启 100% 自动日志记录，单文件独立安装（如直接复制至 `/usr/local/bin/vfix`）亦自包含保障，记录所有处理过的文件 (`processed.log`)、失败告警 (`failed.log`) 与安全删除的文件 (`deleted.log`)。

---

## 🚀 快速安装与配置

### 依赖项
- Python 3.8+
- FFmpeg (包含 `ffmpeg` 与 `ffprobe`)

```bash
# macOS
brew install ffmpeg

# Debian / Ubuntu / Armbian
sudo apt update && sudo apt install -y ffmpeg python3
```

### 部署与全局命令配置 (软链接模式，代码修改即时生效)
```bash
git clone https://github.com/Vanch20/VideoFix.git
cd VideoFix

# 推荐：一键建立全局命令软链接（以后在仓库里改代码或 git pull 即时生效，无需重复复制）
./install.sh

# 或者手动执行软链接：
sudo ln -sf "$(pwd)/video_repair_compress.py" /usr/local/bin/vfix
sudo ln -sf "$(pwd)/video_autocrop.py" /usr/local/bin/vcrop
sudo ln -sf "$(pwd)/video_subtitles.py" /usr/local/bin/vsub
sudo chmod +x video_repair_compress.py video_autocrop.py video_subtitles.py

# 安装 ASR 字幕识别依赖（按平台选择）:
# macOS (Apple Silicon 推荐极速硬件加速):
pip install mlx-whisper

# Linux / NAS / PC (或通用跨平台 CTranslate2 加速):
pip install faster-whisper
```

---

## 📖 命令行帮助速查 (`vfix --help`)

```text
用法: vfix [-h] [-o OUTPUT_DIR] [-d] [-r] [-B] [--status] [--stop] [-M] [-m]
            [-y] [-c] [--crop W:H:X:Y] [-a ASPECT] [-s] [-D]
            [-ss SS] [-to TO] [-t DURATION]
            [--crf CRF] [--preset PRESET] [--threshold THRESHOLD]
            [--encoder ENCODER] [--h264] [--cpu] [--force-compress] [--force-repair-only]
            [-S] [--subtitle-model SUBTITLE_MODEL] [--subtitle-lang SUBTITLE_LANG]
            [--subtitle-format {srt,vtt,txt,json,all} ...] [--subtitle-embed]
            [--log-dir LOG_DIR] [--no-log]
            [inputs ...]
```

### 常用选项说明

| 参数 | 说明 |
| :--- | :--- |
| **`-B, --background`** | **【后台运行】** 将任务转入后台脱机执行（即刻释放终端，终端关闭或网络中断不影响执行） |
| **`--status`** | **【状态查询】** 查看当前后台任务运行状态、PID、已运行时间与最新 15 行实时日志 |
| **`--stop`** | **【安全终止】** 安全优雅停止当前后台任务（未完成转码的源文件严格受保护，绝不删除） |
| `-o, --output-dir` | 指定输出目录或输出文件名（如 `-o /mnt/nas/` 或 `-o full_movie.mp4`） |
| `-d, --delete-source` | 处理或合并成功后自动删除源文件（失败、中断或截断异常时熔断保护绝对不删除） |
| `-k, --keep-name` | **【原名保存】** 生成文件保持与原文件名一致（不添加 `_compressed` / `_repaired`）。未指定 `-d` 时原文件重命名为 `_orig.mp4`；配合 `-d` 则直接就地替换 |
| **`-ss`, `-to`, `-t`** | **【精准剪切】** 遵循 FFmpeg 语法（如 `-ss 30m -to -5m` 去除开头30分钟和结尾5分钟；默认极速无损流复制，亦可配合压缩/去黑边） |
| **`--clean-tmp`** | **【空间卫士】** 扫描并彻底清理历史遗留的僵尸临时文件（因意外中断、断电等残留的 `vrc_*` / `vcrop_*` 并释放空间） |
| `--temp-dir` | 指定自定义临时文件暂存目录（默认自动优先选用大容量持久分区如 `~/.vfix/tmp`，杜绝 `/tmp` 内存盘打满） |
| `-r, --recursive` | 递归扫描并处理指定目录及其下所有子目录中的视频 |
| **`-M, --merge-only`** | **【合并强烈推荐】** 无损极速流复制合并多个分卷（0 重编码、秒级完成） |
| `-m, --merge` | 合并多个分卷并重新压制瘦身（若各分卷分辨率/码率不一时自动对齐重编码） |
| `-y, --yes` | 合并前跳过交互式分卷顺序确认提示，直接开始处理（后台模式下自动启用） |
| `-c, --autocrop` | 智能去除视频黑边（自动识别并剔除字幕广告干扰；亦可指定尺寸） |
| `-a, --aspect` | 锁定目标画幅比例居中裁剪（如 `9:16`, `16:9`, `2.39:1`） |
| `-s, --split-scenes` | 智能场景切片模式（按横竖屏切换时间点自动分段导出） |
| `-D, --drop-ads` | 自动检测并剔除短剧广告/插片分段（仅保留正片） |
| **`-S, --subtitles`** | **【提取字幕】** 视频处理/修复完成后自动提取语音字幕（默认生成同名外挂 `.srt`） |
| `--subtitle-embed` | 将提取的字幕直接软混流封装回原视频容器（MP4/MKV） |
| `--crf` | 恒定画质系数（默认 22；18~24 为黄金画质平衡区间） |
| `--h264` | 指定输出为 H.264 编码（最大化兼容老旧电视或播放器） |
| `--force-repair-only` | 仅修复封装结构损坏与音视频同步，不重新编码 |
| `--log-dir <DIR>` | 自定义运行与删除日志保存目录（默认全自动记录至：`~/.vfix/logs/`） |
| `--no-log` | 禁用所有执行与删除日志记录 |

---

## 🎬 智能字幕独立命令 (`vsub --help`)

`vsub` 是 `vfix` 套件中专门负责高质量、高性能语音字幕提取与混流的独立工具。

```text
用法: vsub [-h] [-o OUTPUT_DIR] [-r] [--engine {auto,mlx,faster-whisper}]
            [--model MODEL] [-l LANGUAGE] [--task {transcribe,translate}]
            [--format {srt,vtt,txt,json,all} ...] [--lang-suffix] [--embed]
            [--max-chars MAX_CHARS] [--initial-prompt INITIAL_PROMPT]
            [inputs ...]
```

### `vsub` 常用选项与场景

| 参数 | 说明 |
| :--- | :--- |
| `inputs` | 视频文件、通配符（如 `*.mp4`）或目录路径 |
| `-o, --output-dir` | 指定字幕文件存放目录（默认与原视频同目录） |
| `-r, --recursive` | 递归扫描目录及子目录下的所有音视频文件 |
| `--engine` | 引擎选择：`auto` (默认), `mlx` (Mac GPU 加速), `faster-whisper` (CTranslate2) |
| `--model` | 模型尺寸（默认: `large-v3-turbo`，可选 `base`/`small`/`medium`/`large-v3` 等） |
| `-l, --language` | 强制指定语言代码（如 `zh`, `en`, `ja`；默认自动检测） |
| `--format` | 导出格式（默认 `srt`，可选 `vtt`、`txt`、`json` 或 `all` 一键全导出） |
| `--lang-suffix` | 启用语言后缀（如生成 `movie.zh.srt`，默认保持纯同名 `movie.srt`） |
| `--embed` | 软封装混流：将提取的字幕直接混流入视频容器（0 重编码、秒级写入） |
| `--max-chars` | 单条字幕行宽字符限制（默认 35 字，超长自动按标点/词意平滑断句） |

---

## 💡 高频经典使用场景

### 1. 修复并压缩损坏/下载中的视频
```bash
# 修复损坏文件并在成功后自动清理原损坏文件
vfix broken_video.part -d

# 批量处理并输出到指定 NAS 共享目录
vfix *.part -o /mnt/nas/media/
```

### 2. 多分卷极速无损合并（秒级完成、0 画质损失）
```bash
# 将 CD1 和 CD2 秒级拼接合并为完整影片，并自动清理分卷文件
vfix CD1.avi CD2.avi -M -d -o Full_Movie.mp4

# 通配符批量合并
vfix Movie_Part*.mp4 -M
```

### 3. 视频智能去黑边 & 竖屏短剧居中锁定
```bash
# 智能检测并裁切上下/左右黑边（自动规避字幕干扰）
vfix movie.mp4 -c

# 将横屏/宽幅短剧精准锁定为 9:16 标准移动端竖屏
vfix drama.mp4 -a 9:16
```

### 4. 批量递归处理整个文件夹或 NAS 资料库
```bash
# 递归扫描整个媒体目录进行高压缩比瘦身转码
vfix /mnt/nas/videos/ -r -o /mnt/nas/compressed/
```

### 5. 快速修复无需重编码
```bash
# 针对仅有容器索引损坏、播放拖动卡顿的视频进行瞬时修复
vfix broken_index.mp4 --force-repair-only
```

### 6. 智能提取视频字幕 (独立 `vsub` 命令)
```bash
# 极速提取同名外挂字幕 (默认生成 movie.srt)
vsub movie.mp4

# 提取字幕并直接软混流进原视频容器中 (无需手动加载字幕)
vsub movie.mp4 --embed

# 同时导出 .srt, .vtt, .txt (文稿), .json 多种格式
vsub movie.mp4 --format all

# 生成带语言后缀的文件名 (如 movie.zh.srt，适配 Emby / Plex / Infuse)
vsub movie.mp4 --lang-suffix

# 递归提取整个目录下所有视频的字幕
vsub /mnt/nas/videos/ -r

# 在低功耗 NAS 上指定轻量模型
vsub movie.mp4 --model small
```

### 7. 视频修复/去黑边/压缩同时一键生成字幕 (`vfix -S`)
```bash
# 修复压缩的同时自动提取生成外挂字幕 movie_compressed.srt
vfix movie.mp4 -S

# 智能去黑边 + 硬件极速压缩 + 提取字幕软封装混流
vfix movie.mp4 -c -S --subtitle-embed
```

### 8. 后台脱机批量运行与任务守护 (大批量/长耗时必备)
```bash
# 后台批量处理整个 NAS 视频目录并删除原片，终端即刻释放，可安心断开 SSH
vfix /mnt/nas/videos/ -r -d -B

# 随时查看后台任务运行状态、处理进度与最新日志
vfix --status

# 实时流式监控处理日志
tail -f ~/.vfix/logs/vfix.log

# 如需提前终止，使用 --stop 优雅停止 (严格保护源文件，清理临时缓存)
vfix --stop
```

---

## 📝 审计日志与对账记录 (Logging System)

`vfix` 与 `vcrop` 内置了线程安全、开箱即用的自动化日志系统，无须任何额外配置。

默认所有日志保存在用户家目录：`~/.vfix/logs/`：

1. **`processed.log`（处理记录台账）**：
   - 记录所有执行处理的视频文件详细情况：
   - 记录输入路径、输出路径、原视频画幅与码率、转码前后体积对比（精确至字节并展示瘦身百分比）、实际耗时、处理模式（如 `COMPRESS`, `MERGE_ONLY`, `AUTOCROP`, `SPLIT_SCENES` 等）以及处理状态（`SUCCESS`, `SKIPPED`, `FAILED`）。
2. **`failed.log`（失败任务独立台账）**：
   - 专门独立收集所有处理失败或异常的文件（如严重损坏无法解码、封装格式异常、截断被安全拦截等）。
   - 记录精确时间戳、文件路径、原属性参数以及底层详尽报错原因，便于批量处理后一目了然排查问题。
3. **`deleted.log`（删除文件审计日志）**：
   - 专为 `-d / --delete-source` 打造的安全审计台账。
   - 无论合并清理旧分卷，还是转码瘦身后清理原片，每一次删除操作均记录：精确时间戳、被删除文件的绝对路径、删除前真实大小、触发原因以及替换该文件的目标产物绝对路径。
4. **`vfix.log` / `vcrop.log`（综合运行日志）**：
   - 记录每次命令执行会话（Session）的开始/结束边界、命令行原始参数、吞吐统计与异常报错堆栈。

### 自定义与禁用日志
```bash
# 自定义日志保存目录 (例如写入挂载盘或项目专用目录)
vfix input.mp4 -d --log-dir /path/to/my_logs/

# 也可以通过环境变量指定
export VFIX_LOG_DIR="/mnt/nas/vfix_logs"

# 临时完全禁用日志记录
vfix input.mp4 --no-log
```

---

## 📄 License
MIT License
