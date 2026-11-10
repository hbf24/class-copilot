# -*- coding: utf-8 -*-
"""Class Copilot 桌面入口: 内置启动 FastAPI 服务并打开独立窗口。

- 默认优先使用 8321, 端口被占用时自动选一个空闲端口
- 窗口关闭时通知 uvicorn 退出并等待线程结束, 不留孤儿进程
- WebView2 不可用时退化为 Edge/Chrome 的 --app 窗口 (代价: 任务栏图标属于浏览器)
- 仍保留 start.bat / python app.py / uvicorn 的原始启动方式
"""
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


def find_base() -> Path:
    """返回「软件本体」目录(含 static/index.html 的那个)。

    目录约定: 项目根下并列「课程库」与「软件本体」, 所以 exe 在
    软件本体/dist/ 下时, 要往上再找一层才能找到 static。
    """
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    exe_dir = Path(sys.executable).resolve().parent
    for p in (exe_dir, exe_dir.parent):
        if (p / "static" / "index.html").is_file():
            return p
    for p in sorted(exe_dir.parent.iterdir()) if exe_dir.parent.is_dir() else []:
        if p.is_dir() and (p / "static" / "index.html").is_file():
            return p
    return exe_dir


def data_root(appdir: Path) -> Path:
    """数据根 = 项目根。「软件本体」在项目根下时, 取它的上一级。"""
    if appdir.name == "软件本体" and appdir.parent.is_dir():
        return appdir.parent
    return appdir


def pick_port(preferred: int = 8321) -> int:
    """优先 preferred; 被占用则让系统分配空闲端口。"""
    for port in (preferred, 0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("找不到可用端口")


def wait_port(port: int, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def notify_error(msg: str):
    base = find_base()
    try:
        (base / ".cache").mkdir(parents=True, exist_ok=True)
        (base / ".cache" / "desktop.log").write_text(msg, encoding="utf-8")
    except OSError:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, "Class Copilot", 0x10)
    except Exception:
        pass


def log_crash(exc: BaseException):
    """打包后的窗口没有控制台, 把未处理异常写到日志便于排查。"""
    try:
        base = find_base()
        (base / ".cache").mkdir(parents=True, exist_ok=True)
        import traceback
        with open(base / ".cache" / "desktop.log", "a", encoding="utf-8") as f:
            f.write("".join(traceback.format_exception(exc)))
    except Exception:
        pass


def icon_path(base: Path):
    for p in (base / "assets" / "class-copilot.ico",
              Path(getattr(sys, "_MEIPASS", "")) / "assets" / "class-copilot.ico"):
        if p.is_file():
            return str(p)
    return None


def browser_fallback(url: str) -> int:
    """没有 WebView2 时的兜底: 用 Edge/Chrome 的应用窗口, 无地址栏。"""
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    for exe in (pf86 / "Microsoft/Edge/Application/msedge.exe",
                pf / "Microsoft/Edge/Application/msedge.exe",
                pf / "Google/Chrome/Application/chrome.exe",
                pf86 / "Google/Chrome/Application/chrome.exe"):
        if exe.is_file():
            subprocess.Popen([str(exe), f"--app={url}", "--window-size=1280,860"]).wait()
            return 0
    webbrowser.open(url)
    return 0


def main() -> int:
    base = find_base()          # 软件本体目录
    root = data_root(base)      # 数据根(项目根)
    os.environ["CC_BASE"] = str(root)
    os.chdir(root)
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    # 窗口模式没有控制台: uvicorn 的日志格式化会访问 sys.stdout.isatty(),
    # 这里补一个文件流, 顺便把运行日志留在 .cache/runtime.log
    if sys.stdout is None or sys.stderr is None:
        try:
            (base / ".cache").mkdir(parents=True, exist_ok=True)
            _log = open(base / ".cache" / "runtime.log", "a", encoding="utf-8", buffering=1)
        except OSError:
            _log = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or _log
        sys.stderr = sys.stderr or _log

    import uvicorn
    from app import app

    port = pick_port(int(os.environ.get("CC_PORT", "8321")))
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    th = threading.Thread(target=server.run, name="class-copilot-server", daemon=True)
    th.start()
    if not wait_port(port):
        server.should_exit = True
        notify_error("后端未能启动，请检查 app.py 或端口配置。")
        return 1

    try:
        import webview
    except Exception as e:
        notify_error(f"pywebview 不可用，改用浏览器窗口：{e}")
        try:
            return browser_fallback(url)
        finally:
            server.should_exit = True
            th.join(8)

    try:
        webview.create_window("Class Copilot", url, width=1280, height=860, min_size=(900, 600))
        icon = icon_path(base)
        if icon:
            webview.start(icon=icon)
        else:
            webview.start()
    except Exception as e:
        notify_error(f"WebView2 窗口启动失败，改用浏览器窗口：{e}")
        try:
            return browser_fallback(url)
        except Exception as e2:
            notify_error(f"窗口启动失败：{e2}")
            return 1
    finally:
        server.should_exit = True
        th.join(8)
        if th.is_alive():
            server.force_exit = True
            th.join(3)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException as e:
        log_crash(e)
        raise
