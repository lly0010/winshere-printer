"""
Winshere Printer 控制面板 (桌面 GUI)

提供图形界面来:
  - 启动 / 停止 内网共享打印 Web 服务
  - 设置自定义监听端口
  - 显示内网访问地址, 一键在浏览器打开
  - 查看服务运行日志

使用 Python 自带的 Tkinter, 无需额外安装依赖。
把 Flask 服务 (app.py) 作为子进程启停, 关闭面板会自动停止服务。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, scrolledtext, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
APP_PATH = os.path.join(HERE, "app.py")
DEFAULT_PORT = 8631


def local_ip() -> str:
    """获取本机在内网中的 IP 地址。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:  # noqa: BLE001
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


class ControlPanel:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.port = tk.StringVar(value=str(load_config().get("port", DEFAULT_PORT)))
        self.status = tk.StringVar(value="● 已停止")

        root.title("Winshere Printer 控制面板")
        root.geometry("560x460")
        root.minsize(480, 400)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(150, self._drain_log)

    # ---- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="🖨️ 内网共享打印服务", font=("Microsoft YaHei", 14, "bold")).pack(
            side="left"
        )
        self.status_label = ttk.Label(
            top, textvariable=self.status, foreground="#dc2626",
            font=("Microsoft YaHei", 11, "bold"),
        )
        self.status_label.pack(side="right")

        # 端口设置
        cfg = ttk.LabelFrame(self.root, text="设置")
        cfg.pack(fill="x", **pad)
        row = ttk.Frame(cfg)
        row.pack(fill="x", padx=10, pady=8)
        ttk.Label(row, text="监听端口:").pack(side="left")
        self.port_entry = ttk.Entry(row, textvariable=self.port, width=8)
        self.port_entry.pack(side="left", padx=8)
        ttk.Label(row, text="(1-65535, 默认 8631)").pack(side="left")

        # 控制按钮
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="▶ 启动服务", command=self.start)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=4)
        self.stop_btn = ttk.Button(btns, text="■ 停止服务", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", expand=True, fill="x", padx=4)
        self.open_btn = ttk.Button(
            btns, text="🌐 浏览器打开", command=self.open_browser, state="disabled"
        )
        self.open_btn.pack(side="left", expand=True, fill="x", padx=4)

        # 访问地址
        addr = ttk.Frame(self.root)
        addr.pack(fill="x", **pad)
        ttk.Label(addr, text="内网访问地址:").pack(side="left")
        self.url_var = tk.StringVar(value="(启动后显示)")
        url_entry = ttk.Entry(addr, textvariable=self.url_var, state="readonly")
        url_entry.pack(side="left", expand=True, fill="x", padx=8)
        ttk.Button(addr, text="复制", command=self.copy_url).pack(side="left")

        # 日志
        logf = ttk.LabelFrame(self.root, text="运行日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(
            logf, height=10, font=("Consolas", 9), state="disabled", wrap="word"
        )
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

    # ---- 业务逻辑 -----------------------------------------------------------
    def _validate_port(self) -> int | None:
        try:
            p = int(self.port.get().strip())
        except ValueError:
            messagebox.showerror("端口错误", "端口必须是数字。")
            return None
        if not (1 <= p <= 65535):
            messagebox.showerror("端口错误", "端口范围应为 1-65535。")
            return None
        return p

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        port = self._validate_port()
        if port is None:
            return
        if not os.path.isfile(APP_PATH):
            messagebox.showerror("错误", f"找不到 app.py:\n{APP_PATH}")
            return

        save_config({"port": port})

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        creationflags = 0
        if sys.platform.startswith("win"):
            # 隐藏子进程自己的控制台窗口
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self.proc = subprocess.Popen(
                [sys.executable, APP_PATH, "--port", str(port)],
                cwd=HERE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启动失败", str(exc))
            return

        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

        url = f"http://{local_ip()}:{port}"
        self.url_var.set(url)
        self._set_running(True)
        self._append_log(f"[控制面板] 服务已启动: {url}\n")

    def _reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log_queue.put(line)
        rc = proc.wait()
        self.log_queue.put(f"\n[控制面板] 服务进程已退出 (code {rc})\n__STOPPED__")

    def _drain_log(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line.endswith("__STOPPED__"):
                    self._append_log(line.replace("__STOPPED__", ""))
                    self._set_running(False)
                else:
                    self._append_log(line)
        except queue.Empty:
            pass
        self.root.after(150, self._drain_log)

    def stop(self) -> None:
        if not (self.proc and self.proc.poll() is None):
            self._set_running(False)
            return
        self._append_log("[控制面板] 正在停止服务 ...\n")
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[控制面板] 停止出错: {exc}\n")
        self._set_running(False)

    def _set_running(self, running: bool) -> None:
        if running:
            self.status.set("● 运行中")
            self.status_label.configure(foreground="#16a34a")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.open_btn.configure(state="normal")
            self.port_entry.configure(state="disabled")
        else:
            self.status.set("● 已停止")
            self.status_label.configure(foreground="#dc2626")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.open_btn.configure(state="disabled")
            self.port_entry.configure(state="normal")

    def open_browser(self) -> None:
        url = self.url_var.get()
        if url.startswith("http"):
            webbrowser.open(url)

    def copy_url(self) -> None:
        url = self.url_var.get()
        if url.startswith("http"):
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self._append_log(f"[控制面板] 已复制地址: {url}\n")

    def on_close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askokcancel("退出", "服务正在运行, 关闭面板将停止服务。确定退出?"):
                return
            self.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        # 高 DPI 屏更清晰 (仅 Windows)
        from ctypes import windll  # type: ignore

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
