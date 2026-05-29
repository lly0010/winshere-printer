"""
Windows 打印后端。

设计目标:把内网设备上传的文件真正打印到 Windows 上的物理/虚拟打印机。
- PDF / 图片: 使用便携版 SumatraPDF 静默打印 (不弹窗、可指定打印机)。
- 其它文档 (Office / txt 等): 回退到 Windows ShellExecute 的 "printto" 动作,
  借助系统已关联的应用程序打印。

在非 Windows 平台 (例如开发机) 导入 pywin32 会失败,这里做了优雅降级,
以便代码在任何平台都能 import 并运行单元测试。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass

# ---- 平台相关依赖的优雅降级 -------------------------------------------------
IS_WINDOWS = sys.platform.startswith("win")

# pypdf 用于 PDF 规范化(自动旋转 / 自动居中 / 按纸张缩放); 缺失时跳过规范化
try:
    from pypdf import PdfReader, PdfWriter, Transformation  # type: ignore

    HAVE_PYPDF = True
except Exception:  # noqa: BLE001
    HAVE_PYPDF = False

try:  # pragma: no cover - 仅 Windows
    import win32api  # type: ignore
    import win32print  # type: ignore

    HAVE_PYWIN32 = True
except Exception:  # noqa: BLE001
    HAVE_PYWIN32 = False


# SumatraPDF 可静默打印 PDF 与常见图片格式
SUMATRA_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}
# 这些交给系统关联程序的 printto 动作
SHELL_PRINT_EXTS = {
    ".txt", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".csv", ".html", ".htm",
}

ALLOWED_EXTS = SUMATRA_EXTS | SHELL_PRINT_EXTS

# SumatraPDF 便携版下载地址 (64 位)。如内网无法访问外网, 可手动放置 exe。
SUMATRA_URL = (
    "https://www.sumatrapdfreader.org/dl/rel/3.5.2/"
    "SumatraPDF-3.5.2-64.exe"
)


@dataclass
class Printer:
    name: str
    is_default: bool


def _here(*parts: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)


def list_printers() -> list[Printer]:
    """枚举本机可用打印机。非 Windows 或无 pywin32 时返回空列表。"""
    if not (IS_WINDOWS and HAVE_PYWIN32):
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:  # noqa: BLE001
        default = ""
    printers: list[Printer] = []
    for p in win32print.EnumPrinters(flags):
        # EnumPrinters 返回元组, 索引 2 是打印机名称
        name = p[2]
        printers.append(Printer(name=name, is_default=(name == default)))
    # 默认打印机排在最前
    printers.sort(key=lambda x: (not x.is_default, x.name.lower()))
    return printers


def default_printer() -> str | None:
    if not (IS_WINDOWS and HAVE_PYWIN32):
        return None
    try:
        return win32print.GetDefaultPrinter()
    except Exception:  # noqa: BLE001
        return None


def find_sumatra() -> str | None:
    """定位 SumatraPDF: 先看项目目录, 再看 PATH, 再看常见安装路径。"""
    candidates = [
        _here("bin", "SumatraPDF.exe"),
        _here("SumatraPDF.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    found = shutil.which("SumatraPDF") or shutil.which("SumatraPDF.exe")
    if found:
        return found
    if IS_WINDOWS:
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            for sub in ("SumatraPDF\\SumatraPDF.exe",
                        "SumatraPDF.exe"):
                p = os.path.join(base, sub)
                if os.path.isfile(p):
                    return p
    return None


def ensure_sumatra(download: bool = True) -> str | None:
    """返回 SumatraPDF 路径; 找不到且允许下载时尝试下载便携版到 bin/。"""
    existing = find_sumatra()
    if existing:
        return existing
    if not (download and IS_WINDOWS):
        return None
    target = _here("bin", "SumatraPDF.exe")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        print(f"[printing] 正在下载 SumatraPDF 便携版 ... {SUMATRA_URL}")
        urllib.request.urlretrieve(SUMATRA_URL, target)
        return target if os.path.isfile(target) else None
    except Exception as exc:  # noqa: BLE001
        print(f"[printing] SumatraPDF 下载失败: {exc}")
        return None


def is_allowed(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in ALLOWED_EXTS


PAPER_CHOICES = ("A4", "A3", "A5", "A6", "letter", "legal")

# 缩放百分比允许范围
SCALE_MIN, SCALE_MAX = 10, 400

# 各纸张的尺寸(单位: PDF point, 1pt=1/72 英寸), 纵向 (宽, 高)
PAPER_POINTS: dict[str, tuple[float, float]] = {
    "A4": (595.2756, 841.8898),
    "A3": (841.8898, 1190.5512),
    "A5": (419.5276, 595.2756),
    "A6": (297.6378, 419.5276),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
}


def clamp_scale(percent) -> int:
    """把缩放百分比限制到合法范围, 非法时回退 100。"""
    try:
        p = int(round(float(percent)))
    except (TypeError, ValueError):
        return 100
    return max(SCALE_MIN, min(p, SCALE_MAX))


def _parse_pages(pages: str, total: int) -> list[int]:
    """解析 "1-5,8" 这样的页码范围为 0 基索引列表; 空或非法返回全部页。"""
    pages = (pages or "").strip().replace(" ", "")
    if not pages:
        return list(range(total))
    result: list[int] = []
    for part in pages.split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                start = int(a) if a else 1
                end = int(b) if b else total
            except ValueError:
                continue
            for n in range(start, end + 1):
                if 1 <= n <= total:
                    result.append(n - 1)
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total:
                result.append(n - 1)
    return result or list(range(total))


def _should_rotate(sw: float, sh: float, pw: float, ph: float) -> bool:
    """页面方向与纸张方向不一致时需要旋转 90° (自动旋转)。"""
    return (sw > sh) != (pw > ph)


def normalize_pdf(
    src: str,
    dst: str,
    paper: str = "A4",
    auto_rotate: bool = True,
    auto_center: bool = True,
    scale_percent: int = 100,
    pages: str = "",
) -> None:
    """把 PDF 每页重排到指定纸张上, 应用自动旋转/自动居中/百分比缩放, 输出到 dst。

    scale_percent: 缩放百分比, 100 表示实际大小。
    pages: 页码范围(如 "1-5,8"), 为空表示全部; 输出仅含所选页。
    生成的每页尺寸都精确等于纸张大小, 内容已按设置摆放; 之后用 SumatraPDF
    以 noscale 1:1 打印即可, 不会再被二次旋转或缩放。
    """
    pw, ph = PAPER_POINTS.get(paper, PAPER_POINTS["A4"])
    scale = clamp_scale(scale_percent) / 100.0
    reader = PdfReader(src)
    writer = PdfWriter()
    indices = _parse_pages(pages, len(reader.pages))

    for idx in indices:
        page = reader.pages[idx]
        # 先把页面自带的 /Rotate 烘焙进内容, 这样 mediabox 反映的就是可视尺寸
        try:
            page.transfer_rotation_to_content()
        except Exception:  # noqa: BLE001
            pass

        box = page.mediabox
        left, bottom = float(box.left), float(box.bottom)
        sw, sh = float(box.width), float(box.height)

        rotate = auto_rotate and _should_rotate(sw, sh, pw, ph)
        ew, eh = (sh, sw) if rotate else (sw, sh)  # 旋转后的可视宽高
        sew, seh = ew * scale, eh * scale

        if auto_center:
            tx, ty = (pw - sew) / 2.0, (ph - seh) / 2.0
        else:
            tx, ty = 0.0, ph - seh  # 不居中则左上对齐

        op = Transformation().translate(-left, -bottom)
        if rotate:
            # 旋转 90° 后内容落到第一象限, 占据 (sh, sw)
            op = op.rotate(90).translate(sh, 0)
        op = op.scale(scale).translate(tx, ty)

        new_page = writer.add_blank_page(width=pw, height=ph)
        new_page.merge_transformed_page(page, op)

    with open(dst, "wb") as fh:
        writer.write(fh)


def _build_sumatra_settings(
    color: str,
    duplex: bool,
    scale: str = "noscale",
    paper: str = "A4",
    pages: str = "",
) -> str:
    """构造 SumatraPDF -print-settings 字符串。

    scale: noscale=100% 实际大小 / fit=缩放铺满 / shrink=过大才缩小。
    paper: 纸张大小, 默认 A4。
    pages: 页面范围, 如 "1-5,8"; 为空表示全部。
    SumatraPDF 打印时默认会按页面方向"自动旋转"并"自动居中", 无需额外参数,
    因此横向 PDF 会自动转正并铺在 A4 上居中打印。
    """
    tokens: list[str] = []
    pages = (pages or "").strip().replace(" ", "")
    if pages:
        tokens.append(pages)  # 如 "1-5,8", 各段以逗号并入整体设置
    tokens.append(scale)
    if paper:
        tokens.append(f"paper={paper}")
    if color == "mono":
        tokens.append("monochrome")
    elif color == "color":
        tokens.append("color")
    if duplex:
        tokens.append("duplex")
    else:
        tokens.append("simplex")
    return ",".join(tokens)


class PrintError(Exception):
    pass


def print_file(
    path: str,
    printer: str | None = None,
    copies: int = 1,
    color: str = "auto",
    duplex: bool = False,
    scale_percent: int = 100,
    paper: str = "A4",
    pages: str = "",
    auto_rotate: bool = True,
    auto_center: bool = True,
) -> str:
    """打印一个文件, 返回所用打印方式的描述。失败抛 PrintError。"""
    if not os.path.isfile(path):
        raise PrintError(f"文件不存在: {path}")
    if not (IS_WINDOWS and HAVE_PYWIN32):
        raise PrintError("当前环境不是 Windows 或缺少 pywin32, 无法打印。")

    copies = max(1, min(int(copies), 99))
    scale_percent = clamp_scale(scale_percent)
    ext = os.path.splitext(path)[1].lower()
    target_printer = printer or default_printer()
    if not target_printer:
        raise PrintError("未找到可用的打印机。")

    if ext in SUMATRA_EXTS:
        sumatra = ensure_sumatra()
        if not sumatra:
            raise PrintError(
                "需要 SumatraPDF 才能静默打印 PDF/图片, 但未找到也无法下载。"
                "请手动下载便携版放到程序目录的 bin/SumatraPDF.exe。"
            )
        if paper not in PAPER_CHOICES:
            paper = "A4"

        # PDF: 用 pypdf 规范化, 把自动旋转/自动居中/百分比缩放/页码烘焙进页面,
        # 之后用 noscale 让 SumatraPDF 1:1 打印, 避免二次旋转/缩放。
        print_path = path
        normalized: str | None = None
        # 图片用 fit 铺满纸张; 无 pypdf 时 PDF 回退为按比例(noscale≈实际大小)
        print_scale = "fit" if ext != ".pdf" else "noscale"
        print_pages = pages
        if ext == ".pdf" and HAVE_PYPDF:
            try:
                fd, normalized = tempfile.mkstemp(
                    suffix=".pdf", dir=os.path.dirname(path) or None
                )
                os.close(fd)
                normalize_pdf(
                    path, normalized, paper=paper,
                    auto_rotate=auto_rotate, auto_center=auto_center,
                    scale_percent=scale_percent, pages=pages,
                )
                print_path = normalized
                print_scale = "noscale"  # 几何已烘焙, 不再缩放
                print_pages = ""          # 页码已在规范化时筛选
            except Exception:  # noqa: BLE001 - 规范化失败则回退原文件
                if normalized and os.path.exists(normalized):
                    os.remove(normalized)
                normalized = None
                print_path = path

        settings = _build_sumatra_settings(
            color, duplex, scale=print_scale, paper=paper, pages=print_pages
        )
        try:
            # SumatraPDF 不直接支持份数, 通过多次提交实现
            for _ in range(copies):
                cmd = [
                    sumatra,
                    "-print-to", target_printer,
                    "-print-settings", settings,
                    "-silent",
                    "-exit-when-done",
                    print_path,
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise PrintError(
                        f"SumatraPDF 打印失败 (code {proc.returncode}): "
                        f"{proc.stderr or proc.stdout}"
                    )
        finally:
            if normalized and os.path.exists(normalized):
                try:
                    os.remove(normalized)
                except OSError:
                    pass
        return f"已通过 SumatraPDF 发送到打印机 [{target_printer}] x{copies} 份"

    # 其它文档: 用系统关联程序的 printto 动作
    for _ in range(copies):
        try:
            # "printto" 动作的参数是目标打印机名称
            win32api.ShellExecute(0, "printto", path, f'"{target_printer}"', ".", 0)
        except Exception as exc:  # noqa: BLE001
            # 部分类型只支持 "print" (打到默认打印机)
            try:
                win32api.ShellExecute(0, "print", path, None, ".", 0)
            except Exception as exc2:  # noqa: BLE001
                raise PrintError(f"系统打印失败: {exc2}") from exc
    return f"已通过系统关联程序发送到打印机 [{target_printer}] x{copies} 份"
