#!/usr/bin/env python3
"""
视频文件智能修复与高能效硬件/软件编码压缩脚本 (video_repair_compress.py / vrc / vfix)

核心改进：
1. 默认启用 Apple VideoToolbox GPU 硬件深度加速 (h264_videotoolbox / hevc_videotoolbox)：
   - 注入硬件 B 帧 (-bf 2) 与空间自适应量化 (-spatial_aq 1)，大幅减少暗部色块与色带，同画质下体积再降 20%~40%。
   - 显式锁定 High/Main Profile 与 BT.709 色彩空间描述，彻底解决 macOS/iOS QuickTime 播放泛白发灰 (Gamma Shift)。
   - 严格配置 -maxrate 与 -bufsize 峰值缓冲约束，杜绝复杂噪点/坏帧画面瞬时码率失控与体积反向膨胀。
   - 原生支持 Apple Silicon (arm64) 全分辨率硬件极速压制，并支持奇数分辨率自动偶数对齐。
   - 彻底解决 CPU 软压缓慢（从 1.1x 跃升至 5x~15x 极速），CPU 负荷降至 10% 以下，防止高温降频。
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
7. 多分卷极速智能合并 (Lossless Concat & Pipeline Merge)：
   - 支持 -M / --merge-only 独立合并模式；在普通合并模式 (-m) 下若分卷码率已在合理区间也优先秒级无损流复制 (-c:v copy)。
   - 各分卷分辨率一致时移除冗余 CPU 重缩放滤镜，直通硬件编码管线；不一致时平滑回退至多源高保真智能滤镜对齐拼接。
   - 硬件编码参数与单文件深度调优全链路对齐。
8. 执行成功安全删除源文件 (-d / --delete-source / --del-src / --remove-source)：
   - 处理或合并完全成功后自动清理原始分卷/源文件，节约存储空间；失败或异常中断时严格防护绝不删除源文件。
"""

import argparse
import errno
import glob
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# 智能去黑边配套模块 (支持自动过滤跑马灯广告/水印及短剧画幅锁定与智能场景切片)
try:
    from video_autocrop import (
        detect_crop_box,
        get_aspect_ratio_name,
        parse_aspect_ratio,
        detect_scene_segments,
        process_video_with_splits
    )
except ImportError:
    _vfix_dir = Path(__file__).resolve().parent
    if str(_vfix_dir) not in sys.path:
        sys.path.insert(0, str(_vfix_dir))
    try:
        from video_autocrop import (
            detect_crop_box,
            get_aspect_ratio_name,
            parse_aspect_ratio,
            detect_scene_segments,
            process_video_with_splits
        )
    except ImportError:
        detect_crop_box = None
        get_aspect_ratio_name = None
        parse_aspect_ratio = None
        detect_scene_segments = None
        process_video_with_splits = None

import atexit
import signal
from datetime import datetime

# ==============================================================================
# 自包含通用日志记录与后台守护引擎 (100% 自动生成，单文件独立运行无任何外部依赖)
# ==============================================================================
class _EmbeddedVideoLogger:
    """自包含线程安全日志记录引擎 (保证脚本独立复制为 /usr/local/bin/vfix 时日志 100% 自动记录)"""
    DEFAULT_LOG_DIR = Path.home() / ".vfix" / "logs"

    def __init__(self):
        self._lock = threading.Lock()
        self._current_log_dir = self.DEFAULT_LOG_DIR
        self._enabled = True
        self._tool_name = "vfix"

    def format_bytes(self, size_bytes: Union[int, float]) -> str:
        try:
            size = float(size_bytes)
        except (ValueError, TypeError):
            return "0 B"
        if size < 0:
            return "0 B"
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024.0 or unit == "TB":
                return f"{size:.2f} {unit}" if unit in ("MB", "GB", "TB") else f"{size:.0f} {unit}"
            size /= 1024.0
        return f"{size:.2f} TB"

    def format_bitrate(self, bps: Union[int, float]) -> str:
        try:
            val = float(bps)
        except (ValueError, TypeError):
            return "未知码率"
        if val <= 0:
            return "未知码率"
        if val >= 1_000_000:
            return f"{val / 1_000_000:.2f} Mbps"
        return f"{val / 1000:.0f} kbps"

    def get_log_dir(self) -> Path:
        env_dir = os.environ.get("VFIX_LOG_DIR")
        if env_dir:
            return Path(env_dir).expanduser().resolve()
        return self._current_log_dir

    def setup_logger(self, log_dir: Optional[Union[str, Path]] = None, enabled: bool = True, tool_name: str = "vfix") -> Path:
        self._enabled = enabled
        self._tool_name = tool_name
        if log_dir:
            self._current_log_dir = Path(log_dir).expanduser().resolve()
        else:
            env_dir = os.environ.get("VFIX_LOG_DIR")
            if env_dir:
                self._current_log_dir = Path(env_dir).expanduser().resolve()
            else:
                self._current_log_dir = self.DEFAULT_LOG_DIR
        if self._enabled:
            try:
                self._current_log_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        return self._current_log_dir

    def _append(self, file_path: Path, text: str) -> bool:
        if not self._enabled:
            return False
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
            return True
        except Exception:
            return False

    def get_processed_log_path(self) -> Path:
        return self.get_log_dir() / "processed.log"

    def get_deleted_log_path(self) -> Path:
        return self.get_log_dir() / "deleted.log"

    def get_main_log_path(self) -> Path:
        return self.get_log_dir() / f"{self._tool_name}.log"

    def log_processed(
        self,
        input_file: Union[str, Path, List[Union[str, Path]]],
        output_target: Optional[Union[str, Path]],
        status: str,
        action: str = "PROCESS",
        orig_size: Optional[Union[int, float]] = None,
        new_size: Optional[Union[int, float]] = None,
        elapsed_sec: Optional[float] = None,
        orig_info: Optional[Dict[str, Any]] = None,
        output_info: Optional[Dict[str, Any]] = None,
        details: Optional[str] = None,
        tool: Optional[str] = None
    ) -> None:
        if not self._enabled:
            return
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tool_label = tool or self._tool_name
        if isinstance(input_file, (list, tuple)):
            input_count = len(input_file)
            first_path = str(Path(input_file[0]).resolve())
            input_desc = f"{first_path} (等共 {input_count} 个分卷)" if input_count > 1 else first_path
        else:
            input_desc = str(Path(input_file).resolve())
        out_desc = str(Path(output_target).resolve()) if output_target else "None"
        size_str = ""
        if orig_size is not None and orig_size >= 0:
            size_str = self.format_bytes(orig_size)
            if new_size is not None and new_size >= 0:
                change_pct = ((new_size - orig_size) / orig_size * 100) if orig_size > 0 else 0
                saved_bytes = orig_size - new_size
                saved_str = f", 节省 {self.format_bytes(saved_bytes)}" if saved_bytes > 0 else (f", 增加 {self.format_bytes(-saved_bytes)}" if saved_bytes < 0 else "")
                size_str = f"{self.format_bytes(orig_size)} -> {self.format_bytes(new_size)} ({change_pct:+.1f}%{saved_str})"
        cost_str = f"{elapsed_sec:.1f}s" if elapsed_sec is not None else "未知"
        media_specs = []
        if orig_info:
            w = orig_info.get("width")
            h = orig_info.get("height")
            fps = orig_info.get("fps")
            codec = orig_info.get("codec")
            br = orig_info.get("bitrate") or orig_info.get("video_bitrate")
            if w and h:
                spec = f"{w}x{h}"
                if fps:
                    spec += f" @ {fps:.1f}fps"
                media_specs.append(spec)
            if codec and codec != "unknown":
                media_specs.append(str(codec))
            if br and float(br) > 0:
                media_specs.append(self.format_bitrate(float(br)))
        media_str = f" | {', '.join(media_specs)}" if media_specs else ""
        lines = [
            f"[{now_str}] [{tool_label}] [{status.upper()}] [{action.upper()}] - {input_desc}",
            f"  * 输入: {input_desc}{media_str}",
            f"  * 输出: {out_desc}",
            f"  * 体积: {size_str} | 耗时: {cost_str}"
        ]
        if details:
            lines.append(f"  * 详情: {details.strip()}")
        lines.append("")
        self._append(self.get_processed_log_path(), "\n".join(lines) + "\n")
        main_line = f"{now_str} [{status.upper()}] [{tool_label}:{action.upper()}] {input_desc} -> {out_desc} ({size_str}, 耗时 {cost_str})"
        if details:
            main_line += f" | {details.strip()}"
        self._append(self.get_main_log_path(), main_line + "\n")

    def log_deleted(
        self,
        deleted_file: Union[str, Path],
        size_bytes: Optional[Union[int, float]] = None,
        reason: str = "源文件清理 (--delete-source)",
        target_path: Optional[Union[str, Path]] = None,
        status: str = "DELETED",
        error_msg: Optional[str] = None,
        tool: Optional[str] = None
    ) -> None:
        if not self._enabled:
            return
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tool_label = tool or self._tool_name
        try:
            del_path = str(Path(deleted_file).resolve())
        except Exception:
            del_path = str(deleted_file)
        target_str = str(Path(target_path).resolve()) if target_path else "None"
        size_str = self.format_bytes(size_bytes) if size_bytes is not None else "未知大小"
        raw_size_str = f" ({int(size_bytes):,} 字节)" if size_bytes is not None and size_bytes > 0 else ""
        lines = [
            f"[{now_str}] [{tool_label}] [{status.upper()}] {del_path} ({size_str})",
            f"  * 路径: {del_path}",
            f"  * 大小: {size_str}{raw_size_str}",
            f"  * 原因: {reason.strip()}",
            f"  * 对应产物: {target_str}"
        ]
        if error_msg:
            lines.append(f"  * 失败原因: {error_msg.strip()}")
        lines.append("")
        self._append(self.get_deleted_log_path(), "\n".join(lines) + "\n")
        main_line = f"{now_str} [{status.upper()}] [{tool_label}:CLEANUP] 已删除源文件: {del_path} ({size_str}) | 原因: {reason.strip()}"
        if error_msg:
            main_line += f" | 错误: {error_msg.strip()}"
        self._append(self.get_main_log_path(), main_line + "\n")

    def log_event(self, level: str, message: str, tool: Optional[str] = None) -> None:
        if not self._enabled:
            return
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tool_label = tool or self._tool_name
        line = f"{now_str} [{level.upper():5s}] [{tool_label}] {message}\n"
        self._append(self.get_main_log_path(), line)

    def log_session_start(self, args: List[str], file_count: int, tool: Optional[str] = None) -> None:
        tool_label = tool or self._tool_name
        cmd_str = " ".join(args)
        self.log_event("INFO", f"========== 开始任务会话: {tool_label} (待处理文件: {file_count} 个) ==========", tool=tool_label)
        self.log_event("INFO", f"执行参数: {cmd_str}", tool=tool_label)

    def log_session_end(self, success_count: int, fail_count: int, total_cost: float, tool: Optional[str] = None) -> None:
        tool_label = tool or self._tool_name
        self.log_event("INFO", f"========== 会话结束: 成功 {success_count} 个, 失败 {fail_count} 个, 总耗时 {total_cost:.1f}s ==========", tool=tool_label)


class _EmbeddedDaemonManager:
    """自包含通用后台守护进程管理器，支持脱机运行、状态查询与安全停止。"""

    def __init__(
        self,
        tool_name: str,
        base_dir: Optional[Path] = None,
        log_file: Optional[Path] = None
    ):
        self.tool_name = tool_name
        self.base_dir = Path(base_dir).expanduser().resolve() if base_dir else (Path.home() / ".vfix")
        self.pid_file = self.base_dir / f"{tool_name}.pid"
        self.status_file = self.base_dir / f"{tool_name}_status.json"
        self.log_file = Path(log_file).expanduser().resolve() if log_file else (self.base_dir / "logs" / f"{tool_name}.log")

    def is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError as err:
            return err.errno == errno.EPERM

    def get_running_info(self) -> Optional[Dict[str, Any]]:
        if not self.pid_file.exists():
            return None
        try:
            with open(self.pid_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("pid")
            if pid and self.is_pid_alive(pid):
                return data
            try:
                self.pid_file.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                self.status_file.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        except Exception:
            return None

    def update_status(
        self,
        current_file: Optional[Union[str, Path]] = None,
        file_index: Optional[int] = None,
        total_files: Optional[int] = None,
        stage: Optional[str] = None,
        progress_str: Optional[str] = None,
        percent: Optional[float] = None
    ) -> None:
        """更新当前后台任务的实时进度快照，供 --status 即刻查询"""
        try:
            status_data = {}
            if self.status_file.exists():
                try:
                    with open(self.status_file, "r", encoding="utf-8") as f:
                        status_data = json.load(f)
                except Exception:
                    status_data = {}

            if current_file is not None:
                status_data["current_file"] = str(Path(current_file).resolve())
            if file_index is not None:
                status_data["file_index"] = file_index
            if total_files is not None:
                status_data["total_files"] = total_files
            if stage is not None:
                status_data["stage"] = stage
            if progress_str is not None:
                status_data["progress_str"] = progress_str
            if percent is not None:
                status_data["percent"] = round(percent, 1)
            status_data["updated_at"] = time.time()
            status_data["updated_at_str"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with open(self.status_file, "w", encoding="utf-8") as f:
                json.dump(status_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def show_status(self) -> None:
        info = self.get_running_info()
        print("=" * 72)
        if info:
            pid = info["pid"]
            start_time = info.get("start_time", 0)
            elapsed = time.time() - start_time if start_time else 0
            m, s = divmod(int(elapsed), 60)
            h, m = divmod(m, 60)
            elapsed_str = f"{h}小时{m}分{s}秒" if h > 0 else (f"{m}分{s}秒" if m > 0 else f"{s}秒")

            print(f"[后台任务状态] 运行中 (RUNNING)")
            print(f"  * 模块工具: {self.tool_name}")
            print(f"  * 进程 PID: {pid}")
            print(f"  * 启动时间: {info.get('start_time_str', '未知')} (已运行: {elapsed_str})")
            print(f"  * 任务总量: {info.get('file_count', '未知')} 个视频文件")
            if info.get("args"):
                print(f"  * 运行指令: {' '.join(info['args'])}")

            # 读取实时心跳与文件路径快照
            if self.status_file.exists():
                try:
                    with open(self.status_file, "r", encoding="utf-8") as f:
                        status_data = json.load(f)
                    curr_file = status_data.get("current_file")
                    file_idx = status_data.get("file_index")
                    total_f = status_data.get("total_files") or info.get("file_count")
                    stage = status_data.get("stage")
                    prog_str = status_data.get("progress_str")
                    pct = status_data.get("percent")

                    if curr_file:
                        idx_str = f" [{file_idx}/{total_f}]" if file_idx and total_f else ""
                        print(f"  * 当前处理{idx_str}: {curr_file}")
                    if stage:
                        print(f"  * 当前阶段: {stage}")
                    if prog_str:
                        pct_str = f" ({pct:.1f}%)" if pct is not None and f"({pct:.1f}%)" not in prog_str else ""
                        print(f"  * 实时转码进度: {prog_str}{pct_str}")
                except Exception:
                    pass

            print(f"  * 运行主日志: {self.log_file}")
            print(f"  * 实时跟踪: tail -f {self.log_file}")
            print(f"  * 停止命令: {self.tool_name} --stop")
        else:
            print(f"[后台任务状态] 当前没有正在运行的 {self.tool_name} 后台任务。")
            if self.log_file.exists():
                print(f"  * 历史运行日志: {self.log_file}")

        # 打印最近 15 行实质性运行日志
        if self.log_file.exists():
            print("-" * 72)
            print(f"[最新日志动态 (最后 15 行)]: {self.log_file}")
            try:
                with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = [l for l in f if l.strip()]
                    lines = all_lines[-15:]
                for line in lines:
                    sys.stdout.write(f"  {line}")
                if lines and not lines[-1].endswith("\n"):
                    sys.stdout.write("\n")
            except Exception as e:
                print(f"  [读取日志失败: {e}]")
        print("=" * 72)

    def stop_daemon(self) -> bool:
        if not self.pid_file.exists():
            print(f"[提示] 未找到 {self.tool_name} 的后台任务锁文件，当前没有运行中的后台任务。")
            return True
        try:
            with open(self.pid_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("pid")
        except Exception:
            pid = None

        if not pid or not self.is_pid_alive(pid):
            try:
                self.pid_file.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                self.status_file.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"[提示] 进程 {pid if pid else ''} 已不在运行，已清理残留的任务状态。")
            return True

        print(f"[正在停止] 发现正在运行的 {self.tool_name} 后台任务 (PID: {pid})，正在安全停止...")
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

        for _ in range(25):
            time.sleep(0.2)
            if not self.is_pid_alive(pid):
                break
        else:
            print(f"[强制终止] 任务未在限定时间内响应 SIGTERM，发送 SIGKILL 强制终止...")
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass

        try:
            self.pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            self.status_file.unlink(missing_ok=True)
        except Exception:
            pass

        print(f"[完成] 后台任务 (PID: {pid}) 已成功停止。")
        print(f"  * 安全保障: 未完成转码的源文件已严格保护，绝不删除。")
        print(f"  * 日志记录: 详见 {self.log_file}")
        return True

    def daemonize(self, file_count: int, raw_argv: List[str]) -> None:
        info = self.get_running_info()
        if info:
            pid = info["pid"]
            print(f"[错误] 检测到已有 {self.tool_name} 后台任务正在运行 (PID: {pid})！")
            print(f"  * 启动时间: {info.get('start_time_str', '未知')}")
            print(f"  * 实时日志: tail -f {self.log_file}")
            print(f"  * 状态查询: {self.tool_name} --status")
            print(f"  * 若需强制终止现有任务，请执行: {self.tool_name} --stop")
            sys.exit(1)

        if not hasattr(os, "fork"):
            print("[警告] 当前操作系统不支持 Unix fork 守护进程脱离，将直接在前台运行。")
            return

        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        r, w = os.pipe()

        # 第一次 fork
        try:
            pid1 = os.fork()
            if pid1 > 0:
                os.close(w)
                try:
                    resp = os.read(r, 1024).decode("utf-8")
                    grandchild_pid = int(resp.strip()) if resp.strip().isdigit() else None
                except Exception:
                    grandchild_pid = None
                finally:
                    os.close(r)
                os.waitpid(pid1, 0)

                print("=" * 72)
                print(f"[后台运行] 任务已成功转入后台脱机运行！")
                if grandchild_pid:
                    print(f"  * 后台进程 PID: {grandchild_pid}")
                print(f"  * 待处理视频数量: {file_count} 个")
                print(f"  * 实时日志追踪: tail -f {self.log_file}")
                print(f"  * 查看任务状态: {self.tool_name} --status")
                print(f"  * 终止后台任务: {self.tool_name} --stop")
                print("=" * 72)
                sys.exit(0)
        except OSError as e:
            sys.stderr.write(f"[错误] 第一次 fork 失败: {e}\n")
            sys.exit(1)

        # 子进程 1
        os.close(r)
        os.setsid()

        # 第二次 fork
        try:
            pid2 = os.fork()
            if pid2 > 0:
                sys.exit(0)
        except OSError as e:
            sys.stderr.write(f"[错误] 第二次 fork 失败: {e}\n")
            sys.exit(1)

        # 孙进程 (真正的守护进程)
        try:
            os.setpgrp()
        except Exception:
            pass

        daemon_pid = os.getpid()

        now = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pid_data = {
            "pid": daemon_pid,
            "start_time": now,
            "start_time_str": now_str,
            "tool": self.tool_name,
            "args": raw_argv,
            "file_count": file_count,
            "log_file": str(self.log_file.resolve())
        }
        try:
            with open(self.pid_file, "w", encoding="utf-8") as f:
                json.dump(pid_data, f, indent=2)
        except Exception as e:
            sys.stderr.write(f"[警告] 写入 PID 文件失败: {e}\n")

        try:
            os.write(w, str(daemon_pid).encode("utf-8"))
        except Exception:
            pass
        finally:
            try:
                os.close(w)
            except Exception:
                pass

        try:
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.close(devnull)

            log_fd = os.open(str(self.log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            os.close(log_fd)
            try:
                sys.stdout.reconfigure(line_buffering=True)
                sys.stderr.reconfigure(line_buffering=True)
            except Exception:
                pass
        except Exception as e:
            sys.stderr.write(f"[警告] 重定向 I/O 失败: {e}\n")

        def cleanup_pid():
            try:
                if self.pid_file.exists():
                    self.pid_file.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                if self.status_file.exists():
                    self.status_file.unlink(missing_ok=True)
            except Exception:
                pass

        atexit.register(cleanup_pid)

        def sig_handler(signum, frame):
            cleanup_pid()
            sys.exit(143)

        try:
            signal.signal(signal.SIGTERM, sig_handler)
            signal.signal(signal.SIGHUP, sig_handler)
        except Exception:
            pass


# 通用日志记录模块与守护进程控制 (优先使用外部模块，缺失时平滑使用内置引擎)
try:
    import video_logger as vlog
    DaemonManager = getattr(vlog, "DaemonManager", _EmbeddedDaemonManager)
except ImportError:
    _vfix_dir = Path(__file__).resolve().parent
    if str(_vfix_dir) not in sys.path:
        sys.path.insert(0, str(_vfix_dir))
    try:
        import video_logger as vlog
        DaemonManager = getattr(vlog, "DaemonManager", _EmbeddedDaemonManager)
    except ImportError:
        vlog = _EmbeddedVideoLogger()
        DaemonManager = _EmbeddedDaemonManager

if not hasattr(vlog, "setup_logger"):
    vlog = _EmbeddedVideoLogger()
if DaemonManager is None:
    DaemonManager = _EmbeddedDaemonManager

# 全局守护管理器句柄 (供任务转码与心跳实时更新)
DAEMON_MGR: Optional[Any] = None

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

# 现代高效视频编码（高画质高压缩比）
MODERN_EFFICIENT_CODECS = {"hevc", "h265", "av1", "av01", "vp9"}

# 现代通用流媒体视频编码
MODERN_STANDARD_CODECS = {"h264"}

# 只有现代主流流媒体编码才能在码率极低时直接 stream-copy 进 MP4 容器
# 注意：严禁将 mpeg4/wmv/mpeg2 等老旧编码加入！老旧格式必须转码换代！
MP4_COMPLIANT_VIDEO_CODECS = MODERN_EFFICIENT_CODECS | MODERN_STANDARD_CODECS

# 常见早期陈旧低效编码（压缩比极低、体积虚高、非现代硬解标准）：
# 无论码率高低，默认必须重新编码升级为现代高效流媒体格式 (HEVC/H.264)！
LEGACY_INEFFICIENT_CODECS = {
    "mpeg4", "msmpeg4", "msmpeg4v1", "msmpeg4v2", "msmpeg4v3",
    "mpeg1video", "mpeg2video", "wmv1", "wmv2", "wmv3", "vc1",
    "flv1", "flv", "vp6", "vp6f", "h263", "h263p", "spark",
    "rv10", "rv20", "rv30", "rv40", "mjpeg", "rawvideo",
    "indeo3", "indeo4", "indeo5", "cinepak", "qtrle", "smc"
}

# 常见老旧或非标准 MP4 视频封装容器后缀（当视频编码已是现代规范如 H.264/HEVC 时，仅需换封装至 MP4 即可秒级无损兼容）
LEGACY_OR_NON_MP4_CONTAINERS = {
    ".avi", ".mkv", ".flv", ".f4v", ".ts", ".m2ts", ".mts",
    ".wmv", ".asf", ".mov", ".3gp", ".3g2", ".mpg", ".mpeg",
    ".vob", ".divx", ".xvid", ".ogv", ".rmvb", ".rm", ".webm"
}

# 各分辨率在现代高能效 H.264 (High Profile, B-frames, AQ) 下的高清黄金基准码率锚点 (bps)
STANDARD_TARGET_BITRATES_H264 = [
    (320 * 240, 400 * 1000),           # 240p  ~ 400 kbps
    (480 * 360, 600 * 1000),           # 360p  ~ 600 kbps
    (720 * 480, 1000 * 1000),          # 480p  ~ 1.0 Mbps
    (1280 * 720, 1800 * 1000),         # 720p  ~ 1.8 Mbps (30fps高质基准)
    (1920 * 1080, 3500 * 1000),        # 1080p ~ 3.5 Mbps
    (2560 * 1440, 6000 * 1000),        # 2K    ~ 6.0 Mbps
    (3840 * 2160, 12000 * 1000),       # 4K    ~ 12.0 Mbps
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
    """检测当前系统及 FFmpeg 是否支持 Apple VideoToolbox H.264 硬件加速。"""
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


def has_videotoolbox_hevc_support() -> bool:
    """检测当前系统及 FFmpeg 是否支持 Apple VideoToolbox HEVC/H.265 硬件加速。"""
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
        return "hevc_videotoolbox" in proc.stdout
    except Exception:
        return False


def has_rkmpp_support() -> bool:
    """检测当前系统及 FFmpeg 是否支持 Rockchip MPP H.264 硬件加速。"""
    if platform.system() != "Linux":
        return False
    try:
        proc = subprocess.run(
            ["ffmpeg", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        return "h264_rkmpp" in proc.stdout
    except Exception:
        return False


def has_rkmpp_hevc_support() -> bool:
    """检测当前系统及 FFmpeg 是否支持 Rockchip MPP HEVC/H.265 硬件加速。"""
    if platform.system() != "Linux":
        return False
    try:
        proc = subprocess.run(
            ["ffmpeg", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        return "hevc_rkmpp" in proc.stdout
    except Exception:
        return False


def get_hwaccel_decode_args(file_path: Path, codec: str) -> List[str]:
    """根据文件状态与编码格式，智能决定是否启用硬件解码加速。"""
    lower_path = str(file_path).lower()
    # 残缺/临时下载中的文件优先 CPU 软件容错解码，确保 100% 抢救成功率
    if any(lower_path.endswith(ext) for ext in TEMP_DOWNLOAD_EXTENSIONS):
        return []
    # Rockchip RKMPP 硬解支持主流高清编码：h264, hevc, h265, vp9, av1 等
    if has_rkmpp_support() and codec.lower() in ("h264", "hevc", "h265", "vp9", "av1"):
        return ["-hwaccel", "rkmpp"]
    return []


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


def get_video_container_ext(file_path: Path) -> str:
    """提取去除临时下载后缀后的真实视频容器扩展名 (如 a.avi.part -> .avi, video.mp4 -> .mp4)。"""
    name = file_path.name
    lower = name.lower()
    stripped = True
    while stripped:
        stripped = False
        for temp_ext in TEMP_DOWNLOAD_EXTENSIONS:
            if lower.endswith(temp_ext):
                name = name[:-len(temp_ext)]
                lower = name.lower()
                stripped = True
                break
    return Path(name).suffix.lower()


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


def safe_move_file(src: Path, dst: Path, max_retries: int = 5) -> None:
    """安全跨文件系统移动文件，彻底规避 macOS 上跨盘/SMB 网络共享时 Python 3.14 fcopyfile 的 ENXIO (Errno 6) 与 Bad file descriptor (Errno 9) 缺陷。

    1. 优先尝试本地原子重命名 (os.replace)；
    2. 若跨文件系统 (EXDEV) 或网络挂载点 (SMB/NFS)，采用流式分块拷贝 (16MB buffer) 写入目标目录下的临时文件；
    3. 内置网络盘断连瞬时重试机制 (针对长时间转码后 SMB 会话重连导致的 EBADF / ENXIO / EIO / ETIMEDOUT)；
    4. 校验目标文件大小与源文件一致后，在目标目录内完成覆盖并安全删除源临时文件；
    5. 若网络完全断开，严密保护本地临时文件不被销毁并输出救援路径。
    """
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)

    # 1. 同一文件系统尝试原子替换 (毫秒级)
    try:
        os.replace(src, dst)
        return
    except OSError as e:
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENXIO, errno.ENOTSUP, errno.EBADF):
            if src.resolve() == dst.resolve():
                return
            raise

    # 2. 跨设备/网络挂载点安全流式分块传输与重试
    src_size = src.stat().st_size
    temp_dst = dst.with_name(f".{dst.name}.vfix_tmp.{os.getpid()}")
    chunk_size = 16 * 1024 * 1024  # 16 MB 缓冲区

    TRANSIENT_ERRNOS = {
        errno.EBADF,        # 9: Bad file descriptor (SMB 连接重连瞬间常见)
        errno.ENXIO,        # 6: Device not configured
        errno.EIO,          # 5: Input/output error
        errno.ETIMEDOUT,    # 60: Operation timed out
        errno.ECONNRESET,   # 54: Connection reset by peer
        errno.EAGAIN,       # 35: Resource temporarily unavailable
        getattr(errno, "ESTALE", 70),       # 70: Stale file handle
        getattr(errno, "EHOSTUNREACH", 65), # 65: No route to host
        getattr(errno, "ENETDOWN", 50),     # 50: Network is down
        getattr(errno, "ENETUNREACH", 51),  # 51: Network unreachable
    }

    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            # 确保目标目录可用
            dst.parent.mkdir(parents=True, exist_ok=True)
            if temp_dst.exists():
                temp_dst.unlink(missing_ok=True)

            with open(src, "rb") as fsrc, open(temp_dst, "wb") as fdst:
                while True:
                    buf = fsrc.read(chunk_size)
                    if not buf:
                        break
                    fdst.write(buf)
                try:
                    fdst.flush()
                    os.fsync(fdst.fileno())
                except OSError:
                    pass  # 部分网络文件系统不支持 fsync，忽略

            # 3. 校验大小完整性
            written_size = temp_dst.stat().st_size
            if written_size != src_size:
                raise OSError(f"传输大小不一致: 源文件 {src_size} 字节, 目标写入 {written_size} 字节")

            # 4. 在目标目录内执行替换覆盖
            try:
                os.replace(temp_dst, dst)
            except OSError as replace_err:
                # 某些 SMB 服务器对重命名覆盖有锁限制，尝试先删后换
                if dst.exists():
                    try:
                        dst.unlink(missing_ok=True)
                        os.replace(temp_dst, dst)
                    except OSError:
                        raise replace_err
                else:
                    raise replace_err

            # 5. 校验完成，删除源临时文件
            src.unlink(missing_ok=True)
            return

        except OSError as e:
            last_error = e
            if temp_dst.exists():
                try:
                    temp_dst.unlink(missing_ok=True)
                except OSError:
                    pass

            is_transient = e.errno in TRANSIENT_ERRNOS or "Bad file descriptor" in str(e)
            if is_transient and attempt < max_retries:
                wait_sec = attempt * 1.5
                print(f"  [网络写入自愈] 检测到网络盘瞬时抖动 ({e.strerror or e}，代码 {e.errno})，"
                      f"正在等待 {wait_sec:.1f}s 后自动重试 ({attempt}/{max_retries})...")
                time.sleep(wait_sec)
                continue
            else:
                break
        except Exception as e:
            last_error = e
            if temp_dst.exists():
                try:
                    temp_dst.unlink(missing_ok=True)
                except OSError:
                    pass
            break

    # 若所有重试均失败，绝不销毁本地 SSD 上的宝贵转码文件！
    print(f"\n  [严重错误] 目标路径无法写入 ({dst}): {last_error}", file=sys.stderr)
    print(f"  [数据抢救] 本地 SSD 已保留转码完成的完整文件，未被删除！", file=sys.stderr)
    print(f"    -> 本地路径: {src} (大小: {src.stat().st_size if src.exists() else 0} 字节)", file=sys.stderr)
    print(f"    -> 您可在网络盘恢复后手动复制该文件至目标位置。\n", file=sys.stderr)
    if last_error:
        raise last_error


def normalize_and_resolve_path(path_input: Union[str, Path]) -> Path:
    """
    智能解析路径并解决 macOS / Linux / SMB 网络共享盘上的 Unicode NFD 与 NFC 编码不一致问题。
    例如：macOS Finder 在 SMB 挂载盘上写入的日语浊音/假名、重音符为 NFD 分解字符 (如 'か' + 浊音符)，
    而用户终端输入/粘贴通常为 NFC 预组合字符 (如 'が')。标准 Path.exists() 在 SMB 上将返回 False。
    本函数自动尝试 NFC/NFD 标准化及逐级目录项匹配，精准定位真实存在的路径。
    """
    p = Path(path_input).expanduser()
    if p.exists():
        return p

    p_str = str(p)
    # 优先尝试整段路径的 NFD 和 NFC 转换
    nfd_p = Path(unicodedata.normalize("NFD", p_str))
    if nfd_p.exists():
        return nfd_p
    nfc_p = Path(unicodedata.normalize("NFC", p_str))
    if nfc_p.exists():
        return nfc_p

    # 逐级目录项解析（针对挂载点路径中部分层级为 NFD，部分为 NFC 的混合情况）
    is_absolute = p.is_absolute()
    parts = p.parts
    if not parts:
        return p

    curr = Path(parts[0]) if is_absolute else Path(".")
    start_idx = 1 if is_absolute else 0

    for part in parts[start_idx:]:
        target = curr / part
        if target.exists():
            curr = target
            continue

        target_nfd = curr / unicodedata.normalize("NFD", part)
        if target_nfd.exists():
            curr = target_nfd
            continue

        target_nfc = curr / unicodedata.normalize("NFC", part)
        if target_nfc.exists():
            curr = target_nfc
            continue

        # 若仍未找到且当前层级是已存在目录，遍历当前目录项对比 NFC 归一化名称
        matched_entry = None
        if curr.is_dir():
            norm_target_nfc = unicodedata.normalize("NFC", part)
            try:
                for entry in curr.iterdir():
                    if unicodedata.normalize("NFC", entry.name) == norm_target_nfc:
                        matched_entry = entry
                        break
            except OSError:
                pass

        if matched_entry is not None:
            curr = matched_entry
        else:
            curr = target

    return curr


def natural_sort_key(p: Path) -> List[Any]:
    """生成用于自然排序的 key，保证文件名中的数字按数值大小排序（如 CD1 < CD2 < CD10）。"""
    normalized_name = unicodedata.normalize("NFC", p.name)
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', normalized_name)]


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


def get_expected_bitrate(info: Dict[str, Any], crf: int = 22, encoder: str = "hevc_videotoolbox") -> int:
    """根据分辨率、帧率、编码器及基准系数估算合理码率 (bps)。采用连续平滑插值避免阶梯跳变。"""
    pixels = info.get("width", 1280) * info.get("height", 720)
    if pixels <= 0:
        pixels = 1280 * 720

    anchors = STANDARD_TARGET_BITRATES_H264
    if pixels <= anchors[0][0]:
        base_bps = anchors[0][1]
    elif pixels >= anchors[-1][0]:
        base_bps = anchors[-1][1]
    else:
        base_bps = anchors[-1][1]
        for i in range(len(anchors) - 1):
            p0, b0 = anchors[i]
            p1, b1 = anchors[i + 1]
            if p0 <= pixels <= p1:
                ratio = (pixels - p0) / (p1 - p0)
                base_bps = int(b0 + ratio * (b1 - b0))
                break

    crf_factor = 2 ** ((22 - crf) / 6.0)
    expected_bps = int(base_bps * crf_factor)

    # 编码器效率折算：HEVC (H.265) 仅需 H.264 约 58% 码率即可达到同等乃至更优画质
    is_hevc = "hevc" in encoder or "265" in encoder
    if is_hevc:
        expected_bps = int(expected_bps * 0.58)

    # 帧率动态调节：帧率高则画面信息量大，帧率低（如 18fps/24fps）则单位秒画面帧数少，合理下调基准码率
    fps_val = float(info.get("fps", 30.0) or 30.0)
    if fps_val > 45:
        expected_bps = int(expected_bps * 1.30)
    elif fps_val > 35:
        expected_bps = int(expected_bps * 1.15)
    elif fps_val < 20:
        expected_bps = int(expected_bps * 0.70)
    elif fps_val < 25:
        expected_bps = int(expected_bps * 0.85)

    return max(expected_bps, 200 * 1000)


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


def split_annexb_nals(data: bytes) -> List[bytes]:
    """将 Annex-B 字节流按照 00 00 01 或 00 00 00 01 规范切分为独立的 NAL 单元。"""
    nals = []
    pos = 0
    n = len(data)
    start_offsets = []
    while pos < n:
        if pos + 4 <= n and data[pos : pos + 4] == b"\x00\x00\x00\x01":
            start_offsets.append((pos, 4))
            pos += 4
        elif pos + 3 <= n and data[pos : pos + 3] == b"\x00\x00\x01":
            start_offsets.append((pos, 3))
            pos += 3
        else:
            pos += 1
    for i in range(len(start_offsets)):
        curr_start, prefix_len = start_offsets[i]
        nal_start = curr_start + prefix_len
        nal_end = start_offsets[i + 1][0] if i + 1 < len(start_offsets) else n
        nal = data[nal_start:nal_end].rstrip(b"\x00")
        if nal:
            nals.append(nal)
    return nals


def detect_h264_sps_pps_for_idr(idr_data: bytes, sei_data: Optional[bytes] = None) -> Tuple[Optional[int], Optional[int], Optional[bytes]]:
    """根据首个 IDR 关键帧与 SEI 逆向探测并生成匹配的 H.264 SPS & PPS 头部。
    毫秒级穷举常见分辨率，经 FFmpeg 解码器真实零报错验证后返回 (width, height, sps_pps_annexb)。
    """
    cabac = 0
    ref = 1
    bframes = 0
    if sei_data:
        s_text = sei_data.decode("latin1", errors="ignore")
        if "cabac=1" in s_text:
            cabac = 1
        if "bframes=" in s_text:
            m = re.search(r"bframes=(\d+)", s_text)
            if m:
                bframes = int(m.group(1))
        if "ref=" in s_text:
            m = re.search(r"ref=(\d+)", s_text)
            if m:
                ref = int(m.group(1))

    candidates = [
        (1280, 720), (1920, 1080), (854, 480), (848, 480), (720, 480),
        (640, 480), (960, 540), (1024, 576), (720, 576), (1280, 960),
        (1440, 1080), (1600, 900), (640, 360), (480, 320), (320, 240)
    ]

    START = b"\x00\x00\x00\x01"
    temp_dir = Path(tempfile.gettempdir())
    probe_gen = temp_dir / f"_vfix_probe_gen_{os.getpid()}.h264"
    probe_test = temp_dir / f"_vfix_probe_test_{os.getpid()}.h264"

    try:
        for w, h in candidates:
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=s={w}x{h}:d=0.04:r=30",
                "-c:v", "libx264", "-preset", "ultrafast", "-qp", "23",
                "-x264-params", f"cabac={cabac}:ref={ref}:bframes={bframes}:keyint=250:keyint_min=25:scenecut=0",
                "-f", "h264", str(probe_gen)
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not probe_gen.exists() or probe_gen.stat().st_size == 0:
                continue
            with open(probe_gen, "rb") as f:
                gen_data = f.read(1024)

            nals = split_annexb_nals(gen_data)
            sps_nal = next((n for n in nals if (n[0] & 0x1f) == 7), None)
            pps_nal = next((n for n in nals if (n[0] & 0x1f) == 8), None)
            if not sps_nal or not pps_nal:
                continue

            probe_stream = START + sps_nal + START + pps_nal + START + idr_data
            with open(probe_test, "wb") as f:
                f.write(probe_stream)

            r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(probe_test), "-f", "null", "-"], capture_output=True, text=True)
            if not r.stderr.strip():
                return w, h, (START + sps_nal + START + pps_nal)
    finally:
        for p in (probe_gen, probe_test):
            if p.exists():
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    return None, None, None


def salvage_corrupted_mp4(file_path: Path, output_file: Path) -> bool:
    """针对头部/索引损毁（如 moov 丢失、含全零空洞的 .part、下载未完成文件）的 MP4/MOV 实施深度底层裸流抢救：
    1. 扫描文件寻找残存的 stco / stsz / stsc 及 audio trak 索引；
    2. 提取并验证首个 IDR 关键帧，毫秒级逆向推导出精准分辨率与 SPS/PPS 参数；
    3. 采用单遍流式线性扫描 (8MB 缓存)，将 H.264 视频帧与 AAC 音频包极速分离并注入标准头；
    4. 交由 FFmpeg 原生 -c copy 无损封装为标准带有 faststart 索引头的完好 MP4；
    5. 成功返回 True，失败返回 False。
    """
    t0 = time.time()
    file_size = file_path.stat().st_size
    if file_size < 1024:
        return False

    scan_len = min(file_size, 35 * 1024 * 1024)
    with open(file_path, "rb") as f:
        f.seek(file_size - scan_len)
        tail_data = f.read(scan_len)
        tail_base = file_size - scan_len

        # 1. 扫描有效 stco 表
        stco_tables = []
        idx = 0
        while True:
            p = tail_data.find(b"stco", idx)
            if p == -1:
                break
            abs_p = tail_base + p
            if p >= 4:
                atom_size = int.from_bytes(tail_data[p-4:p], "big")
                entry_count = int.from_bytes(tail_data[p+8:p+12], "big")
                if 10 < entry_count < 5000000 and atom_size == 16 + entry_count * 4:
                    stco_tables.append((abs_p - 4, entry_count, atom_size))
            idx = p + 1

        # 2. 扫描 audio trak
        audio_trak_info = None
        idx = 0
        while True:
            p = tail_data.find(b"trak", idx)
            if p == -1:
                break
            abs_p = tail_base + p
            if p >= 4:
                atom_size = int.from_bytes(tail_data[p-4:p], "big")
                trak_bytes = tail_data[p-4 : min(len(tail_data), p-4+atom_size)]
                if b"soun" in trak_bytes or b"smhd" in trak_bytes or b"mp4a" in trak_bytes:
                    audio_trak_info = (abs_p - 4, atom_size, trak_bytes)
            idx = p + 1

        if not stco_tables:
            return False

        # 区分音频 stco 与视频 stco
        audio_stco_offset = None
        if audio_trak_info:
            a_start, a_size, a_bytes = audio_trak_info
            for off, cnt, sz in stco_tables:
                if a_start <= off < a_start + a_size:
                    audio_stco_offset = off
                    break

        video_stco = next((item for item in stco_tables if item[0] != audio_stco_offset), None)
        if not video_stco:
            return False

        v_stco_off, v_count, _ = video_stco
        f.seek(v_stco_off + 16)
        v_offsets = struct.unpack(f">{v_count}I", f.read(v_count * 4))

        # 解析音频帧及索引
        a_sample_offsets = []
        sample_rate = 44100
        channels = 2
        if audio_trak_info:
            a_start, a_size, a_bytes = audio_trak_info
            p_mp4a = a_bytes.find(b"mp4a")
            if p_mp4a != -1 and p_mp4a + 26 <= len(a_bytes):
                channels = int.from_bytes(a_bytes[p_mp4a+16:p_mp4a+18], "big") or 2
                sample_rate = int.from_bytes(a_bytes[p_mp4a+24:p_mp4a+26], "big") or 44100

            p_stsz = a_bytes.find(b"stsz")
            p_stsc = a_bytes.find(b"stsc")
            p_stco = a_bytes.find(b"stco")
            if p_stsz != -1 and p_stsc != -1 and p_stco != -1:
                a_count = int.from_bytes(a_bytes[p_stsz+12:p_stsz+16], "big")
                if 0 < a_count < 5000000:
                    a_sizes = struct.unpack(f">{a_count}I", a_bytes[p_stsz+16 : p_stsz+16 + a_count*4])
                    a_co_count = int.from_bytes(a_bytes[p_stco+8:p_stco+12], "big")
                    a_chunk_offsets = struct.unpack(f">{a_co_count}I", a_bytes[p_stco+12 : p_stco+12 + a_co_count*4])
                    sc_count = int.from_bytes(a_bytes[p_stsc+8:p_stsc+12], "big")
                    stsc_entries = [struct.unpack(">III", a_bytes[p_stsc+12+i*12 : p_stsc+12+(i+1)*12]) for i in range(sc_count)]

                    curr_stsc_idx = 0
                    sample_idx = 0
                    for chunk_idx in range(len(a_chunk_offsets)):
                        chunk_num = chunk_idx + 1
                        if curr_stsc_idx + 1 < len(stsc_entries) and chunk_num >= stsc_entries[curr_stsc_idx + 1][0]:
                            curr_stsc_idx += 1
                        spc = stsc_entries[curr_stsc_idx][1]
                        offset = a_chunk_offsets[chunk_idx]
                        for s in range(spc):
                            if sample_idx >= a_count:
                                break
                            a_sample_offsets.append((offset, a_sizes[sample_idx]))
                            offset += a_sizes[sample_idx]
                            sample_idx += 1
                        if sample_idx >= a_count:
                            break

        # 3. 读取首个 IDR 关键帧与 SEI 逆向解析视频参数
        idr = None
        sei = None
        for try_chunk_idx in range(min(5, len(v_offsets))):
            f.seek(v_offsets[try_chunk_idx])
            chunk_head = f.read(256 * 1024)
            c_pos = 0
            while c_pos + 4 <= len(chunk_head):
                n_len = int.from_bytes(chunk_head[c_pos : c_pos + 4], "big")
                c_pos += 4
                if n_len == 0 or c_pos + n_len > len(chunk_head):
                    break
                nal = chunk_head[c_pos : c_pos + n_len]
                c_pos += n_len
                ntype = nal[0] & 0x1f
                if ntype == 6 and not sei:
                    sei = nal
                elif ntype == 5 and not idr:
                    idr = nal
                    break
            if idr:
                break

        if not idr:
            return False

        w, h, sps_pps = detect_h264_sps_pps_for_idr(idr, sei)
        if not sps_pps:
            return False

        # 4. 流式提取音视频并重构 Annex-B / ADTS
        valid_v = [off for off in v_offsets if 0 < off < file_size - 4]
        valid_a = [(off, sz) for off, sz in a_sample_offsets if 0 < off < file_size - sz]

        events = [(off, "V", 0) for off in valid_v]
        events.extend([(off, "A", sz) for off, sz in valid_a])
        events.sort(key=lambda x: x[0])

        temp_dir = Path(tempfile.gettempdir())
        temp_v = temp_dir / f"_vfix_stream_{os.getpid()}.h264"
        temp_a = temp_dir / f"_vfix_stream_{os.getpid()}.aac"

        START_CODE = b"\x00\x00\x00\x01"
        freq_map = {96000:0, 88200:1, 64000:2, 48000:3, 44100:4, 32000:5, 24000:6, 22050:7, 16000:8, 12000:9, 11025:10, 8000:11}
        freq_idx = freq_map.get(sample_rate, 4)
        ch_cfg = channels & 7

        try:
            with open(file_path, "rb", buffering=8*1024*1024) as f_in, \
                 open(temp_v, "wb", buffering=8*1024*1024) as f_v, \
                 open(temp_a, "wb", buffering=8*1024*1024) as f_a:
                f_v.write(sps_pps)
                n_events = len(events)
                for i, (off, kind, sz) in enumerate(events):
                    f_in.seek(off)
                    if kind == "V":
                        next_off = events[i+1][0] if i+1 < n_events else off + 1048576
                        chunk_len = max(0, min(next_off - off, 10 * 1024 * 1024))
                        chunk_data = f_in.read(chunk_len)
                        c_pos = 0
                        while c_pos + 4 <= len(chunk_data):
                            n_len = int.from_bytes(chunk_data[c_pos:c_pos+4], "big")
                            c_pos += 4
                            if n_len == 0 or c_pos + n_len > len(chunk_data):
                                break
                            nal = chunk_data[c_pos:c_pos+n_len]
                            c_pos += n_len
                            nal_type = nal[0] & 0x1f
                            if nal_type not in (6, 9):
                                f_v.write(START_CODE + nal)
                    else:
                        raw = f_in.read(sz)
                        if len(raw) < sz or raw == b"\x00" * sz:
                            continue
                        flen = len(raw) + 7
                        b0 = 0xFF; b1 = 0xF1
                        b2 = (1 << 6) | (freq_idx << 2) | (ch_cfg >> 2)
                        b3 = ((ch_cfg & 3) << 6) | (flen >> 11)
                        b4 = (flen >> 3) & 0xFF
                        b5 = ((flen & 7) << 5) | 0x1F
                        b6 = 0xFC
                        f_a.write(bytes([b0, b1, b2, b3, b4, b5, b6]) + raw)

            # 5. FFmpeg 无损流拷贝封装并注入 faststart
            cmd_mux = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-r", "30", "-i", str(temp_v)
            ]
            has_audio_out = temp_a.exists() and temp_a.stat().st_size > 0
            if has_audio_out:
                cmd_mux.extend(["-i", str(temp_a)])
            cmd_mux.extend([
                "-c", "copy", "-movflags", "+faststart",
                str(output_file)
            ])

            r = subprocess.run(cmd_mux, capture_output=True, text=True)
            return r.returncode == 0 and output_file.exists() and output_file.stat().st_size > 1024
        finally:
            for p in (temp_v, temp_a):
                if p.exists():
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass


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


def build_video_encode_args(
    encoder: str,
    target_bps: int,
    orig_v_bitrate: float = 0.0,
    crf: int = 22,
    preset: str = "veryfast",
    gop_size: int = 60,
    keyint_min: int = 30,
    width: int = 0,
    height: int = 0,
    is_merge: bool = False
) -> List[str]:
    """
    统一构建经过深度画质与硬件效能调优的视频编码参数。
    - VideoToolbox (H.264/HEVC):
        * 注入硬件 B 帧 (-bf 2) 与空间自适应量化 (-spatial_aq 1)，消除暗部色块与色带，体积立减 20%~40%；
        * 锁定 High Profile 与标准色彩空间描述 (BT.709)，彻底解决 macOS/iOS QuickTime 播放泛白发灰 (Gamma Shift)；
        * 严格配置 -maxrate 与 -bufsize 峰值缓冲约束，杜绝复杂噪点画面体积反向膨胀。
    - CPU 软件编码 (libx264/libx265):
        * 标准 CRF 配合 GOP/Keyint 规整与码率封顶保护。
    """
    args = []
    # 颜色描述元数据：防止 macOS/iOS QuickTime 发生 Gamma Shift (发灰泛白)
    # 对于常规 720p/1080p 视频，使用 BT.709 标准空间；更小标清采用 SMPTE-170M
    color_args = []
    if width >= 1280 or height >= 720 or (width == 0 and height == 0):
        color_args = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    elif width > 0 and height > 0:
        color_args = ["-color_primaries", "smpte170m", "-color_trc", "smpte170m", "-colorspace", "smpte170m"]

    if encoder == "h264_videotoolbox":
        args = [
            "-c:v", "h264_videotoolbox",
            "-b:v", str(target_bps),
            "-profile:v", "high",
            "-spatial_aq", "1",
            "-bf", "2",
            "-pix_fmt", "yuv420p",
            "-g", str(gop_size),
        ]
        # 码率防膨胀限制：杜绝动态场景瞬时码率暴涨
        if orig_v_bitrate > 0:
            max_limit = min(int(target_bps * 1.25), int(orig_v_bitrate))
        else:
            max_limit = int(target_bps * 1.25)
        args += ["-maxrate", str(max_limit), "-bufsize", str(int(target_bps * 2.0))]
        args += color_args

    elif encoder == "hevc_videotoolbox":
        args = [
            "-c:v", "hevc_videotoolbox",
            "-b:v", str(target_bps),
            "-profile:v", "main",
            "-spatial_aq", "1",
            "-bf", "2",
            "-tag:v", "hvc1",
            "-pix_fmt", "yuv420p",
            "-g", str(gop_size),
        ]
        if orig_v_bitrate > 0:
            max_limit = min(int(target_bps * 1.25), int(orig_v_bitrate))
        else:
            max_limit = int(target_bps * 1.25)
        args += ["-maxrate", str(max_limit), "-bufsize", str(int(target_bps * 2.0))]
        args += color_args

    elif encoder == "h264_rkmpp":
        args = [
            "-c:v", "h264_rkmpp",
            "-b:v", str(target_bps),
            "-profile", "high",
            "-rc_mode", "0",
            "-pix_fmt", "yuv420p",
            "-g", str(gop_size),
        ]
        if orig_v_bitrate > 0:
            max_limit = min(int(target_bps * 1.25), int(orig_v_bitrate))
        else:
            max_limit = int(target_bps * 1.25)
        args += ["-maxrate", str(max_limit), "-bufsize", str(int(target_bps * 2.0))]
        # 注意：RKMPP 硬件编码切勿追加 color_args，否则 FFmpeg 会插入 CPU 软件颜色滤镜导致吞吐骤降

    elif encoder == "hevc_rkmpp":
        args = [
            "-c:v", "hevc_rkmpp",
            "-b:v", str(target_bps),
            "-profile", "main",
            "-rc_mode", "0",
            "-tag:v", "hvc1",
            "-pix_fmt", "yuv420p",
            "-g", str(gop_size),
        ]
        if orig_v_bitrate > 0:
            max_limit = min(int(target_bps * 1.25), int(orig_v_bitrate))
        else:
            max_limit = int(target_bps * 1.25)
        args += ["-maxrate", str(max_limit), "-bufsize", str(int(target_bps * 2.0))]
        # 注意：RKMPP 硬件编码切勿追加 color_args，否则 FFmpeg 会插入 CPU 软件颜色滤镜导致吞吐骤降

    elif encoder == "libx264":
        args = [
            "-c:v", "libx264",
            "-crf", str(crf),
            "-preset", preset,
            "-pix_fmt", "yuv420p",
            "-g", str(gop_size),
            "-keyint_min", str(keyint_min),
            "-sc_threshold", "40",
        ]
        if orig_v_bitrate > 0:
            max_rate_limit = min(int(target_bps * 1.15), int(orig_v_bitrate))
            args += ["-maxrate", str(max_rate_limit), "-bufsize", str(int(target_bps * 2.0))]
        args += color_args

    elif encoder == "libx265":
        args = [
            "-c:v", "libx265",
            "-crf", str(crf),
            "-preset", preset,
            "-pix_fmt", "yuv420p",
            "-tag:v", "hvc1",
            "-g", str(gop_size),
            "-keyint_min", str(keyint_min),
        ]
        if orig_v_bitrate > 0:
            max_rate_limit = min(int(target_bps * 0.90), int(orig_v_bitrate * 0.75))
            args += ["-maxrate", str(max_rate_limit), "-bufsize", str(int(target_bps * 2.0))]
        args += color_args

    else:
        args = ["-c:v", encoder, "-b:v", str(target_bps), "-g", str(gop_size)]

    return args


def should_compress(
    file_path: Path,
    info: Dict[str, Any],
    expected_bitrate: int,
    threshold: float,
    encoder: str = "hevc_videotoolbox",
    force_compress: bool = False,
    force_repair_only: bool = False
) -> Tuple[bool, str]:
    """
    判断当前视频是否需要执行重新编码压缩。
    - 对于残缺的 .part 文件及早期陈旧低效编码 (mpeg4/wmv/mpeg2 等)，默认坚决执行修复并转码压缩。
    - 对于原片已是主流 H.264 或现代高效编码 (HEVC/AV1) 的老旧/非标封装容器 (.avi/.mkv/.flv/.ts/.wmv 等)，
      坚决优先执行极速无损换封装至标准 MP4 (-c:v copy)，杜绝耗时且有损的重新编码。
    """
    if force_repair_only:
        return False, "用户指定强制仅修复封装 (--force-repair-only)"
    if force_compress:
        return True, "用户指定强制压缩 (--force-compress)"

    name_lower = file_path.name.lower()
    is_part_file = any(name_lower.endswith(ext) for ext in TEMP_DOWNLOAD_EXTENSIONS)
    if is_part_file:
        return True, "检测到残缺/未完成文件 (.part)，执行容错流修复并重新编码压缩"

    orig_bitrate = info.get("bitrate", 0.0)
    orig_v_bitrate = info.get("video_bitrate", 0.0)
    ref_orig_bitrate = orig_v_bitrate if orig_v_bitrate > 0 else orig_bitrate
    codec = info.get("codec", "").lower()
    container_ext = get_video_container_ext(file_path)

    # 1. 陈旧低效编码（如 mpeg4/xvid/divx, wmv, flv, mpeg2 等）：
    # 压缩比极差、体积严重虚高且缺乏现代硬解支持，坚决必须转码压缩升级！
    if codec in LEGACY_INEFFICIENT_CODECS or (codec and codec not in MP4_COMPLIANT_VIDEO_CODECS):
        return True, f"原视频编码格式为早期低效编码 ({codec})，体积虚高且现代硬解兼容性差，必须升级转码为现代高效流媒体格式并压缩"

    # 2. 判断是否为早期老旧容器或非标准 MP4 封装
    is_legacy_or_non_mp4_container = (
        container_ext in LEGACY_OR_NON_MP4_CONTAINERS or
        container_ext not in (".mp4", ".m4v")
    )
    c_label = container_ext if container_ext else file_path.suffix

    # 3. 原片已是主流工业级 H.264 (AVC) 编码：
    is_h264 = codec in ("h264", "avc", "avc1")
    if is_h264:
        # 若容器为老旧/非标封装 (如 .avi, .mkv, .flv, .ts, .wmv, .mov 等)：
        # 原画质已是高兼容现代标准，仅需换封装至 MP4，坚决杜绝耗时且损耗画质的重新编码！
        if is_legacy_or_non_mp4_container:
            return False, f"原片已是主流 H.264 编码，容器为早期/非标封装 ({c_label})，直接执行极速无损换封装至标准 MP4 (0重编码、0画质损失、秒级完成)"

        # 若原片本身已是标准 MP4 容器：
        # 仅当目标编码器为 HEVC 且原码率明显高于基准、节约率显著 (>= 25%) 时才转码压缩
        is_target_hevc = "hevc" in encoder or "265" in encoder
        if is_target_hevc and ref_orig_bitrate > expected_bitrate:
            saving_ratio = (ref_orig_bitrate - expected_bitrate) / ref_orig_bitrate
            if saving_ratio >= 0.25:
                if "rkmpp" in encoder:
                    hw_desc = "Rockchip HEVC 硬件编码"
                elif "videotoolbox" in encoder:
                    hw_desc = "Apple VideoToolbox HEVC 硬件编码"
                elif "nvenc" in encoder:
                    hw_desc = "NVENC HEVC 硬件编码"
                elif "qsv" in encoder:
                    hw_desc = "Intel QSV HEVC 硬件编码"
                elif "vaapi" in encoder:
                    hw_desc = "VAAPI HEVC 硬件编码"
                else:
                    hw_desc = "HEVC/H.265 编码"
                return True, f"原片为 H.264 ({format_bitrate(ref_orig_bitrate)})，转换为 {hw_desc} 预计可节省 ~{saving_ratio*100:.0f}% 存储空间"

    # 4. 若原视频已经是 HEVC/H.265/AV1 高效编码：
    if codec in MODERN_EFFICIENT_CODECS:
        if is_legacy_or_non_mp4_container:
            return False, f"原视频已是现代高效编码 ({codec})，容器为早期/非标封装 ({c_label})，直接执行极速无损换封装至标准 MP4 (0重编码、0画质损失、秒级完成)"
        if ref_orig_bitrate <= expected_bitrate * 1.10:
            return False, f"原视频已是现代高效编码 ({codec}) 且码率已高度紧凑 ({format_bitrate(ref_orig_bitrate)})，无需二次压缩，仅执行快速无损修复封装"

    if ref_orig_bitrate <= 0:
        return True, "原文件码率无法精确探测，执行容错修复并转码压缩"

    # 5. 常规码率阈值收益判定
    if ref_orig_bitrate <= expected_bitrate:
        return False, (
            f"原视频码率 ({format_bitrate(ref_orig_bitrate)}) 已处于或低于基准区间 ({format_bitrate(expected_bitrate)})，"
            f"无需重复压缩，仅执行快速无损修复封装"
        )

    bitrate_diff_ratio = (ref_orig_bitrate - expected_bitrate) / ref_orig_bitrate
    if bitrate_diff_ratio <= threshold:
        return False, (
            f"原码率 ({format_bitrate(ref_orig_bitrate)}) 与预期基准 ({format_bitrate(expected_bitrate)}) 差别不大 "
            f"(预期节约仅 {bitrate_diff_ratio*100:.1f}% <= {threshold*100:.1f}%)，跳过压缩以保留原画质"
        )

    return True, (
        f"原码率 ({format_bitrate(ref_orig_bitrate)}) 明显高于预期标准 ({format_bitrate(expected_bitrate)})，"
        f"启动硬件/高质量编码压缩，预计节约空间 ~{bitrate_diff_ratio*100:.1f}%"
    )


CURRENT_ACTIVE_PROCESS: Optional[subprocess.Popen] = None


def setup_signal_handlers():
    """注册进程信号处理，确保被 SIGTERM/SIGHUP 终止时妥善清理子进程 FFmpeg"""
    def handle_termination(signum, frame):
        global CURRENT_ACTIVE_PROCESS
        if CURRENT_ACTIVE_PROCESS:
            try:
                CURRENT_ACTIVE_PROCESS.terminate()
                CURRENT_ACTIVE_PROCESS.wait(timeout=2)
            except Exception:
                try:
                    CURRENT_ACTIVE_PROCESS.kill()
                except Exception:
                    pass
        sys.exit(143)

    try:
        signal.signal(signal.SIGTERM, handle_termination)
        signal.signal(signal.SIGHUP, handle_termination)
    except Exception:
        pass


def execute_ffmpeg(
    cmd: List[str],
    duration: float = 0.0,
    current_file: Optional[Union[str, Path]] = None,
    current_idx: Optional[int] = None,
    total_files: Optional[int] = None
) -> Tuple[bool, str]:
    """执行 ffmpeg 命令，实时显示进度并通过后台线程持续排空 stderr，防止坏帧日志塞满管道缓冲区导致进程卡死死锁。"""
    global CURRENT_ACTIVE_PROCESS, DAEMON_MGR
    full_cmd = [cmd[0]] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    try:
        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        CURRENT_ACTIVE_PROCESS = proc

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
        last_milestone_pct = -1
        last_non_tty_print_time = 0.0
        last_status_update_time = 0.0
        is_tty = sys.stdout.isatty()

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
                    cur_time = time.time()
                    curr_sec = 0.0
                    pct = None
                    try:
                        parts = last_time.split(":")
                        curr_sec = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                        if duration > 0:
                            pct = min(100.0, max(0.0, (curr_sec / duration) * 100))
                    except Exception:
                        pass

                    pct_str = f" ({pct:.1f}%)" if pct is not None else ""
                    info_str = f"已编码时间: {last_time}{pct_str} | 速度: {last_speed} | 帧率: {last_fps} fps"

                    # 1. 实时向后台守护状态文件写入心跳快照 (供 vfix --status 随时高频查看最新进度，间隔 >= 1.5s)
                    if DAEMON_MGR and (cur_time - last_status_update_time >= 1.5):
                        DAEMON_MGR.update_status(
                            current_file=current_file,
                            file_index=current_idx,
                            total_files=total_files,
                            stage="转码压缩中",
                            progress_str=f"已编码: {last_time} | 速度: {last_speed} | 帧率: {last_fps} fps",
                            percent=pct
                        )
                        last_status_update_time = cur_time

                    # 2. 控制台与日志输出控制
                    if is_tty:
                        # 交互式终端：\r 原地刷新单行
                        sys.stdout.write(f"\r  [处理进度] {info_str}")
                        sys.stdout.flush()
                    else:
                        # 非终端（后台脱机运行 / 重定向至日志文件）：
                        # 严禁每 4 秒重复输出导致日志海量刷屏！
                        # 仅在跨越 10% 进度里程碑时输出一行；若未知时长则至多每 60 秒记录一行
                        should_log = False
                        milestone_label = ""
                        if pct is not None:
                            curr_bucket = int(pct // 10) * 10
                            if curr_bucket > last_milestone_pct and curr_bucket > 0:
                                should_log = True
                                milestone_label = f" [{curr_bucket}%]"
                                last_milestone_pct = curr_bucket
                        else:
                            if cur_time - last_non_tty_print_time >= 60.0:
                                should_log = True
                                last_non_tty_print_time = cur_time

                        if should_log:
                            sys.stdout.write(f"  [处理进度{milestone_label}] {info_str}\n")
                            sys.stdout.flush()

        proc.wait()
        stderr_thread.join(timeout=2.0)

        if last_time and is_tty:
            sys.stdout.write("\n")
            sys.stdout.flush()

        if DAEMON_MGR:
            DAEMON_MGR.update_status(
                current_file=current_file,
                file_index=current_idx,
                total_files=total_files,
                stage="转码完成，正在封装校验" if proc.returncode == 0 else "转码异常终止"
            )

        if proc.returncode != 0:
            err_msg = "\n".join(last_stderr_lines)
            return False, err_msg
        return True, ""
    except KeyboardInterrupt:
        sys.stdout.write("\n  [中断] 接收到中断信号，正在终止 FFmpeg 进程...\n")
        sys.stdout.flush()
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        raise
    except Exception as e:
        return False, str(e)
    finally:
        CURRENT_ACTIVE_PROCESS = None


def remove_source_files(
    sources: List[Path],
    final_output: Path,
    reason: str = "源文件清理 (--delete-source)",
    tool: str = "vfix"
) -> None:
    """
    在视频处理或合并成功后安全删除源文件（具有最严密的安全熔断保护机制）：
    1. 校验目标文件是否存在且大小正常；
    2. 探测目标文件是否包含有效视频流；
    3. 比对源文件与目标文件的时长，若目标文件时长缩水超过 6% 且少于 4 秒，绝对禁止删除并发出致命拦截警告；
    4. 比对体积异常暴跌，防止截断残废文件误删源文件；
    5. 记录完整审计日志至 deleted.log。
    """
    try:
        final_res = final_output.resolve()
    except Exception:
        final_res = None

    is_dir_output = False
    if final_output.is_dir():
        is_dir_output = True
        sub_videos = [f for f in final_output.glob("*") if f.is_file() and f.stat().st_size > 0]
        if not sub_videos:
            print(f"  * [安全熔断拦截] 切片目录为空或无有效分段视频 ({final_output.resolve()})，绝对禁止删除源文件！", file=sys.stderr)
            return
        out_dur = 0.0
        out_size = sum(f.stat().st_size for f in sub_videos)
    else:
        if not final_output.exists() or final_output.stat().st_size <= 0:
            print(f"  * [安全熔断拦截] 目标输出文件不存在或为空 ({final_output.resolve()})，绝对禁止删除源文件！", file=sys.stderr)
            return

        # 深度前置探针：验证目标产物的有效性
        out_info = probe_video(final_output)
        if not out_info or not out_info.get("width"):
            print(f"  * [安全熔断拦截] 目标输出文件无有效视频画面流 ({final_output.resolve()})，绝对禁止删除源文件！", file=sys.stderr)
            return

        out_dur = out_info.get("duration", 0.0)
        out_size = final_output.stat().st_size

    for s in sources:
        try:
            if not s.exists():
                continue
            s_res = s.resolve()
            if final_res is not None and s_res == final_res:
                print(f"  * [源文件清理跳过] 源文件与输出文件路径相同，已保护: {s_res}")
                continue

            s_size = s.stat().st_size

            # 核心安全守门员：源文件与输出文件的时长严格核验
            s_info = probe_video(s)
            s_dur = s_info.get("duration", 0.0) if s_info else 0.0

            # 若源文件探测到了有效时长且大于 15 秒
            if s_dur > 15.0 and out_dur > 0.0:
                # 若产物时长比源视频短超过 6% 且缺失超过 4 秒
                if out_dur < (s_dur * 0.94) and (s_dur - out_dur) > 4.0:
                    err_msg = (
                        f"产物时长严重截断！源视频时长 {s_dur:.1f}s ({s_dur/60:.1f}m)，"
                        f"产物仅 {out_dur:.1f}s ({out_dur/60:.1f}m) "
                        f"(丢失 {(s_dur - out_dur):.1f}s, 缺失率 {((s_dur - out_dur) / s_dur * 100):.1f}%)"
                    )
                    print(f"  * [致命安全拦截] {err_msg}！绝对禁止删除源文件: {s_res}", file=sys.stderr)
                    if vlog:
                        vlog.log_deleted(
                            deleted_file=s,
                            size_bytes=s_size,
                            reason=f"安全拦截阻止删除: {err_msg}",
                            target_path=final_output,
                            status="BLOCKED_PROTECTED",
                            tool=tool
                        )
                    continue

            # 体积异常暴跌熔断保护：如原文件 700MB，产物只有 4MB (-99.5%)
            if s_size > 20 * 1024 * 1024 and out_size < (s_size * 0.10) and (s_dur > 30.0 or s_size > 50 * 1024 * 1024):
                if out_dur < (s_dur * 0.90) if s_dur > 0 else True:
                    err_msg = f"产物体积异常暴跌 ({format_bytes(out_size)} vs 原文件 {format_bytes(s_size)}, 降幅超 90%) 且时长存疑"
                    print(f"  * [致命安全拦截] {err_msg}！疑似破损截断残片，绝对禁止删除源文件: {s_res}", file=sys.stderr)
                    if vlog:
                        vlog.log_deleted(
                            deleted_file=s,
                            size_bytes=s_size,
                            reason=f"安全拦截阻止删除: {err_msg}",
                            target_path=final_output,
                            status="BLOCKED_PROTECTED",
                            tool=tool
                        )
                    continue

            s.unlink()
            if vlog:
                vlog.log_deleted(
                    deleted_file=s,
                    size_bytes=s_size,
                    reason=reason,
                    target_path=final_output,
                    status="DELETED",
                    tool=tool
                )
            print(f"  * [源文件清理] 已安全删除源文件: {s_res} ({format_bytes(s_size)}, 已记录至 deleted.log)")
        except Exception as e:
            if vlog:
                vlog.log_deleted(
                    deleted_file=s,
                    size_bytes=0,
                    reason=reason,
                    target_path=final_output,
                    status="DELETE_FAILED",
                    error_msg=str(e),
                    tool=tool
                )
            print(f"  * [警告] 删除源文件失败 ({s.resolve()}): {e}", file=sys.stderr)


def process_video(
    file_path: Path,
    output_dir: Optional[Path],
    threshold: float,
    crf: int,
    preset: str,
    encoder: str,
    force_compress: bool,
    force_repair_only: bool,
    delete_source: bool = False,
    crop: Optional[str] = None,
    aspect: Optional[str] = None,
    split_scenes: bool = False,
    min_segment_duration: float = 12.0,
    drop_ads: bool = False,
    max_ad_duration: float = 120.0,
    current_idx: int = 1,
    total_files: int = 1
) -> bool:
    """处理单个视频文件（支持深度容错修复、智能去黑边、硬件极速压缩与本地 SSD 缓冲写入）。"""
    print("=" * 70)
    print(f"[处理中] [{current_idx}/{total_files}] 文件: {file_path.resolve()}")

    if DAEMON_MGR:
        DAEMON_MGR.update_status(
            current_file=file_path,
            file_index=current_idx,
            total_files=total_files,
            stage="元数据探测与黑边分析"
        )

    salvaged_src_file: Optional[Path] = None
    info = probe_video(file_path)
    if not info or not info.get("width"):
        print(f"  * [残缺救砖] 检测到容器头或索引严重损毁（常规解复用失败），启动 MP4/MOV 底层原子流提取抢救引擎...")
        temp_dir = Path(tempfile.gettempdir())
        clean_stem = get_clean_stem(file_path)
        salvaged_candidate = temp_dir / f"vfix_salvaged_{os.getpid()}_{clean_stem}.mp4"
        if salvage_corrupted_mp4(file_path, salvaged_candidate):
            salvaged_src_file = salvaged_candidate
            info = probe_video(salvaged_src_file)
            if info and info.get("width"):
                print(f"  * [残缺救砖] 成功抢救并重构标准 MP4 容器！(大小: {format_bytes(salvaged_src_file.stat().st_size)})")

    if not info:
        orig_size = file_path.stat().st_size
        print(f"  * 状态: 文件头/索引严重缺失（常见于 .part 残缺文件），启动深度容错修复...")
        info = {
            "size": orig_size, "bitrate": 0.0, "video_bitrate": 0.0, "audio_bitrate": 0.0,
            "width": 1280, "height": 720, "fps": 30.0, "codec": "unknown", "has_audio": True
        }
    else:
        orig_size = file_path.stat().st_size
        orig_bitrate = info["bitrate"]
        orig_v_bitrate = info.get("video_bitrate", 0.0)
        orig_a_bitrate = info.get("audio_bitrate", 0.0)
        v_str = format_bitrate(orig_v_bitrate) if orig_v_bitrate > 0 else (format_bitrate(orig_bitrate) if orig_bitrate > 0 else "未知")
        a_str = format_bitrate(orig_a_bitrate) if orig_a_bitrate > 0 else ("有" if info.get("has_audio") else "无")
        print(f"  * 原始属性: {info['width']}x{info['height']} @ {info['fps']:.1f}fps | "
              f"编码: {info['codec']} (视频码率: {v_str}) | 音频: {a_str} | 大小: {format_bytes(orig_size)}")

    width = info.get("width", 0)
    height = info.get("height", 0)
    duration = info.get("duration", 0.0)

    # 智能黑边探测与裁剪参数配置
    crop_info = None
    crop_filter = None
    crop_applied = False
    out_w = width
    out_h = height

    if crop and not force_repair_only:
        # 智能场景切片模式分支
        if split_scenes and detect_scene_segments is not None:
            print(f"  * 智能场景切片模式: [开启] (最小片段门槛: {min_segment_duration:.1f}s)")
            if drop_ads:
                print(f"    * 广告智能剔除: [开启] (最大广告时长门槛: {max_ad_duration:.1f}s)")
            print(f"    - 正在高速扫描全片画幅时序突变时间点...")
            t0_split = time.time()
            detected_segments = detect_scene_segments(
                file_path=file_path,
                orig_w=width,
                orig_h=height,
                duration=duration,
                min_segment_duration=min_segment_duration,
                target_aspect=aspect,
                drop_ads=drop_ads,
                max_ad_duration=max_ad_duration
            )
            t_split = time.time() - t0_split
            if detected_segments and len(detected_segments) > 1:
                print(f"  * 场景切片检测完成 (耗时: {t_split:.2f}s): 成功识别出 {len(detected_segments)} 个不同画幅场景分段！")
                for seg in detected_segments:
                    s, e, k = seg["start"], seg["end"], seg["kind"]
                    c_info = seg["crop"]
                    print(f"    - Part {seg['part_index']:02d}: [{s:6.1f}s -> {e:6.1f}s] (时长: {seg['duration']:5.1f}s) | 画幅: {seg['aspect_name']} | 滤镜: -vf \"crop={c_info['crop_str']}\"")

                return process_video_with_splits(
                    file_path=file_path,
                    output_target=output_dir,
                    segments=detected_segments,
                    info=info,
                    encoder=encoder,
                    crf=crf,
                    preset=preset,
                    delete_source=delete_source
                )
            else:
                print(f"  * 场景切片检测完成 (耗时: {t_split:.2f}s): 全片画幅风格高度一致，自动平滑回退为单文件全局裁剪流程。")

        if detect_crop_box is None:
            print("  [警告] 未找到配套的 video_autocrop 模块，无法执行自动去黑边，跳过裁切...")
        else:
            if crop != "auto" and ":" in str(crop):
                parts = str(crop).strip().split(":")
                if len(parts) == 4:
                    try:
                        cw, ch, cx, cy = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
                        crop_info = {
                            "w": cw, "h": ch, "x": cx, "y": cy,
                            "top": cy, "bottom": height - (cy + ch),
                            "left": cx, "right": width - (cx + cw),
                            "crop_str": f"{cw}:{ch}:{cx}:{cy}",
                            "has_black_bars": True,
                            "ad_immune": False,
                            "confidence": 100.0
                        }
                    except ValueError:
                        print(f"  [警告] 手动 crop 格式无效: '{crop}'，期望格式为 W:H:X:Y (如 1920:800:0:140)")
            else:
                print(f"  * 黑边智能检测中: 采样分析中 (附带去黑边模式)...")
                t_detect = time.time()
                crop_info = detect_crop_box(
                    file_path=file_path,
                    orig_w=width,
                    orig_h=height,
                    duration=duration,
                    target_aspect=aspect
                )
                detect_elapsed = time.time() - t_detect

            if crop_info and crop_info.get("has_black_bars"):
                crop_applied = True
                crop_filter = f"crop={crop_info['crop_str']}"
                out_w = crop_info["w"]
                out_h = crop_info["h"]
                aspect_desc = get_aspect_ratio_name(out_w, out_h) if get_aspect_ratio_name else f"{out_w}:{out_h}"
                time_str = f"耗时: {detect_elapsed:.2f}s, " if 'detect_elapsed' in locals() else ""
                print(f"  * 黑边检测完成 ({time_str}置信度: {crop_info.get('confidence', 90.0):.1f}%):")
                if crop_info.get("ad_immune"):
                    print("    * 智能过滤: [已成功排除顶部横幅广告标语与边角水印干扰]")
                print(f"    - 黑边分布: 上={crop_info['top']}px | 下={crop_info['bottom']}px | 左={crop_info['left']}px | 右={crop_info['right']}px")
                print(f"    - 裁后分辨率: {out_w}x{out_h} | 新画幅比例: {aspect_desc}")
                print(f"    - 对应滤镜: -vf \"{crop_filter}\"")
            else:
                print("  * 黑边检测完成: 画面未检测到明显黑边，保持原始画幅。")
    elif crop and force_repair_only:
        print("  [提示] --force-repair-only 模式为无损修复封装，无法切除物理黑边（去黑边必须重编码）。")

    cropped_info = dict(info)
    if crop_applied:
        cropped_info["width"] = out_w
        cropped_info["height"] = out_h

    expected_bps = get_expected_bitrate(cropped_info, crf, encoder)
    orig_bitrate = info.get("bitrate", 0.0)
    orig_v_bitrate = info.get("video_bitrate", 0.0)
    if orig_v_bitrate <= 0:
        orig_v_bitrate = orig_bitrate

    # 策略决策：基于真实的视频规格与基准码率判定是否值得重新编码压缩
    need_compress, reason = should_compress(
        file_path, info, expected_bps, threshold, encoder, force_compress, force_repair_only
    )
    if crop_applied:
        need_compress = True
        reason = f"附带去黑边裁切并压缩 ({out_w}x{out_h})"

    codec = info.get("codec", "").lower()
    is_legacy = codec in LEGACY_INEFFICIENT_CODECS

    # 严格的动态调优目标码率与防倒挂保护策略：
    pixel_ratio = (out_w * out_h) / max(1, width * height)
    if need_compress:
        if orig_v_bitrate > 0 and not is_legacy:
            if crop_applied:
                # 裁剪黑边后有效画面像素减少，码率等比缩减，绝不膨胀
                crop_capped_bps = int(orig_v_bitrate * pixel_ratio * 0.95)
                target_bps = min(expected_bps, max(120_000, crop_capped_bps))
            else:
                target_bps = min(expected_bps, int(orig_v_bitrate * 0.85))
            if expected_bps > orig_v_bitrate:
                print(f"  * 码率保护策略: 预期基准码率 ({format_bitrate(expected_bps)}) 高于原视频码率 ({format_bitrate(orig_v_bitrate)})，"
                      f"自动降级封顶至 ~{format_bitrate(target_bps)} (严禁高于源文件，杜绝反向膨胀)")
        else:
            target_bps = expected_bps
    else:
        target_bps = expected_bps

    is_hardware = "videotoolbox" in encoder or "rkmpp" in encoder
    target_w = out_w if crop_applied else width
    target_h = out_h if crop_applied else height

    # 智能硬件编码门槛判断：
    # Apple Silicon 与 Rockchip RK3588 (arm64) 原生极速支持全分辨率压制；
    # 仅早期 Intel 核显存在 >= 640x360 的限制
    is_arm_arch = platform.machine().lower() in ("arm64", "aarch64")
    if is_hardware and not is_arm_arch and (target_w < 640 or target_h < 360):
        fallback_cpu = "libx265" if "hevc" in encoder else "libx264"
        print(f"  * 提示: 视频分辨率 ({target_w}x{target_h}) 偏小且处于 x86_64 架构，自动选用 CPU 软件极速编码 ({fallback_cpu})")
        encoder = fallback_cpu
        is_hardware = False

    # 组合滤镜：裁剪滤镜 + 偶数对齐滤镜（杜绝硬件编码器报错或播放器绿边）
    vf_filters = []
    if crop_filter:
        vf_filters.append(crop_filter)
    if target_w > 0 and target_h > 0 and (target_w % 2 != 0 or target_h % 2 != 0):
        vf_filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")

    vf_fix_args = ["-vf", ",".join(vf_filters)] if vf_filters else []

    if need_compress:
        if is_hardware:
            hw_name = "Rockchip RKMPP" if "rkmpp" in encoder else "Apple VideoToolbox"
            print(f"  * 编码配置: {hw_name} 硬件极速加速 ({encoder}) | 目标码率: ~{format_bitrate(target_bps)}")
        else:
            print(f"  * 编码配置: CPU 软件编码 ({encoder}, CRF: {crf}, Preset: {preset}) | 目标码率: ~{format_bitrate(target_bps)}")
    policy_action = "修复并转码压缩" if need_compress else "极速无损换封装"
    print(f"  * 策略决策: [{policy_action}] -> {reason}")
    if not need_compress and not force_repair_only:
        print(f"  * 提示: 该文件已处于工业级高兼容或高能效状态，若执意强制重新编码压制可添加参数: --force-compress")

    if output_dir:
        # 兼容用户传递的是目录，还是直接指定了输出目标文件名
        if output_dir.is_dir() or str(output_dir).endswith(("/", "\\")) or not output_dir.suffix:
            target_dir = output_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            clean_stem = get_clean_stem(file_path)
            action_tag = "compressed" if need_compress else "repaired"
            output_name = f"{clean_stem}_{action_tag}.mp4"
            final_output_file = target_dir / output_name
        else:
            target_dir = output_dir.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            final_output_file = output_dir
            output_name = final_output_file.name
    else:
        target_dir = file_path.parent
        clean_stem = get_clean_stem(file_path)
        action_tag = "compressed" if need_compress else "repaired"
        output_name = f"{clean_stem}_{action_tag}.mp4"
        final_output_file = target_dir / output_name

    # 避免输入输出路径完全相同覆盖原文件
    try:
        if final_output_file.resolve() == file_path.resolve():
            output_name = f"{file_path.stem}_crop.mp4" if crop_applied else f"{file_path.stem}_fixed.mp4"
            final_output_file = target_dir / output_name
    except Exception:
        pass

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
        effective_input = salvaged_src_file if (salvaged_src_file and salvaged_src_file.exists()) else file_path
        hw_decode_args = get_hwaccel_decode_args(effective_input, codec)
        base_args = (
            ["ffmpeg", "-y"]
            + hw_decode_args
            + [
                "-hide_banner",
                "-loglevel", "error",
                "-max_error_rate", "1.0",
                "-err_detect", "ignore_err",
                "-fflags", "+discardcorrupt",
                "-i", str(effective_input),
                "-max_muxing_queue_size", "2048"
            ]
        )

        sw_base_args = [
            "ffmpeg", "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-max_error_rate", "1.0",
            "-err_detect", "ignore_err",
            "-fflags", "+discardcorrupt+genpts",
            "-i", str(effective_input),
            "-max_muxing_queue_size", "2048"
        ]

        # 音频预检与码率自适应：0.1 秒锁定正确的音频处理策略，并杜绝音频码率虚高膨胀
        audio_mode = preflight_audio(effective_input, info)
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
                base_args = (
                    ["ffmpeg", "-y"]
                    + hw_decode_args
                    + [
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
                )
                sw_base_args = [
                    "ffmpeg", "-y",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-max_error_rate", "1.0",
                    "-err_detect", "ignore_err",
                    "-fflags", "+discardcorrupt+genpts",
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
            # === 仅修复/无损换封装模式：尝试极速流复制 ===
            if salvaged_src_file and salvaged_src_file.exists():
                print(f"  * 抢救重构产物已为标准高兼容 MP4，直接执行原子归档: {output_name}")
                safe_move_file(salvaged_src_file, final_output_file)
                success = True
                salvaged_src_file = None
            else:
                tag_args = ["-tag:v", "hvc1"] if codec in ("hevc", "h265") else []
                cmd_copy = base_args + map_args + ["-c:v", "copy"] + tag_args + audio_args + [
                    "-movflags", "+faststart",
                    str(temp_output_file)
                ]
                success, last_err_msg = execute_ffmpeg(
                    cmd_copy,
                    duration=duration,
                    current_file=file_path,
                    current_idx=current_idx,
                    total_files=total_files
                )

            fps_val = float(info.get("fps", 30.0) or 30.0)
            gop_size = max(15, int(fps_val * 2))
            keyint_min = max(15, int(fps_val))

            if not success:
                # 格式不兼容时（如 WMV 视频编码无法直接封装进 MP4），自动启动转码修复
                video_encode_args = build_video_encode_args(
                    encoder=encoder,
                    target_bps=target_bps,
                    orig_v_bitrate=orig_v_bitrate,
                    crf=crf,
                    preset=preset,
                    gop_size=gop_size,
                    keyint_min=keyint_min,
                    width=target_w,
                    height=target_h
                )
                cmd_transcode = base_args + map_args + vf_fix_args + video_encode_args + audio_args + [
                    "-avoid_negative_ts", "make_zero",
                    "-movflags", "+faststart",
                    str(temp_output_file)
                ]
                success, last_err_msg = execute_ffmpeg(
                    cmd_transcode,
                    duration=duration,
                    current_file=file_path,
                    current_idx=current_idx,
                    total_files=total_files
                )
                if not success and is_hardware:
                    fallback_video_args = build_video_encode_args(
                        encoder="libx264",
                        target_bps=target_bps,
                        orig_v_bitrate=orig_v_bitrate,
                        crf=crf,
                        preset=preset,
                        gop_size=gop_size,
                        keyint_min=keyint_min,
                        width=target_w,
                        height=target_h
                    )
                    cmd_transcode_sw = sw_base_args + map_args + vf_fix_args + fallback_video_args + audio_args + [
                        "-avoid_negative_ts", "make_zero",
                        "-movflags", "+faststart",
                        str(temp_output_file)
                    ]
                    success, last_err_msg = execute_ffmpeg(
                        cmd_transcode_sw,
                        duration=duration,
                        current_file=file_path,
                        current_idx=current_idx,
                        total_files=total_files
                    )
        else:
            fps_val = float(info.get("fps", 30.0) or 30.0)
            gop_size = max(15, int(fps_val * 2))
            keyint_min = max(15, int(fps_val))

            # === 修复并转码压缩模式 ===
            video_encode_args = build_video_encode_args(
                encoder=encoder,
                target_bps=target_bps,
                orig_v_bitrate=orig_v_bitrate,
                crf=crf,
                preset=preset,
                gop_size=gop_size,
                keyint_min=keyint_min,
                width=target_w,
                height=target_h
            )

            cmd_compress = base_args + map_args + vf_fix_args + video_encode_args + audio_args + [
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(temp_output_file)
            ]
            success, last_err_msg = execute_ffmpeg(
                cmd_compress,
                duration=duration,
                current_file=file_path,
                current_idx=current_idx,
                total_files=total_files
            )

            # 仅当音频为 copy 且异常报错时，才兜底退回 aac 转码
            if not success and audio_mode == "copy":
                cmd_compress_fallback = base_args + map_args + vf_fix_args + video_encode_args + [
                    "-c:a", "aac", "-b:a", f"{target_audio_kbps}k",
                    "-avoid_negative_ts", "make_zero",
                    "-movflags", "+faststart",
                    str(temp_output_file)
                ]
                success, last_err_msg = execute_ffmpeg(
                    cmd_compress_fallback,
                    duration=duration,
                    current_file=file_path,
                    current_idx=current_idx,
                    total_files=total_files
                )

        duration_sec = time.time() - t_start

        # === 核心安全防线：全方位产物完整性与时长截断核验 ===
        is_truncated = False
        out_probe = None
        if success and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
            out_probe = probe_video(temp_output_file)
            if not out_probe or not out_probe.get("width"):
                success = False
                last_err_msg = "产物无有效视频画面流 (生成的文件可能破损或为空壳)"
            else:
                out_dur = out_probe.get("duration", 0.0)
                ref_dur = duration
                if ref_dur <= 0.0 and orig_bitrate > 0:
                    ref_dur = (orig_size * 8.0) / orig_bitrate

                if ref_dur > 15.0 and out_dur > 0.0:
                    if out_dur < (ref_dur * 0.94) and (ref_dur - out_dur) > 4.0:
                        is_truncated = True
                        last_err_msg = (
                            f"产物严重过早截断！源时长: {ref_dur:.1f}s ({ref_dur/60:.1f}m)，"
                            f"转码产物仅: {out_dur:.1f}s ({out_dur/60:.1f}m)，"
                            f"丢失时长: {(ref_dur - out_dur):.1f}s (缺失率: {((ref_dur - out_dur) / ref_dur * 100):.1f}%)"
                        )

        # 硬件加速（如 RKMPP / VideoToolbox）若遇特殊编码/损坏包出现报错或意外提前截断，自动平滑切换至 CPU 软件强力容错编码
        if (not success or is_truncated) and is_hardware:
            if is_truncated:
                print(f"  [严重警告] 硬件加速器 ({encoder}) 发生意外截断退出！\n    * {last_err_msg}\n    * 正在丢弃残片，并立即自动启动 CPU 软件强力容错编码 (libx264) 重新压制...")
            else:
                print("  [提示] 硬件加速不支持该特殊规格或遇到破损异常，自动平滑切换至 CPU 软件容错编码优化...")

            if temp_output_file.exists():
                temp_output_file.unlink(missing_ok=True)

            fallback_video_args = build_video_encode_args(
                encoder="libx264",
                target_bps=target_bps,
                orig_v_bitrate=orig_v_bitrate,
                crf=crf,
                preset=preset,
                gop_size=gop_size,
                keyint_min=keyint_min,
                width=target_w,
                height=target_h
            )
            cmd_sw_fallback = sw_base_args + map_args + vf_fix_args + fallback_video_args + audio_args + [
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(temp_output_file)
            ]
            success, last_err_msg = execute_ffmpeg(
                cmd_sw_fallback,
                duration=duration,
                current_file=file_path,
                current_idx=current_idx,
                total_files=total_files
            )
            is_truncated = False
            if success and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
                out_probe = probe_video(temp_output_file)
                if not out_probe or not out_probe.get("width"):
                    success = False
                    last_err_msg = "CPU 软件转码产物无有效视频流"
                else:
                    out_dur = out_probe.get("duration", 0.0)
                    ref_dur = duration
                    if ref_dur <= 0.0 and orig_bitrate > 0:
                        ref_dur = (orig_size * 8.0) / orig_bitrate
                    if ref_dur > 15.0 and out_dur > 0.0 and out_dur < (ref_dur * 0.94) and (ref_dur - out_dur) > 4.0:
                        is_truncated = True
                        last_err_msg = f"CPU 软件转码后依然截断 (源时长: {ref_dur:.1f}s, 产物仅: {out_dur:.1f}s)！原片损坏严重"
                        success = False
            else:
                success = False

        if is_truncated:
            success = False

        if not success and not salvaged_src_file and any(k in (last_err_msg or "").lower() for k in ["invalid data found", "moov atom not found", "error opening input"]):
            print("  * [残缺救砖] 捕获到解复用器致命错误，自动启动 MP4/MOV 底层原子流提取抢救引擎...")
            temp_dir = Path(tempfile.gettempdir())
            clean_stem = get_clean_stem(file_path)
            salvaged_candidate = temp_dir / f"vfix_salvaged_{os.getpid()}_{clean_stem}.mp4"
            if salvage_corrupted_mp4(file_path, salvaged_candidate):
                salvaged_src_file = salvaged_candidate
                if not need_compress:
                    print(f"  * 抢救重构产物已为标准高兼容 MP4，直接执行原子归档: {output_name}")
                    safe_move_file(salvaged_src_file, final_output_file)
                    success = True
                    salvaged_src_file = None
                else:
                    cmd_retry = [
                        "ffmpeg", "-y",
                        "-hide_banner", "-loglevel", "error",
                        "-i", str(salvaged_src_file)
                    ] + map_args + vf_fix_args + video_encode_args + audio_args + [
                        "-avoid_negative_ts", "make_zero",
                        "-movflags", "+faststart",
                        str(temp_output_file)
                    ]
                    success, last_err_msg = execute_ffmpeg(
                        cmd_retry,
                        duration=duration,
                        current_file=file_path,
                        current_idx=current_idx,
                        total_files=total_files
                    )

        if success and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
            new_size = temp_output_file.stat().st_size

            # 兜底防膨胀：若压缩后的体积居然大于原文件
            if need_compress and new_size > orig_size and orig_size > 0:
                codec = info.get("codec", "").lower()
                name_lower = file_path.name.lower()
                is_part_file = any(name_lower.endswith(ext) for ext in TEMP_DOWNLOAD_EXTENSIONS)

                if codec in MP4_COMPLIANT_VIDEO_CODECS and not is_part_file and not crop_applied:
                    print(f"  [体积保护] 压缩体积 ({format_bytes(new_size)}) 超过原文件 ({format_bytes(orig_size)})，自动转为无损修复封装...")
                    cmd_protect = base_args + map_args + ["-c:v", "copy"] + audio_args + ["-movflags", "+faststart", str(temp_output_file)]
                    success_p, _ = execute_ffmpeg(
                        cmd_protect,
                        duration=duration,
                        current_file=file_path,
                        current_idx=current_idx,
                        total_files=total_files
                    )
                    if success_p and temp_output_file.exists():
                        new_size = temp_output_file.stat().st_size
                else:
                    print(f"  [体积保护] 压缩体积仍偏大 ({format_bytes(new_size)} > {format_bytes(orig_size)})，执行深层紧凑压制...")
                    strict_target_bps = int(orig_bitrate * 0.70) if orig_bitrate > 0 else int(target_bps * 0.75)
                    compact_video_args = build_video_encode_args(
                        encoder="libx264",
                        target_bps=strict_target_bps,
                        orig_v_bitrate=orig_bitrate,
                        crf=crf + 3,
                        preset=preset,
                        gop_size=gop_size,
                        keyint_min=keyint_min,
                        width=target_w,
                        height=target_h
                    )
                    cmd_compact = base_args + map_args + vf_fix_args + compact_video_args + audio_args + [
                        "-avoid_negative_ts", "make_zero",
                        "-movflags", "+faststart",
                        str(temp_output_file)
                    ]
                    success_c, _ = execute_ffmpeg(
                        cmd_compact,
                        duration=duration,
                        current_file=file_path,
                        current_idx=current_idx,
                        total_files=total_files
                    )
                    if success_c and temp_output_file.exists():
                        new_size = temp_output_file.stat().st_size

            size_change = (new_size - orig_size) / orig_size * 100 if orig_size > 0 else 0

            # 将本地 SSD 临时生成的文件安全移动到目标位置（支持跨文件系统/SMB共享盘，规避 macOS fcopyfile ENXIO 缺陷）
            safe_move_file(temp_output_file, final_output_file)

            action_desc = "修复与压缩" if need_compress else "极速无损换封装"
            size_label = "压缩后大小" if need_compress else "换封装后大小"
            print(f"[完成] {action_desc}成功! 耗时: {duration_sec:.1f}s")
            print(f"  * 原始大小: {format_bytes(orig_size)} -> {size_label}: {format_bytes(new_size)} ({size_change:+.1f}%)")
            print(f"  * 输出路径: {final_output_file.resolve()}")
            if vlog:
                vlog.log_processed(
                    input_file=file_path,
                    output_target=final_output_file,
                    status="SUCCESS",
                    action="COMPRESS" if need_compress else "REPACKAGE",
                    orig_size=orig_size,
                    new_size=new_size,
                    elapsed_sec=duration_sec,
                    orig_info=info,
                    details=reason,
                    tool="vfix"
                )
            if delete_source:
                remove_source_files([file_path], final_output_file, reason="转码压缩成功后安全清理源文件 (--delete-source)")
            return True
        else:
            print(f"[失败] 残缺数据破坏过于严重，未能成功生成: {file_path.resolve()}", file=sys.stderr)
            if last_err_msg:
                snippet = "\n".join(last_err_msg.splitlines()[-4:])
                print(f"  [底层错误] {snippet}", file=sys.stderr)
            if temp_output_file.exists():
                temp_output_file.unlink(missing_ok=True)
            if vlog:
                vlog.log_processed(
                    input_file=file_path,
                    output_target=None,
                    status="FAILED",
                    action="COMPRESS" if need_compress else "REPACKAGE",
                    orig_size=orig_size,
                    elapsed_sec=duration_sec,
                    orig_info=info,
                    details=last_err_msg or "未知转码错误",
                    tool="vfix"
                )
            return False
    finally:
        if temp_output_file.exists():
            temp_output_file.unlink(missing_ok=True)
        if salvaged_audio_file and salvaged_audio_file.exists():
            salvaged_audio_file.unlink(missing_ok=True)
        if salvaged_src_file and salvaged_src_file.exists():
            salvaged_src_file.unlink(missing_ok=True)



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
    delete_source: bool = False,
    crop: Optional[str] = None,
    aspect: Optional[str] = None
) -> bool:
    """多分卷视频智能对齐合并与硬件/软件容错编码压缩 (支持极速无损流复制合并与 Filter Concat Pipeline)。"""
    is_merge_only = merge_only or force_repair_only
    if is_merge_only and crop:
        print("  [提示] -M / --merge-only 为无损流复制合并，无法直接裁切像素；如需去黑边请使用普通合并模式 (-m --crop)。")
    file_paths = sorted(file_paths, key=natural_sort_key)
    n_files = len(file_paths)
    print("=" * 70)
    if is_merge_only:
        print(f"[单独合并] 共有 {n_files} 个分卷视频待拼接 (已按自然顺序预排, 仅合并无重编码):")
    else:
        print(f"[合并并压缩] 共有 {n_files} 个分卷视频待拼接 (已按自然顺序预排):")
    for idx, f in enumerate(file_paths, 1):
        print(f"  [{idx}/{n_files}] {f.resolve()}")

    input_exts = {p.suffix.lower() for p in file_paths}
    tentative_ext = ".mp4"
    if is_merge_only and len(input_exts) == 1 and list(input_exts)[0] in (".wmv", ".asf", ".mkv"):
        tentative_ext = list(input_exts)[0]

    preview_output_file = derive_merged_output_path(
        file_paths, output_target, is_compressed=not is_merge_only, default_ext=tentative_ext
    )
    print(f"  * 拟定输出路径: {preview_output_file.resolve()}")

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
                            print(f"  [{idx}/{n_files}] {f.resolve()}")
                        print(f"  * 拟定输出路径: {preview_output_file.resolve()}")
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
    expected_bps = get_expected_bitrate(synth_info, crf, encoder)

    # 计算原视频分卷的平均视频净码率
    valid_v_bitrates = [
        p.get("video_bitrate", p.get("bitrate", 0.0))
        for p in probes if p.get("video_bitrate", p.get("bitrate", 0.0)) > 0
    ]
    if valid_v_bitrates:
        avg_v_bps = sum(valid_v_bitrates) / len(valid_v_bitrates)
    else:
        avg_v_bps = 0.0

    codecs_set = {p.get("codec", "").lower() for p in probes if p}
    has_legacy = any(c in LEGACY_INEFFICIENT_CODECS for c in codecs_set)

    if is_merge_only:
        target_bps = int(avg_v_bps * 1.05) if avg_v_bps > 0 else expected_bps
        merge_crf = min(crf, 19)
    else:
        if avg_v_bps > 0 and not has_legacy:
            target_bps = min(expected_bps, int(avg_v_bps * 0.85))
            if expected_bps > avg_v_bps:
                print(f"  * 码率保护策略: 基准推算码率 ({format_bitrate(expected_bps)}) 高于分卷平均视频码率 ({format_bitrate(avg_v_bps)})，"
                      f"自动降级封顶至 ~{format_bitrate(target_bps)} (严禁高于源分卷，杜绝反向膨胀)")
        else:
            target_bps = expected_bps
        merge_crf = crf

    is_hardware = "videotoolbox" in encoder or "rkmpp" in encoder
    is_arm_arch = platform.machine().lower() in ("arm64", "aarch64")
    if is_hardware and not is_arm_arch and (max_w < 640 or max_h < 360):
        fallback_cpu = "libx265" if "hevc" in encoder else "libx264"
        if not is_merge_only:
            print(f"  * 提示: 目标分辨率 ({max_w}x{max_h}) 偏小且处于 x86_64 架构，自动选用 CPU 软件极速编码 ({fallback_cpu})")
        encoder = fallback_cpu
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

    # 预先生成视频编码参数（全面继承 B 帧、Profile、Spatial-AQ 与色彩保真标签）
    video_encode_args = build_video_encode_args(
        encoder=encoder,
        target_bps=target_bps,
        orig_v_bitrate=avg_v_bps,
        crf=merge_crf,
        preset=preset,
        gop_size=merge_gop,
        keyint_min=merge_keyint_min,
        width=max_w,
        height=max_h,
        is_merge=True
    )
    sw_video_args = build_video_encode_args(
        encoder="libx264",
        target_bps=target_bps,
        orig_v_bitrate=avg_v_bps,
        crf=merge_crf,
        preset=preset,
        gop_size=merge_gop,
        keyint_min=merge_keyint_min,
        width=max_w,
        height=max_h,
        is_merge=True
    )

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
            # 策略 1A: 优先尝试极速无损流复制 (-c:v copy)
            # 适用场景:
            #   1) 单独合并模式 (is_merge_only)
            #   2) 普通合并模式 (-m) 且分卷视频平均码率已处于或低于预期区间，免重复二次压缩
            # ----------------------------------------------------
            should_try_stream_copy = is_merge_only or (
                not force_compress and not crop and avg_v_bps > 0 and avg_v_bps <= expected_bps
            )
            if should_try_stream_copy and (all_mp4_compliant or final_output_file.suffix.lower() != ".mp4"):
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

                action_intro = "[单独合并]" if is_merge_only else "[分卷规整合并]"
                print(f"  * 策略决策: {action_intro} -> 启动极速无损流复制合并 (-c:v copy，跳过视频重编码)...")
                t_start = time.time()
                success_fast, fast_err = execute_ffmpeg(cmd_fast)

                if success_fast and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
                    duration_sec = time.time() - t_start
                    new_size = temp_output_file.stat().st_size
                    size_change = (new_size - total_orig_size) / total_orig_size * 100 if total_orig_size > 0 else 0

                    safe_move_file(temp_output_file, final_output_file)

                    done_label = "单独合并成功" if is_merge_only else "合并成功 (分卷码率已在合理区间，免二次压缩)"
                    print(f"[完成] {done_label} (极速无损流复制)! 耗时: {duration_sec:.1f}s")
                    print(f"  * 原始总体积: {format_bytes(total_orig_size)} -> 合并后大小: {format_bytes(new_size)} ({size_change:+.1f}%)")
                    print(f"  * 输出路径: {final_output_file}")
                    if vlog:
                        vlog.log_processed(
                            input_file=file_paths,
                            output_target=final_output_file,
                            status="SUCCESS",
                            action="MERGE_ONLY" if is_merge_only else "MERGE_STREAM_COPY",
                            orig_size=total_orig_size,
                            new_size=new_size,
                            elapsed_sec=duration_sec,
                            orig_info={"width": max_w, "height": max_h, "fps": target_fps, "bitrate": avg_v_bps},
                            details=f"极速无损流复制合并 ({n_files} 个分卷)",
                            tool="vfix"
                        )
                    if delete_source:
                        remove_source_files(file_paths, final_output_file, reason="分卷合并成功后安全清理源文件 (--delete-source)")
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
                    hw_name = "Rockchip RKMPP" if "rkmpp" in encoder else "Apple VideoToolbox"
                    print(f"  * 编码配置: {hw_name} 硬件极速加速 ({encoder})")
                else:
                    print(f"  * 编码配置: CPU 软件编码 ({encoder}, CRF: {crf}, Preset: {preset})")
            else:
                print(f"  * 规整拼接基准: {max_w}x{max_h} @ {target_fps}fps | 高保真保留画质 (Concat Demuxer)")
            print(f"  * 目标输出路径: {final_output_file}")
            if has_any_audio and audio_bitrates and target_audio_kbps < 128:
                orig_a_kbps = max(32, int((sum(audio_bitrates) / len(audio_bitrates)) / 1000))
                print(f"  * 音频码率策略: 分卷平均音频码率 ~{orig_a_kbps} kbps，合并音频转码限制为 {target_audio_kbps} kbps (不高于原始码率)")

            merge_crop_filter = ""
            if crop and detect_crop_box:
                m_crop_info = None
                if crop != "auto" and ":" in str(crop):
                    parts = str(crop).strip().split(":")
                    if len(parts) == 4:
                        m_crop_info = {"crop_str": f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[3]}"}
                else:
                    m_crop_info = detect_crop_box(file_paths[0], max_w, max_h, probes[0].get("duration", 0.0), target_aspect=aspect)
                if m_crop_info and m_crop_info.get("has_black_bars", True):
                    merge_crop_filter = f"crop={m_crop_info['crop_str']},"

            if same_res:
                # 各分卷分辨率完全一致，跳过昂贵的 CPU 重缩放与填充滤镜，大幅提升吞吐效率
                vf_filter = f"{merge_crop_filter}fps={target_fps},setpts=PTS-STARTPTS"
            else:
                vf_filter = (
                    f"{merge_crop_filter}scale={max_w}:{max_h}:force_original_aspect_ratio=decrease,"
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

                safe_move_file(temp_output_file, final_output_file)

                action_label = "单独合并与规整" if is_merge_only else "多分卷合并与压缩"
                print(f"[完成] {action_label}成功! 耗时: {duration_sec:.1f}s")
                print(f"  * 原始总体积: {format_bytes(total_orig_size)} -> 合并后大小: {format_bytes(new_size)} ({size_change:+.1f}%)")
                print(f"  * 输出路径: {final_output_file}")
                if vlog:
                    vlog.log_processed(
                        input_file=file_paths,
                        output_target=final_output_file,
                        status="SUCCESS",
                        action="MERGE_STANDARDIZE" if is_merge_only else "MERGE_COMPRESS",
                        orig_size=total_orig_size,
                        new_size=new_size,
                        elapsed_sec=duration_sec,
                        orig_info={"width": max_w, "height": max_h, "fps": target_fps, "bitrate": avg_v_bps},
                        details=f"Concat Demuxer 流式转码合并 ({n_files} 个分卷)",
                        tool="vfix"
                    )
                if delete_source:
                    remove_source_files(file_paths, final_output_file, reason="分卷合并成功后安全清理源文件 (--delete-source)")
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
            hw_name = "Rockchip RKMPP" if "rkmpp" in encoder else "Apple VideoToolbox"
            print(f"  * 编码配置: {hw_name} 硬件极速加速 ({encoder})")
        else:
            print(f"  * 编码配置: CPU 软件编码 ({encoder}, CRF: {crf}, Preset: {preset})")
    else:
        print(f"  * 规整拼接基准: {max_w}x{max_h} @ {target_fps}fps | 高保真保留画质 (多源滤镜)")
    print(f"  * 目标输出路径: {final_output_file}")

    # 构建 FFmpeg 输入与复杂滤镜
    input_args = []
    filter_chains = []
    input_counter = 0

    if 'merge_crop_filter' not in locals():
        merge_crop_filter = ""
        if crop and detect_crop_box:
            m_crop_info = None
            if crop != "auto" and ":" in str(crop):
                parts = str(crop).strip().split(":")
                if len(parts) == 4:
                    m_crop_info = {"crop_str": f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[3]}"}
            else:
                m_crop_info = detect_crop_box(file_paths[0], max_w, max_h, probes[0].get("duration", 0.0), target_aspect=aspect)
            if m_crop_info and m_crop_info.get("has_black_bars", True):
                merge_crop_filter = f"crop={m_crop_info['crop_str']},"

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
            f"[{v_in}:v:0]{merge_crop_filter}scale={max_w}:{max_h}:force_original_aspect_ratio=decrease,"
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

            safe_move_file(temp_output_file, final_output_file)

            action_label = "单独合并与规整" if is_merge_only else "多分卷合并与压缩"
            print(f"[完成] {action_label}成功! 耗时: {duration_sec:.1f}s")
            print(f"  * 原始总体积: {format_bytes(total_orig_size)} -> 合并后大小: {format_bytes(new_size)} ({size_change:+.1f}%)")
            print(f"  * 输出路径: {final_output_file}")
            if vlog:
                vlog.log_processed(
                    input_file=file_paths,
                    output_target=final_output_file,
                    status="SUCCESS",
                    action="MERGE_STANDARDIZE" if is_merge_only else "MERGE_COMPRESS",
                    orig_size=total_orig_size,
                    new_size=new_size,
                    elapsed_sec=duration_sec,
                    orig_info={"width": max_w, "height": max_h, "fps": target_fps, "bitrate": avg_v_bps},
                    details=f"多源滤镜对齐拼接合并 ({n_files} 个分卷)",
                    tool="vfix"
                )
            if delete_source:
                remove_source_files(file_paths, final_output_file, reason="分卷合并成功后安全清理源文件 (--delete-source)")
            return True
        else:
            action_label = "单独合并" if is_merge_only else "多分卷合并压缩"
            print(f"[失败] {action_label}失败。", file=sys.stderr)
            if last_err_msg:
                snippet = "\n".join(last_err_msg.splitlines()[-4:])
                print(f"  [底层错误] {snippet}", file=sys.stderr)
            if vlog:
                vlog.log_processed(
                    input_file=file_paths,
                    output_target=None,
                    status="FAILED",
                    action="MERGE_FAILED",
                    orig_size=total_orig_size,
                    elapsed_sec=duration_sec,
                    orig_info={"width": max_w, "height": max_h, "fps": target_fps, "bitrate": avg_v_bps},
                    details=f"合并失败 ({n_files} 个分卷): {last_err_msg or '未知错误'}",
                    tool="vfix"
                )
            if temp_output_file.exists():
                temp_output_file.unlink(missing_ok=True)
            return False
    finally:
        if temp_output_file.exists():
            temp_output_file.unlink(missing_ok=True)
        if concat_list_file and concat_list_file.exists():
            concat_list_file.unlink(missing_ok=True)
        for sf in salvaged_audio_files:
            if sf and sf.exists():
                sf.unlink(missing_ok=True)


def normalize_cli_args(raw_args: List[str]) -> List[str]:
    """
    智能规范化与容错命令行参数：
    1. 彻底解决 -c / --crop 与位置参数 (inputs) 之间的贪婪吞噬冲突；
    2. 兼容用户常见的简写/误拼输入：-corp, -crop, -rc, -cr 等；
    3. 支持 -c 作为无参布尔开关，同时支持 -c 1920:800:0:140 或 --crop 1920:800:0:140 指定具体裁剪坐标。
    """
    norm_args = []
    i = 0
    crop_geom_pattern = re.compile(r"^\d+:\d+(:\d+:\d+)?$")

    while i < len(raw_args):
        arg = raw_args[i]

        # 容错：用户误输入了 -corp (可能是 -crop 的误拼，或 -c 与 -r 的拼写)
        if arg == "-corp":
            norm_args.append("-c")
            i += 1
            continue

        # 容错：单横杠 -crop
        if arg == "-crop":
            arg = "--crop"

        if arg in ("--crop", "--autocrop"):
            # 检查下一个参数是否是具体裁剪坐标 (如 1920:800:0:140 或 auto)
            if i + 1 < len(raw_args):
                next_val = raw_args[i + 1]
                if crop_geom_pattern.match(next_val) or next_val.lower() == "auto":
                    norm_args.extend(["--crop-size", next_val])
                    i += 2
                    continue
            norm_args.append("-c")
            i += 1
            continue

        if arg.startswith("--crop="):
            crop_val = arg.split("=", 1)[1]
            norm_args.extend(["--crop-size", crop_val])
            i += 1
            continue

        if arg == "-c":
            # 检查 -c 后面紧跟的是否是具体尺寸参数
            if i + 1 < len(raw_args):
                next_val = raw_args[i + 1]
                if crop_geom_pattern.match(next_val) or next_val.lower() == "auto":
                    norm_args.extend(["--crop-size", next_val])
                    i += 2
                    continue
            norm_args.append("-c")
            i += 1
            continue

        # 容错：后台运行与任务守护相关参数
        if arg in ("-background", "-daemon"):
            norm_args.append("--background")
            i += 1
            continue
        if arg == "-status":
            norm_args.append("--status")
            i += 1
            continue
        if arg == "-stop":
            norm_args.append("--stop")
            i += 1
            continue

        norm_args.append(arg)
        i += 1

    return norm_args


def expand_glob_pattern(pattern_str: str, recursive: bool) -> List[str]:
    """
    智能展开通配符并支持跨平台大小写不敏感与递归深层搜索：
    1. 当 recursive=True 时，自动将 *.wmv 或 path/*.wmv 转换为 **/*.[wW][mM][vV]，确保 Python 的 glob 能够穿透所有子目录；
    2. 解决 Linux 下大小写敏感导致的 *.wmv 无法匹配 *.WMV 的问题；
    3. 支持 macOS / Linux SMB 挂载盘上的 NFD / NFC Unicode 自动对齐匹配。
    """
    expanded = os.path.expanduser(pattern_str)
    candidate_patterns = [expanded]

    # 1. 若开启递归遍历且未显式指定 **，自动注入 ** 以穿透子目录
    rec_pattern = expanded
    if recursive and "**" not in expanded:
        dir_part, base_part = os.path.split(expanded)
        if dir_part:
            rec_pattern = os.path.join(dir_part, "**", base_part)
        else:
            rec_pattern = os.path.join("**", base_part)
        if rec_pattern not in candidate_patterns:
            candidate_patterns.insert(0, rec_pattern)

    # 2. 为模式中的文件名部分生成大小写不敏感变体 (如 *.wmv -> *.[wW][mM][vV])
    ci_candidates = []
    for pat in candidate_patterns:
        dir_part, base_part = os.path.split(pat)
        res = []
        in_bracket = False
        has_alpha = False
        for char in base_part:
            if char == '[':
                in_bracket = True
                res.append(char)
            elif char == ']':
                in_bracket = False
                res.append(char)
            elif not in_bracket and char.isalpha():
                has_alpha = True
                res.append(f"[{char.lower()}{char.upper()}]")
            else:
                res.append(char)
        if has_alpha:
            ci_base = "".join(res)
            ci_pat = os.path.join(dir_part, ci_base) if dir_part else ci_base
            ci_candidates.append(ci_pat)

    all_patterns = ci_candidates + candidate_patterns

    matched = []
    for p in all_patterns:
        m = glob.glob(p, recursive=recursive)
        if not m:
            nfd = unicodedata.normalize("NFD", p)
            if nfd != p:
                m = glob.glob(nfd, recursive=recursive)
        if not m:
            nfc = unicodedata.normalize("NFC", p)
            if nfc != p:
                m = glob.glob(nfc, recursive=recursive)
        if m:
            matched.extend(m)
            break

    return matched


def collect_video_files(input_patterns: List[str], recursive: bool, is_merge: bool = False) -> List[Path]:
    """收集输入路径列表或通配符匹配下的所有视频及 .part 残缺文件。"""
    collected = []
    seen = set()

    # 预先解析单文件判断，避免 SMB 挂载盘上的 NFC/NFD 不匹配导致误判
    first_p = normalize_and_resolve_path(input_patterns[0]) if len(input_patterns) == 1 else None
    is_single_explicit_file = first_p.is_file() if first_p else False

    def should_skip(path: Path) -> bool:
        name = path.name
        # 严格过滤 macOS / SMB 共享盘上的 AppleDouble 隐藏资源文件 (如 ._video.mp4) 与普通隐藏文件
        if name.startswith("._") or name.startswith("."):
            return True
        stem = path.stem
        if is_single_explicit_file:
            return False
        if is_merge:
            # 合并模式下允许合并已压缩(_compressed)或已修复(_repaired)的分卷，仅自动排除合并结果(_merged)
            return stem.endswith("_merged")
        return stem.endswith("_repaired") or stem.endswith("_compressed") or stem.endswith("_merged")

    for pattern_str in input_patterns:
        p = normalize_and_resolve_path(pattern_str)
        if not p.exists():
            for ext in [".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".wmv"]:
                cand = normalize_and_resolve_path(f"{pattern_str}{ext}")
                if cand.is_file():
                    p = cand
                    break
        if p.exists():
            if p.is_file():
                if not should_skip(p):
                    if is_supported_video_file(p) or probe_video(p) is not None:
                        res = p.resolve()
                        if res not in seen:
                            seen.add(res)
                            collected.append(res)
            elif p.is_dir():
                # 友好检查与提示：未开启 -r 时提醒用户子目录情况
                if not recursive:
                    try:
                        has_subdirs = any(item.is_dir() and not item.name.startswith(".") for item in p.iterdir())
                        if has_subdirs:
                            print(f"[提示] 检测到目录 '{p.name}' 包含子文件夹。当前未开启 -r，仅扫描本级目录；若需遍历子目录请加上 -r 参数。")
                    except Exception:
                        pass
                else:
                    print(f"正在扫描目录: {p} (递归遍历已开启)...")

                iterator = p.rglob("*") if recursive else p.glob("*")
                for f in iterator:
                    if f.is_file() and not should_skip(f) and is_supported_video_file(f):
                        res = f.resolve()
                        if res not in seen:
                            seen.add(res)
                            collected.append(res)
            continue

        matched = expand_glob_pattern(pattern_str, recursive=recursive)

        for m in matched:
            mp = normalize_and_resolve_path(m).resolve()
            if mp.is_file() and not should_skip(mp) and is_supported_video_file(mp):
                if mp not in seen:
                    seen.add(mp)
                    collected.append(mp)
            elif mp.is_dir():
                if recursive:
                    print(f"正在扫描目录: {mp} (递归遍历已开启)...")
                iterator = mp.rglob("*") if recursive else mp.glob("*")
                for f in iterator:
                    if f.is_file() and not should_skip(f) and is_supported_video_file(f):
                        res = f.resolve()
                        if res not in seen:
                            seen.add(res)
                            collected.append(res)

    return sorted(collected, key=natural_sort_key)


def main():
    has_vt_h264 = has_videotoolbox_support()
    has_vt_hevc = has_videotoolbox_hevc_support()
    has_rk_h264 = has_rkmpp_support()
    has_rk_hevc = has_rkmpp_hevc_support()

    # 默认编码器选择：在支持硬件加速的平台上默认采用最高能效硬件极速压制
    if has_vt_hevc:
        default_encoder = "hevc_videotoolbox"
    elif has_vt_h264:
        default_encoder = "h264_videotoolbox"
    elif has_rk_hevc:
        default_encoder = "hevc_rkmpp"
    elif has_rk_h264:
        default_encoder = "h264_rkmpp"
    else:
        default_encoder = "libx264"

    prog_name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "vfix"
    if prog_name not in ("vfix", "vrc"):
        prog_name = "vfix"

    parser = argparse.ArgumentParser(
        prog=prog_name,
        description="""
================================================================================
  vfix - 视频智能修复、高效压缩、多分卷合并与自动去黑边工具
================================================================================
【核心特性】
  1. 深度抢救修复：专治损坏/未下完视频（如 .part、.tmp、高坏帧率文件，突破 FFmpeg 限制）
  2. 极速硬件加速：全自动匹配最佳引擎（Apple Silicon GPU / Linux 多核，体积立减 30%~70%）
  3. 多分卷秒级合并：支持 -M 无损极速流复制合并（0 重编码、0 损画质，如 CD1.avi CD2.avi）
  4. 智能自动去黑边：自动识别画幅黑边，强力过滤弹幕/字幕跑马灯与边角广告干扰
  5. 码率智能防膨胀：若原片码率已在合理区间，自动转为极速封装，严防转码后体积变大
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
--------------------------------------------------------------------------------
【常用高频命令速查】
--------------------------------------------------------------------------------
1. 修复并压缩单个或批量视频（成功后自动删除源文件以释放空间）:
   vfix *.part -d
   vfix broken_video.mp4 -o /mnt/nas/output/

2. 多分卷极速无损合并（强烈推荐，秒级完成、零画质损失）:
   vfix CD1.avi CD2.avi -M
   vfix Movie_Part1.mp4 Movie_Part2.mp4 -M -d   # 合并完成后自动清理分卷

3. 视频智能去黑边 / 竖屏短剧锁定:
   vfix movie.mp4 -c                            # 自动剔除上下/左右黑边与跑马灯水印
   vfix drama.mp4 -a 9:16                       # 锁定标准 9:16 竖屏短剧画幅居中裁剪

4. 批量递归处理整个目录或 NAS 共享盘:
   vfix /mnt/nas/videos/ -r -o /mnt/nas/compressed/

5. 老旧播放器兼容模式（强制编码为 H.264）:
   vfix input.mkv --h264

6. 仅修复容器封装损坏（不改变画质、不耗费重编码时间）:
   vfix broken_index.mp4 --force-repair-only

7. 后台脱机批量运行与状态管理 (多任务/长耗时必备，关闭终端/断开SSH不中断):
   vfix *.part -d -B                            # 后台脱机运行，终端即刻释放
   vfix /mnt/nas/videos/ -r -d -B               # 后台脱机递归处理整个目录并安全清理源文件
   vfix --status                                # 实时查看后台任务运行状态、当前进度与最新日志
   vfix --stop                                  # 安全终止后台任务 (严格保护源文件，清理临时缓存)
================================================================================
"""
    )
    # 输入与输出
    parser.add_argument("inputs", nargs="*", type=str, default=[],
                        help="输入文件、文件夹或通配符 (例如: video.mp4, *.part, CD1.avi CD2.avi, /path/to/dir/)")
    parser.add_argument("-o", "--output-dir", type=str, default=None,
                        help="指定输出目录或输出文件名 (例如: -o /mnt/nas/ 或 -o full_movie.mp4)")
    parser.add_argument("-d", "--delete-source", "--del-src", "--remove-source", dest="delete_source", action="store_true",
                        help="处理或合并完全成功后自动删除源文件 (谨慎使用；若失败或手动中断则严格保护绝不删除)")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="当输入为目录时，递归扫描并处理其下所有子目录中的视频")

    # 后台运行与任务守护
    daemon_group = parser.add_argument_group("后台运行与任务守护 (脱机运行)")
    daemon_group.add_argument("-B", "--background", "--daemon", action="store_true",
                              help="将任务转入后台脱机运行 (即刻释放终端，终端关闭或网络中断不影响执行)")
    daemon_group.add_argument("--status", action="store_true",
                              help="查看当前后台任务运行状态、PID 及最新实时日志")
    daemon_group.add_argument("--stop", action="store_true",
                              help="安全终止当前正在运行的后台任务 (确保保护源文件并清理临时缓存)")

    # 分卷合并选项
    parser.add_argument("-M", "--merge-only", "--concat-only", dest="merge_only", action="store_true",
                        help="【合并推荐】单独合并多个分卷为一个完整视频 (极速无损流复制，0重编码、秒级完成)")
    parser.add_argument("-m", "--merge", action="store_true",
                        help="将输入的多个分卷合并为一个完整视频并重新压制瘦身 (支持规格自适应对齐)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="合并前跳过交互式分卷顺序确认提示 (直接自动开始处理)")

    # 画幅与黑边裁剪
    parser.add_argument("-c", "--autocrop", dest="autocrop", action="store_true",
                        help="智能去除视频黑边 (默认 auto 自动检测并剔除字幕广告与黑边)")
    parser.add_argument("--crop", "--crop-size", dest="crop_size", type=str, default=None, metavar="W:H:X:Y",
                        help="手动指定固定裁剪区域 (格式如 1920:800:0:140；若不指定则默认 auto 全自动检测)")
    parser.add_argument("-a", "--aspect", "--ratio", dest="aspect", type=str, default=None,
                        help="锁定目标画幅比例居中裁剪 (如 9:16, 2:3, 16:9, 2.39:1，短剧必备)")
    parser.add_argument("-s", "--split-scenes", "--split", dest="split_scenes", action="store_true",
                        help="配合 -c 使用：智能场景切片模式 (按横竖屏切换时间点自动分段导出)")
    parser.add_argument("-D", "--drop-ads", "--skip-ads", dest="drop_ads", action="store_true",
                        help="配合 -s 使用：自动检测并剔除短剧广告/插片分段 (仅保留并导出正片)")
    parser.add_argument("--max-ad-duration", type=float, default=120.0,
                        help="判定为广告/插片的最大时长阈值/秒 (默认 120.0 秒)")
    parser.add_argument("--min-segment-duration", type=float, default=12.0,
                        help="切片最小片段时长/秒 (默认 12.0 秒，短镜头自动并入相邻主画幅)")

    # 编码与画质调优
    parser.add_argument("--crf", type=int, default=22,
                        help="恒定画质系数 CRF (默认 22；数值越小画质越高体积越大，通常 18~24 为黄金区间)")
    parser.add_argument("--preset", type=str, default="veryfast",
                        choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"],
                        help="CPU 软件编码速度预设 (默认 veryfast，显著降低耗时)")
    parser.add_argument("--threshold", type=float, default=0.20,
                        help="预估体积缩减阈值 (默认 0.20 即 20%%；若重编码收益低于此比例将自动跳过以保护画质)")
    parser.add_argument("--encoder", type=str, default=default_encoder,
                        choices=["hevc_videotoolbox", "h264_videotoolbox", "hevc_rkmpp", "h264_rkmpp", "libx264", "libx265"],
                        help=f"视频编码器 (当前系统默认最优: {default_encoder})")
    parser.add_argument("--h264", action="store_true",
                        help="指定输出为 H.264 编码 (保证极其老旧的电视或老旧播放器设备兼容性)")
    parser.add_argument("--cpu", action="store_true",
                        help="强制使用 CPU 纯软件编码 (跳过 GPU 硬件加速)")
    parser.add_argument("--force-compress", action="store_true",
                        help="强制重新压缩 (无视原片码率是否已很小，强制重压)")
    parser.add_argument("--force-repair-only", action="store_true",
                        help="强制仅修复损坏封装结构与音视频同步 (不重新编码，0 耗时)")

    # 运行审计与日志记录 (默认 100% 自动记录至 ~/.vfix/logs/)
    log_group = parser.add_argument_group("运行审计与日志记录 (默认全自动生成)")
    log_group.add_argument("--log-dir", type=str, default=None,
                           help="自定义日志保存目录 (默认全自动存储至: ~/.vfix/logs/)")
    log_group.add_argument("--no-log", action="store_true",
                           help="禁用所有文件处理与删除日志记录 (默认已开启自动记录)")

    norm_argv = normalize_cli_args(sys.argv[1:])
    args = parser.parse_args(norm_argv)

    global DAEMON_MGR
    vlog.setup_logger(log_dir=args.log_dir, enabled=not args.no_log, tool_name="vfix")
    daemon_mgr = DaemonManager(tool_name="vfix", log_file=vlog.get_main_log_path())
    DAEMON_MGR = daemon_mgr

    # 优先响应后台任务管理指令 (--status / --stop)
    if args.status:
        daemon_mgr.show_status()
        sys.exit(0)

    if args.stop:
        daemon_mgr.stop_daemon()
        sys.exit(0)

    if not args.inputs:
        parser.print_help()
        print("\n[错误] 请提供待处理的视频文件、目录或通配符。")
        print("  常用示例:")
        print("    vfix video.mp4                                 # 自动修复并压缩单个视频")
        print("    vfix *.mp4 -B                                  # 后台脱机批量处理当前目录所有视频")
        print("    vfix --status                                  # 查看后台任务运行状态与实时日志")
        print("    vfix --stop                                    # 安全停止当前后台任务")
        sys.exit(1)

    setup_signal_handlers()

    if not check_ffmpeg():
        sys.exit(1)

    crop_setting = None
    if args.crop_size:
        crop_setting = args.crop_size
    elif args.autocrop or args.aspect or args.split_scenes:
        crop_setting = "auto"

    if args.cpu:
        encoder = "libx264" if args.h264 else "libx265"
    elif args.h264:
        if has_vt_h264:
            encoder = "h264_videotoolbox"
        elif has_rk_h264:
            encoder = "h264_rkmpp"
        else:
            encoder = "libx264"
    else:
        encoder = args.encoder
    output_target = normalize_and_resolve_path(args.output_dir).resolve() if args.output_dir else None

    is_merge = args.merge or args.merge_only
    is_merge_only = args.merge_only or (args.merge and args.force_repair_only)

    def get_engine_desc(enc: str) -> str:
        return {
            "hevc_videotoolbox": "Apple VideoToolbox HEVC/H.265 GPU 硬件极速加速 (体积减半、高画质、低发热)",
            "h264_videotoolbox": "Apple VideoToolbox H.264 GPU 硬件加速 (极速、高兼容、低发热)",
            "hevc_rkmpp": "Rockchip RKMPP HEVC/H.265 硬件极速加速 (VPU硬压、体积小、超高速)",
            "h264_rkmpp": "Rockchip RKMPP H.264 硬件极速加速 (VPU硬压、高兼容、超高速)",
            "libx265": f"CPU 软件高效编码 libx265 (CRF: {args.crf}, Preset: {args.preset})",
            "libx264": f"CPU 软件标准编码 libx264 (CRF: {args.crf}, Preset: {args.preset})"
        }.get(enc, f"{enc} 编码引擎")

    video_files = collect_video_files(args.inputs, args.recursive, is_merge=is_merge)
    if not video_files:
        print(f"[提示] 未找到符合条件的受支持视频文件: {args.inputs}")
        sys.exit(0)

    # 若指定了后台运行模式，在此处分叉并退出父进程
    if args.background:
        daemon_mgr.daemonize(file_count=len(video_files), raw_argv=sys.argv[1:])

    session_start_time = time.time()
    vlog.log_session_start(sys.argv[1:], len(video_files), tool="vfix")

    if is_merge:
        if len(video_files) == 1:
            print(f"[提示] 仅检测到 1 个视频文件 ({video_files[0].resolve()})，无需合并，自动转为单文件修复{'与压缩' if not is_merge_only else ''}流程。")
            ok = process_video(
                file_path=video_files[0],
                output_dir=output_target,
                threshold=args.threshold,
                crf=args.crf,
                preset=args.preset,
                encoder=encoder,
                force_compress=args.force_compress,
                force_repair_only=is_merge_only or args.force_repair_only,
                delete_source=args.delete_source,
                crop=crop_setting,
                aspect=args.aspect,
                split_scenes=args.split_scenes,
                min_segment_duration=args.min_segment_duration,
                drop_ads=args.drop_ads,
                max_ad_duration=args.max_ad_duration,
                current_idx=1,
                total_files=1
            )
            total_cost = time.time() - session_start_time
            if vlog:
                vlog.log_session_end(1 if ok else 0, 0 if ok else 1, total_cost, tool="vfix")
            if not args.no_log:
                print(f"  * 运行与删除日志: {vlog.get_log_dir()}/ (processed.log / deleted.log / vfix.log)")
            sys.exit(0 if ok else 1)
        else:
            if not is_merge_only:
                print(f"加速引擎: [{get_engine_desc(encoder)}]")

            ok = process_merged_videos(
                file_paths=video_files,
                output_target=output_target,
                crf=args.crf,
                preset=args.preset,
                encoder=encoder,
                force_compress=args.force_compress,
                force_repair_only=args.force_repair_only,
                merge_only=args.merge_only,
                skip_confirm=args.yes or args.background,
                delete_source=args.delete_source,
                crop=crop_setting,
                aspect=args.aspect
            )
            total_cost = time.time() - session_start_time
            if vlog:
                vlog.log_session_end(1 if ok else 0, 0 if ok else 1, total_cost, tool="vfix")
            print("\n" + "=" * 70)
            action_desc = "单独合并" if is_merge_only else "合并并压缩"
            print(f"处理完成汇总: {action_desc} {len(video_files)} 个分卷，状态: {'成功' if ok else '失败'}。")
            if not args.no_log:
                print(f"  * 运行与删除日志: {vlog.get_log_dir()}/ (processed.log / deleted.log / vfix.log)")
            sys.exit(0 if ok else 1)

    print(f"找到 {len(video_files)} 个待处理视频文件。")
    if not args.no_log:
        print(f"  * 运行与删除日志: {vlog.get_log_dir()}/ (processed.log / deleted.log / vfix.log) [自动记录]")
    print(f"加速引擎: [{get_engine_desc(encoder)}]")

    success_count = 0
    fail_count = 0

    for idx, v_file in enumerate(video_files, 1):
        print(f"\n[{idx}/{len(video_files)}] 文件: {v_file.resolve()}")
        ok = process_video(
            file_path=v_file,
            output_dir=output_target,
            threshold=args.threshold,
            crf=args.crf,
            preset=args.preset,
            encoder=encoder,
            force_compress=args.force_compress,
            force_repair_only=args.force_repair_only,
            delete_source=args.delete_source,
            crop=crop_setting,
            aspect=args.aspect,
            split_scenes=args.split_scenes,
            min_segment_duration=args.min_segment_duration,
            drop_ads=args.drop_ads,
            max_ad_duration=args.max_ad_duration,
            current_idx=idx,
            total_files=len(video_files)
        )
        if ok:
            success_count += 1
        else:
            fail_count += 1

    total_cost = time.time() - session_start_time
    if vlog:
        vlog.log_session_end(success_count, fail_count, total_cost, tool="vfix")
    if DAEMON_MGR:
        DAEMON_MGR.update_status(
            stage="任务全部完成",
            progress_str=f"共完成 {success_count}/{len(video_files)} 个视频"
        )

    print("\n" + "=" * 70)
    print(f"处理完成汇总: 共 {len(video_files)} 个，成功: {success_count} 个，失败: {fail_count} 个。")
    if not args.no_log:
        print(f"  * 运行与删除日志: {vlog.get_log_dir()}/ (processed.log / deleted.log / vfix.log)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[中断] 用户已中止操作。")
        sys.exit(130)

