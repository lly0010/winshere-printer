"""
Winshere Printer — 内网共享打印 Web 服务

在接了打印机的 Windows 电脑上运行本程序, 内网中的任意设备
(安卓 / iOS / Mac / Windows) 只需用浏览器打开本机地址, 上传文件即可打印。
客户端无需安装任何驱动或软件。
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import tempfile
import uuid

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

import printing

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 单文件最大 100MB

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "winshere_printer")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def local_ip() -> str:
    """获取本机在内网中的 IP 地址 (用于展示访问地址)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:  # noqa: BLE001
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


@app.route("/")
def index():
    printers = printing.list_printers()
    return render_template(
        "index.html",
        printers=printers,
        allowed=sorted(e.lstrip(".") for e in printing.ALLOWED_EXTS),
        host_ip=local_ip(),
    )


@app.route("/api/printers")
def api_printers():
    printers = printing.list_printers()
    return jsonify(
        printers=[{"name": p.name, "default": p.is_default} for p in printers]
    )


TRUTHY = ("1", "true", "on", "yes")


def _read_options(form) -> dict:
    """从表单解析并清洗打印/预览选项 (打印与预览共用)。"""
    try:
        copies = int(form.get("copies", "1"))
    except ValueError:
        copies = 1
    scale_percent = printing.clamp_scale(form.get("scale", "100"))
    paper = form.get("paper", "A4")
    if paper not in printing.PAPER_CHOICES:
        paper = "A4"
    # 页面范围只允许数字/逗号/连字符, 防注入
    pages = form.get("pages", "").strip().replace(" ", "")
    if not re.fullmatch(r"[0-9,\-]*", pages):
        pages = ""
    return {
        "printer": form.get("printer") or None,
        "copies": copies,
        "color": form.get("color", "auto"),
        "duplex": form.get("duplex", "false").lower() in TRUTHY,
        "scale_percent": scale_percent,
        "paper": paper,
        "pages": pages,
        "auto_rotate": form.get("auto_rotate", "true").lower() in TRUTHY,
        "auto_center": form.get("auto_center", "true").lower() in TRUTHY,
    }


@app.route("/api/print", methods=["POST"])
def api_print():
    f = request.files.get("file")
    if f is None or f.filename == "":
        return jsonify(ok=False, error="未选择文件"), 400

    filename = secure_filename(f.filename) or "upload"
    if not printing.is_allowed(filename):
        return jsonify(
            ok=False,
            error=f"不支持的文件类型: {os.path.splitext(filename)[1] or '(无扩展名)'}",
        ), 400

    opts = _read_options(request.form)

    # 落盘到临时目录, 用唯一前缀避免冲突
    safe_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{filename}")
    f.save(safe_path)

    try:
        msg = printing.print_file(safe_path, **opts)
        return jsonify(ok=True, message=msg)
    except printing.PrintError as exc:
        return jsonify(ok=False, error=str(exc)), 500
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=f"未知错误: {exc}"), 500
    finally:
        try:
            os.remove(safe_path)
        except OSError:
            pass


@app.route("/api/preview", methods=["POST"])
def api_preview():
    """返回按当前设置排版后的 PDF, 让网页预览与实际打印一致。

    仅对 PDF 生效 (需 pypdf); 其它类型返回 reason=raw, 由前端做客户端预览。
    """
    f = request.files.get("file")
    if f is None or f.filename == "":
        return jsonify(ok=False, error="未选择文件"), 400

    filename = secure_filename(f.filename) or "upload"
    ext = os.path.splitext(filename)[1].lower()
    if ext != ".pdf" or not printing.HAVE_PYPDF:
        return jsonify(ok=False, reason="raw"), 200

    opts = _read_options(request.form)

    src = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{filename}")
    dst = src + ".preview.pdf"
    f.save(src)
    try:
        printing.normalize_pdf(
            src, dst,
            paper=opts["paper"],
            auto_rotate=opts["auto_rotate"],
            auto_center=opts["auto_center"],
            scale_percent=opts["scale_percent"],
            pages=opts["pages"],
        )
        with open(dst, "rb") as fh:
            data = fh.read()
        return Response(data, mimetype="application/pdf")
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=f"预览生成失败: {exc}"), 500
    finally:
        for p in (src, dst):
            try:
                os.remove(p)
            except OSError:
                pass


def print_startup_banner(host: str, port: int) -> None:
    ip = local_ip()
    url = f"http://{ip}:{port}"
    line = "=" * 52
    print("\n" + line)
    print("  Winshere Printer 内网共享打印服务已启动")
    print(line)
    print(f"  本机访问 : http://127.0.0.1:{port}")
    print(f"  内网访问 : {url}")
    print("  内网中的手机/平板/电脑用浏览器打开上面的「内网访问」地址即可。")
    print(line)

    # 控制台二维码, 手机扫码直达 (qrcode 可选)
    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        print("  扫码直达:")
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001
        pass
    print(flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="内网共享打印 Web 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8631, help="监听端口 (默认 8631)")
    args = parser.parse_args()

    print_startup_banner(args.host, args.port)
    # threaded=True 让多个设备可并发上传
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
