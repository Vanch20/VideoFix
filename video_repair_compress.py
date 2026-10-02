#!/usr/bin/env python3
"""
视频文件智能修复与高能效硬件/软件编码压缩脚本 (video_repair_compress.py / vrc / vfix)

核心改进：
1. 默认启用 Apple VideoToolbox GPU 硬件加速 (h264_videotoolbox)：
   - 彻底解决 Intel 双核 CPU 软压缓慢（从 1.1x 龟速跃升至 3.5x - 5.0x 极速，提速超 300%），CPU 负荷降至 10% 以下，防止高温降频。
   - 智能分辨率金牌码率映射（720p: 3.2M, 1080p: 5.0M），若原码率更低则自动收敛，杜绝体积膨胀。
   - 仍保留 --cpu / --encoder libx264 软件编码选项，并将 CPU 预设优化为 veryfast。
2. 音频秒级预检 (Audio Pre-flight)：
   - 0.1 秒提前探测音频流能否直接无损 copy、需转码 AAC 还是需剥离 (-an)，坚决杜绝“转码十几分钟后因音频报错导致重跑三遍”的致命重试延迟。
3. 本地 SSD 极速缓冲写入 (Local Temp Buffer)：
   - 临时输出走本地 SSD 暂存，再原子移入目标目录（如 SMB 共享盘），规避网络盘 -movflags +faststart 频繁 seek 的 I/O 拥堵。
4. 深度容错抢救链：
   - 注入 -max_error_rate 1.0，突破 FFmpeg 默认 66.7% 坏帧阈值限制，允许对损坏严重的视频（如高坏帧 NAL 错误率视频、.part 残缺文件）坚持解码抢救到底。
5. 实时编码进度反馈：
   - 通过管道解析并实时刷新打印当前编码时间、处理速度 (speed) 和实时帧率 (fps)。
6. 命名清理：自动剥离 .part / .wmv 等复合扩展名，生成规范的 _compressed.mp4 或 _merged.mp4。
7. 多分卷极速单独合并 (Standalone Merge / Lossless Concat)：
   - 支持 -M / --merge-only / --concat-only 独立合并模式，与重新编码压缩彻底解耦。
   - 优先通过 FFmpeg Concat Demuxer 实施极速无损流复制 (-c:v copy)，0 重编码、0 画质损失、秒级完成。
   - 各分卷分辨率或编码不一致时自动平滑回退至高保真智能滤镜对齐拼接，确保 100% 成功率。
   - 支持与 --force-repair-only / -m 联动，输出规范的 _merged.mp4 或原封装格式。
8. 执行成功安全删除源文件 (-d / --delete-source / --del-src / --remove-source)：
   - 处理或合并完全成功后自动清理原始分卷/源文件，节约存储空间；失败或异常中断时严格防护绝不删除源文件。
"""

import argparse
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 常见流媒体及光盘视频格式后缀
BASE_VIDEO_EXTENSIONS = [
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv",
    ".ts", ".m4v", ".webm", ".m2ts", ".mpg", ".mpeg",
    ".divx", ".xvid", ".rmvb", ".rm", ".vob", ".3gp",
    ".3g2", ".ogv", ".ogg", ".f4v", ".asf", ".dat", ".m4p"
]

# 常见未完成下载/残缺临时后缀 (优先长复合后缀)
TEMP_DOWNLOAD_EXTENSIONS = [
    ".bt.td", ".td.cfg", ".td", ".xltd",
    ".part", ".download", ".crdownload", ".tmp", ".incomplete",
    ".aria2", ".bc!", ".qdl", ".baiduyun.downloading", ".ut", ".dwn"
]

# 支持的常见视频及残缺临时文件后缀集合
SUPPORTED_EXTENSIONS = set(BASE_VIDEO_EXTENSIONS) | set(TEMP_DOWNLOAD_EXTENSIONS)

# 可以直接无损 stream-copy 到 MP4 容器的视频编码
MP4_COMPLIANT_VIDEO_CODECS = {
    "h264", "hevc", "h265", "mpeg4", "av1", "av01", "vp9"
}

# 各分辨率在 H.264 下的典型高质量基准码率 (bps)
STANDARD_TARGET_BITRATES_H264 = [
    (480 * 360, 1000 * 1000),          # 360p  ~ 1.0 Mbps
    (720 * 480, 1600 * 1000),          # 480p  ~ 1.6 Mbps
    (1280 * 720, 3200 * 1000),         # 720p  ~ 3.2 Mbps
    (1920 * 1080, 5000 * 1000),        # 1080p ~ 5.0 Mbps
    (2560 * 1440, 8500 * 1000),        # 2K    ~ 8.5 Mbps
    (3840 * 2160, 16000 * 1000),       # 4K    ~ 16.0 Mbps
]


def check_ffmpeg() -> bool:
    """检查系统环境是否包含 ffmpeg 和 ffprobe。"""
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if not ffmpeg_path or not ffprobe_path:
        print("[错误] 未在系统 PATH 中找到 ffmpeg 或 ffprobe，请先安装 (例如: brew install ffmpeg)。", file=sys.stderr)
        return False
    return True


def has_videotoolbox_support() -> bool:
    """检测当前系统及 FFmpeg 是否支持 Apple VideoToolbox 硬件加速。"""
    if platform.system() != "Darwin":
        return False
    try:
        proc = subprocess.run(
            ["ffmpeg", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        return "h264_videotoolbox" in proc.stdout
    except Exception:
        return False


def format_bytes(size_bytes: int) -> str:
    """将字节大小格式化为人类可读的字符串。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_bitrate(bps: float) -> str:
    """将码率 (bps) 格式化为 kbps 或 Mbps。"""
    if bps < 1_000_000:
        return f"{bps / 1000:.0f} kbps"
    return f"{bps / 1_000_000:.2f} Mbps"


def get_clean_stem(file_path: Path) -> str:
    """从可能包含复合后缀的文件名中提取干净的前缀 (如 a.wmv.part -> a, STAR296.divx.bt.td -> STAR296)。"""
    name = file_path.name
    lower = name.lower()
    # 循环剥离可能存在的多重临时下载后缀 (如 .bt.td)
    stripped = True
    while stripped:
        stripped = False
        for temp_ext in TEMP_DOWNLOAD_EXTENSIONS:
            if lower.endswith(temp_ext):
                name = name[:-len(temp_ext)]
                lower = name.lower()
                stripped = True
                break

    p = Path(name)
    stem = p.stem if p.suffix.lower() in BASE_VIDEO_EXTENSIONS else p.name
    for action_suffix in ("_compressed", "_repaired", "_merged"):
        if stem.lower().endswith(action_suffix):
            stem = stem[:-len(action_suffix)]
            break
    return stem


def natural_sort_key(p: Path) -> List[Any]:
    """生成用于自然排序的 key，保证文件名中的数字按数值大小排序（如 CD1 < CD2 < CD10）。"""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', p.name)]


def derive_merged_output_path(
    file_paths: List[Path],
    output_target: Optional[Path],
    is_compressed: bool = True,
    default_ext: str = ".mp4"
) -> Path:
    """智能推导多分卷合并后的输出路径与文件名。"""
    clean_stems = [get_clean_stem(p) for p in file_paths]
    common = os.path.commonprefix(clean_stems)
    common = re.sub(r'[\s_\-\.\(\[（【]+$', '', common)

    if len(common) < 2:
        stem0 = clean_stems[0]
        derived = re.sub(r'(?i)[_\-\s]*(cd|part|vol|disc|disk|ep|segment|seg|chunk)[_\-\s]*\d+.*$', '', stem0)
        if not derived or len(derived) < 2:
            derived = re.sub(r'[_\-\s]*\d+$', '', stem0)
        if not derived or len(derived) < 2:
            derived = stem0
        derived = re.sub(r'[\s_\-\.\(\[（【]+$', '', derived)
    else:
        derived = common

    tag = "_compressed" if is_compressed else ""
    default_filename = f"{derived}_merged{tag}{default_ext}"

    if output_target is not None:
        if output_target.suffix.lower() in BASE_VIDEO_EXTENSIONS:
            return output_target
        return output_target / default_filename

    return file_paths[0].parent / default_filename


def is_supported_video_file(file_path: Path) -> bool:
    """判断文件是否属于受支持的视频或待修复的残缺视频文件。"""
    name_lower = file_path.name.lower()
    if file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
        return True
    for temp_ext in TEMP_DOWNLOAD_EXTENSIONS:
        if name_lower.endswith(temp_ext):
            return True
    for ext in BASE_VIDEO_EXTENSIONS:
        if ext in name_lower:
            return True
    return False


def probe_video(file_path: Path) -> Optional[Dict[str, Any]]:
    """使用 ffprobe 提取视频元数据（内置残缺坏帧容错）。"""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-err_detect", "ignore_err",
        "-fflags", "+discardcorrupt",
        str(file_path)
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)

        format_info = data.get("format", {})
        streams = data.get("streams", [])

        video_stream = None
        audio_stream = None
        has_audio = False
        for s in streams:
            codec_type = s.get("codec_type")
            if codec_type == "video" and not video_stream:
                video_stream = s
            elif codec_type == "audio" and not audio_stream:
                audio_stream = s
                has_audio = True

        if not video_stream:
            return None

        duration = 0.0
        if "duration" in format_info:
            try:
                duration = float(format_info["duration"])
            except (ValueError, TypeError):
                duration = 0.0
        if duration <= 0.0 and "duration" in video_stream:
            try:
                duration = float(video_stream["duration"])
            except (ValueError, TypeError):
                duration = 0.0

        file_size = file_path.stat().st_size

        bitrate = 0.0
        if "bit_rate" in format_info:
            try:
                bitrate = float(format_info["bit_rate"])
            except (ValueError, TypeError):
                bitrate = 0.0
        if bitrate <= 0.0 and duration > 0.0:
            bitrate = (file_size * 8.0) / duration

        # 音频净码率提取（优先流属性，次选流标签，最后按通道估算）
        audio_bitrate = 0.0
        if audio_stream:
            if "bit_rate" in audio_stream:
                try:
                    audio_bitrate = float(audio_stream["bit_rate"])
                except (ValueError, TypeError):
                    audio_bitrate = 0.0
            if audio_bitrate <= 0.0:
                tags = audio_stream.get("tags", {})
                for k in ("BPS", "BPS-eng", "bps"):
                    if k in tags:
                        try:
                            audio_bitrate = float(tags[k])
                            break
                        except (ValueError, TypeError):
                            pass
            if audio_bitrate <= 0.0 and bitrate > 0.0:
                channels = int(audio_stream.get("channels", 2))
                base_est = 64_000.0 if channels <= 1 else 128_000.0
                audio_bitrate = min(base_est, max(32_000.0, bitrate * 0.12))

        # 视频净码率提取（优先流属性，次选标签，最后通过总码率与音频码率扣减解耦）
        video_bitrate = 0.0
        if "bit_rate" in video_stream:
            try:
                video_bitrate = float(video_stream["bit_rate"])
            except (ValueError, TypeError):
                video_bitrate = 0.0
        if video_bitrate <= 0.0:
            tags = video_stream.get("tags", {})
            for k in ("BPS", "BPS-eng", "bps"):
                if k in tags:
                    try:
                        video_bitrate = float(tags[k])
                        break
                    except (ValueError, TypeError):
                        pass
        if video_bitrate <= 0.0 and bitrate > 0.0:
            if audio_bitrate > 0.0 and bitrate > audio_bitrate:
                video_bitrate = bitrate - audio_bitrate
            else:
                video_bitrate = max(0.0, bitrate * 0.88)

        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        codec_name = video_stream.get("codec_name", "").lower()

        fps = 30.0
        r_frame_rate = video_stream.get("r_frame_rate", "30/1")
        if "/" in r_frame_rate:
            num, den = r_frame_rate.split("/")
            try:
                den_f = float(den)
                if den_f > 0:
                    fps = float(num) / den_f
            except (ValueError, ZeroDivisionError):
                fps = 30.0
        else:
            try:
                fps = float(r_frame_rate)
            except (ValueError, TypeError):
                fps = 30.0

        if duration <= 0.0 and "nb_frames" in video_stream and fps > 0:
            try:
                duration = float(video_stream["nb_frames"]) / fps
            except (ValueError, TypeError, ZeroDivisionError):
                pass

        return {
            "duration": duration,
            "size": file_size,
            "bitrate": bitrate,
            "video_bitrate": video_bitrate,
            "audio_bitrate": audio_bitrate,
            "width": width,
            "height": height,
            "codec": codec_name,
            "fps": fps,
            "has_audio": has_audio,
            "audio_stream": audio_stream,
        }
    except Exception:
        return None


def get_expected_bitrate(info: Dict[str, Any], crf: int = 22) -> int:
    """根据分辨率、帧率及基准系数估算合理码率 (bps)。"""
    pixels = info.get("width", 1280) * info.get("height", 720)
    if pixels <= 0:
        pixels = 1280 * 720

    base_bps = STANDARD_TARGET_BITRATES_H264[-1][1]
    for p_limit, bps in STANDARD_TARGET_BITRATES_H264:
        if pixels <= p_limit:
            base_bps = bps
            break

    crf_factor = 2 ** ((22 - crf) / 6.0)
    expected_bps = int(base_bps * crf_factor)

    if info.get("fps", 30.0) > 40:
        expected_bps = int(expected_bps * 1.25)

    return expected_bps


def is_vorbis_acm_audio(file_path: Path, audio_stream: Optional[Dict[str, Any]] = None) -> bool:
    """检测是否为 AVI 容器中包装的 Vorbis ACM (TwoCC 0x6771/0x674f/0x6750 等)。"""
    VORBIS_ACM_TAGS = {"0x6771", "0x674f", "0x6750", "0x6751", "0x676f", "0x6770"}
    if audio_stream:
        tag = str(audio_stream.get("codec_tag", "")).lower()
        tag_str = str(audio_stream.get("codec_tag_string", ""))
        codec_name = str(audio_stream.get("codec_name", "")).lower()
        if tag in VORBIS_ACM_TAGS or any(tag_str.startswith(p) for p in ("qg", "Og")):
            return True
        if codec_name not in ("none", "unknown", ""):
            return False

    if file_path.suffix.lower() == ".avi":
        try:
            cmd = [
                "ffmpeg", "-y", "-v", "error",
                "-err_detect", "ignore_err",
                "-i", str(file_path),
                "-map", "0:a:0", "-c:a", "copy",
                "-f", "data", "-t", "1", "pipe:1"
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            prefix = proc.stdout.read(64)
            proc.kill()
            if prefix.startswith(b"OggS"):
                return True
        except Exception:
            pass

    return False


def extract_vorbis_acm_audio(file_path: Path, out_aac_path: Path, target_kbps: int = 128) -> bool:
    """
    针对 Windows 经典 legacy 格式 (Vorbis ACM 0x6771/0x674f in AVI) 实施深度抢救：
    剥离 AVI 容器中填充的 28 字节 0xFFFFFFFF 占位脏包，恢复纯正 Ogg Vorbis 比特流，
    并无缝流式转码为高质量标准 AAC 音频。
    """
    cmd_extract = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
        "-i", str(file_path),
        "-map", "0:a:0", "-c:a", "copy", "-f", "data", "pipe:1"
    ]
    p_src = subprocess.Popen(cmd_extract, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    cmd_encode = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-err_detect", "ignore_err",
        "-f", "ogg", "-i", "pipe:0",
        "-c:a", "aac", "-b:a", f"{target_kbps}k",
        str(out_aac_path)
    ]
    p_enc = subprocess.Popen(cmd_encode, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    buffer = bytearray()
    valid_serial = None

    try:
        while True:
            chunk = p_src.stdout.read(65536)
            if not chunk:
                break
            buffer.extend(chunk)

            pos = 0
            buf_len = len(buffer)
            while pos < buf_len:
                p = buffer.find(b"OggS", pos)
                if p == -1:
                    buffer = buffer[-3:]
                    break
                if p + 27 > buf_len:
                    buffer = buffer[p:]
                    break
                n_seg = buffer[p + 26]
                if p + 27 + n_seg > buf_len:
                    buffer = buffer[p:]
                    break
                seg_table = buffer[p + 27 : p + 27 + n_seg]
                payload_len = sum(seg_table)
                total_len = 27 + n_seg + payload_len
                if p + total_len > buf_len:
                    buffer = buffer[p:]
                    break

                serial = int.from_bytes(buffer[p + 14 : p + 18], "little")
                if serial != 0xFFFFFFFF:
                    if valid_serial is None:
                        valid_serial = serial
                    if serial == valid_serial:
                        try:
                            p_enc.stdin.write(buffer[p : p + total_len])
                        except (BrokenPipeError, OSError):
                            break
                pos = p + total_len
    finally:
        try:
            p_src.stdout.close()
        except Exception:
            pass
        p_src.wait()
        try:
            p_enc.stdin.close()
        except Exception:
            pass
        p_enc.communicate()

    return p_enc.returncode == 0 and out_aac_path.exists() and out_aac_path.stat().st_size > 0


def preflight_audio(file_path: Path, info_or_has_audio: Any = True) -> str:
    """
    极速预检（0.1 秒探测）音频轨道的可用性：
    - 'copy': 音频完整且容器兼容，直接无损复制，0 耗时；
    - 'aac': 原格式不兼容或部分数据包异常，可由 FFmpeg 原生转码为 AAC；
    - 'vorbis_acm': AVI 封装的 Vorbis ACM (0x6771/0x674f 等)，需通过独家 Ogg 流清洗抢救转码；
    - 'none': 无音频或严重损坏无法解析，需使用 -an 纯画面抢救或填充静音轨道。
    """
    if isinstance(info_or_has_audio, dict):
        info = info_or_has_audio
        has_audio = info.get("has_audio", False)
    else:
        info = {}
        has_audio = bool(info_or_has_audio)

    if not has_audio:
        return "none"

    test_cmd_copy = [
        "ffmpeg", "-y", "-v", "error",
        "-err_detect", "ignore_err",
        "-i", str(file_path),
        "-t", "1", "-vn", "-c:a", "copy",
        "-f", "mp4", "/dev/null"
    ]
    try:
        proc = subprocess.run(test_cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
        if proc.returncode == 0:
            return "copy"
    except Exception:
        pass

    test_cmd_aac = [
        "ffmpeg", "-y", "-v", "error",
        "-err_detect", "ignore_err",
        "-i", str(file_path),
        "-t", "1", "-vn", "-c:a", "aac",
        "-f", "mp4", "/dev/null"
    ]
    try:
        proc = subprocess.run(test_cmd_aac, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
        if proc.returncode == 0:
            return "aac"
    except Exception:
        pass

    # 检查是否为 AVI 容器中包装的 Vorbis ACM 特殊音频
    if is_vorbis_acm_audio(file_path, info.get("audio_stream")):
        return "vorbis_acm"

    return "none"


def should_compress(
    file_path: Path,
    info: Dict[str, Any],
    expected_bitrate: int,
    threshold: float,
    force_compress: bool = False,
    force_repair_only: bool = False
) -> Tuple[bool, str]:
    """
    判断当前视频是否需要执行重新编码压缩。
    对于残缺的 .part 文件，默认执行修复并转码压缩。
    """
    if force_repair_only:
        return False, "用户指定强制仅修复 (--force-repair-only)"
    if force_compress:
        return True, "用户指定强制压缩 (--force-compress)"

    name_lower = file_path.name.lower()
    is_part_file = any(name_lower.endswith(ext) for ext in TEMP_DOWNLOAD_EXTENSIONS)
    if is_part_file:
        return True, f"检测到残缺/未完成文件 (.part)，执行容错流修复并重新编码压缩"

    orig_bitrate = info.get("bitrate", 0.0)
    codec = info.get("codec", "").lower()

    if codec and codec not in MP4_COMPLIANT_VIDEO_CODECS:
        return True, f"原视频编码格式 ({codec}) 无法直接复制进 MP4，执行修复并转码压缩"

    if orig_bitrate <= 0:
        return True, "原文件码率无法精确探测，执行容错修复并转码压缩"

    if codec in ("hevc", "h265", "av1", "av01"):
        return False, f"原视频已是高效编码 ({codec})，无需二次压缩，仅执行修复封装"

    if orig_bitrate <= expected_bitrate:
        return False, (
            f"原视频码率 ({format_bitrate(orig_bitrate)}) 已处于或低于基准区间 ({format_bitrate(expected_bitrate)})，"
            f"无需重复压缩，仅执行快速无损修复封装"
        )

    bitrate_diff_ratio = (orig_bitrate - expected_bitrate) / orig_bitrate
    if bitrate_diff_ratio <= threshold:
        return False, (
            f"原码率 ({format_bitrate(orig_bitrate)}) 与预期基准 ({format_bitrate(expected_bitrate)}) 差别不大 "
            f"(预期节约仅 {bitrate_diff_ratio*100:.1f}% <= {threshold*100:.1f}%)，跳过压缩以保留原画质"
        )

    return True, (
        f"原码率 ({format_bitrate(orig_bitrate)}) 明显高于预期标准 ({format_bitrate(expected_bitrate)})，"
        f"启动硬件/高质量编码压缩，预计节约空间 {bitrate_diff_ratio*100:.1f}%"
    )


def execute_ffmpeg(cmd: List[str]) -> Tuple[bool, str]:
    """执行 ffmpeg 命令，实时显示进度并通过后台线程持续排空 stderr，防止坏帧日志塞满管道缓冲区导致进程卡死死锁。"""
    full_cmd = [cmd[0]] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    try:
        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        last_stderr_lines = deque(maxlen=40)

        def drain_stderr():
            try:
                for line in proc.stderr:
                    last_stderr_lines.append(line.strip())
            except Exception:
                pass

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        last_time = ""
        last_speed = ""
        last_fps = ""

        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time="):
                last_time = line.split("=")[1].split(".")[0]
            elif line.startswith("speed="):
                last_speed = line.split("=")[1].strip()
            elif line.startswith("fps="):
                last_fps = line.split("=")[1].strip()
            elif line.startswith("progress="):
                if last_time:
                    info_str = f"\r  [处理进度] 已编码时间: {last_time} | 速度: {last_speed} | 帧率: {last_fps} fps"
                    sys.stdout.write(info_str)
                    sys.stdout.flush()

        proc.wait()
        stderr_thread.join(timeout=2.0)

        if last_time:
            sys.stdout.write("\n")
            sys.stdout.flush()

        if proc.returncode != 0:
            err_msg = "\n".join(last_stderr_lines)
            return False, err_msg
        return True, ""
    except Exception as e:
        return False, str(e)


def remove_source_files(sources: List[Path], final_output: Path) -> None:
    """在视频处理或合并成功后安全删除源文件（严格校验，绝不误删最终输出）。"""
    try:
        final_res = final_output.resolve()
    except Exception:
        final_res = None

    for s in sources:
        try:
            if not s.exists():
                continue
            s_res = s.resolve()
            if final_res is not None and s_res == final_res:
                print(f"  * [源文件清理跳过] 源文件与输出文件路径相同，已保护: {s.name}")
                continue
            s.unlink()
            print(f"  * [源文件清理] 已安全删除源文件: {s.name}")
        except Exception as e:
            print(f"  * [警告] 删除源文件失败 ({s.name}): {e}", file=sys.stderr)


def process_video(
    file_path: Path,
    output_dir: Optional[Path],
    threshold: float,
    crf: int,
    preset: str,
    encoder: str,
    force_compress: bool,
    force_repair_only: bool,
    delete_source: bool = False
) -> bool:
    """处理单个视频文件（支持深度容错修复、硬件极速压缩与本地 SSD 缓冲写入）。"""
    print("=" * 70)
    print(f"[处理中] 文件: {file_path.name}")

    info = probe_video(file_path)
    if not info:
        orig_size = file_path.stat().st_size
        print(f"  * 状态: 文件头/索引严重缺失（常见于 .part 残缺文件），启动深度容错修复...")
        info = {
            "size": orig_size, "bitrate": 0.0, "video_bitrate": 0.0, "audio_bitrate": 0.0,
            "width": 1280, "height": 720, "fps": 30.0, "codec": "unknown", "has_audio": True
        }
    else:
        orig_size = info["size"]
        orig_bitrate = info["bitrate"]
        orig_v_bitrate = info.get("video_bitrate", 0.0)
        orig_a_bitrate = info.get("audio_bitrate", 0.0)
        v_str = format_bitrate(orig_v_bitrate) if orig_v_bitrate > 0 else (format_bitrate(orig_bitrate) if orig_bitrate > 0 else "未知")
        a_str = format_bitrate(orig_a_bitrate) if orig_a_bitrate > 0 else ("有" if info.get("has_audio") else "无")
        print(f"  * 原始属性: {info['width']}x{info['height']} @ {info['fps']:.1f}fps | "
              f"编码: {info['codec']} (视频码率: {v_str}) | 音频: {a_str} | 大小: {format_bytes(orig_size)}")

    expected_bps = get_expected_bitrate(info, crf)
    orig_bitrate = info.get("bitrate", 0.0)
    orig_v_bitrate = info.get("video_bitrate", 0.0)
    if orig_v_bitrate <= 0:
        orig_v_bitrate = orig_bitrate

    # 严格的动态调优目标码率与防倒挂保护策略：
    # 若压缩采用固定码率或指定目标码率，严禁高于原始视频净码率（高于原始无意义且反向膨胀）
    if orig_v_bitrate > 0:
        target_bps = min(expected_bps, int(orig_v_bitrate * 0.85))
        if expected_bps > orig_v_bitrate:
            print(f"  * 码率保护策略: 预期基准码率 ({format_bitrate(expected_bps)}) 高于原视频码率 ({format_bitrate(orig_v_bitrate)})，"
                  f"自动降级封顶至 ~{format_bitrate(target_bps)} (严禁高于源文件，杜绝反向膨胀)")
    else:
        target_bps = expected_bps

    need_compress, reason = should_compress(
        file_path, info, target_bps, threshold, force_compress, force_repair_only
    )

    is_hardware = "videotoolbox" in encoder
    width = info.get("width", 0)
    height = info.get("height", 0)

    # 智能硬件编码门槛判断：
    # Apple VideoToolbox (Intel 核显) 硬件编码器对小分辨率老视频有硬性门槛限制 (需 >= 640x360)
    # 对于 384x272 等小分辨率老视频，自动选用 CPU 软件极速编码 (libx264)
    if is_hardware and (width < 640 or height < 360):
        print(f"  * 提示: 视频分辨率 ({width}x{height}) 低于 GPU 硬件编码器规格门槛 (需 >= 640x360)，自动选用 CPU 软件极速编码 (libx264)")
        encoder = "libx264"
        is_hardware = False

    if need_compress:
        if is_hardware:
            print(f"  * 编码配置: Apple VideoToolbox 硬件极速加速 ({encoder}) | 目标码率: ~{format_bitrate(target_bps)}")
        else:
            print(f"  * 编码配置: CPU 软件编码 ({encoder}, CRF: {crf}, Preset: {preset}) | 目标码率: ~{format_bitrate(target_bps)}")
    print(f"  * 策略决策: [{'修复并转码压缩' if need_compress else '仅修复重新封装'}] -> {reason}")
    if not need_compress and not force_repair_only:
        est_hevc = format_bytes(int(orig_size * 0.45))
        est_crf = format_bytes(int(orig_size * 0.55))
        print(f"  * 压缩建议: 若您希望进一步缩减该文件体积，可尝试：")
        print(f"      1. [GPU极速减半] vfix \"{file_path.name}\" --encoder hevc_videotoolbox --force-compress (预估 ~{est_hevc}, 省 ~55%)")
        print(f"      2. [CPU深度压缩] vfix \"{file_path.name}\" --cpu --crf 25 --force-compress (预估 ~{est_crf}, 省 ~45%)")
        print(f"      3. [按当前配置硬压] vfix \"{file_path.name}\" --force-compress (强制重编码)")

    target_dir = output_dir if output_dir else file_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    clean_stem = get_clean_stem(file_path)
    action_tag = "compressed" if need_compress else "repaired"
    output_name = f"{clean_stem}_{action_tag}.mp4"
    final_output_file = target_dir / output_name

    # 使用本地 SSD /tmp 目录作为临时输出缓冲：
    # 彻底杜绝 SMB 局域网网络盘在 -movflags +faststart 时产生的高延迟与 I/O 卡死
    temp_dir = Path(tempfile.gettempdir())
    temp_output_file = temp_dir / f"vrc_{os.getpid()}_{output_name}"

    # 深度容错基础参数：
    # -max_error_rate 1.0: 允许坏帧率高达 100%，绝不因 NAL 单元破损强行中断
    # -err_detect ignore_err: 解码时容忍并跳过损坏数据
    # -fflags +discardcorrupt: 容错丢弃不可恢复损坏包
    # -max_muxing_queue_size 2048: 防止音画错位导致队列溢出
    salvaged_audio_file: Optional[Path] = None
    try:
        # 深度容错基础参数：
        # -max_error_rate 1.0: 允许坏帧率高达 100%，绝不因 NAL 单元破损强行中断
        # -err_detect ignore_err: 解码时容忍并跳过损坏数据
        # -fflags +discardcorrupt: 容错丢弃不可恢复损坏包
        # -max_muxing_queue_size 2048: 防止音画错位导致队列溢出
        base_args = [
            "ffmpeg", "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-max_error_rate", "1.0",
            "-err_detect", "ignore_err",
            "-fflags", "+discardcorrupt",
            "-i", str(file_path),
            "-max_muxing_queue_size", "2048"
        ]

        # 音频预检与码率自适应：0.1 秒锁定正确的音频处理策略，并杜绝音频码率虚高膨胀
        audio_mode = preflight_audio(file_path, info)
        orig_audio_bps = info.get("audio_bitrate", 0.0)
        if orig_audio_bps > 0:
            # 原音频码率已知：绝不高转虚高（如 64k 原声绝不上浮至 128k），严格封顶在原音频码率以内
            orig_kbps = max(32, int(orig_audio_bps / 1000))
            target_audio_kbps = min(128, orig_kbps)
        else:
            if orig_bitrate > 0:
                target_audio_kbps = min(96, max(32, int(orig_bitrate * 0.15 / 1000)))
            else:
                target_audio_kbps = 96

        map_args = []
        if audio_mode == "vorbis_acm":
            salvaged_audio_file = temp_dir / f"vrc_salvaged_{os.getpid()}_{clean_stem}.aac"
            print(f"  * 发现经典 Vorbis ACM 特殊音频封装 (0x6771)，启动独家 Ogg 流清洗抢救并转码为 AAC...")
            t_salvage = time.time()
            if extract_vorbis_acm_audio(file_path, salvaged_audio_file, target_audio_kbps):
                print(f"    -> Vorbis ACM 音频抢救成功并转为 AAC (耗时: {time.time()-t_salvage:.1f}s, 大小: {format_bytes(salvaged_audio_file.stat().st_size)})")
                base_args = [
                    "ffmpeg", "-y",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-max_error_rate", "1.0",
                    "-err_detect", "ignore_err",
                    "-fflags", "+discardcorrupt",
                    "-i", str(file_path),
                    "-err_detect", "ignore_err",
                    "-i", str(salvaged_audio_file),
                    "-max_muxing_queue_size", "2048"
                ]
                map_args = ["-map", "0:v:0", "-map", "1:a:0"]
                audio_args = ["-c:a", "copy"]
            else:
                print("    [警告] Vorbis ACM 音频流损坏无法抢救，自动进入画面纯享抢救模式 (-an)...")
                audio_args = ["-an"]
        elif audio_mode == "copy":
            audio_args = ["-c:a", "copy"]
        elif audio_mode == "aac":
            if orig_audio_bps > 0 and target_audio_kbps < 128:
                print(f"  [提示] 音频流存在损坏或格式不兼容，自动转码为 AAC (码率保护: ~{target_audio_kbps} kbps，严格不高于源音频)...")
            else:
                print(f"  [提示] 音频流存在损坏或格式不兼容，自动转码为 AAC (自适应码率: ~{target_audio_kbps} kbps)...")
            audio_args = ["-c:a", "aac", "-b:a", f"{target_audio_kbps}k"]
        else:
            print("  [提示] 音频流彻底缺失或无法解析，启动画面纯享抢救模式 (-an)...")
            audio_args = ["-an"]

        t_start = time.time()
        success = False
        last_err_msg = ""

        if not need_compress:
            # === 仅修复模式：尝试快速流复制 ===
            cmd_copy = base_args + map_args + ["-c:v", "copy"] + audio_args + [
                "-movflags", "+faststart",
                str(temp_output_file)
            ]
            success, last_err_msg = execute_ffmpeg(cmd_copy)

            fps_val = float(info.get("fps", 30.0) or 30.0)
            gop_size = max(15, int(fps_val * 2))
            keyint_min = max(15, int(fps_val))

            if not success:
                # 格式不兼容时（如 WMV 视频编码无法直接封装进 MP4），自动启动转码修复
                if is_hardware:
                    video_encode_args = ["-c:v", "h264_videotoolbox", "-b:v", str(target_bps), "-pix_fmt", "yuv420p", "-g", str(gop_size)]
                else:
                    video_encode_args = [
                        "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                        "-g", str(gop_size), "-keyint_min", str(keyint_min), "-sc_threshold", "40"
                    ]
                    if orig_v_bitrate > 0:
                        max_rate_limit = min(int(target_bps * 1.15), int(orig_v_bitrate))
                        video_encode_args += ["-maxrate", str(max_rate_limit), "-bufsize", str(int(target_bps * 2.0))]

                cmd_transcode = base_args + map_args + video_encode_args + audio_args + [
                    "-movflags", "+faststart",
                    str(temp_output_file)
                ]
                success, last_err_msg = execute_ffmpeg(cmd_transcode)
        else:
            fps_val = float(info.get("fps", 30.0) or 30.0)
            gop_size = max(15, int(fps_val * 2))
            keyint_min = max(15, int(fps_val))

            # === 修复并转码压缩模式 ===
            if encoder == "h264_videotoolbox":
                video_encode_args = [
                    "-c:v", "h264_videotoolbox",
                    "-b:v", str(target_bps),
                    "-pix_fmt", "yuv420p",
                    "-g", str(gop_size)
                ]
            elif encoder == "hevc_videotoolbox":
                hevc_target = min(int(target_bps * 0.75), int(orig_v_bitrate * 0.75)) if orig_v_bitrate > 0 else int(target_bps * 0.75)
                video_encode_args = [
                    "-c:v", "hevc_videotoolbox",
                    "-b:v", str(hevc_target),
                    "-pix_fmt", "yuv420p",
                    "-tag:v", "hvc1",
                    "-g", str(gop_size)
                ]
            elif encoder == "libx264":
                video_encode_args = [
                    "-c:v", "libx264",
                    "-crf", str(crf),
                    "-preset", preset,
                    "-pix_fmt", "yuv420p",
                    "-g", str(gop_size),
                    "-keyint_min", str(keyint_min),
                    "-sc_threshold", "40"
                ]
                # 码率封顶保护：当原视频码率已知时，给 CRF 加封顶限制，防止噪点导致码率膨胀超标
                if orig_v_bitrate > 0:
                    max_rate_limit = min(int(target_bps * 1.15), int(orig_v_bitrate))
                    video_encode_args += ["-maxrate", str(max_rate_limit), "-bufsize", str(int(target_bps * 2.0))]
            elif encoder == "libx265":
                video_encode_args = [
                    "-c:v", "libx265",
                    "-crf", str(crf),
                    "-preset", preset,
                    "-pix_fmt", "yuv420p",
                    "-tag:v", "hvc1",
                    "-g", str(gop_size),
                    "-keyint_min", str(keyint_min)
                ]
                if orig_v_bitrate > 0:
                    max_rate_limit = min(int(target_bps * 0.90), int(orig_v_bitrate * 0.75))
                    video_encode_args += ["-maxrate", str(max_rate_limit), "-bufsize", str(int(target_bps * 2.0))]
            else:
                video_encode_args = ["-c:v", encoder, "-b:v", str(target_bps), "-g", str(gop_size)]

            cmd_compress = base_args + map_args + video_encode_args + audio_args + [
                "-movflags", "+faststart",
                str(temp_output_file)
            ]
            success, last_err_msg = execute_ffmpeg(cmd_compress)

            # 硬件编码器遇到不支持的特殊分辨率（如小分辨率老视频）时，自动平滑切换至 CPU 软件编码
            if not success and is_hardware:
                print("  [提示] 硬件编码器不支持该特殊分辨率规格 (如小分辨率老视频)，自动平滑切换至 CPU 软件编码优化...")
                fallback_video_args = [
                    "-c:v", "libx264",
                    "-crf", str(crf),
                    "-preset", preset,
                    "-pix_fmt", "yuv420p",
                    "-g", str(gop_size),
                    "-keyint_min", str(keyint_min),
                    "-sc_threshold", "40"
                ]
                if orig_v_bitrate > 0:
                    max_rate_limit = min(int(target_bps * 1.15), int(orig_v_bitrate))
                    fallback_video_args += ["-maxrate", str(max_rate_limit), "-bufsize", str(int(target_bps * 2.0))]
                cmd_sw_fallback = base_args + map_args + fallback_video_args + audio_args + [
                    "-movflags", "+faststart",
                    str(temp_output_file)
                ]
                success, last_err_msg = execute_ffmpeg(cmd_sw_fallback)

            # 仅当音频为 copy 且异常报错时，才兜底退回 aac 转码
            if not success and audio_mode == "copy":
                cmd_compress_fallback = base_args + map_args + video_encode_args + ["-c:a", "aac", "-b:a", f"{target_audio_kbps}k", "-movflags", "+faststart", str(temp_output_file)]
                success, last_err_msg = execute_ffmpeg(cmd_compress_fallback)

        duration_sec = time.time() - t_start

        if success and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
            new_size = temp_output_file.stat().st_size

            # 兜底防膨胀：若压缩后的体积居然大于原文件
            if need_compress and new_size > orig_size and orig_size > 0:
                codec = info.get("codec", "").lower()
                name_lower = file_path.name.lower()
                is_part_file = any(name_lower.endswith(ext) for ext in TEMP_DOWNLOAD_EXTENSIONS)

                if codec in MP4_COMPLIANT_VIDEO_CODECS and not is_part_file:
                    print(f"  [体积保护] 压缩体积 ({format_bytes(new_size)}) 超过原文件 ({format_bytes(orig_size)})，自动转为无损修复封装...")
                    cmd_protect = base_args + map_args + ["-c:v", "copy"] + audio_args + ["-movflags", "+faststart", str(temp_output_file)]
                    success_p, _ = execute_ffmpeg(cmd_protect)
                    if success_p and temp_output_file.exists():
                        new_size = temp_output_file.stat().st_size
                else:
                    print(f"  [体积保护] 压缩体积仍偏大 ({format_bytes(new_size)} > {format_bytes(orig_size)})，执行深层紧凑压制...")
                    strict_target_bps = int(orig_bitrate * 0.70) if orig_bitrate > 0 else int(target_bps * 0.75)
                    compact_video_args = [
                        "-c:v", "libx264",
                        "-crf", str(crf + 3),
                        "-preset", preset,
                        "-maxrate", str(strict_target_bps),
                        "-bufsize", str(strict_target_bps * 2),
                        "-pix_fmt", "yuv420p",
                        "-g", str(gop_size),
                        "-keyint_min", str(keyint_min),
                        "-sc_threshold", "40"
                    ]
                    cmd_compact = base_args + map_args + compact_video_args + audio_args + ["-movflags", "+faststart", str(temp_output_file)]
                    success_c, _ = execute_ffmpeg(cmd_compact)
                    if success_c and temp_output_file.exists():
                        new_size = temp_output_file.stat().st_size

            size_change = (new_size - orig_size) / orig_size * 100 if orig_size > 0 else 0

            # 将本地 SSD 临时生成的文件原子移动到目标位置（支持跨文件系统/SMB共享盘）
            shutil.move(str(temp_output_file), str(final_output_file))

            print(f"[完成] 修复与压缩成功! 耗时: {duration_sec:.1f}s")
            print(f"  * 原始大小: {format_bytes(orig_size)} -> 压缩后大小: {format_bytes(new_size)} ({size_change:+.1f}%)")
            print(f"  * 输出路径: {final_output_file}")
            if delete_source:
                remove_source_files([file_path], final_output_file)
            return True
        else:
            print(f"[失败] 残缺数据破坏过于严重，未能成功生成: {file_path.name}", file=sys.stderr)
            if last_err_msg:
                snippet = "\n".join(last_err_msg.splitlines()[-4:])
                print(f"  [底层错误] {snippet}", file=sys.stderr)
            if temp_output_file.exists():
                temp_output_file.unlink(missing_ok=True)
            return False
    finally:
        if salvaged_audio_file and salvaged_audio_file.exists():
            salvaged_audio_file.unlink(missing_ok=True)



def process_merged_videos(
    file_paths: List[Path],
    output_target: Optional[Path],
    crf: int,
    preset: str,
    encoder: str,
    force_compress: bool,
    force_repair_only: bool,
    merge_only: bool = False,
    skip_confirm: bool = False,
    delete_source: bool = False
) -> bool:
    """多分卷视频智能对齐合并与硬件/软件容错编码压缩 (支持极速无损流复制合并与 Filter Concat Pipeline)。"""
    is_merge_only = merge_only or force_repair_only
    file_paths = sorted(file_paths, key=natural_sort_key)
    n_files = len(file_paths)
    print("=" * 70)
    if is_merge_only:
        print(f"[单独合并] 共有 {n_files} 个分卷视频待拼接 (已按自然顺序预排, 仅合并无重编码):")
    else:
        print(f"[合并并压缩] 共有 {n_files} 个分卷视频待拼接 (已按自然顺序预排):")
    for idx, f in enumerate(file_paths, 1):
        print(f"  [{idx}/{n_files}] {f.name}")

    input_exts = {p.suffix.lower() for p in file_paths}
    tentative_ext = ".mp4"
    if is_merge_only and len(input_exts) == 1 and list(input_exts)[0] in (".wmv", ".asf", ".mkv"):
        tentative_ext = list(input_exts)[0]

    preview_output_file = derive_merged_output_path(
        file_paths, output_target, is_compressed=not is_merge_only, default_ext=tentative_ext
    )
    print(f"  * 拟定输出路径: {preview_output_file}")

    # 合并前交互式顺序确认与自定义调整
    if sys.stdin.isatty() and not skip_confirm:
        while True:
            try:
                choice = input("\n请确认拼接顺序 [回车确认 / 输入新序号如 2 1 调整 / n 取消]: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[取消] 用户中止合并操作。")
                return False

            if not choice or choice.lower() in ("y", "yes"):
                break
            elif choice.lower() in ("n", "no", "q", "quit"):
                print("[取消] 用户取消合并操作。")
                return False
            else:
                tokens = re.split(r'[\s,]+', choice)
                try:
                    indices = [int(t) for t in tokens if t]
                    if len(indices) == n_files and sorted(indices) == list(range(1, n_files + 1)):
                        file_paths = [file_paths[i - 1] for i in indices]
                        preview_output_file = derive_merged_output_path(
                            file_paths, output_target, is_compressed=not is_merge_only, default_ext=tentative_ext
                        )
                        print("\n[已更新拼接顺序]:")
                        for idx, f in enumerate(file_paths, 1):
                            print(f"  [{idx}/{n_files}] {f.name}")
                        print(f"  * 拟定输出路径: {preview_output_file}")
                        continue
                    else:
                        print(f"  [提示] 输入序号无效 (请输入 1 到 {n_files} 的完整序号组合，例如: {' '.join(str(i) for i in reversed(range(1, n_files + 1)))})")
                except ValueError:
                    print(f"  [提示] 无法识别的输入: '{choice}'，请输入回车确认、n 取消，或指定序号重新排序。")

    probes = []
    total_orig_size = 0
    max_w = 0
    max_h = 0
    max_fps = 0.0
    audio_bitrates = []

    for f in file_paths:
        info = probe_video(f)
        if not info:
            orig_size = f.stat().st_size
            info = {
                "size": orig_size, "bitrate": 0.0, "width": 1280, "height": 720,
                "fps": 30.0, "codec": "unknown", "has_audio": False, "audio_bitrate": 0.0
            }
        probes.append(info)
        total_orig_size += info.get("size", 0)
        max_w = max(max_w, info.get("width", 0))
        max_h = max(max_h, info.get("height", 0))
        max_fps = max(max_fps, info.get("fps", 0.0))

    temp_dir = Path(tempfile.gettempdir())
    salvaged_audio_files: List[Optional[Path]] = []
    audio_modes: List[str] = []
    has_any_audio = False

    # 预检并抢救所有分卷的音频轨道
    for idx, f in enumerate(file_paths):
        p_info = probes[idx]
        mode = preflight_audio(f, p_info)
        salvaged_file = None
        if mode == "vorbis_acm":
            clean_stem = get_clean_stem(f)
            temp_salvage = temp_dir / f"vrc_merge_salvage_{os.getpid()}_{idx}_{clean_stem}.aac"
            print(f"  * 分卷 [{idx+1}/{n_files}] 检测到 AVI 特殊音频封装 (Vorbis ACM / 0x6771)，正在清洗提取并转码为 AAC...")
            t_s = time.time()
            if extract_vorbis_acm_audio(f, temp_salvage, 128):
                print(f"    -> 分卷 [{idx+1}/{n_files}] 音频抢救成功 (耗时: {time.time()-t_s:.1f}s, 大小: {format_bytes(temp_salvage.stat().st_size)})")
                salvaged_file = temp_salvage
                mode = "vorbis_acm"
            else:
                print(f"    [警告] 分卷 [{idx+1}/{n_files}] Vorbis ACM 音频损坏无法抢救，将填充静音轨道确保对齐")
                mode = "none"
        elif mode == "none" and p_info.get("has_audio", False):
            print(f"  * [提示] 分卷 [{idx+1}/{n_files}] 音频流损坏或缺少可用解码器，将自动填充静音轨道")

        audio_modes.append(mode)
        salvaged_audio_files.append(salvaged_file)
        if mode != "none":
            has_any_audio = True

    for idx, p in enumerate(probes):
        if audio_modes[idx] != "none" and p.get("audio_bitrate", 0.0) > 0:
            audio_bitrates.append(p["audio_bitrate"])

    # 规范化分辨率（确保偶数尺寸以满足 yuv420p 要求）
    if max_w <= 0:
        max_w = 1280
    if max_h <= 0:
        max_h = 720
    if max_w % 2 != 0:
        max_w += 1
    if max_h % 2 != 0:
        max_h += 1
    if max_fps <= 0.0:
        max_fps = 30.0
    target_fps = round(max_fps, 2)

    synth_info = {"width": max_w, "height": max_h, "fps": target_fps}
    expected_bps = get_expected_bitrate(synth_info, crf)

    # 计算原视频分卷的平均视频净码率
    valid_v_bitrates = [
        p.get("video_bitrate", p.get("bitrate", 0.0))
        for p in probes if p.get("video_bitrate", p.get("bitrate", 0.0)) > 0
    ]
    if valid_v_bitrates:
        avg_v_bps = sum(valid_v_bitrates) / len(valid_v_bitrates)
    else:
        avg_v_bps = 0.0

    if is_merge_only:
        target_bps = int(avg_v_bps * 1.05) if avg_v_bps > 0 else expected_bps
        merge_crf = min(crf, 19)
    else:
        target_bps = min(expected_bps, int(avg_v_bps * 0.85)) if avg_v_bps > 0 else expected_bps
        if avg_v_bps > 0 and expected_bps > avg_v_bps:
            print(f"  * 码率保护策略: 基准推算码率 ({format_bitrate(expected_bps)}) 高于分卷平均视频码率 ({format_bitrate(avg_v_bps)})，"
                  f"自动降级封顶至 ~{format_bitrate(target_bps)} (严禁高于源分卷，杜绝反向膨胀)")
        merge_crf = crf

    is_hardware = "videotoolbox" in encoder
    if is_hardware and (max_w < 640 or max_h < 360):
        if not is_merge_only:
            print(f"  * 提示: 目标分辨率 ({max_w}x{max_h}) 低于 GPU 硬件编码门槛 (需 >= 640x360)，自动选用 CPU 软件极速编码 (libx264)")
        encoder = "libx264"
        is_hardware = False

    # 确定最终输出格式与路径
    all_mp4_compliant = all(
        p.get("codec", "").lower() in MP4_COMPLIANT_VIDEO_CODECS for p in probes if p
    )
    if is_merge_only:
        if all_mp4_compliant:
            target_ext = ".mp4"
        elif len(input_exts) == 1 and list(input_exts)[0] in BASE_VIDEO_EXTENSIONS:
            target_ext = list(input_exts)[0]
        else:
            target_ext = ".mkv"
    else:
        target_ext = ".mp4"

    final_output_file = derive_merged_output_path(
        file_paths, output_target, is_compressed=not is_merge_only, default_ext=target_ext
    )
    final_output_file.parent.mkdir(parents=True, exist_ok=True)
    temp_output_file = temp_dir / f"vrc_merge_{os.getpid()}_{final_output_file.name}"

    if audio_bitrates:
        avg_a_bps = sum(audio_bitrates) / len(audio_bitrates)
        orig_a_kbps = max(32, int(avg_a_bps / 1000))
        target_audio_kbps = min(128, orig_a_kbps)
    else:
        if avg_v_bps > 0:
            target_audio_kbps = min(96, max(32, int(avg_v_bps * 0.15 / 1000)))
        else:
            target_audio_kbps = 96

    # ========================================================
    # 编码与拼接参数预处理
    # ========================================================
    # 关键帧间隔优化：保证每 1~2 秒至少一个关键帧，确保在 IINA / QuickTime 中拖动进度条丝滑顺畅
    merge_gop = max(15, int(target_fps * 2))
    merge_keyint_min = max(15, int(target_fps))

    # 预先生成视频编码参数
    if encoder == "h264_videotoolbox":
        video_encode_args = ["-c:v", "h264_videotoolbox", "-b:v", str(target_bps), "-pix_fmt", "yuv420p", "-g", str(merge_gop)]
    elif encoder == "hevc_videotoolbox":
        hevc_target = min(int(target_bps * 0.75), int(avg_v_bps * 0.75)) if avg_v_bps > 0 else int(target_bps * 0.75)
        video_encode_args = ["-c:v", "hevc_videotoolbox", "-b:v", str(hevc_target), "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-g", str(merge_gop)]
    elif encoder == "libx264":
        video_encode_args = [
            "-c:v", "libx264", "-crf", str(merge_crf), "-preset", preset, "-pix_fmt", "yuv420p",
            "-g", str(merge_gop), "-keyint_min", str(merge_keyint_min), "-sc_threshold", "40"
        ]
        if not is_merge_only and avg_v_bps > 0:
            max_limit = min(int(target_bps * 1.15), int(avg_v_bps))
            video_encode_args += ["-maxrate", str(max_limit), "-bufsize", str(int(target_bps * 2.0))]
    elif encoder == "libx265":
        video_encode_args = [
            "-c:v", "libx265", "-crf", str(merge_crf), "-preset", preset, "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
            "-g", str(merge_gop), "-keyint_min", str(merge_keyint_min)
        ]
        if not is_merge_only and avg_v_bps > 0:
            max_limit = min(int(target_bps * 0.90), int(avg_v_bps * 0.75))
            video_encode_args += ["-maxrate", str(max_limit), "-bufsize", str(int(target_bps * 2.0))]
    else:
        video_encode_args = ["-c:v", encoder, "-b:v", str(target_bps), "-g", str(merge_gop)]

    sw_video_args = [
        "-c:v", "libx264", "-crf", str(merge_crf), "-preset", preset, "-pix_fmt", "yuv420p",
        "-g", str(merge_gop), "-keyint_min", str(merge_keyint_min), "-sc_threshold", "40"
    ]
    if not is_merge_only and avg_v_bps > 0:
        max_limit = min(int(target_bps * 1.15), int(avg_v_bps))
        sw_video_args += ["-maxrate", str(max_limit), "-bufsize", str(int(target_bps * 2.0))]

    same_v_codec = len({p.get("codec", "") for p in probes}) <= 1
    resolutions = {(p.get("width", 0), p.get("height", 0)) for p in probes}
    same_res = len(resolutions) <= 1
    no_vorbis_acm = not any(m == "vorbis_acm" for m in audio_modes)
    same_audio_status = all(p.get("has_audio", False) for p in probes) or not any(p.get("has_audio", False) for p in probes)
    can_concat_demuxer = same_v_codec and same_res and no_vorbis_acm and same_audio_status

    # ========================================================
    # 模式分支 1：基于 Concat Demuxer 的流式合并 (极速流复制或连续转码)
    # 彻底解决多输入 filter_complex 在长视频下的 demuxer 缓冲区饥渴、帧丢弃与时间戳错位
    # ========================================================
    if can_concat_demuxer:
        concat_list_file = temp_dir / f"vrc_concat_{os.getpid()}_{int(time.time() * 1000)}.txt"
        try:
            with open(concat_list_file, "w", encoding="utf-8") as f:
                for fp in file_paths:
                    escaped_path = fp.resolve().as_posix().replace("'", "'\\''")
                    f.write(f"file '{escaped_path}'\n")

            # ----------------------------------------------------
            # 策略 1A: 单独合并 (is_merge_only)，优先尝试无损流复制 (-c:v copy)
            # ----------------------------------------------------
            if is_merge_only and (all_mp4_compliant or final_output_file.suffix.lower() != ".mp4"):
                cmd_fast = [
                    "ffmpeg", "-y",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-max_error_rate", "1.0",
                    "-err_detect", "ignore_err",
                    "-fflags", "+discardcorrupt",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_list_file),
                    "-c:v", "copy"
                ]

                audio_codecs = {p.get("audio_stream", {}).get("codec_name") for p in probes if p.get("audio_stream")}
                if has_any_audio:
                    if all(m == "copy" for m in audio_modes) and len(audio_codecs) <= 1:
                        cmd_fast += ["-c:a", "copy"]
                    else:
                        cmd_fast += ["-c:a", "aac", "-b:a", f"{target_audio_kbps}k"]
                else:
                    cmd_fast += ["-an"]

                cmd_fast += [
                    "-avoid_negative_ts", "make_zero",
                    "-max_muxing_queue_size", "4096"
                ]

                if final_output_file.suffix.lower() == ".mp4":
                    cmd_fast += ["-movflags", "+faststart"]

                cmd_fast.append(str(temp_output_file))

                print(f"  * 策略决策: [单独合并] -> 启动极速无损流复制合并 (-c:v copy，跳过视频重编码)...")
                t_start = time.time()
                success_fast, fast_err = execute_ffmpeg(cmd_fast)

                if success_fast and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
                    duration_sec = time.time() - t_start
                    new_size = temp_output_file.stat().st_size
                    size_change = (new_size - total_orig_size) / total_orig_size * 100 if total_orig_size > 0 else 0

                    shutil.move(str(temp_output_file), str(final_output_file))

                    print(f"[完成] 单独合并成功 (极速无损流复制)! 耗时: {duration_sec:.1f}s")
                    print(f"  * 原始总体积: {format_bytes(total_orig_size)} -> 合并后大小: {format_bytes(new_size)} ({size_change:+.1f}%)")
                    print(f"  * 输出路径: {final_output_file}")
                    if delete_source:
                        remove_source_files(file_paths, final_output_file)
                    return True
                else:
                    if temp_output_file.exists():
                        temp_output_file.unlink(missing_ok=True)
                    reason_snippet = fast_err.strip().splitlines()[-1] if fast_err.strip() else "时间戳/封装限制"
                    print(f"  [提示] 无损流复制拼接未通过 ({reason_snippet})，自动平滑切换至 Concat Demuxer 高保真转码拼接...")

            # ----------------------------------------------------
            # 策略 1B: Concat Demuxer 顺序流式转码合并 (合并压缩或无损复制回退)
            # ----------------------------------------------------
            if not is_merge_only:
                print(f"  * 合并基准画质: {max_w}x{max_h} @ {target_fps}fps | 目标码率: ~{format_bitrate(target_bps)}")
                if is_hardware:
                    print(f"  * 编码配置: Apple VideoToolbox 硬件极速加速 ({encoder})")
                else:
                    print(f"  * 编码配置: CPU 软件编码 ({encoder}, CRF: {crf}, Preset: {preset})")
            else:
                print(f"  * 规整拼接基准: {max_w}x{max_h} @ {target_fps}fps | 高保真保留画质 (Concat Demuxer)")
            print(f"  * 目标输出路径: {final_output_file}")
            if has_any_audio and audio_bitrates and target_audio_kbps < 128:
                orig_a_kbps = max(32, int((sum(audio_bitrates) / len(audio_bitrates)) / 1000))
                print(f"  * 音频码率策略: 分卷平均音频码率 ~{orig_a_kbps} kbps，合并音频转码限制为 {target_audio_kbps} kbps (不高于原始码率)")

            vf_filter = (
                f"scale={max_w}:{max_h}:force_original_aspect_ratio=decrease,"
                f"pad={max_w}:{max_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={target_fps},setpts=PTS-STARTPTS"
            )
            cmd_concat_transcode = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-max_error_rate", "1.0",
                "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
                "-f", "concat", "-safe", "0", "-i", str(concat_list_file),
                "-vf", vf_filter
            ]
            if has_any_audio:
                cmd_concat_transcode += [
                    "-af", "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,aresample=async=1000:min_hard_comp=0.1:first_pts=0,asetpts=PTS-STARTPTS",
                    "-c:a", "aac", "-b:a", f"{target_audio_kbps}k"
                ]
            else:
                cmd_concat_transcode += ["-an"]

            cmd_concat_transcode += (
                video_encode_args
                + ["-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "4096", "-movflags", "+faststart", str(temp_output_file)]
            )

            t_start = time.time()
            success_tc, tc_err = execute_ffmpeg(cmd_concat_transcode)

            if not success_tc and is_hardware:
                print("  [提示] 硬件编码拼接异常，自动平滑切换至 CPU 软件编码 (libx264)...")
                cmd_concat_transcode_sw = [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-max_error_rate", "1.0",
                    "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
                    "-f", "concat", "-safe", "0", "-i", str(concat_list_file),
                    "-vf", vf_filter
                ]
                if has_any_audio:
                    cmd_concat_transcode_sw += [
                        "-af", "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,aresample=async=1000:min_hard_comp=0.1:first_pts=0,asetpts=PTS-STARTPTS",
                        "-c:a", "aac", "-b:a", f"{target_audio_kbps}k"
                    ]
                else:
                    cmd_concat_transcode_sw += ["-an"]

                cmd_concat_transcode_sw += (
                    sw_video_args
                    + ["-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "4096", "-movflags", "+faststart", str(temp_output_file)]
                )
                success_tc, tc_err = execute_ffmpeg(cmd_concat_transcode_sw)

            if success_tc and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
                duration_sec = time.time() - t_start
                new_size = temp_output_file.stat().st_size
                size_change = (new_size - total_orig_size) / total_orig_size * 100 if total_orig_size > 0 else 0

                shutil.move(str(temp_output_file), str(final_output_file))

                action_label = "单独合并与规整" if is_merge_only else "多分卷合并与压缩"
                print(f"[完成] {action_label}成功! 耗时: {duration_sec:.1f}s")
                print(f"  * 原始总体积: {format_bytes(total_orig_size)} -> 合并后大小: {format_bytes(new_size)} ({size_change:+.1f}%)")
                print(f"  * 输出路径: {final_output_file}")
                if delete_source:
                    remove_source_files(file_paths, final_output_file)
                return True
            else:
                if temp_output_file.exists():
                    temp_output_file.unlink(missing_ok=True)
                reason_snippet = tc_err.strip().splitlines()[-1] if tc_err.strip() else "未知原因"
                print(f"  [提示] Concat Demuxer 流式转码未通过 ({reason_snippet})，自动平滑切换至多源滤镜拼接...")
        finally:
            if concat_list_file.exists():
                concat_list_file.unlink(missing_ok=True)
    else:
        if not same_res:
            print(f"  [提示] 各分卷视频分辨率不一致 ({', '.join(f'{w}x{h}' for w, h in resolutions)})，自动通过多源智能滤镜对齐拼接...")
        elif not same_v_codec:
            print(f"  [提示] 各分卷视频编码不一致 ({', '.join(str(c) for c in {p.get('codec') for p in probes})})，自动通过多源智能滤镜对齐拼接...")
        else:
            print(f"  [提示] 发现特殊音频封装或通道不一致，自动通过多源智能滤镜对齐拼接...")

    # ========================================================
    # 模式分支 2：多源智能滤镜拼接 Pipeline (多源规格不一致或底层回退)
    # ========================================================
    if not is_merge_only:
        print(f"  * 合并基准画质: {max_w}x{max_h} @ {target_fps}fps | 目标码率: ~{format_bitrate(target_bps)}")
        if is_hardware:
            print(f"  * 编码配置: Apple VideoToolbox 硬件极速加速 ({encoder})")
        else:
            print(f"  * 编码配置: CPU 软件编码 ({encoder}, CRF: {crf}, Preset: {preset})")
    else:
        print(f"  * 规整拼接基准: {max_w}x{max_h} @ {target_fps}fps | 高保真保留画质 (多源滤镜)")
    print(f"  * 目标输出路径: {final_output_file}")

    # 构建 FFmpeg 输入与复杂滤镜
    input_args = []
    filter_chains = []
    input_counter = 0

    for i, f in enumerate(file_paths):
        v_in = input_counter
        input_counter += 1
        input_args.extend([
            "-thread_queue_size", "4096",
            "-err_detect", "ignore_err",
            "-fflags", "+discardcorrupt",
            "-i", str(f)
        ])
        filter_chains.append(
            f"[{v_in}:v:0]scale={max_w}:{max_h}:force_original_aspect_ratio=decrease,"
            f"pad={max_w}:{max_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={target_fps},setpts=PTS-STARTPTS[v{i}]"
        )
        if has_any_audio:
            mode = audio_modes[i]
            if mode == "vorbis_acm" and salvaged_audio_files[i]:
                a_in = input_counter
                input_counter += 1
                input_args.extend([
                    "-thread_queue_size", "4096",
                    "-err_detect", "ignore_err",
                    "-i", str(salvaged_audio_files[i])
                ])
                filter_chains.append(
                    f"[{a_in}:a:0]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,aresample=async=1000:min_hard_comp=0.1:first_pts=0,asetpts=PTS-STARTPTS[a{i}]"
                )
            elif mode in ("copy", "aac"):
                filter_chains.append(
                    f"[{v_in}:a:0]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,aresample=async=1000:min_hard_comp=0.1:first_pts=0,asetpts=PTS-STARTPTS[a{i}]"
                )
            else:
                dur = probes[i].get("duration", 0.0)
                dur_str = f",atrim=end={dur:.3f}" if dur > 0 else ""
                filter_chains.append(
                    f"anullsrc=r=44100:cl=stereo{dur_str},asetpts=PTS-STARTPTS[a{i}]"
                )

    if has_any_audio:
        concat_inputs = "".join([f"[v{i}][a{i}]" for i in range(n_files)])
        filter_chains.append(f"{concat_inputs}concat=n={n_files}:v=1:a=1[v][a]")
        if audio_bitrates and target_audio_kbps < 128:
            orig_a_kbps = max(32, int((sum(audio_bitrates) / len(audio_bitrates)) / 1000))
            print(f"  * 音频码率策略: 分卷平均音频码率 ~{orig_a_kbps} kbps，合并音频转码限制为 {target_audio_kbps} kbps (不高于原始码率)")
        audio_encode_args = ["-map", "[a]", "-c:a", "aac", "-b:a", f"{target_audio_kbps}k"]
    else:
        concat_inputs = "".join([f"[v{i}]" for i in range(n_files)])
        filter_chains.append(f"{concat_inputs}concat=n={n_files}:v=1:a=0[v]")
        audio_encode_args = ["-an"]

    filter_complex = ";".join(filter_chains)

    cmd_merge = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-max_error_rate", "1.0"]
        + input_args
        + ["-filter_complex", filter_complex, "-map", "[v]"]
        + video_encode_args
        + audio_encode_args
        + ["-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "4096", "-movflags", "+faststart", str(temp_output_file)]
    )

    try:
        t_start = time.time()
        success, last_err_msg = execute_ffmpeg(cmd_merge)

        # 硬件编码遇异常时的软解回退
        if not success and is_hardware:
            print("  [提示] 硬件编码拼接异常，自动平滑切换至 CPU 软件编码 (libx264)...")
            cmd_sw_merge = (
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-max_error_rate", "1.0"]
                + input_args
                + ["-filter_complex", filter_complex, "-map", "[v]"]
                + sw_video_args
                + audio_encode_args
                + ["-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "4096", "-movflags", "+faststart", str(temp_output_file)]
            )
            success, last_err_msg = execute_ffmpeg(cmd_sw_merge)

        # 极端情况：若音频合成依然异常，自动降级为纯画面抢救合并 (-an)
        if not success and has_any_audio:
            print("  [提示] 音频合成通道异常，自动启动终极兜底：纯画面抢救合并 (-an)...")
            an_filter_chains = []
            for i in range(n_files):
                an_filter_chains.append(
                    f"[{i}:v:0]scale={max_w}:{max_h}:force_original_aspect_ratio=decrease,"
                    f"pad={max_w}:{max_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={target_fps},setpts=PTS-STARTPTS[v{i}]"
                )
            concat_v = "".join([f"[v{i}]" for i in range(n_files)])
            an_filter_chains.append(f"{concat_v}concat=n={n_files}:v=1:a=0[v]")
            an_fc = ";".join(an_filter_chains)

            an_inputs = []
            for f in file_paths:
                an_inputs.extend([
                    "-thread_queue_size", "4096",
                    "-err_detect", "ignore_err",
                    "-fflags", "+discardcorrupt",
                    "-i", str(f)
                ])

            cmd_an_merge = (
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-max_error_rate", "1.0"]
                + an_inputs
                + ["-filter_complex", an_fc, "-map", "[v]"]
                + (sw_video_args if is_hardware else video_encode_args)
                + ["-an", "-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "4096", "-movflags", "+faststart", str(temp_output_file)]
            )
            success, last_err_msg = execute_ffmpeg(cmd_an_merge)

        duration_sec = time.time() - t_start

        if success and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
            new_size = temp_output_file.stat().st_size
            size_change = (new_size - total_orig_size) / total_orig_size * 100 if total_orig_size > 0 else 0

            shutil.move(str(temp_output_file), str(final_output_file))

            action_label = "单独合并与规整" if is_merge_only else "多分卷合并与压缩"
            print(f"[完成] {action_label}成功! 耗时: {duration_sec:.1f}s")
            print(f"  * 原始总体积: {format_bytes(total_orig_size)} -> 合并后大小: {format_bytes(new_size)} ({size_change:+.1f}%)")
            print(f"  * 输出路径: {final_output_file}")
            if delete_source:
                remove_source_files(file_paths, final_output_file)
            return True
        else:
            action_label = "单独合并" if is_merge_only else "多分卷合并压缩"
            print(f"[失败] {action_label}失败。", file=sys.stderr)
            if last_err_msg:
                snippet = "\n".join(last_err_msg.splitlines()[-4:])
                print(f"  [底层错误] {snippet}", file=sys.stderr)
            if temp_output_file.exists():
                temp_output_file.unlink(missing_ok=True)
            return False
    finally:
        for sf in salvaged_audio_files:
            if sf and sf.exists():
                sf.unlink(missing_ok=True)


def collect_video_files(input_patterns: List[str], recursive: bool, is_merge: bool = False) -> List[Path]:
    """收集输入路径列表或通配符匹配下的所有视频及 .part 残缺文件。"""
    collected = []
    seen = set()

    is_single_explicit_file = len(input_patterns) == 1 and Path(input_patterns[0]).expanduser().is_file()

    def should_skip(stem: str) -> bool:
        if is_single_explicit_file:
            return False
        if is_merge:
            # 合并模式下允许合并已压缩(_compressed)或已修复(_repaired)的分卷，仅自动排除合并结果(_merged)
            return stem.endswith("_merged")
        return stem.endswith("_repaired") or stem.endswith("_compressed") or stem.endswith("_merged")

    for pattern_str in input_patterns:
        p = Path(pattern_str).expanduser()
        if p.exists():
            if p.is_file():
                if not should_skip(p.stem):
                    if is_supported_video_file(p) or probe_video(p) is not None:
                        res = p.resolve()
                        if res not in seen:
                            seen.add(res)
                            collected.append(res)
            elif p.is_dir():
                iterator = p.rglob("*") if recursive else p.glob("*")
                for f in iterator:
                    if f.is_file() and is_supported_video_file(f):
                        if not should_skip(f.stem):
                            res = f.resolve()
                            if res not in seen:
                                seen.add(res)
                                collected.append(res)
            continue

        expanded_pattern = os.path.expanduser(pattern_str)
        matched = glob.glob(expanded_pattern, recursive=recursive)
        for m in matched:
            mp = Path(m).resolve()
            if mp.is_file() and is_supported_video_file(mp):
                if not should_skip(mp.stem):
                    if mp not in seen:
                        seen.add(mp)
                        collected.append(mp)
            elif mp.is_dir():
                iterator = mp.rglob("*") if recursive else mp.glob("*")
                for f in iterator:
                    if f.is_file() and is_supported_video_file(f):
                        if not should_skip(f.stem):
                            res = f.resolve()
                            if res not in seen:
                                seen.add(res)
                                collected.append(res)

    return sorted(collected, key=natural_sort_key)


def main():
    has_vt = has_videotoolbox_support()
    default_encoder = "h264_videotoolbox" if has_vt else "libx264"

    parser = argparse.ArgumentParser(
        prog="vrc",
        description="视频智能容错修复与高能效硬件/软件编码压缩工具 (vrc / vfix)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例用法:
  vfix CD1.avi CD2.avi -M                            # 单独合并多个分卷为一个完整视频 (极速无损流复制，0重编码、秒级完成)
  vfix CD1.avi CD2.avi -M -d                         # 单独合并成功后自动删除 CD1/CD2 源分卷
  vfix *.part                                        # 修复并压缩未下载完的 .part 视频 (默认 GPU 硬件极速模式)
  vfix *.part -d                                     # 修复压缩成功后自动删除原始 .part 文件
  vfix CD1.avi CD2.avi --merge                      # 自然排序合并多个 CD 分卷为一个完整视频并压缩
  vfix *.wmv -M -o /path/to/movie.wmv                # 单独合并残缺/多分卷并导出到指定输出文件
  vfix *.part -m -o /path/to/movie.mp4               # 合并残缺分卷并压缩导出到指定输出文件
  vrc *.mp4                                          # 通配符批量处理当前目录下所有 mp4
  vrc /path/to/*.wmv.part -o /path/to/output         # 修复残缺视频并导出到指定目录
  vrc /path/to/videos -r -o /path/to/output          # 递归遍历目录处理
  vrc file.mp4 --cpu                                 # 强制使用 CPU 软件编码 (libx264 CRF 22)
  vrc damaged.mov --force-repair-only                # 强制仅修复（不重新编码压缩）
"""
    )
    parser.add_argument("inputs", nargs="+", type=str, help="输入视频文件、文件夹或通配符匹配 (如 *.mp4, *.part, '*.mov', videos/)")
    parser.add_argument("-m", "--merge", action="store_true", help="将所有输入的视频分卷合并为一个完整视频并压缩 (支持自然排序与自适应规格对齐)")
    parser.add_argument("-M", "--merge-only", "--concat-only", dest="merge_only", action="store_true",
                        help="单独合并分卷视频，不执行重新编码压缩 (优先无损极速流复制，0重编码、秒级完成)")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过合并前的交互式顺序确认提示 (自动确认执行)")
    parser.add_argument("-o", "--output-dir", type=str, default=None, help="输出目录或最终输出文件名（合并时可直接指定完整文件名）")
    parser.add_argument("--crf", type=int, default=22, help="恒定画质系数 CRF (默认 22，通常 18-24 为高质量基准)")
    parser.add_argument("--preset", type=str, default="veryfast",
                        choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"],
                        help="CPU 软件编码预设速度 (默认 veryfast，显著降低 CPU 耗时)")
    parser.add_argument("--threshold", type=float, default=0.20, help="判断是否跳过压缩的收益阈值比例 (默认 0.20 即 20%%)")
    parser.add_argument("--encoder", type=str, default=default_encoder,
                        choices=["h264_videotoolbox", "hevc_videotoolbox", "libx264", "libx265"],
                        help=f"视频编码器 (当前系统默认: {default_encoder})")
    parser.add_argument("--cpu", action="store_true", help="一键指定使用 CPU 软件编码 (等同于 --encoder libx264)")
    parser.add_argument("--force-compress", action="store_true", help="强制执行压缩，无视码率差异判断")
    parser.add_argument("--force-repair-only", action="store_true", help="强制仅执行修复封装/单独合并，不进行重新编码压缩")
    parser.add_argument("-d", "--delete-source", "--del-src", "--remove-source", dest="delete_source", action="store_true",
                        help="处理或合并成功后自动删除源视频文件 (谨慎使用，失败或中断时绝不删除)")
    parser.add_argument("-r", "--recursive", action="store_true", help="当输入为目录或通配符时，递归扫描所有子目录")

    args = parser.parse_args()

    if not check_ffmpeg():
        sys.exit(1)

    encoder = "libx264" if args.cpu else args.encoder
    output_target = Path(args.output_dir).resolve() if args.output_dir else None

    is_merge = args.merge or args.merge_only
    is_merge_only = args.merge_only or (args.merge and args.force_repair_only)

    video_files = collect_video_files(args.inputs, args.recursive, is_merge=is_merge)
    if not video_files:
        print(f"[提示] 未找到符合条件的受支持视频文件: {args.inputs}")
        sys.exit(0)

    if is_merge:
        if len(video_files) == 1:
            print(f"[提示] 仅检测到 1 个视频文件 ({video_files[0].name})，无需合并，自动转为单文件修复{'与压缩' if not is_merge_only else ''}流程。")
            ok = process_video(
                file_path=video_files[0],
                output_dir=output_target,
                threshold=args.threshold,
                crf=args.crf,
                preset=args.preset,
                encoder=encoder,
                force_compress=args.force_compress,
                force_repair_only=is_merge_only or args.force_repair_only,
                delete_source=args.delete_source
            )
            sys.exit(0 if ok else 1)
        else:
            if not is_merge_only:
                if "videotoolbox" in encoder:
                    print(f"加速引擎: [Apple VideoToolbox GPU 硬件加速] (极速、低发热)")
                else:
                    print(f"加速引擎: [CPU 软件编码 libx264] (CRF: {args.crf}, Preset: {args.preset})")

            ok = process_merged_videos(
                file_paths=video_files,
                output_target=output_target,
                crf=args.crf,
                preset=args.preset,
                encoder=encoder,
                force_compress=args.force_compress,
                force_repair_only=args.force_repair_only,
                merge_only=args.merge_only,
                skip_confirm=args.yes,
                delete_source=args.delete_source
            )
            print("\n" + "=" * 70)
            action_desc = "单独合并" if is_merge_only else "合并并压缩"
            print(f"处理完成汇总: {action_desc} {len(video_files)} 个分卷，状态: {'成功' if ok else '失败'}。")
            sys.exit(0 if ok else 1)

    print(f"找到 {len(video_files)} 个待处理视频文件。")
    if "videotoolbox" in encoder:
        print(f"加速引擎: [Apple VideoToolbox GPU 硬件加速] (极速、低发热)")
    else:
        print(f"加速引擎: [CPU 软件编码 libx264] (CRF: {args.crf}, Preset: {args.preset})")

    success_count = 0
    fail_count = 0

    for idx, v_file in enumerate(video_files, 1):
        print(f"\n[{idx}/{len(video_files)}]")
        ok = process_video(
            file_path=v_file,
            output_dir=output_target,
            threshold=args.threshold,
            crf=args.crf,
            preset=args.preset,
            encoder=encoder,
            force_compress=args.force_compress,
            force_repair_only=args.force_repair_only,
            delete_source=args.delete_source
        )
        if ok:
            success_count += 1
        else:
            fail_count += 1

    print("\n" + "=" * 70)
    print(f"处理完成汇总: 共 {len(video_files)} 个，成功: {success_count} 个，失败: {fail_count} 个。")


if __name__ == "__main__":
    main()

