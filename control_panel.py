"""
Winshere Printer 控制面板 (桌面 GUI)

功能:
  - 启动 / 停止 内网共享打印 Web 服务
  - 设置自定义监听端口 (记忆到 config.json)
  - 显示内网访问地址, 一键在浏览器打开 / 复制
  - 查看服务运行日志
  - 最小化到系统托盘 (需要 pystray + Pillow, 缺失时回退为普通最小化)
  - 开机自动启动 (写入 Windows 注册表 Run 项)
  - 静默启动 (--silent): 启动即隐藏到托盘并自动开启打印服务

命令行参数:
  --silent     静默启动: 隐藏窗口到托盘 + 自动启动打印服务 (开机启动使用此参数)
  --minimized  启动后直接最小化到托盘
"""

from __future__ import annotations

import argparse
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
APP_REG_NAME = "WinsherePrinter"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

IS_WINDOWS = sys.platform.startswith("win")

# ---- 可选依赖: 系统托盘 ------------------------------------------------------
try:
    import pystray  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore

    HAVE_TRAY = True
except Exception:  # noqa: BLE001
    HAVE_TRAY = False

# ---- 可选依赖: Windows 注册表 (开机启动) -------------------------------------
try:
    import winreg  # type: ignore

    HAVE_WINREG = True
except Exception:  # noqa: BLE001
    HAVE_WINREG = False


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


# ---- 开机自启 (Windows 注册表 Run 项) ---------------------------------------
def _pythonw_path() -> str:
    """优先用无控制台的 pythonw.exe 启动, 避免开机弹黑窗。"""
    base = os.path.dirname(sys.executable)
    cand = os.path.join(base, "pythonw.exe")
    return cand if os.path.isfile(cand) else sys.executable


def autostart_command() -> str:
    return f'"{_pythonw_path()}" "{os.path.join(HERE, "control_panel.py")}" --silent'


def is_autostart_enabled() -> bool:
    if not (IS_WINDOWS and HAVE_WINREG):
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_REG_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enable: bool) -> tuple[bool, str]:
    """开启/关闭开机自启, 返回 (是否成功, 提示信息)。"""
    if not (IS_WINDOWS and HAVE_WINREG):
        return False, "开机自启仅支持 Windows。"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            if enable:
                winreg.SetValueEx(k, APP_REG_NAME, 0, winreg.REG_SZ, autostart_command())
                return True, "已设置开机自动启动 (静默)。"
            try:
                winreg.DeleteValue(k, APP_REG_NAME)
            except FileNotFoundError:
                pass
            return True, "已取消开机自动启动。"
    except OSError as exc:
        return False, f"写入注册表失败: {exc}"


def make_tray_image():
    """生成一个简单的打印机图标 (PIL Image)。"""
    img = Image.new("RGBA", (64, 64), (37, 99, 235, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 28, 54, 46], fill=(255, 255, 255, 255))   # 机身
    d.rectangle([18, 14, 46, 28], fill=(255, 255, 255, 255))   # 顶部进纸
    d.rectangle([18, 42, 46, 56], fill=(255, 255, 255, 255))   # 出纸
    d.rectangle([18, 42, 46, 56], outline=(37, 99, 235, 255))
    d.ellipse([44, 32, 49, 37], fill=(22, 163, 74, 255))       # 指示灯
    return img


class ControlPanel:
    def __init__(self, root: tk.Tk, silent: bool = False, minimized: bool = False) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.tray = None
        self._quitting = False
        self._hint_shown = False

        cfg = load_config()
        self.port = tk.StringVar(value=str(cfg.get("port", DEFAULT_PORT)))
        self.status = tk.StringVar(value="● 已停止")
        self.var_autostart = tk.BooleanVar(value=is_autostart_enabled())
        self.var_tray = tk.BooleanVar(value=bool(cfg.get("minimize_to_tray", True)))
        self.var_autorun = tk.BooleanVar(value=bool(cfg.get("auto_start_service", False)))

        root.title("Winshere Printer 控制面板")
        root.geometry("580x520")
        root.minsize(500, 460)

        self._build_ui()
        self._setup_tray()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.after(150, self._drain_log)

        # 静默启动: 隐藏到托盘 + 自动开启服务
        if silent:
            self.var_autorun.set(True)
            self.root.after(200, self.start)
            self.root.after(400, self.hide_to_tray)
        else:
            if self.var_autorun.get():
                self.root.after(200, self.start)
            if minimized:
                self.root.after(400, self.hide_to_tray)

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

        # 设置区
        cfg = ttk.LabelFrame(self.root, text="设置")
        cfg.pack(fill="x", **pad)
        row = ttk.Frame(cfg)
        row.pack(fill="x", padx=10, pady=8)
        ttk.Label(row, text="监听端口:").pack(side="left")
        self.port_entry = ttk.Entry(row, textvariable=self.port, width=8)
        self.port_entry.pack(side="left", padx=8)
        ttk.Label(row, text="(1-65535, 默认 8631)").pack(side="left")

        opts = ttk.Frame(cfg)
        opts.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Checkbutton(
            opts, text="开机自动启动 (静默)", variable=self.var_autostart,
            command=self.toggle_autostart,
        ).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Checkbutton(
            opts, text="启动面板时自动开启服务", variable=self.var_autorun,
            command=self._save_options,
        ).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Checkbutton(
            opts, text="最小化 / 关闭时隐藏到系统托盘", variable=self.var_tray,
            command=self._save_options,
        ).grid(row=2, column=0, sticky="w", pady=2)
        if not HAVE_TRAY:
            ttk.Label(
                opts, text="(未安装 pystray/Pillow, 托盘不可用, 将普通最小化)",
                foreground="#9ca3af",
            ).grid(row=3, column=0, sticky="w")

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

    # ---- 托盘 --------------------------------------------------------------
    def _setup_tray(self) -> None:
        if not HAVE_TRAY:
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示面板", self._tray_show, default=True),
            pystray.MenuItem("启动服务", self._tray_start),
            pystray.MenuItem("停止服务", self._tray_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_quit),
        )
        try:
            self.tray = pystray.Icon(
                "winshere_printer", make_tray_image(), "Winshere Printer", menu
            )
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:  # noqa: BLE001
            self.tray = None

    # 托盘菜单回调在托盘线程触发, 需用 after 切回 Tk 主线程
    def _tray_show(self, *_):
        self.root.after(0, self.show_window)

    def _tray_start(self, *_):
        self.root.after(0, self.start)

    def _tray_stop(self, *_):
        self.root.after(0, self.stop)

    def _tray_quit(self, *_):
        self.root.after(0, self.real_quit)

    def hide_to_tray(self) -> None:
        if self.tray is not None:
            self.root.withdraw()
            if not self._hint_shown:
                self._append_log("[控制面板] 已隐藏到系统托盘, 双击托盘图标可恢复。\n")
                self._hint_shown = True
        else:
            self.root.iconify()

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _on_unmap(self, event) -> None:
        # 仅当主窗口被最小化且启用了"隐藏到托盘"时, 收进托盘
        if event.widget is self.root and self.var_tray.get() and self.tray is not None:
            if self.root.state() == "iconic":
                self.root.after(10, self.hide_to_tray)

    # ---- 设置持久化 ---------------------------------------------------------
    def _save_options(self) -> None:
        cfg = load_config()
        cfg.update(
            {
                "port": self._current_port_or_default(),
                "auto_start_service": self.var_autorun.get(),
                "minimize_to_tray": self.var_tray.get(),
            }
        )
        save_config(cfg)

    def _current_port_or_default(self) -> int:
        try:
            return int(self.port.get().strip())
        except ValueError:
            return DEFAULT_PORT

    def toggle_autostart(self) -> None:
        enable = self.var_autostart.get()
        ok, msg = set_autostart(enable)
        self._append_log(f"[控制面板] {msg}\n")
        if not ok:
            # 失败则回滚勾选状态
            self.var_autostart.set(is_autostart_enabled())
            messagebox.showwarning("开机自启", msg)

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

        self._save_options()

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        creationflags = 0
        if IS_WINDOWS:
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
        if self.tray is not None:
            self.tray.title = f"Winshere Printer ({'运行中' if running else '已停止'})"

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
        # 启用托盘时, 关闭按钮收进托盘而非退出
        if self.var_tray.get() and self.tray is not None and not self._quitting:
            self.hide_to_tray()
            return
        self.real_quit()

    def real_quit(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not self._quitting and not messagebox.askokcancel(
                "退出", "服务正在运行, 退出将停止服务。确定退出?"
            ):
                return
            self.stop()
        self._quitting = True
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Winshere Printer 控制面板")
    parser.add_argument("--silent", action="store_true",
                        help="静默启动: 隐藏到托盘并自动开启服务 (开机自启使用)")
    parser.add_argument("--minimized", action="store_true",
                        help="启动后最小化到托盘")
    args = parser.parse_args()

    root = tk.Tk()
    try:
        from ctypes import windll  # type: ignore

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    ControlPanel(root, silent=args.silent, minimized=args.minimized)
    root.mainloop()


if __name__ == "__main__":
    main()
