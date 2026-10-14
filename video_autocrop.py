#!/usr/bin/env python3
"""
视频智能自动去黑边工具 (video_autocrop.py / vcrop)

核心特性：
1. 智能多采样点黑边分析 (Smart Multi-Point Crop Detection)：
   - 绝非单一帧或简单首帧探测；在视频主体时段内均匀多点抽样，避开片头片尾黑屏与暗场镜头干扰。
   - 汇总统计所有样本帧数据，自动剔除极端暗场误检，通过频次统计 (Mode) 锁定最精准的黑边尺寸。
   - 自动校验奇偶数并偶数对齐，杜绝硬件编码器或播放器绿边报错。
   - 自动检测画面是否本就无黑边；若无需裁剪默认跳过，避免无效重编码造成画质损耗。

2. 丰富的运行模式：
   - 默认模式：自动检测黑边并执行硬件/高质量转码。
   - 仅检测模式 (-n / --detect-only)：仅输出黑边分析报告与建议的 crop 参数，不写入文件。
   - 实时预览模式 (-p / --preview)：自动调用 ffplay 实时预览裁剪后画面 10~15 秒，核验效果。
   - 手动指定模式 (--crop W:H:X:Y)：直接指定裁剪区域，跳过自动检测。

3. 原生 Apple Silicon GPU 硬件极速加速 + CPU 备选：
   - macOS 默认启用 Apple VideoToolbox GPU 硬件加速 (hevc_videotoolbox / h264_videotoolbox)：
     * 极速、低发热、低 CPU 占用；
     * 注入硬件 B 帧 (-bf 2) 与空间自适应量化 (-spatial_aq 1)，消除暗部色带；
     * 显式锁定 BT.709 色彩空间，杜绝 macOS/iOS QuickTime 播放泛白发灰 (Gamma Shift)。
   - 亦支持 CPU 软件编码 (--cpu / libx264 / libx265)，支持自定义 --crf 与 --preset。

4. 音频无损直通 (Stream Copy)：
   - 裁剪仅调整画面几何结构，音频流默认无损流复制 (-c:a copy)，0 耗时且 100% 保留原始音质。
   - 内置音频预检容错，若容器不兼容则自动平滑转码为高质量 AAC。

5. 本地 SSD 缓冲与原子文件安全：
   - 临时输出走本地 SSD 暂存，再原子移入目标目录，规避网络盘/SMB 频繁 seek 的 I/O 拥堵。
   - 支持处理成功后安全删除源文件 (-d / --delete-source)。
   - 实时进度反馈 (已编码时间、处理速度、实时帧率)。
"""

import argparse
import errno
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
import unicodedata
import statistics
import atexit
import signal
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# ==============================================================================
# 自包含通用日志记录与后台守护引擎 (100% 自动生成，单文件独立运行无任何外部依赖)
# ==============================================================================
class _EmbeddedVideoLogger:
    """自包含线程安全日志记录引擎 (保证脚本独立复制为 /usr/local/bin/vcrop 时日志 100% 自动记录)"""
    DEFAULT_LOG_DIR = Path.home() / ".vfix" / "logs"

    def __init__(self):
        self._lock = threading.Lock()
        self._current_log_dir = self.DEFAULT_LOG_DIR
        self._enabled = True
        self._tool_name = "vcrop"

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

    def setup_logger(self, log_dir: Optional[Union[str, Path]] = None, enabled: bool = True, tool_name: str = "vcrop") -> Path:
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
        action: str = "AUTOCROP",
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


# 导入通用日志审计模块与守护进程控制 (优先使用外部模块，缺失时平滑使用内置引擎)
try:
    current_script_dir = Path(__file__).resolve().parent
    if str(current_script_dir) not in sys.path:
        sys.path.insert(0, str(current_script_dir))
    import video_logger as vlog
    DaemonManager = getattr(vlog, "DaemonManager", _EmbeddedDaemonManager)
except Exception:
    try:
        import video_logger as vlog
        DaemonManager = getattr(vlog, "DaemonManager", _EmbeddedDaemonManager)
    except Exception:
        vlog = _EmbeddedVideoLogger()
        DaemonManager = _EmbeddedDaemonManager

if not hasattr(vlog, "setup_logger"):
    vlog = _EmbeddedVideoLogger()
if DaemonManager is None:
    DaemonManager = _EmbeddedDaemonManager

# 全局守护管理器句柄
DAEMON_MGR: Optional[Any] = None

# 常见视频后缀
SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv",
    ".ts", ".m4v", ".webm", ".m2ts", ".mpg", ".mpeg",
    ".divx", ".xvid", ".rmvb", ".rm", ".vob", ".3gp"
}


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


def safe_move_file(src: Path, dst: Path, max_retries: int = 5) -> None:
    """安全跨文件系统移动文件，彻底规避 macOS 上跨盘/SMB 网络共享时 Python 3.14 fcopyfile 的 ENXIO (Errno 6) 与 Bad file descriptor (Errno 9) 缺陷。"""
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)

    # 1. 同一文件系统尝试原子替换
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
    temp_dst = dst.with_name(f".{dst.name}.vcrop_tmp.{os.getpid()}")
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
                    pass

            written_size = temp_dst.stat().st_size
            if written_size != src_size:
                raise OSError(f"传输大小不一致: 源文件 {src_size} 字节, 目标写入 {written_size} 字节")

            try:
                os.replace(temp_dst, dst)
            except OSError as replace_err:
                if dst.exists():
                    try:
                        dst.unlink(missing_ok=True)
                        os.replace(temp_dst, dst)
                    except OSError:
                        raise replace_err
                else:
                    raise replace_err

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

    print(f"\n  [严重错误] 目标路径无法写入 ({dst}): {last_error}", file=sys.stderr)
    print(f"  [数据抢救] 本地 SSD 已保留转码完成的完整文件，未被删除！", file=sys.stderr)
    print(f"    -> 本地路径: {src} (大小: {src.stat().st_size if src.exists() else 0} 字节)", file=sys.stderr)
    print(f"    -> 您可在网络盘恢复后手动复制该文件至目标位置。\n", file=sys.stderr)
    if last_error:
        raise last_error


def get_aspect_ratio_name(w: int, h: int) -> str:
    """计算画幅比例并返回通俗比例描述。"""
    if w <= 0 or h <= 0:
        return "未知比例"
    ratio = w / h
    if abs(ratio - (16 / 9)) < 0.05:
        return f"16:9 ({ratio:.2f}:1, 标准横屏宽幅)"
    elif abs(ratio - (4 / 3)) < 0.05:
        return f"4:3 ({ratio:.2f}:1, 传统标清)"
    elif abs(ratio - 2.35) < 0.06 or abs(ratio - 2.39) < 0.06 or abs(ratio - 2.40) < 0.06:
        return f"2.39:1 ({ratio:.2f}:1, 电影宽银幕 Cinemascope)"
    elif abs(ratio - 1.85) < 0.05:
        return f"1.85:1 ({ratio:.2f}:1, 院线遮幅宽银幕 Flat)"
    elif abs(ratio - (21 / 9)) < 0.06:
        return f"21:9 ({ratio:.2f}:1, 超宽荧幕 Ultrawide)"
    elif abs(ratio - (9 / 16)) < 0.05:
        return f"9:16 ({ratio:.2f}:1, 手机竖屏短剧)"
    elif abs(ratio - (2 / 3)) < 0.05:
        return f"2:3 ({ratio:.2f}:1, 竖屏短剧)"
    elif abs(ratio - (3 / 4)) < 0.05:
        return f"3:4 ({ratio:.2f}:1, 经典竖屏)"
    elif abs(ratio - (9 / 19.5)) < 0.05 or abs(ratio - (9 / 20)) < 0.05:
        return f"9:19.5 ({ratio:.2f}:1, 全面屏手机竖屏)"
    elif ratio < 1.0:
        return f"{ratio:.2f}:1 (竖屏画面)"
    return f"{ratio:.2f}:1"


def probe_video(file_path: Path) -> Optional[Dict[str, Any]]:
    """使用 ffprobe 提取视频元数据。"""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-err_detect", "ignore_err",
        str(file_path)
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
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

        color_space = video_stream.get("color_space")
        color_primaries = video_stream.get("color_primaries")
        color_transfer = video_stream.get("color_trc") or video_stream.get("color_transfer")

        return {
            "duration": duration,
            "size": file_size,
            "bitrate": bitrate,
            "video_bitrate": video_bitrate,
            "width": width,
            "height": height,
            "codec": codec_name,
            "fps": fps,
            "color_space": color_space,
            "color_primaries": color_primaries,
            "color_transfer": color_transfer,
            "has_audio": has_audio,
            "audio_stream": audio_stream,
        }
    except Exception:
        return None


def remove_source_files(
    sources: List[Path],
    final_output: Path,
    reason: str = "源文件清理 (--delete-source)",
    tool: str = "vcrop"
) -> None:
    """
    在视频处理或切片成功后安全删除源文件（具有最严密的安全熔断保护机制）：
    1. 校验目标文件/目录是否存在且大小正常；
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
            if not is_dir_output:
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


def parse_aspect_ratio(aspect_str: Optional[str]) -> Optional[float]:
    """解析画幅比例字符串 (如 '9:16', '2:3', '16:9', '2.39:1', '1.777') 为浮点数。"""
    if not aspect_str:
        return None
    try:
        s = aspect_str.strip()
        if ":" in s:
            parts = s.split(":")
            return float(parts[0]) / float(parts[1])
        elif "/" in s:
            parts = s.split("/")
            return float(parts[0]) / float(parts[1])
        else:
            return float(s)
    except Exception:
        return None


def find_primary_continuous_span(
    densities: List[float],
    min_density: float = 0.15,
    max_gap: int = 12
) -> Optional[Tuple[int, int, int]]:
    """寻找连续密度最高的主体画面跨度（自动忽略黑边上的零星边角文字或细长广告标语）。
    返回: (start, end, width)
    """
    spans = []
    in_span = False
    start = 0
    last_active = 0
    for i, d in enumerate(densities):
        if d >= min_density:
            if not in_span:
                in_span = True
                start = i
            last_active = i
        else:
            if in_span and (i - last_active > max_gap):
                spans.append((start, last_active, last_active - start + 1))
                in_span = False
    if in_span:
        spans.append((start, last_active, last_active - start + 1))
    if not spans:
        return None
    # 按跨度宽度降序排序，取最宽且最连贯的主体画面跨度
    spans.sort(key=lambda s: s[2], reverse=True)
    return spans[0]


def analyze_frame_content_box(
    raw_bytes: bytes,
    w: int,
    h: int,
    thresh: int = 24
) -> Optional[Tuple[int, int, int, int]]:
    """
    智能单帧像素能量与连通域分析（核心算法：抗横幅广告标语与边角水印）。
    1. 左右黑边探测：在中间 60% 高度区域 (y: 20%..80%，避开顶部横幅广告与底部角落水印) 统计列密度，精准定位主体连续视频列范围；
    2. 上下黑边探测：在主体视频列范围内 (避开黑边区域干扰) 统计行密度，精准定位主体连续视频行范围。
    """
    if len(raw_bytes) < w * h:
        return None

    # 1. 左右黑边探测：采用中间 60% 垂直切片 (y: 20%..80%)
    # 彻底避开常见顶部全宽横幅滚动广告 (如"客服邮箱/未收到回复请到垃圾邮件") 和底部左右角水印广告
    y_m1, y_m2 = int(h * 0.20), int(h * 0.80)
    mid_h = max(1, y_m2 - y_m1)
    col_densities = []
    for x in range(w):
        cnt = sum(1 for y in range(y_m1, y_m2) if raw_bytes[y * w + x] > thresh)
        col_densities.append(cnt / mid_h)

    h_span = find_primary_continuous_span(col_densities, min_density=0.15)
    if not h_span or h_span[2] < w * 0.15:
        x_start, x_end = 0, w - 1
    else:
        x_start, x_end = h_span[0], h_span[1]

    left_b = x_start
    right_b = w - 1 - x_end
    # 工业标准对称性防误裁：真正的画幅黑边 (Pillarbox/Letterbox) 在左右或上下必定大体对称
    # 若单侧很深而另一侧很浅 (如一侧暗角/单侧打光/侧身特写)，属于电影正常明暗光影，绝不能当成黑边裁掉！
    if left_b > 16 or right_b > 16:
        total_lr = left_b + right_b
        diff_lr = abs(left_b - right_b)
        if diff_lr > max(16, total_lr * 0.40):
            x_start, x_end = 0, w - 1

    content_w = x_end - x_start + 1

    # 2. 上下黑边探测：在识别出的主体横向画面内探测纵向画面范围
    inner_pad = int(content_w * 0.15)
    inner_x1 = min(w - 1, x_start + inner_pad)
    inner_x2 = max(0, x_end - inner_pad)
    inner_w = max(1, inner_x2 - inner_x1 + 1)

    row_densities = []
    for y in range(h):
        cnt = sum(1 for x in range(inner_x1, inner_x2 + 1) if raw_bytes[y * w + x] > thresh)
        row_densities.append(cnt / inner_w)

    v_span = find_primary_continuous_span(row_densities, min_density=0.15)
    if not v_span or v_span[2] < h * 0.15:
        y_start, y_end = 0, h - 1
    else:
        y_start, y_end = v_span[0], v_span[1]

    top_b = y_start
    bottom_b = h - 1 - y_end
    if top_b > 16 or bottom_b > 16:
        total_tb = top_b + bottom_b
        diff_tb = abs(top_b - bottom_b)
        if diff_tb > max(16, total_tb * 0.45):
            y_start, y_end = 0, h - 1

    cw = ((x_end - x_start + 1) // 2) * 2
    ch = ((y_end - y_start + 1) // 2) * 2
    cx = (x_start // 2) * 2
    cy = (y_start // 2) * 2

    cw = min(cw, w)
    ch = min(ch, h)
    if cx + cw > w:
        cx = max(0, w - cw)
    if cy + ch > h:
        cy = max(0, h - ch)

    return cw, ch, cx, cy


def detect_crop_box(
    file_path: Path,
    orig_w: int,
    orig_h: int,
    duration: float,
    limit: int = 24,
    round_align: int = 2,
    num_samples: int = 6,
    sample_duration: float = 4.0,
    skip_intro: Optional[float] = None,
    target_aspect: Optional[str] = None,
    time_range: Optional[Tuple[float, float]] = None
) -> Optional[Dict[str, Any]]:
    """
    智能多采样点黑边探测（内置抗广告标语与抗边角水印技术）。
    1. 首选高精度单帧像素连通域分析：彻底屏蔽黑边上的跑马灯横幅标语与边角水印；
    2. 多点均匀抽样计算中位数，锁定最精准、最稳定的主体画面裁剪区域；
    3. 支持强制对齐指定画幅比例 (--aspect / -a，如 9:16)；
    4. 支持指定 time_range (start, end) 针对特定分段独立分析黑边。
    """
    if orig_w <= 0 or orig_h <= 0:
        return None

    # 计算抽样时间点（尽量避开前 5% 的片头和后 5% 的片尾黑屏/滚动字幕）
    sample_points: List[float] = []

    if time_range:
        seg_start, seg_end = time_range
        seg_duration = max(0.1, seg_end - seg_start)
        if seg_duration <= 5.0:
            sample_points = [seg_start + seg_duration / 2]
        else:
            pad = min(1.5, seg_duration * 0.05)
            step = (seg_duration - 2 * pad) / (num_samples + 1)
            sample_points = [seg_start + pad + i * step for i in range(1, num_samples + 1)]
    elif duration <= 10.0:
        sample_points = [max(0.0, (duration - 1.0) / 2)]
    elif duration <= 60.0:
        sample_points = [duration * 0.20, duration * 0.40, duration * 0.60, duration * 0.80]
    else:
        intro_offset = skip_intro if skip_intro is not None else max(30.0, duration * 0.08)
        outro_offset = max(30.0, duration * 0.05)
        usable_start = intro_offset
        usable_end = max(usable_start + 10.0, duration - outro_offset)

        step = (usable_end - usable_start) / (num_samples + 1)
        for i in range(1, num_samples + 1):
            sample_points.append(usable_start + i * step)

    # 第一阶段：高精度抗广告帧分析 (Frame Content Energy Analysis)
    all_detections: List[Tuple[int, int, int, int]] = []
    ad_immune_success = False

    for t in sample_points:
        cmd_raw = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{t:.2f}",
            "-i", str(file_path),
            "-vframes", "1",
            "-f", "rawvideo",
            "-pix_fmt", "gray",
            "pipe:1"
        ]
        try:
            proc = subprocess.run(cmd_raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
            raw_bytes = proc.stdout
            if len(raw_bytes) >= orig_w * orig_h:
                box = analyze_frame_content_box(raw_bytes, orig_w, orig_h, thresh=limit)
                if box:
                    cw, ch, cx, cy = box
                    # 过滤全黑过渡帧
                    if (cw * ch) >= (orig_w * orig_h) * 0.05:
                        all_detections.append(box)
        except Exception:
            continue

    if len(all_detections) >= max(1, len(sample_points) // 3):
        ad_immune_success = True

    # 第二阶段：如果高精度帧分析未收集到足够数据，平滑回退至 cropdetect 滤镜
    if not all_detections:
        crop_pattern = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")
        for idx, t in enumerate(sample_points, 1):
            seek_str = f"{t:.2f}"
            cmd = [
                "ffmpeg", "-hide_banner", "-nostats",
                "-ss", seek_str,
                "-i", str(file_path),
                "-t", str(sample_duration),
                "-vf", f"cropdetect={limit}:{round_align}:0",
                "-f", "null", "-"
            ]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=25)
                stderr_text = proc.stderr or ""
                matches = crop_pattern.findall(stderr_text)
                if matches:
                    stable_matches = matches[len(matches) // 2:] if len(matches) > 6 else matches
                    for m in stable_matches:
                        cw, ch, cx, cy = int(m[0]), int(m[1]), int(m[2]), int(m[3])
                        min_area = (orig_w * orig_h) * 0.05
                        if (cw * ch) < min_area or cw < 64 or ch < 64:
                            continue
                        all_detections.append((cw, ch, cx, cy))
            except Exception:
                continue

    if not all_detections:
        return None

    # 多样本中位数聚类锁定最稳定的主体画面
    med_w = int(statistics.median([d[0] for d in all_detections]))
    med_h = int(statistics.median([d[1] for d in all_detections]))
    med_x = int(statistics.median([d[2] for d in all_detections]))
    med_y = int(statistics.median([d[3] for d in all_detections]))

    # 画幅比例强行对齐与居中调整（如果指定了 --aspect / -a 参数）
    if target_aspect:
        ratio_val = parse_aspect_ratio(target_aspect)
        if ratio_val and ratio_val > 0:
            if ratio_val < 1.0:
                # 竖屏目标比例 (如 9:16 ~ 0.5625)
                target_w = int(med_h * ratio_val // round_align) * round_align
                target_w = min(target_w, orig_w)
                center_x = med_x + med_w / 2.0
                new_x = max(0, min(orig_w - target_w, int((center_x - target_w / 2.0) // round_align) * round_align))
                med_w, med_x = target_w, new_x
            else:
                # 横屏目标比例 (如 16:9 ~ 1.777, 2.39:1)
                target_h = int(med_w / ratio_val // round_align) * round_align
                target_h = min(target_h, orig_h)
                center_y = med_y + med_h / 2.0
                new_y = max(0, min(orig_h - target_h, int((center_y - target_h / 2.0) // round_align) * round_align))
                med_h, med_y = target_h, new_y

    # 偶数对齐与边界安全保护
    cw = ((med_w // round_align) * round_align)
    ch = ((med_h // round_align) * round_align)
    cx = ((med_x // round_align) * round_align)
    cy = ((med_y // round_align) * round_align)

    cw = min(cw, orig_w)
    ch = min(ch, orig_h)
    if cx + cw > orig_w:
        cx = max(0, orig_w - cw)
    if cy + ch > orig_h:
        cy = max(0, orig_h - ch)

    # 计算四周黑边像素
    top_bar = cy
    bottom_bar = orig_h - (cy + ch)
    left_bar = cx
    right_bar = orig_w - (cx + cw)

    # 是否存在实质性黑边
    has_black_bars = (
        top_bar > 4 or bottom_bar > 4 or left_bar > 4 or right_bar > 4
    )

    confidence = 100.0 if ad_immune_success else 90.0

    return {
        "w": cw,
        "h": ch,
        "x": cx,
        "y": cy,
        "top": top_bar,
        "bottom": bottom_bar,
        "left": left_bar,
        "right": right_bar,
        "confidence": confidence,
        "samples_collected": len(all_detections),
        "has_black_bars": has_black_bars,
        "ad_immune": ad_immune_success,
        "crop_str": f"{cw}:{ch}:{cx}:{cy}"
    }


def preview_crop(file_path: Path, crop_str: str, seek_time: float = 30.0) -> bool:
    """使用 ffplay 实时预览裁剪效果。"""
    ffplay_path = shutil.which("ffplay")
    if not ffplay_path:
        print("\n[警告] 未找到 ffplay 命令，无法打开实时预览窗口。可安装后再试: brew install ffmpeg", file=sys.stderr)
        return False

    print(f"\n[预览] 正在启动 ffplay 预览窗口 (跳转至 ~{seek_time:.0f}s，按 'Q' 或关闭窗口结束预览)...")
    cmd = [
        "ffplay",
        "-ss", str(seek_time),
        "-i", str(file_path),
        "-vf", f"crop={crop_str}",
        "-window_title", f"Crop Preview [{crop_str}] - {file_path.name}"
    ]
    try:
        subprocess.run(cmd)
        return True
    except Exception as e:
        print(f"[错误] 预览窗口启动失败: {e}", file=sys.stderr)
        return False


def preflight_audio_mode(file_path: Path, has_audio: bool) -> str:
    """极速预检音频流能否直接无损 copy，还是需转码 AAC。"""
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
        proc = subprocess.run(test_cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
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
        proc = subprocess.run(test_cmd_aac, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if proc.returncode == 0:
            return "aac"
    except Exception:
        pass

    return "none"


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


def execute_ffmpeg_with_progress(
    cmd: List[str],
    duration: float = 0.0,
    current_file: Optional[Union[str, Path]] = None,
    current_idx: Optional[int] = None,
    total_files: Optional[int] = None
) -> Tuple[bool, str]:
    """执行 FFmpeg 并在控制台实时刷新显示转码进度，日志中采用里程碑记录避免重复刷屏。"""
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
                    pct = None
                    curr_sec = 0.0
                    if duration > 0:
                        try:
                            parts = last_time.split(":")
                            curr_sec = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                            pct = min(100.0, max(0.0, (curr_sec / duration) * 100))
                        except Exception:
                            pct = None

                    pct_str = f" ({pct:.1f}%)" if pct is not None else ""
                    info_str = f"已编码时间: {last_time}{pct_str} | 速度: {last_speed} | 帧率: {last_fps} fps"

                    # 1. 实时向后台守护状态文件写入心跳快照 (供 vcrop --status 随时高频查看最新进度，间隔 >= 1.5s)
                    if DAEMON_MGR and (cur_time - last_status_update_time >= 1.5):
                        DAEMON_MGR.update_status(
                            current_file=current_file,
                            file_index=current_idx,
                            total_files=total_files,
                            stage="裁剪转码中",
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
        sys.stdout.write("\n  [中断] 接收到终止信号，正在退出 FFmpeg 进程...\n")
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
def smooth_and_merge_segments(
    segments: List[Tuple[float, float, str]],
    min_duration: float = 12.0,
    drop_ads: bool = False,
    max_ad_duration: float = 120.0,
    target_aspect: Optional[str] = None
) -> List[Tuple[float, float, str]]:
    """
    智能平滑与吸收瞬态镜头：
    1. 彻底吸收低于 min_duration 的碎片短镜头（如暗光场景、瞬时转场闪黑、短镜头切换），并入相邻主要片段；
    2. 合并连续相同画幅片段；
    3. 若某种主导画幅占比超 85% 且非主导总时长极短，自动判定全片为统一画幅，防止普通影片暗光误切；
    4. 若开启 drop_ads，根据优势主画幅识别混剪中的广告/插片并直接剔除。
    """
    if not segments:
        return []

    curr_segs = [(s, e, k) for s, e, k in segments]

    # 统计各画幅累计时长以判定核心正片类型
    kind_durations: Dict[str, float] = {}
    for s, e, k in curr_segs:
        kind_durations[k] = kind_durations.get(k, 0.0) + (e - s)
    total_len = sum(kind_durations.values())

    target_ratio = parse_aspect_ratio(target_aspect) if target_aspect else None
    if target_ratio:
        dominant_kind = "vertical" if target_ratio < 0.9 else "horizontal"
    else:
        dominant_kind = max(kind_durations, key=kind_durations.get) if kind_durations else "horizontal"

    dominant_ratio = (kind_durations.get(dominant_kind, 0.0) / total_len) if total_len > 0 else 1.0

    # 循环合并相邻同画幅并吸收低于 min_duration 的碎片段
    while True:
        merged = []
        for seg in curr_segs:
            if not merged:
                merged.append(seg)
            else:
                prev_s, prev_e, prev_k = merged[-1]
                if seg[2] == prev_k:
                    merged[-1] = (prev_s, seg[1], prev_k)
                else:
                    merged.append(seg)
        curr_segs = merged

        if len(curr_segs) <= 1:
            break

        changed = False
        new_segs = []
        i = 0
        while i < len(curr_segs):
            s, e, k = curr_segs[i]
            dur = e - s
            is_potential_ad = drop_ads and (k != dominant_kind) and (dur <= max_ad_duration)

            if dur < min_duration and not is_potential_ad:
                changed = True
                if new_segs:
                    ps, pe, pk = new_segs[-1]
                    new_segs[-1] = (ps, e, pk)
                elif i + 1 < len(curr_segs):
                    ns, ne, nk = curr_segs[i + 1]
                    curr_segs[i + 1] = (s, ne, nk)
                else:
                    new_segs.append((s, e, k))
            else:
                new_segs.append((s, e, k))
            i += 1
        curr_segs = new_segs
        if not changed:
            break

    # 若主导画幅占比极高 (>= 85%)，且非主导片段总时长很短（<= 30s），直接判定为统一画幅
    non_dominant_dur = sum((e - s) for s, e, k in curr_segs if k != dominant_kind)
    if dominant_ratio >= 0.85 and non_dominant_dur < 30.0 and not drop_ads:
        return [(curr_segs[0][0], curr_segs[-1][1], dominant_kind)]

    # 广告/片头过滤 (如果启用了 drop_ads)
    if drop_ads:
        filtered: List[Tuple[float, float, str]] = []
        for s, e, k in curr_segs:
            dur = e - s
            is_ad = False
            if target_ratio:
                if k != dominant_kind:
                    is_ad = True
            else:
                if k != dominant_kind and dur <= max_ad_duration:
                    is_ad = True
                elif s == 0.0 and k != dominant_kind and dur <= 60.0:
                    is_ad = True

            if not is_ad:
                filtered.append((s, e, k))
        return filtered

    return curr_segs


def detect_scene_segments(
    file_path: Path,
    orig_w: int,
    orig_h: int,
    duration: float,
    min_segment_duration: float = 12.0,
    scan_step: Optional[float] = None,
    limit: int = 24,
    round_align: int = 2,
    target_aspect: Optional[str] = None,
    drop_ads: bool = False,
    max_ad_duration: float = 120.0
) -> List[Dict[str, Any]]:
    """
    智能识别混剪视频中横竖屏切换转场时间点，并对每个切片独立计算最佳 crop 区域。
    1. 轻量级时间线全片高速抽样扫描 (scale 360:-2, 内存管道直通解析，20~30 分钟视频仅需数秒)；
    2. 画幅迟滞判定与时间线中值滤波平滑单样本瞬态噪点；
    3. 短镜头严格吸收平滑过滤与广告/片头识别剔除；
    4. 仅对确认真实存在的场景分段转场边界做毫秒级精确定位 (避免上百次无意义重试)；
    5. 对每一个独立画幅分段调用多点采样算法计算最佳 Crop 参数；
    6. 若检测到全片画幅自始至终一致（仅 1 段），返回空列表 [] 触发平滑回退。
    """
    if duration <= min_segment_duration * 1.5:
        return []

    # 1. 确定扫描时间步长
    if scan_step is not None:
        step = max(1.0, float(scan_step))
    else:
        if duration <= 120.0:
            step = 2.0
        elif duration <= 600.0:
            step = 3.0
        elif duration <= 1800.0:
            step = 5.0
        else:
            step = max(5.0, min(10.0, duration / 300))

    fps_val = 1.0 / step
    scaled_w = 360
    scaled_h = int(orig_h * (scaled_w / orig_w))
    if scaled_h % 2 != 0:
        scaled_h += 1
    frame_size = scaled_w * scaled_h

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(file_path),
        "-vf", f"fps={fps_val:.4f},scale={scaled_w}:{scaled_h}",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"
    ]

    samples: List[Tuple[float, str, float]] = []
    last_stable_kind = "horizontal"
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        frame_idx = 0
        while True:
            raw_b = proc.stdout.read(frame_size)
            if len(raw_b) < frame_size:
                break
            t_sec = min(duration, frame_idx * step)
            box = analyze_frame_content_box(raw_b, scaled_w, scaled_h, thresh=limit)
            if box:
                cw, ch, cx, cy = box
                ratio = cw / max(1, ch)
                left_bar = cx
                right_bar = scaled_w - (cx + cw)
                is_centered = abs(left_bar - right_bar) <= (scaled_w * 0.15)
                # 判定横屏与竖屏：
                # 真正的手机竖屏短剧居中放置时，左右必定对称且宽高比显著 <= 0.80 (如 9:16 ~ 0.56, 3:4 ~ 0.75)
                # 若左右不对称 (如电影单侧打光/靠墙暗景) 或比例在 0.80~1.25 之间，保持上一稳定状态，彻底消除伪切换
                if ratio <= 0.80 and is_centered:
                    kind = "vertical"
                elif ratio >= 1.25:
                    kind = "horizontal"
                else:
                    kind = last_stable_kind
                last_stable_kind = kind
                samples.append((t_sec, kind, ratio))
            frame_idx += 1
        proc.stdout.close()
        proc.wait()
    except Exception as e:
        print(f"  [扫描提示] 全片画幅采样出现异常 ({e})，将回退为常规单视频模式", file=sys.stderr)
        return []

    if not samples:
        return []

    # 时间线中值滤波平滑孤立单帧毛刺
    clean_samples = [s for s in samples]
    for i in range(1, len(clean_samples) - 1):
        prev_k = clean_samples[i - 1][1]
        curr_k = clean_samples[i][1]
        next_k = clean_samples[i + 1][1]
        if curr_k != prev_k and prev_k == next_k:
            clean_samples[i] = (clean_samples[i][0], prev_k, clean_samples[i][2])

    # 2. 初始粗聚类并直接平滑吸收（将采样噪点直接消除）
    coarse_transitions = []
    last_kind = None
    last_t = 0.0
    for t_sec, kind, _ in clean_samples:
        if kind != last_kind:
            if last_kind is not None:
                coarse_transitions.append((last_t, t_sec, last_kind))
                last_t = t_sec
            last_kind = kind
    coarse_transitions.append((last_t, duration, last_kind))

    clean_segments = smooth_and_merge_segments(
        coarse_transitions,
        min_duration=min_segment_duration,
        drop_ads=drop_ads,
        max_ad_duration=max_ad_duration,
        target_aspect=target_aspect
    )

    if len(clean_segments) <= 1:
        # 全片画幅风格高度一致，直接平滑回退为常规单视频去黑边流程
        return []

    # 3. 对确认真实存在的场景分段转场边界做微调精确切点定位 (仅对真正的转场做微调，毫秒级完成)
    def get_kind_at(t_probe: float) -> str:
        cmd_p = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", f"{t_probe:.3f}",
            "-i", str(file_path),
            "-vframes", "1",
            "-vf", f"scale={scaled_w}:{scaled_h}",
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"
        ]
        try:
            p_res = subprocess.run(cmd_p, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4)
            if len(p_res.stdout) >= frame_size:
                p_box = analyze_frame_content_box(p_res.stdout, scaled_w, scaled_h, thresh=limit)
                if p_box:
                    p_ratio = p_box[0] / max(1, p_box[1])
                    return "vertical" if p_ratio < 0.85 else ("horizontal" if p_ratio >= 1.25 else "unknown")
        except Exception:
            pass
        return "unknown"

    def refine_transition(t_low: float, t_high: float, target_k: str, tol: float = 0.25) -> float:
        low, high = t_low, t_high
        while high - low > tol:
            mid = (low + high) / 2.0
            k = get_kind_at(mid)
            if k == target_k:
                high = mid
            else:
                low = mid
        return round(high, 2)

    refined_segments: List[Tuple[float, float, str]] = []
    for idx, (s, e, kind) in enumerate(clean_segments):
        start_pt = s
        end_pt = e
        if idx > 0:
            start_pt = refined_segments[-1][1]
        if idx < len(clean_segments) - 1:
            next_kind = clean_segments[idx + 1][2]
            p_start = max(start_pt, e - step)
            p_end = min(duration, e + step)
            try:
                exact_cut = refine_transition(p_start, p_end, next_kind)
                end_pt = exact_cut
            except Exception:
                end_pt = e
        else:
            end_pt = duration
        refined_segments.append((start_pt, end_pt, kind))

    # 4. 针对每个独立分段，高精度探测最佳 Crop 裁切参数
    final_segments: List[Dict[str, Any]] = []
    for idx, (s, e, kind) in enumerate(refined_segments, 1):
        seg_dur = e - s
        crop_res = detect_crop_box(
            file_path=file_path,
            orig_w=orig_w,
            orig_h=orig_h,
            duration=duration,
            limit=limit,
            round_align=round_align,
            target_aspect=target_aspect,
            time_range=(s, e)
        )
        if not crop_res:
            crop_res = {
                "w": orig_w, "h": orig_h, "x": 0, "y": 0,
                "crop_str": f"{orig_w}:{orig_h}:0:0",
                "has_black_bars": False
            }

        aspect_label = "9-16" if kind == "vertical" else "16-9"
        final_segments.append({
            "part_index": idx,
            "start": s,
            "end": e,
            "duration": seg_dur,
            "kind": kind,
            "aspect_label": aspect_label,
            "aspect_name": get_aspect_ratio_name(crop_res["w"], crop_res["h"]),
            "crop": crop_res
        })

    return final_segments


def process_video_with_splits(
    file_path: Path,
    output_target: Optional[Path],
    segments: List[Dict[str, Any]],
    info: Dict[str, Any],
    encoder: str,
    crf: int,
    preset: str,
    bitrate_kbps: Optional[int] = None,
    delete_source: bool = False,
    preview: bool = False
) -> bool:
    """
    针对检测到的横竖屏混剪切片列表，执行分段帧级精确切片与独立黑边裁剪。
    输出至源文件所在目录下的 [视频名]_segments/ 子目录中。
    """
    clean_stem = file_path.stem
    if output_target:
        if output_target.is_dir() or not output_target.suffix:
            base_dir = output_target
        else:
            base_dir = output_target.parent
    else:
        base_dir = file_path.parent

    segments_dir = base_dir / f"{clean_stem}_segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n===========================================================================")
    print(f"[切片输出目录] {segments_dir}")
    print(f"准备开始执行 {len(segments)} 个分段的切片与裁剪转码...")

    if preview:
        print(f"\n[预览模式] 启动前两个分段裁剪预览:")
        for seg in segments[:2]:
            seek_time = seg["start"] + min(5.0, seg["duration"] / 2)
            preview_crop(file_path, seg["crop"]["crop_str"], seek_time=seek_time)
        return True

    orig_size = info["size"]
    orig_w = info["width"]
    orig_h = info["height"]
    fps = info["fps"]
    has_audio = info["has_audio"]
    orig_bitrate = info["bitrate"]
    orig_v_bitrate = info.get("video_bitrate", 0.0)
    if orig_v_bitrate <= 0.0 and orig_bitrate > 0.0:
        orig_v_bitrate = orig_bitrate * 0.88
    ref_v_bitrate = orig_v_bitrate if orig_v_bitrate > 0 else 2_500_000

    audio_mode = preflight_audio_mode(file_path, has_audio)
    gop_size = max(15, int(fps * 2))
    is_hardware = "videotoolbox" in encoder

    color_args: List[str] = []
    src_primaries = info.get("color_primaries")
    src_trc = info.get("color_transfer")
    src_space = info.get("color_space")
    if src_primaries and src_primaries != "unknown":
        color_args.extend(["-color_primaries", src_primaries])
    if src_trc and src_trc != "unknown":
        color_args.extend(["-color_trc", src_trc])
    if src_space and src_space != "unknown":
        color_args.extend(["-colorspace", src_space])
    if not color_args:
        color_args = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]

    success_segments = 0
    total_written_bytes = 0
    t_start_all = time.time()

    for idx, seg in enumerate(segments, 1):
        s = seg["start"]
        e = seg["end"]
        seg_dur = seg["duration"]
        kind = seg["kind"]
        crop_info = seg["crop"]
        aspect_label = seg["aspect_label"]
        aspect_name = seg["aspect_name"]

        part_filename = f"{clean_stem}_part{idx:02d}_{aspect_label}_crop.mp4"
        final_part_path = segments_dir / part_filename

        print(f"\n---------------------------------------------------------------------------")
        print(f">> [切片 {idx}/{len(segments)}] {part_filename}")
        print(f"  * 时间区间: {s:.1f}s -> {e:.1f}s (片段时长: {seg_dur:.1f}s)")
        print(f"  * 画幅风格: {aspect_name} ({kind})")
        print(f"  * 裁切区域: {crop_info['crop_str']} (裁后分辨率: {crop_info['w']}x{crop_info['h']})")

        temp_part_file = Path(tempfile.gettempdir()) / f"vcrop_split_{os.getpid()}_{idx}_{part_filename}"
        if temp_part_file.exists():
            temp_part_file.unlink(missing_ok=True)

        pixel_ratio = (crop_info["w"] * crop_info["h"]) / max(1, orig_w * orig_h)
        if bitrate_kbps:
            target_bps = bitrate_kbps * 1000
        else:
            calc_bps = int(ref_v_bitrate * pixel_ratio * 0.95)
            target_bps = max(120_000, min(int(ref_v_bitrate), calc_bps))

        is_hardware = "videotoolbox" in encoder or "rkmpp" in encoder
        video_args: List[str] = []
        if is_hardware:
            max_rate_limit = min(int(target_bps * 1.25), int(ref_v_bitrate * 1.15))
            buf_size_limit = int(target_bps * 2.0)
            if encoder == "hevc_videotoolbox":
                video_args = [
                    "-c:v", "hevc_videotoolbox",
                    "-b:v", str(target_bps),
                    "-profile:v", "main",
                    "-spatial_aq", "1",
                    "-bf", "2",
                    "-tag:v", "hvc1",
                    "-pix_fmt", "yuv420p",
                    "-g", str(gop_size),
                    "-maxrate", str(max_rate_limit),
                    "-bufsize", str(buf_size_limit)
                ] + color_args
            elif encoder == "hevc_rkmpp":
                video_args = [
                    "-c:v", "hevc_rkmpp",
                    "-b:v", str(target_bps),
                    "-profile", "main",
                    "-rc_mode", "0",
                    "-tag:v", "hvc1",
                    "-pix_fmt", "yuv420p",
                    "-g", str(gop_size),
                    "-maxrate", str(max_rate_limit),
                    "-bufsize", str(buf_size_limit)
                ]
            elif encoder == "h264_rkmpp":
                video_args = [
                    "-c:v", "h264_rkmpp",
                    "-b:v", str(target_bps),
                    "-profile", "high",
                    "-rc_mode", "0",
                    "-pix_fmt", "yuv420p",
                    "-g", str(gop_size),
                    "-maxrate", str(max_rate_limit),
                    "-bufsize", str(buf_size_limit)
                ]
            else:
                video_args = [
                    "-c:v", "h264_videotoolbox",
                    "-b:v", str(target_bps),
                    "-profile:v", "high",
                    "-spatial_aq", "1",
                    "-bf", "2",
                    "-pix_fmt", "yuv420p",
                    "-g", str(gop_size),
                    "-maxrate", str(max_rate_limit),
                    "-bufsize", str(buf_size_limit)
                ] + color_args
        else:
            cpu_maxrate = str(min(int(target_bps * 1.25), int(ref_v_bitrate * 1.15)))
            cpu_bufsize = str(int(target_bps * 2.0))
            if encoder == "libx265":
                video_args = [
                    "-c:v", "libx265",
                    "-crf", str(crf),
                    "-preset", preset,
                    "-tag:v", "hvc1",
                    "-pix_fmt", "yuv420p",
                    "-g", str(gop_size),
                    "-maxrate", cpu_maxrate,
                    "-bufsize", cpu_bufsize
                ] + color_args
            else:
                video_args = [
                    "-c:v", "libx264",
                    "-crf", str(crf),
                    "-preset", preset,
                    "-pix_fmt", "yuv420p",
                    "-g", str(gop_size),
                    "-maxrate", cpu_maxrate,
                    "-bufsize", cpu_bufsize
                ] + color_args

        # 音频参数
        if audio_mode == "copy":
            audio_args = ["-c:a", "copy"]
        elif audio_mode == "aac":
            audio_args = ["-c:a", "aac", "-b:a", "128k"]
        else:
            audio_args = ["-an"]

        # 两级精准 Seek 策略：前置 fast_seek (至多提前 10 秒) 跳转至关键帧附近，
        # 后置 fine_seek 精准丢弃前导帧，彻底消除关键帧跨度导致的上一片段画面残留！
        fast_seek = max(0.0, s - 10.0)
        fine_seek = s - fast_seek

        cmd = [
            "ffmpeg", "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-err_detect", "ignore_err",
            "-fflags", "+discardcorrupt",
            "-ss", f"{fast_seek:.3f}",
            "-i", str(file_path),
            "-ss", f"{fine_seek:.3f}",
            "-t", f"{seg_dur:.3f}",
            "-vf", f"crop={crop_info['crop_str']}",
            "-map", "0:v:0",
            "-map", "0:a?"
        ] + video_args + audio_args + [
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(temp_part_file)
        ]

        t_part_start = time.time()
        success, err_msg = execute_ffmpeg_with_progress(
            cmd,
            seg_dur,
            current_file=final_part_path,
            current_idx=idx,
            total_files=len(segments)
        )

        # 音频 copy 若在切片边界异常，回退 aac
        if not success and audio_mode == "copy":
            fallback_cmd = [
                "ffmpeg", "-y",
                "-hide_banner",
                "-loglevel", "error",
                "-err_detect", "ignore_err",
                "-fflags", "+discardcorrupt",
                "-ss", f"{fast_seek:.3f}",
                "-i", str(file_path),
                "-ss", f"{fine_seek:.3f}",
                "-t", f"{seg_dur:.3f}",
                "-vf", f"crop={crop_info['crop_str']}",
                "-map", "0:v:0",
                "-map", "0:a?"
            ] + video_args + ["-c:a", "aac", "-b:a", "128k", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(temp_part_file)]
            success, err_msg = execute_ffmpeg_with_progress(
                fallback_cmd,
                seg_dur,
                current_file=final_part_path,
                current_idx=idx,
                total_files=len(segments)
            )

        if not success or not temp_part_file.exists() or temp_part_file.stat().st_size == 0:
            print(f"  [错误] 分段 {idx} 转码失败: {err_msg}", file=sys.stderr)
            if temp_part_file.exists():
                temp_part_file.unlink(missing_ok=True)
            continue

        part_size = temp_part_file.stat().st_size
        total_written_bytes += part_size
        safe_move_file(temp_part_file, final_part_path)
        t_part_cost = time.time() - t_part_start
        print(f"  * [完成] 分段 {idx} 裁剪成功! 耗时: {t_part_cost:.1f}s | 大小: {format_bytes(part_size)} | 导出路径: {final_part_path.resolve()}")
        success_segments += 1

    total_cost = time.time() - t_start_all
    print(f"\n===========================================================================")
    print(f"[智能切片总结] 全部处理完成! 成功切片: {success_segments}/{len(segments)} 个 | 总耗时: {total_cost:.1f}s")
    print(f"  * 原始单文件: {format_bytes(orig_size)} -> 分段总大小: {format_bytes(total_written_bytes)}")
    print(f"  * 所有切片视频已汇总存放至: {segments_dir.resolve()}")

    is_all_ok = (success_segments == len(segments))
    if vlog:
        vlog.log_processed(
            input_file=file_path,
            output_target=segments_dir,
            status="SUCCESS" if is_all_ok else "PARTIAL" if success_segments > 0 else "FAILED",
            action="SPLIT_SCENES",
            orig_size=orig_size,
            new_size=total_written_bytes,
            elapsed_sec=total_cost,
            orig_info=info,
            details=f"智能场景切片共 {len(segments)} 段 (成功导出 {success_segments} 段)",
            tool="vcrop"
        )

    if is_all_ok and delete_source:
        remove_source_files([file_path], segments_dir, reason="智能场景切片全部成功后安全清理源文件 (--delete-source)", tool="vcrop")

    return is_all_ok


def process_single_video(
    file_path: Path,
    output_target: Optional[Path],
    custom_crop: Optional[str],
    detect_only: bool,
    preview: bool,
    force: bool,
    encoder: str,
    crf: int,
    preset: str,
    bitrate_kbps: Optional[int],
    limit: int,
    round_align: int,
    samples: int,
    sample_duration: float,
    skip_intro: Optional[float],
    delete_source: bool,
    aspect: Optional[str] = None,
    split_scenes: bool = False,
    min_segment_duration: float = 12.0,
    scan_step: Optional[float] = None,
    drop_ads: bool = False,
    max_ad_duration: float = 120.0,
    current_idx: int = 1,
    total_files: int = 1
) -> bool:
    """处理单个视频的黑边检测与裁剪输出。"""
    print(f"\n{'='*75}")
    print(f"[处理中] [{current_idx}/{total_files}] 文件: {file_path.resolve()}")
    if DAEMON_MGR:
        DAEMON_MGR.update_status(
            current_file=file_path,
            file_index=current_idx,
            total_files=total_files,
            stage="元数据探测与黑边分析"
        )
    orig_size = file_path.stat().st_size
    print(f"  * 原始大小: {format_bytes(orig_size)}")

    info = probe_video(file_path)
    if not info:
        print("  [错误] 无法解析该视频文件元数据，跳过处理。", file=sys.stderr)
        if vlog:
            vlog.log_processed(
                input_file=file_path,
                output_target=None,
                status="FAILED",
                action="AUTOCROP",
                orig_size=orig_size,
                details="无法解析视频文件元数据",
                tool="vcrop"
            )
        return False

    orig_w = info["width"]
    orig_h = info["height"]
    duration = info["duration"]
    orig_bitrate = info["bitrate"]
    fps = info["fps"]
    has_audio = info["has_audio"]

    orig_ratio_desc = get_aspect_ratio_name(orig_w, orig_h)
    print(f"  * 原始分辨率: {orig_w}x{orig_h} | 画幅比例: {orig_ratio_desc}")
    # 1. 智能场景切片模式判定 (如果启用了 --split-scenes 且未手动指定 --crop)
    if split_scenes and not custom_crop:
        print(f"  * 智能场景切片模式: [开启] (最小片段门槛: {min_segment_duration:.1f}s)")
        if drop_ads:
            print(f"    * 广告智能剔除: [开启] (最大广告时长门槛: {max_ad_duration:.1f}s)")
        print(f"    - 正在高速扫描全片画幅时序突变时间点...")
        t0_split = time.time()
        detected_segments = detect_scene_segments(
            file_path=file_path,
            orig_w=orig_w,
            orig_h=orig_h,
            duration=duration,
            min_segment_duration=min_segment_duration,
            scan_step=scan_step,
            limit=limit,
            round_align=round_align,
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

            if detect_only:
                print(f"\n  [检测模式] 已输出全部 {len(detected_segments)} 个分段的切片时间点与推荐裁剪参数，跳过转码。")
                return True

            return process_video_with_splits(
                file_path=file_path,
                output_target=output_target,
                segments=detected_segments,
                info=info,
                encoder=encoder,
                crf=crf,
                preset=preset,
                bitrate_kbps=bitrate_kbps,
                delete_source=delete_source,
                preview=preview
            )
        else:
            print(f"  * 场景切片检测完成 (耗时: {t_split:.2f}s): 全片画幅风格高度一致，自动平滑回退为单文件全局裁剪流程。")

    # 2. 确定单文件裁剪参数
    crop_info: Optional[Dict[str, Any]] = None
    if custom_crop:
        parts = custom_crop.split(":")
        if len(parts) == 4:
            cw, ch, cx, cy = map(int, parts)
            crop_info = {
                "w": cw, "h": ch, "x": cx, "y": cy,
                "top": cy, "bottom": orig_h - (cy + ch),
                "left": cx, "right": orig_w - (cx + cw),
                "confidence": 100.0,
                "samples_collected": 1,
                "has_black_bars": True,
                "crop_str": custom_crop
            }
            print(f"  * 裁剪模式: [手动指定] crop={custom_crop}")
        else:
            print(f"  [错误] 手动指定的 --crop 参数格式不符合 W:H:X:Y 标准: {custom_crop}", file=sys.stderr)
            return False
    else:
        print(f"  * 黑边智能检测中: 阈值 limit={limit}, 对齐 round={round_align}, 采样点数={samples} ...")
        t0 = time.time()
        crop_info = detect_crop_box(
            file_path=file_path,
            orig_w=orig_w,
            orig_h=orig_h,
            duration=duration,
            limit=limit,
            round_align=round_align,
            num_samples=samples,
            sample_duration=sample_duration,
            skip_intro=skip_intro,
            target_aspect=aspect
        )
        t_detect = time.time() - t0

        if not crop_info:
            print(f"  [警告] 无法可靠探测该视频黑边参数 (耗时: {t_detect:.2f}s)。")
            return False

        print(f"  * 检测完成 (耗时: {t_detect:.2f}s, 置信度: {crop_info['confidence']:.1f}%):")
        if crop_info.get("ad_immune"):
            print(f"    * 智能过滤: [已成功排除顶部横幅广告标语与边角水印干扰]")
        if aspect:
            print(f"    * 画幅比例: [已强制对齐目标比例 --aspect {aspect}]")
        print(f"    - 黑边分布: 上={crop_info['top']}px | 下={crop_info['bottom']}px | 左={crop_info['left']}px | 右={crop_info['right']}px")
        cropped_ratio_desc = get_aspect_ratio_name(crop_info['w'], crop_info['h'])
        print(f"    - 裁后分辨率: {crop_info['w']}x{crop_info['h']} | 新画幅比例: {cropped_ratio_desc}")
        print(f"    - 对应滤镜: -vf \"crop={crop_info['crop_str']}\"")

    # 2. 判断是否需要裁剪
    if not crop_info["has_black_bars"] and not custom_crop:
        if not force:
            print(f"\n  [跳过] 该视频画面四边均无明显黑边 (上/下/左/右 <= 4px)，无需裁剪！")
            print(f"  [提示] 若仍需强制执行重编码可添加参数: --force")
            if vlog:
                vlog.log_processed(
                    input_file=file_path,
                    output_target=None,
                    status="SKIPPED",
                    action="AUTOCROP",
                    orig_size=orig_size,
                    orig_info=info,
                    details="四边无明显黑边 (<= 4px)，跳过裁剪重编码以保护画质",
                    tool="vcrop"
                )
            return True
        else:
            print(f"  [强制执行] 虽未检测到明显黑边，因设置了 --force，继续执行裁剪重编码。")

    # 3. 预览模式
    if preview:
        preview_crop(file_path, crop_info["crop_str"], seek_time=max(10.0, min(60.0, duration * 0.3)))
        # 预览后直接结束当前文件处理，方便用户调参
        return True

    # 4. 仅检测模式
    if detect_only:
        print(f"\n  [检测模式] 已输出分析结果。推荐执行命令:")
        print(f"  ffmpeg -i \"{file_path.name}\" -vf \"crop={crop_info['crop_str']}\" -c:v {encoder} -c:a copy \"{file_path.stem}_crop.mp4\"")
        return True

    # 5. 推导输出文件路径
    if output_target:
        if output_target.is_dir() or str(output_target).endswith(("/", "\\")):
            output_dir = output_target
            output_file = output_dir / f"{file_path.stem}_crop.mp4"
        else:
            output_dir = output_target.parent
            output_file = output_target
    else:
        output_dir = file_path.parent
        output_file = output_dir / f"{file_path.stem}_crop.mp4"

    output_dir.mkdir(parents=True, exist_ok=True)

    # 本地 SSD 临时暂存路径
    temp_dir = Path(tempfile.gettempdir())
    temp_output_file = temp_dir / f"vcrop_{os.getpid()}_{output_file.name}"

    # 6. 音频流策略
    audio_mode = preflight_audio_mode(file_path, has_audio)
    if audio_mode == "copy":
        audio_args = ["-c:a", "copy"]
        print("  * 音频策略: [无损直通 stream-copy] (保留 100% 原始音质)")
    elif audio_mode == "aac":
        audio_args = ["-c:a", "aac", "-b:a", "192k"]
        print("  * 音频策略: [自动转码 AAC 192k] (原音频格式不兼容 MP4 封装)")
    else:
        audio_args = ["-an"]
        print("  * 音频策略: [无音频流]")

    # 7. 构建视频编码参数
    is_hardware = "videotoolbox" in encoder or "rkmpp" in encoder
    gop_size = max(15, int(fps * 2))

    src_primaries = info.get("color_primaries")
    src_trc = info.get("color_transfer")
    src_space = info.get("color_space")

    color_args: List[str] = []
    if src_primaries and src_primaries != "unknown":
        color_args.extend(["-color_primaries", src_primaries])
    if src_trc and src_trc != "unknown":
        color_args.extend(["-color_trc", src_trc])
    if src_space and src_space != "unknown":
        color_args.extend(["-colorspace", src_space])

    if not color_args:
        color_args = [
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709"
        ]

    # 提取纯视频码率（排除音频流）
    orig_v_bitrate = info.get("video_bitrate", 0.0)
    if orig_v_bitrate <= 0.0 and orig_bitrate > 0.0:
        orig_v_bitrate = orig_bitrate * 0.88
    ref_v_bitrate = orig_v_bitrate if orig_v_bitrate > 0 else 2_500_000

    pixel_ratio = (crop_info["w"] * crop_info["h"]) / max(1, orig_w * orig_h)

    if bitrate_kbps:
        target_bps = bitrate_kbps * 1000
    else:
        # 核心防体积膨胀策略：
        # 裁剪黑边后有效画面像素减少（如 800x480 -> 640x480，像素减少 20%）
        # 保持等同画质密度的目标纯视频码率 = 原纯视频码率 * 像素比例 * 0.95
        calc_bps = int(ref_v_bitrate * pixel_ratio * 0.95)
        # 严格防倒挂上限：目标码率绝对不得高于原纯视频码率；下限保底 120 kbps (针对极低码率极老视频)
        target_bps = max(120_000, min(int(ref_v_bitrate), calc_bps))

    video_args: List[str] = []
    if is_hardware:
        max_rate_limit = min(int(target_bps * 1.25), int(ref_v_bitrate * 1.15))
        buf_size_limit = int(target_bps * 2.0)
        if encoder == "hevc_videotoolbox":
            video_args = [
                "-c:v", "hevc_videotoolbox",
                "-b:v", str(target_bps),
                "-profile:v", "main",
                "-spatial_aq", "1",
                "-bf", "2",
                "-tag:v", "hvc1",
                "-pix_fmt", "yuv420p",
                "-g", str(gop_size),
                "-maxrate", str(max_rate_limit),
                "-bufsize", str(buf_size_limit)
            ] + color_args
        elif encoder == "hevc_rkmpp":
            video_args = [
                "-c:v", "hevc_rkmpp",
                "-b:v", str(target_bps),
                "-profile", "main",
                "-rc_mode", "0",
                "-tag:v", "hvc1",
                "-pix_fmt", "yuv420p",
                "-g", str(gop_size),
                "-maxrate", str(max_rate_limit),
                "-bufsize", str(buf_size_limit)
            ]
        elif encoder == "h264_rkmpp":
            video_args = [
                "-c:v", "h264_rkmpp",
                "-b:v", str(target_bps),
                "-profile", "high",
                "-rc_mode", "0",
                "-pix_fmt", "yuv420p",
                "-g", str(gop_size),
                "-maxrate", str(max_rate_limit),
                "-bufsize", str(buf_size_limit)
            ]
        else:
            video_args = [
                "-c:v", "h264_videotoolbox",
                "-b:v", str(target_bps),
                "-profile:v", "high",
                "-spatial_aq", "1",
                "-bf", "2",
                "-pix_fmt", "yuv420p",
                "-g", str(gop_size),
                "-maxrate", str(max_rate_limit),
                "-bufsize", str(buf_size_limit)
            ] + color_args
        hw_label = "Rockchip RKMPP" if "rkmpp" in encoder else "Apple VideoToolbox"
        print(f"  * 编码引擎: [{hw_label} 硬件极速加速] ({encoder})")
        print(f"    - 原视频码率: ~{format_bitrate(orig_bitrate)} (纯视频: ~{format_bitrate(ref_v_bitrate)})")
        print(f"    - 防膨胀目标码率: ~{format_bitrate(target_bps)} (像素减少 {(1-pixel_ratio)*100:.1f}%%，码率等比缩减)")
    else:
        cpu_maxrate = str(min(int(target_bps * 1.25), int(ref_v_bitrate * 1.15)))
        cpu_bufsize = str(int(target_bps * 2.0))
        if encoder == "libx265":
            video_args = [
                "-c:v", "libx265",
                "-crf", str(crf),
                "-preset", preset,
                "-tag:v", "hvc1",
                "-pix_fmt", "yuv420p",
                "-g", str(gop_size),
                "-maxrate", cpu_maxrate,
                "-bufsize", cpu_bufsize
            ] + color_args
        else:
            video_args = [
                "-c:v", "libx264",
                "-crf", str(crf),
                "-preset", preset,
                "-pix_fmt", "yuv420p",
                "-g", str(gop_size),
                "-maxrate", cpu_maxrate,
                "-bufsize", cpu_bufsize
            ] + color_args
        print(f"  * 编码引擎: [CPU 软件编码] ({encoder}, CRF: {crf}, Preset: {preset}) | 码率上限: ~{format_bitrate(target_bps)}")

    # 8. 组装 FFmpeg 裁剪命令
    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-err_detect", "ignore_err",
        "-fflags", "+discardcorrupt",
        "-i", str(file_path),
        "-vf", f"crop={crop_info['crop_str']}",
        "-map", "0:v:0",
        "-map", "0:a?"
    ] + video_args + audio_args + [
        "-movflags", "+faststart",
        str(temp_output_file)
    ]

    print(f"  * 开始裁剪转码 -> 目标文件: {output_file.resolve()}")
    t_start = time.time()
    success, err_msg = execute_ffmpeg_with_progress(
        cmd,
        duration,
        current_file=file_path,
        current_idx=current_idx,
        total_files=total_files
    )
    elapsed = time.time() - t_start

    # === 核心安全防线：全方位产物完整性与时长截断核验 ===
    is_truncated = False
    out_probe = None
    if success and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
        out_probe = probe_video(temp_output_file)
        if not out_probe or not out_probe.get("width"):
            success = False
            err_msg = "产物无有效视频画面流 (生成的文件可能破损或为空壳)"
        elif duration > 15.0:
            out_dur = out_probe.get("duration", 0.0)
            if out_dur > 0.0 and out_dur < (duration * 0.94) and (duration - out_dur) > 4.0:
                is_truncated = True
                err_msg = (
                    f"产物严重过早截断！源时长: {duration:.1f}s ({duration/60:.1f}m)，"
                    f"转码产物仅: {out_dur:.1f}s ({out_dur/60:.1f}m)，"
                    f"丢失时长: {(duration - out_dur):.1f}s (缺失率: {((duration - out_dur) / duration * 100):.1f}%)"
                )

    # 硬件加速（如 RKMPP / VideoToolbox）若遇特殊编码/损坏包出现报错或意外提前截断，自动平滑切换至 CPU 软件强力容错编码
    if (not success or is_truncated) and is_hardware:
        if is_truncated:
            print(f"\n  [严重警告] 硬件加速器 ({encoder}) 发生意外截断退出！\n    * {err_msg}\n    * 正在丢弃残片，并立即自动启动 CPU 软件强力容错编码 (libx264) 重新压制...")
        else:
            print("\n  [提示] 硬件加速转码未通过，自动平滑切换至 CPU 软件编码优化...")

        if temp_output_file.exists():
            temp_output_file.unlink(missing_ok=True)

        cpu_maxrate = f"{int(target_bps * 1.5 / 1000)}k"
        cpu_bufsize = f"{int(target_bps * 3.0 / 1000)}k"
        sw_video_args = [
            "-c:v", "libx264",
            "-crf", str(crf),
            "-preset", preset,
            "-pix_fmt", "yuv420p",
            "-g", str(gop_size),
            "-maxrate", cpu_maxrate,
            "-bufsize", cpu_bufsize
        ] + color_args

        cmd_sw = [
            "ffmpeg", "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-max_error_rate", "1.0",
            "-err_detect", "ignore_err",
            "-fflags", "+discardcorrupt+genpts",
            "-i", str(file_path),
            "-vf", f"crop={crop_info['crop_str']}",
            "-map", "0:v:0",
            "-map", "0:a?"
        ] + sw_video_args + audio_args + [
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(temp_output_file)
        ]
        success, err_msg = execute_ffmpeg_with_progress(
            cmd_sw,
            duration,
            current_file=file_path,
            current_idx=current_idx,
            total_files=total_files
        )
        is_truncated = False
        if success and temp_output_file.exists() and temp_output_file.stat().st_size > 0:
            out_probe = probe_video(temp_output_file)
            if not out_probe or not out_probe.get("width"):
                success = False
                err_msg = "CPU 软件转码产物无有效视频流"
            elif duration > 15.0:
                out_dur = out_probe.get("duration", 0.0)
                if out_dur > 0.0 and out_dur < (duration * 0.94) and (duration - out_dur) > 4.0:
                    is_truncated = True
                    err_msg = f"CPU 软件转码后依然截断 (源时长: {duration:.1f}s, 产物仅: {out_dur:.1f}s)！原片损坏严重"
                    success = False
        else:
            success = False

    if is_truncated:
        success = False

    if not success or not temp_output_file.exists() or temp_output_file.stat().st_size == 0:
        err_snippet = err_msg if not success else "生成的临时输出文件为空"
        print(f"\n  [错误] 裁剪转码失败: {err_snippet}", file=sys.stderr)
        if temp_output_file.exists():
            temp_output_file.unlink(missing_ok=True)
        if vlog:
            vlog.log_processed(
                input_file=file_path,
                output_target=output_file,
                status="FAILED",
                action="AUTOCROP",
                orig_size=orig_size,
                elapsed_sec=elapsed,
                orig_info=info,
                details=f"裁剪转码失败: {err_snippet}",
                tool="vcrop"
            )
        return False

    new_size = temp_output_file.stat().st_size
    size_diff = (new_size - orig_size) / orig_size * 100 if orig_size > 0 else 0

    # 安全原子移动到最终输出路径
    safe_move_file(temp_output_file, output_file)

    print(f"\n[完成] 裁剪成功! 耗时: {elapsed:.1f}s")
    print(f"  * 最终输出: {output_file.resolve()}")
    print(f"  * 文件大小: {format_bytes(orig_size)} -> {format_bytes(new_size)} ({size_diff:+.1f}%)")
    if vlog:
        vlog.log_processed(
            input_file=file_path,
            output_target=output_file,
            status="SUCCESS",
            action="AUTOCROP",
            orig_size=orig_size,
            new_size=new_size,
            elapsed_sec=elapsed,
            orig_info=info,
            details=f"去黑边 crop={crop_info['crop_str']} (上={crop_info.get('top',0)} 下={crop_info.get('bottom',0)} 左={crop_info.get('left',0)} 右={crop_info.get('right',0)})",
            tool="vcrop"
        )

    # 9. 源文件清理逻辑（内置安全熔断核验）
    if delete_source:
        remove_source_files([file_path], output_file, reason="视频去黑边成功后安全清理源文件 (--delete-source)", tool="vcrop")

    return True


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


def normalize_cli_args(raw_args: List[str]) -> List[str]:
    """智能规范化与容错命令行参数 (兼容 -corp, -c, --crop 等)。"""
    norm_args = []
    i = 0
    crop_geom_pattern = re.compile(r"^\d+:\d+(:\d+:\d+)?$")

    while i < len(raw_args):
        arg = raw_args[i]
        if arg == "-corp":
            norm_args.append("-c")
            i += 1
            continue
        if arg == "-crop":
            arg = "--crop"
        if arg in ("--crop", "--autocrop"):
            if i + 1 < len(raw_args):
                next_val = raw_args[i + 1]
                if crop_geom_pattern.match(next_val):
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
            if i + 1 < len(raw_args):
                next_val = raw_args[i + 1]
                if crop_geom_pattern.match(next_val):
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
    """智能展开通配符并支持跨平台大小写不敏感与递归深层搜索。"""
    expanded = os.path.expanduser(pattern_str)
    candidate_patterns = [expanded]

    if recursive and "**" not in expanded:
        dir_part, base_part = os.path.split(expanded)
        if dir_part:
            rec_pattern = os.path.join(dir_part, "**", base_part)
        else:
            rec_pattern = os.path.join("**", base_part)
        if rec_pattern not in candidate_patterns:
            candidate_patterns.insert(0, rec_pattern)

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


def collect_video_files(patterns: List[str], recursive: bool = False) -> List[Path]:
    """收集待处理视频文件列表。"""
    collected: List[Path] = []
    seen = set()

    for pat in patterns:
        path_obj = normalize_and_resolve_path(pat)
        if not path_obj.exists():
            for ext in [".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".wmv"]:
                cand = normalize_and_resolve_path(f"{pat}{ext}")
                if cand.is_file():
                    path_obj = cand
                    break
        if path_obj.is_file():
            if not path_obj.name.startswith("._") and not path_obj.name.startswith("."):
                res = path_obj.resolve()
                if res not in seen:
                    seen.add(res)
                    collected.append(path_obj)
        elif path_obj.is_dir():
            if not recursive:
                try:
                    has_subdirs = any(item.is_dir() and not item.name.startswith(".") for item in path_obj.iterdir())
                    if has_subdirs:
                        print(f"[提示] 检测到目录 '{path_obj.name}' 包含子文件夹。当前未开启 -r，仅扫描本级目录；若需遍历子目录请加上 -r 参数。")
                except Exception:
                    pass
            else:
                print(f"正在扫描目录: {path_obj} (递归遍历已开启)...")

            glob_func = path_obj.rglob if recursive else path_obj.glob
            for f in glob_func("*"):
                if f.name.startswith("._") or f.name.startswith("."):
                    continue
                if f.is_file() and f.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:
                    res = f.resolve()
                    if res not in seen:
                        seen.add(res)
                        collected.append(f)
        else:
            # 通配符匹配
            matched = expand_glob_pattern(pat, recursive=recursive)

            for match_str in matched:
                p = normalize_and_resolve_path(match_str)
                if p.is_file() and not p.name.startswith("._") and not p.name.startswith("."):
                    if p.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:
                        res = p.resolve()
                        if res not in seen:
                            seen.add(res)
                            collected.append(p)
                elif p.is_dir():
                    if recursive:
                        print(f"正在扫描目录: {p} (递归遍历已开启)...")
                    glob_func = p.rglob if recursive else p.glob
                    for f in glob_func("*"):
                        if f.name.startswith("._") or f.name.startswith("."):
                            continue
                        if f.is_file() and f.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:
                            res = f.resolve()
                            if res not in seen:
                                seen.add(res)
                                collected.append(f)

    return collected


def main():
    has_vt_h264 = has_videotoolbox_support()
    has_vt_hevc = has_videotoolbox_hevc_support()
    has_rk_h264 = has_rkmpp_support()
    has_rk_hevc = has_rkmpp_hevc_support()

    # 默认编码器推荐
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

    parser = argparse.ArgumentParser(
        prog="vcrop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="视频智能自动去黑边工具 (video_autocrop.py / vcrop)",
        epilog="""
常用示例：
  vcrop video.mp4                                    # 智能自动去黑边 (内置抗广告算法，自动过滤黑边横幅标语/边角水印)
  vcrop video.mp4 -a 9:16                            # 竖屏短剧神器：强制对齐并锁定标准 9:16 画幅居中裁切
  vcrop video.mp4 -p                                 # 自动检测并在 ffplay 窗口中实时预览裁切效果 (不转码)
  vcrop video.mp4 -n                                 # 仅检测黑边尺寸并输出推荐命令 (不转码)
  vcrop video.mp4 --crop 1920:800:0:140              # 手动强制指定裁切区域 (跳过自动探测)
  vcrop video.mp4 --h264                             # 使用 H.264 硬件编码输出 (兼容老旧设备)
  vcrop video.mp4 --cpu --crf 18                     # 强制使用 CPU 软件编码 (libx264 CRF 18)
  vcrop *.mp4 -o /path/to/output_dir/                # 批量裁切当前目录下所有 mp4 并导出到指定目录
  vcrop video.mp4 -d                                 # 裁切成功后自动安全删除源文件
  vcrop *.mp4 -B                                     # 后台脱机批量裁切当前目录下所有视频 (终端即刻释放)
  vcrop --status                                     # 查看后台任务运行状态、处理进度与最新日志
  vcrop --stop                                       # 安全停止当前后台任务 (严格保护源文件不被误删)
"""
    )

    parser.add_argument("inputs", nargs="*", default=[], help="输入视频文件路径、通配符或目录 (例如: EP01.mp4 或 *.mp4)")
    parser.add_argument("-o", "--output", type=str, default=None, help="指定输出文件路径或目标目录 (默认保存在源文件同级，追加 _crop.mp4)")
    parser.add_argument("-p", "--preview", action="store_true", help="调用 ffplay 实时预览裁切效果 15 秒 (不转码输出)")
    parser.add_argument("-n", "--detect-only", "--dry-run", action="store_true", help="仅执行黑边探测分析并打印参数，不执行实际编码")
    parser.add_argument("-f", "--force", action="store_true", help="强制执行裁切重编码，即使未检测到明显黑边也不跳过")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归遍历子目录查找视频文件")
    parser.add_argument("-d", "--delete-source", "--del-src", action="store_true", help="裁切完全成功后自动删除源视频文件 (节约空间)")

    # 后台运行与任务守护
    daemon_group = parser.add_argument_group("后台运行与任务守护 (脱机运行)")
    daemon_group.add_argument("-B", "--background", "--daemon", action="store_true",
                              help="将任务转入后台脱机运行 (即刻释放终端，终端关闭或网络中断不影响执行)")
    daemon_group.add_argument("--status", action="store_true",
                              help="查看当前后台任务运行状态、PID 及最新实时日志")
    daemon_group.add_argument("--stop", action="store_true",
                              help="安全终止当前正在运行的后台任务 (确保保护源文件并清理临时缓存)")

    # 裁剪微调参数
    crop_group = parser.add_argument_group("黑边检测调优参数")
    crop_group.add_argument("-c", "--autocrop", action="store_true", help="启用智能自动去黑边 (默认即为自动去黑边)")
    crop_group.add_argument("--crop", "--crop-size", dest="crop", type=str, default=None, help="手动指定裁剪尺寸 W:H:X:Y (例如: 1920:800:0:140，跳过自动检测)")
    crop_group.add_argument("-a", "--aspect", "--ratio", type=str, default=None,
                            help="强制锁定目标画幅比例并居中裁剪 (例如: 9:16, 2:3, 16:9, 2.39:1，特别适用于竖屏短剧精准提取标准画幅)")
    crop_group.add_argument("--limit", type=int, default=24, help="黑度检测阈值 0~255 (默认 24；越小对纯黑要求越严，暗场景防误切可调低至 12~16)")
    crop_group.add_argument("--round", type=int, default=2, choices=[2, 4, 8, 16], help="输出分辨率偶数对齐基数 (默认 2，推荐 2 或 16)")
    crop_group.add_argument("--samples", type=int, default=6, help="全片抽样检测片段数量 (默认 6 个时间点)")
    crop_group.add_argument("--sample-duration", type=float, default=4.0, help="每个抽样片段的检测时长/秒 (默认 4.0 秒)")
    crop_group.add_argument("--skip-intro", type=float, default=None, help="跳过片头探测的秒数 (默认自动跳过片头 30s~8%% 避免龙标/厂标黑屏干扰)")

    # 编码与性能参数
    encode_group = parser.add_argument_group("编码器与画质设置")
    encode_group.add_argument("--encoder", type=str, default=default_encoder,
                              choices=["hevc_videotoolbox", "h264_videotoolbox", "hevc_rkmpp", "h264_rkmpp", "libx264", "libx265"],
                              help=f"视频编码器 (当前系统默认: {default_encoder})")
    encode_group.add_argument("--h264", action="store_true", help="优先使用 H.264 编码 (兼容老电视或老旧播放设备)")
    encode_group.add_argument("--cpu", action="store_true", help="强制使用 CPU 软件编码 (libx264 / libx265)")
    encode_group.add_argument("--crf", type=int, default=18, help="CPU 软件编码画质 CRF 值 (默认 18 高清，数值越小画质越高)")
    encode_group.add_argument("--preset", type=str, default="veryfast",
                              choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"],
                              help="CPU 编码预设速度 (默认 veryfast)")
    encode_group.add_argument("-b", "--bitrate", type=int, default=None, help="指定输出视频码率 (单位 kbps，若未指定则自适应最佳码率)")

    # 智能画幅切片参数
    split_group = parser.add_argument_group("智能场景画幅切片参数")
    split_group.add_argument("-s", "--split-scenes", "--split", dest="split_scenes", action="store_true",
                             help="启用智能场景切片模式：自动识别横竖屏混剪转场时间点，切分为多个独立视频并分别去黑边")
    split_group.add_argument("-D", "--drop-ads", "--skip-ads", dest="drop_ads", action="store_true",
                             help="智能剔除广告/插片分段：自动识别混剪视频中的横屏短广告与过渡插片并直接丢弃，仅保留并导出核心正片剧集")
    split_group.add_argument("--max-ad-duration", type=float, default=120.0,
                             help="判定为广告/插片的最大时长阈值/秒 (默认 120.0 秒)")
    split_group.add_argument("--min-segment-duration", type=float, default=12.0,
                             help="切片最小片段时长/秒 (默认 12.0 秒，低于此阈值的短镜头/转场自动并入相邻主画幅，防止碎片化)")
    split_group.add_argument("--scan-step", type=float, default=None,
                             help="全片画幅扫描抽样间隔/秒 (默认自适应，通常 4~6 秒)")

    # 运行审计与日志记录 (默认 100% 自动记录至 ~/.vfix/logs/)
    log_group = parser.add_argument_group("运行审计与日志记录 (默认全自动生成)")
    log_group.add_argument("--log-dir", type=str, default=None, help="自定义日志保存目录 (默认全自动存储至: ~/.vfix/logs/)")
    log_group.add_argument("--no-log", action="store_true", help="禁用所有文件处理与删除日志记录 (默认已开启自动记录)")

    norm_argv = normalize_cli_args(sys.argv[1:])
    args = parser.parse_args(norm_argv)

    global DAEMON_MGR
    vlog.setup_logger(log_dir=args.log_dir, enabled=not args.no_log, tool_name="vcrop")
    daemon_mgr = DaemonManager(tool_name="vcrop", log_file=vlog.get_main_log_path())
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
        print("    vcrop video.mp4                                # 智能自动去黑边")
        print("    vcrop *.mp4 -B                                 # 后台脱机批量裁切当前目录所有视频")
        print("    vcrop --status                                 # 查看后台任务运行状态与实时日志")
        print("    vcrop --stop                                   # 安全停止当前后台任务")
        sys.exit(1)

    if getattr(args, "preview", False) and args.background:
        print("[错误] 实时预览模式 (-p / --preview) 需要图形交互窗口，不能在后台脱机模式 (-B) 下运行。")
        sys.exit(1)

    setup_signal_handlers()

    if not check_ffmpeg():
        sys.exit(1)

    # 决策最终编码器
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

    output_target = normalize_and_resolve_path(args.output).resolve() if args.output else None
    video_files = collect_video_files(args.inputs, recursive=args.recursive)

    if not video_files:
        print("[提示] 未找到符合条件的视频文件，请检查输入路径或通配符规则。")
        sys.exit(0)

    # 若指定了后台运行模式，在此处分叉并退出父进程
    if args.background:
        daemon_mgr.daemonize(file_count=len(video_files), raw_argv=sys.argv[1:])

    session_start_time = time.time()
    vlog.log_session_start(sys.argv[1:], len(video_files), tool="vcrop")

    print(f"找到 {len(video_files)} 个待处理视频文件。")
    if not args.no_log:
        print(f"  * 运行与删除日志: {vlog.get_log_dir()}/ (processed.log / deleted.log / vcrop.log) [自动记录]")
    if not args.detect_only and not args.preview:
        engine_desc = {
            "hevc_videotoolbox": "Apple VideoToolbox HEVC/H.265 GPU 硬件加速 (极速、体积小、画质高)",
            "h264_videotoolbox": "Apple VideoToolbox H.264 GPU 硬件加速 (极速、高兼容、低发热)",
            "hevc_rkmpp": "Rockchip RKMPP HEVC/H.265 硬件极速加速 (VPU硬压、体积小、超高速)",
            "h264_rkmpp": "Rockchip RKMPP H.264 硬件极速加速 (VPU硬压、高兼容、超高速)",
            "libx265": f"CPU 软件高效编码 libx265 (CRF: {args.crf}, Preset: {args.preset})",
            "libx264": f"CPU 软件标准编码 libx264 (CRF: {args.crf}, Preset: {args.preset})"
        }.get(encoder, f"{encoder} 编码引擎")
        print(f"加速引擎: [{engine_desc}]")

    success_count = 0
    fail_count = 0

    for idx, video_path in enumerate(video_files, 1):
        print(f"\n>> 正在处理 [{idx}/{len(video_files)}]: {video_path.resolve()}")
        try:
            ok = process_single_video(
                file_path=video_path,
                output_target=output_target,
                custom_crop=args.crop,
                detect_only=args.detect_only,
                preview=args.preview,
                force=args.force,
                encoder=encoder,
                crf=args.crf,
                preset=args.preset,
                bitrate_kbps=args.bitrate,
                limit=args.limit,
                round_align=args.round,
                samples=args.samples,
                sample_duration=args.sample_duration,
                skip_intro=args.skip_intro,
                delete_source=args.delete_source,
                aspect=args.aspect,
                split_scenes=args.split_scenes,
                min_segment_duration=args.min_segment_duration,
                scan_step=args.scan_step,
                drop_ads=args.drop_ads,
                max_ad_duration=args.max_ad_duration,
                current_idx=idx,
                total_files=len(video_files)
            )
            if ok:
                success_count += 1
            else:
                fail_count += 1
        except KeyboardInterrupt:
            print("\n[中断] 用户已手动中止操作。")
            sys.exit(130)
        except Exception as e:
            print(f"[异常] 处理出现未捕获异常: {e}", file=sys.stderr)
            fail_count += 1

    total_cost = time.time() - session_start_time
    if vlog:
        vlog.log_session_end(success_count, fail_count, total_cost, tool="vcrop")
    if DAEMON_MGR:
        DAEMON_MGR.update_status(
            stage="任务全部完成",
            progress_str=f"共完成 {success_count}/{len(video_files)} 个视频"
        )

    print(f"\n{'='*75}")
    print(f"[处理总结] 全部文件处理完成: 成功 {success_count} 个, 失败/异常 {fail_count} 个。")
    if not args.no_log:
        print(f"  * 运行与删除日志: {vlog.get_log_dir()}/ (processed.log / deleted.log / vcrop.log)")
    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
