#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VideoFix & VideoAutoCrop 通用日志记录模块 (video_logger.py)
--------------------------------------------------------
专门记录:
1. 所有执行处理过的视频文件 (输入、输出、分辨率、码率、体积变化、处理模式、耗时、状态) -> processed.log
2. 所有被安全删除的源文件 (删除时间、原路径、体积、删除原因、对应生成的目标文件、状态) -> deleted.log
3. 全局综合运行日志 (会话边界、操作详情、告警与错误) -> vfix.log

默认存储路径: ~/.vfix/logs/ (可通过 --log-dir 或环境变量 VFIX_LOG_DIR 自定义)
"""

import atexit
import errno
import json
import os
import signal
import sys
import time
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

_LOG_LOCK = threading.Lock()

# 默认全局配置
DEFAULT_LOG_DIR = Path.home() / ".vfix" / "logs"
_CURRENT_LOG_DIR: Path = DEFAULT_LOG_DIR
_LOG_ENABLED: bool = True
_TOOL_NAME: str = "vfix"


def format_bytes(size_bytes: Union[int, float]) -> str:
    """人类可读的字节格式化 (B, KB, MB, GB, TB)"""
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


def format_bitrate(bps: Union[int, float]) -> str:
    """人类可读的码率格式化 (kbps, Mbps)"""
    try:
        val = float(bps)
    except (ValueError, TypeError):
        return "未知码率"
    if val <= 0:
        return "未知码率"
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f} Mbps"
    return f"{val / 1000:.0f} kbps"


def get_log_dir() -> Path:
    """获取当前生效的日志存储目录"""
    global _CURRENT_LOG_DIR
    env_dir = os.environ.get("VFIX_LOG_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return _CURRENT_LOG_DIR


def setup_logger(
    log_dir: Optional[Union[str, Path]] = None,
    enabled: bool = True,
    tool_name: str = "vfix"
) -> Path:
    """初始化配置日志系统"""
    global _CURRENT_LOG_DIR, _LOG_ENABLED, _TOOL_NAME
    _LOG_ENABLED = enabled
    _TOOL_NAME = tool_name

    if log_dir:
        _CURRENT_LOG_DIR = Path(log_dir).expanduser().resolve()
    else:
        env_dir = os.environ.get("VFIX_LOG_DIR")
        if env_dir:
            _CURRENT_LOG_DIR = Path(env_dir).expanduser().resolve()
        else:
            _CURRENT_LOG_DIR = DEFAULT_LOG_DIR

    if _LOG_ENABLED:
        try:
            _CURRENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            sys.stderr.write(f"[警告] 无法创建日志目录 {_CURRENT_LOG_DIR}: {e}\n")

    return _CURRENT_LOG_DIR


def _append_to_file(file_path: Path, text: str) -> bool:
    """线程安全的追加写入日志文件"""
    if not _LOG_ENABLED:
        return False
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(text)
                f.flush()
        return True
    except Exception as e:
        sys.stderr.write(f"[日志异常] 写入日志失败 ({file_path.name}): {e}\n")
        return False


def get_processed_log_path() -> Path:
    return get_log_dir() / "processed.log"


def get_failed_log_path() -> Path:
    return get_log_dir() / "failed.log"


def get_deleted_log_path() -> Path:
    return get_log_dir() / "deleted.log"


def get_main_log_path() -> Path:
    return get_log_dir() / f"{_TOOL_NAME}.log"


def log_failed(
    input_file: Union[str, Path, List[Union[str, Path]]],
    error_msg: Optional[str] = None,
    action: str = "PROCESS",
    orig_size: Optional[Union[int, float]] = None,
    elapsed_sec: Optional[float] = None,
    orig_info: Optional[Dict[str, Any]] = None,
    details: Optional[str] = None,
    tool: Optional[str] = None
) -> None:
    """单独记录失败任务的便捷入口"""
    log_processed(
        input_file=input_file,
        output_target=None,
        status="FAILED",
        action=action,
        orig_size=orig_size,
        elapsed_sec=elapsed_sec,
        orig_info=orig_info,
        details=details or error_msg,
        tool=tool
    )


def log_processed(
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
    """
    记录执行处理过的文件。
    :param input_file: 单个输入文件路径，或分卷列表 (合并场景)
    :param output_target: 输出文件路径或切片目录
    :param status: SUCCESS, SKIPPED, FAILED
    :param action: COMPRESS, REPAIR_ONLY, REPACKAGE, AUTOCROP, SPLIT_SCENES, MERGE, MERGE_ONLY 等
    :param orig_size: 原始大小（字节）
    :param new_size: 输出大小（字节）
    :param elapsed_sec: 耗时（秒）
    :param orig_info: 原始音视频信息字典 (width, height, fps, codec, bitrate 等)
    :param output_info: 输出音视频信息字典
    :param details: 附加策略、错误信息或备注
    :param tool: vfix 或 vcrop
    """
    if not _LOG_ENABLED:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tool_label = tool or _TOOL_NAME

    # 输入文件格式化
    if isinstance(input_file, (list, tuple)):
        input_count = len(input_file)
        first_path = str(Path(input_file[0]).resolve())
        if input_count > 1:
            input_desc = f"{first_path} (等共 {input_count} 个分卷)"
        else:
            input_desc = first_path
    else:
        input_desc = str(Path(input_file).resolve())

    # 输出路径格式化
    out_desc = str(Path(output_target).resolve()) if output_target else "None"

    # 体积与比例计算
    size_str = ""
    if orig_size is not None and orig_size >= 0:
        size_str = format_bytes(orig_size)
        if new_size is not None and new_size >= 0:
            change_pct = ((new_size - orig_size) / orig_size * 100) if orig_size > 0 else 0
            saved_bytes = orig_size - new_size
            if saved_bytes > 0:
                saved_str = f", 节省 {format_bytes(saved_bytes)}"
            elif saved_bytes < 0:
                saved_str = f", 增加 {format_bytes(-saved_bytes)}"
            else:
                saved_str = ""
            size_str = f"{format_bytes(orig_size)} -> {format_bytes(new_size)} ({change_pct:+.1f}%{saved_str})"

    # 耗时格式化
    cost_str = f"{elapsed_sec:.1f}s" if elapsed_sec is not None else "未知"

    # 原始音视频参数摘要
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
            media_specs.append(format_bitrate(float(br)))
    media_str = f" | {', '.join(media_specs)}" if media_specs else ""

    is_fail = status.upper() in ("FAILED", "FAIL", "ERROR")

    # 构建结构化日志块
    lines = [
        f"[{now_str}] [{tool_label}] [{status.upper()}] [{action.upper()}] - {input_desc}",
        f"  * 输入: {input_desc}{media_str}",
        f"  * 输出: {out_desc}",
        f"  * 体积: {size_str} | 耗时: {cost_str}"
    ]
    if details:
        field_name = "失败原因" if is_fail else "详情"
        lines.append(f"  * {field_name}: {details.strip()}")
    lines.append("")  # 空行分隔

    log_entry = "\n".join(lines) + "\n"

    # 写入 processed.log
    _append_to_file(get_processed_log_path(), log_entry)

    # 若失败则单独写入 failed.log
    if is_fail:
        _append_to_file(get_failed_log_path(), log_entry)

    # 同步写入综合日志 vfix.log
    main_line = f"{now_str} [{status.upper()}] [{tool_label}:{action.upper()}] {input_desc} -> {out_desc} ({size_str}, 耗时 {cost_str})"
    if details:
        main_line += f" | {details.strip()}"
    _append_to_file(get_main_log_path(), main_line + "\n")


def log_deleted(
    deleted_file: Union[str, Path],
    size_bytes: Optional[Union[int, float]] = None,
    reason: str = "源文件清理 (--delete-source)",
    target_path: Optional[Union[str, Path]] = None,
    status: str = "DELETED",
    error_msg: Optional[str] = None,
    tool: Optional[str] = None
) -> None:
    """
    记录所有被删除的文件（包括被清理的原文件、合并后的旧分卷等）。
    :param deleted_file: 被删除的文件路径
    :param size_bytes: 删除前的文件大小
    :param reason: 删除原因 (例如: --delete-source 处理成功后删除)
    :param target_path: 生成并替代该源文件的目标产物路径
    :param status: DELETED 或 DELETE_FAILED
    :param error_msg: 删除失败时的错误提示
    :param tool: vfix 或 vcrop
    """
    if not _LOG_ENABLED:
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tool_label = tool or _TOOL_NAME

    try:
        del_path = str(Path(deleted_file).resolve())
    except Exception:
        del_path = str(deleted_file)

    target_str = str(Path(target_path).resolve()) if target_path else "None"
    size_str = format_bytes(size_bytes) if size_bytes is not None else "未知大小"
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

    log_entry = "\n".join(lines) + "\n"

    # 写入 deleted.log
    _append_to_file(get_deleted_log_path(), log_entry)

    # 同步写入综合日志 vfix.log
    main_line = f"{now_str} [{status.upper()}] [{tool_label}:CLEANUP] 已删除源文件: {del_path} ({size_str}) | 原因: {reason.strip()}"
    if error_msg:
        main_line += f" | 错误: {error_msg.strip()}"
    _append_to_file(get_main_log_path(), main_line + "\n")


def log_event(level: str, message: str, tool: Optional[str] = None) -> None:
    """向综合日志记录一般运行事件"""
    if not _LOG_ENABLED:
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tool_label = tool or _TOOL_NAME
    line = f"{now_str} [{level.upper():5s}] [{tool_label}] {message}\n"
    _append_to_file(get_main_log_path(), line)


def log_session_start(args: List[str], file_count: int, tool: Optional[str] = None) -> None:
    """记录一次任务运行会话启动"""
    tool_label = tool or _TOOL_NAME
    cmd_str = " ".join(args)
    log_event("INFO", f"========== 开始任务会话: {tool_label} (待处理文件: {file_count} 个) ==========", tool=tool_label)
    log_event("INFO", f"执行参数: {cmd_str}", tool=tool_label)


def log_session_end(success_count: int, fail_count: int, total_cost: float, tool: Optional[str] = None) -> None:
    """记录任务会话结束"""
    tool_label = tool or _TOOL_NAME
    log_event("INFO", f"========== 会话结束: 成功 {success_count} 个, 失败 {fail_count} 个, 总耗时 {total_cost:.1f}s ==========", tool=tool_label)


class DaemonManager:
    """通用后台守护进程管理器，支持脱机运行、状态查询与安全停止。"""

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
        """检查指定 PID 的进程是否存活"""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError as err:
            return err.errno == errno.EPERM

    def get_running_info(self) -> Optional[Dict[str, Any]]:
        """获取当前正在运行的后台任务信息，若 PID 已死亡则自动清理残留锁文件"""
        if not self.pid_file.exists():
            return None
        try:
            with open(self.pid_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("pid")
            if pid and self.is_pid_alive(pid):
                return data
            # 进程已死亡，清理僵死/遗留的 PID 锁文件与状态文件
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
        """查询并打印当前后台任务状态及最新实时日志"""
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
        """优雅安全终止后台任务，确保未完成转码的源文件受到严格保护"""
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
        # 优先发送 SIGTERM 给进程组
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

        # 最多等待 5 秒确认退出
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
        """执行守护进程化 (双重 fork + setsid + 重定向标准输出)"""
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
                # 父进程：等待接收孙进程的 PID
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
                # 子进程 1 退出，让孙进程由系统托管
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

        # 写入 PID JSON 文件
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

        # 通知父进程
        try:
            os.write(w, str(daemon_pid).encode("utf-8"))
        except Exception:
            pass
        finally:
            try:
                os.close(w)
            except Exception:
                pass

        # 重定向标准输入输出至日志文件
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

        # 注册进程正常退出时清理 PID 与状态文件
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

        # 注册终端中断与挂断信号
        def sig_handler(signum, frame):
            cleanup_pid()
            sys.exit(143)

        try:
            signal.signal(signal.SIGTERM, sig_handler)
            signal.signal(signal.SIGHUP, sig_handler)
        except Exception:
            pass

