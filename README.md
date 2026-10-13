# VideoFix (vfix) - 视频智能修复、高效压缩、多分卷合并与自动去黑边工具

`vfix` 是一款集**深度损坏修复**、**硬件加速高压缩比转码**、**多分卷无损秒级合并**、**智能去除黑边与短剧切片**于一身的全能视频处理工具。

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
5. **智能防体积膨胀**：
   - 智能分析源片码率，若原片码率已在合理精简区间，自动转为极速重封装，严防转码后体积不降反增。

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

### 部署与全局命令配置
```bash
git clone https://github.com/Vanch20/VideoFix.git
cd VideoFix

# 创建系统全局快捷命令
sudo cp video_repair_compress.py /usr/local/bin/vfix
sudo cp video_autocrop.py /usr/local/bin/video_autocrop.py
sudo ln -sf /usr/local/bin/video_autocrop.py /usr/local/bin/vcrop
sudo chmod +x /usr/local/bin/vfix /usr/local/bin/vcrop
```

---

## 📖 命令行帮助速查 (`vfix --help`)

```text
用法: vfix [-h] [-o OUTPUT_DIR] [-d] [-r] [-M] [-m] [-y] [-c [CROP]]
            [-a ASPECT] [-s] [-D] [--max-ad-duration MAX_AD_DURATION]
            [--min-segment-duration MIN_SEGMENT_DURATION] [--crf CRF]
            [--preset {ultrafast,superfast,veryfast,faster,fast,medium,slow}]
            [--threshold THRESHOLD]
            [--encoder {hevc_videotoolbox,h264_videotoolbox,libx264,libx265}]
            [--h264] [--cpu] [--force-compress] [--force-repair-only]
            inputs [inputs ...]
```

### 常用选项说明

| 参数 | 说明 |
| :--- | :--- |
| `-o, --output-dir` | 指定输出目录或输出文件名（如 `-o /mnt/nas/` 或 `-o full_movie.mp4`） |
| `-d, --delete-source` | 处理成功后自动删除源文件（失败或中断时严密保护不删除） |
| `-r, --recursive` | 递归扫描并处理指定目录及其下所有子目录中的视频 |
| **`-M, --merge-only`** | **【合并强烈推荐】** 无损极速流复制合并多个分卷（0 重编码、秒级完成） |
| `-m, --merge` | 合并多个分卷并重新压制瘦身（若各分卷分辨率/码率不一时自动对齐重编码） |
| `-y, --yes` | 合并前跳过交互式分卷顺序确认提示，直接开始处理 |
| `-c, --crop` | 智能去除视频黑边（自动识别并剔除字幕广告干扰；亦可指定尺寸） |
| `-a, --aspect` | 锁定目标画幅比例居中裁剪（如 `9:16`, `16:9`, `2.39:1`） |
| `-s, --split-scenes` | 智能场景切片模式（按横竖屏切换时间点自动分段导出） |
| `-D, --drop-ads` | 自动检测并剔除短剧广告/插片分段（仅保留正片） |
| `--crf` | 恒定画质系数（默认 22；18~24 为黄金画质平衡区间） |
| `--h264` | 指定输出为 H.264 编码（最大化兼容老旧电视或播放器） |
| `--force-repair-only` | 仅修复封装结构损坏与音视频同步，不重新编码 |

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

---

## 📄 License
MIT License
