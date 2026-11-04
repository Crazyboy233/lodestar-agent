"""在运行后端的本机上弹出系统文件夹选择器。

浏览器拿不到本地绝对路径（File System Access API 只给 handle，不给 path），
所以选择动作必须由本机 Python 进程代劳：macOS 用 osascript，Windows 用
PowerShell + FolderBrowserDialog，Linux 用 zenity / kdialog。

返回选中目录的绝对路径；用户取消返回 None；打不开则抛 PickUnavailable。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import threading

# 选择器是全局单例：一次只允许开一个，避免两个请求叠出两个系统弹窗
_LOCK = threading.Lock()

_PROMPT = "选择产出目录"
_TIMEOUT = 300.0


class PickUnavailable(RuntimeError):
    """当前系统打不开文件夹选择器（缺 GUI / 缺命令 / 子进程报错）。"""


def pick_directory(prompt: str = _PROMPT, timeout: float = _TIMEOUT) -> str | None:
    """弹出系统选择器，返回目录路径；取消返回 None。"""
    if not _LOCK.acquire(blocking=False):
        raise PickUnavailable("已经有一个文件夹选择器打开了，先在那边选完")
    try:
        if sys.platform == "darwin":
            raw = _pick_macos(prompt, timeout)
        elif os.name == "nt":
            raw = _pick_windows(prompt, timeout)
        else:
            raw = _pick_linux(prompt, timeout)
        return _clean_path(raw)
    finally:
        _LOCK.release()


# ── 各平台实现 ────────────────────────────────────────────────

def _pick_macos(prompt: str, timeout: float) -> str:
    # on error number -128 是「用户取消」，让它变成空输出而不是非零退出码
    script = (
        "try\n"
        f'  set chosen to choose folder with prompt "{prompt}"\n'
        "  return POSIX path of chosen\n"
        "on error number -128\n"
        '  return ""\n'
        "end try\n"
    )
    proc = _run(["osascript", "-e", script], timeout)
    if proc.returncode != 0:
        _raise_proc("打开系统文件夹选择器失败", proc)
    return proc.stdout or ""


def _pick_windows(prompt: str, timeout: float) -> str:
    exe = (
        shutil.which("powershell.exe")
        or shutil.which("powershell")
        or shutil.which("pwsh")
    )
    if not exe:
        raise PickUnavailable("找不到 PowerShell，无法打开 Windows 文件夹选择器")

    # 用 EncodedCommand 传脚本：避开中文 / 引号 / 路径分隔符在命令行里的转义问题。
    # OutputEncoding 固定成 UTF-8，否则中文目录名会按控制台代码页被解错。
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog\n"
        f"$dialog.Description = '{prompt}'\n"
        "$dialog.ShowNewFolderButton = $true\n"
        "$result = $dialog.ShowDialog()\n"
        "if ($result -eq [System.Windows.Forms.DialogResult]::OK) {\n"
        "  [Console]::Out.Write($dialog.SelectedPath)\n"
        "}\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = _run(
        [exe, "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden",
         "-EncodedCommand", encoded],
        timeout,
    )
    if proc.returncode != 0:
        _raise_proc("打开 Windows 文件夹选择器失败", proc)
    return proc.stdout or ""


def _pick_linux(prompt: str, timeout: float) -> str:
    zenity = shutil.which("zenity")
    if zenity:
        proc = _run([zenity, "--file-selection", "--directory", f"--title={prompt}"], timeout)
        if proc.returncode == 1:
            return ""
        if proc.returncode != 0:
            _raise_proc("打开文件夹选择器失败", proc)
        return proc.stdout or ""

    kdialog = shutil.which("kdialog")
    if kdialog:
        proc = _run(
            [kdialog, "--getexistingdirectory", os.path.expanduser("~"),
             "--title", prompt],
            timeout,
        )
        if proc.returncode == 1:
            return ""
        if proc.returncode != 0:
            _raise_proc("打开文件夹选择器失败", proc)
        return proc.stdout or ""

    raise PickUnavailable("Linux 需要安装 zenity 或 kdialog 才能打开文件夹选择器")


# ── 公共小工具 ────────────────────────────────────────────────

def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PickUnavailable(f"找不到命令：{cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PickUnavailable("文件夹选择器等待超时，已取消") from exc
    except OSError as exc:
        raise PickUnavailable(f"启动文件夹选择器失败：{exc}") from exc


def _raise_proc(prefix: str, proc: subprocess.CompletedProcess) -> None:
    detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
    raise PickUnavailable(f"{prefix}：{detail}" if detail else prefix)


def _clean_path(raw: str) -> str | None:
    # PowerShell 在某些主机上会带 BOM；Windows 路径里出现它，isdir 一定失败
    path = (raw or "").strip().lstrip("\ufeff").strip()
    if not path:
        return None
    path = os.path.normpath(path)
    # 极端情况下选中项在对话框关闭后被删了；直接说清楚，别让任务后在别的目录里跑
    if not os.path.isdir(path):
        raise PickUnavailable(f"选择的目录不存在：{path}")
    return path
