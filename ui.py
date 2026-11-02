"""SinLens desktop UI and OCR pipeline.

This file contains the OCR pipeline and native desktop interface. The Convert
to TXT button calls run() directly for the full document or selected zones.
"""

from __future__ import annotations
import argparse
import logging
import os
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import statistics
from urllib.parse import urlparse, unquote

# OpenBLAS may try to allocate one worker pool per detected CPU. On machines
# with limited commit/pagefile this can fail before OCR starts; one thread is
# sufficient for this single-worker OCR pipeline and can still be overridden
# by the caller through the environment.
for _blas_variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                       "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_blas_variable, "1")

import pymupdf
import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

LOG = logging.getLogger("indra")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def fetch_source(source: str, folder: Path) -> Path:
    parsed = urlparse(source)
    if parsed.scheme.lower() in {"http", "https"}:
        name = Path(unquote(parsed.path)).name or "remote_document"
        suffix = Path(name).suffix.lower()
        if suffix not in IMAGE_EXT | {".pdf"}:
            suffix = ".pdf" if parsed.path.lower().endswith("pdf") else ".jpg"
            name = "remote_document" + suffix
        target = folder / name
        with requests.get(source, stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            with target.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file.write(chunk)
        return target
    local = Path(source).expanduser().resolve()
    if not local.is_file():
        raise FileNotFoundError(f"Source not found: {local}")
    target = folder / local.name
    if local != target.resolve():
        shutil.copy2(local, target)
    return target


def page_images(document: Path, dpi: int):
    if document.suffix.lower() == ".pdf":
        pdf = pymupdf.open(str(document))
        try:
            for index, page in enumerate(pdf):
                pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
                yield index + 1, Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            pdf.close()
    else:
        with Image.open(document) as image:
            yield 1, ImageOps.exif_transpose(image).convert("RGB").copy()


def enhanced(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = ImageEnhance.Contrast(gray).enhance(1.4)
    return gray.filter(ImageFilter.MedianFilter(size=3)).convert("RGB")


def ocr_cli(path: Path, timeout: int) -> tuple[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # sys.executable ensures the same virtual environment is used.
    command = [sys.executable, "-m", "indra_ocr", str(path), "--format", "text"]
    process = subprocess.run(command, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=env,
                             timeout=timeout)
    if process.returncode:
        LOG.warning("OCR failed for %s (exit %s): %s", path.name,
                    process.returncode, process.stderr[-1200:])
        return "", process.stderr
    return process.stdout.strip("\ufeff\r\n "), ""


def score_text(text: str) -> int:
    # Ignore whitespace, punctuation and common CLI empty outputs.
    return sum(char.isalnum() for char in text)


def process_page(number: int, image: Image.Image, work: Path,
                 threshold: int, timeout: int) -> tuple[str, str, int]:
    page_dir = work / f"page_{number:03d}"
    page_dir.mkdir(parents=True, exist_ok=True)
    original = page_dir / "original.png"
    image.save(original)
    text, _ = ocr_cli(original, timeout)
    best = (score_text(text), text, "original", original)
    LOG.info("Page %s original: score=%s", number, best[0])

    # Only preprocess if the original OCR result is insufficient.
    if best[0] < threshold:
        for rotation in (0, 90, 180, 270):
            rotated = image.rotate(rotation, expand=True)
            for variant, candidate in (("normal", rotated), ("enhanced", enhanced(rotated))):
                path = page_dir / f"rot_{rotation}_{variant}.png"
                candidate.save(path)
                candidate_text, _ = ocr_cli(path, timeout)
                candidate_score = score_text(candidate_text)
                LOG.info("Page %s rotation=%s variant=%s score=%s",
                         number, rotation, variant, candidate_score)
                if candidate_score > best[0]:
                    best = (candidate_score, candidate_text,
                            f"rotation={rotation}, variant={variant}", path)

    score, selected_text, description, selected_path = best
    LOG.info("Page %s SELECTED %s score=%s", number, description, score)
    # Keep only the winning preprocessing image; no OCR TXT per candidate.
    winner = work / f"page_{number:03d}_selected.png"
    shutil.copy2(selected_path, winner)
    shutil.rmtree(page_dir)
    return selected_text, description, score



def parse_box(value):
    """Accept xyxy, polygon, dict and array-like Indra boxes."""
    if value is None:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, dict):
        for keys in (("x1", "y1", "x2", "y2"), ("left", "top", "right", "bottom")):
            if all(key in value for key in keys):
                return tuple(float(value[key]) for key in keys)
        return None
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) == 4 and all(isinstance(x, (float, int)) for x in value):
        return tuple(map(float, value))
    if len(value) >= 3 and all(isinstance(pt, (tuple, list)) and len(pt) >= 2 for pt in value):
        xs, ys = [float(pt[0]) for pt in value], [float(pt[1]) for pt in value]
        return min(xs), min(ys), max(xs), max(ys)
    return None


def extract_positioned_lines(ocr, path: Path):
    records = []
    for _, page in ocr.convert(str(path)):
        for line in getattr(page, "lines", []) or []:
            text = str(getattr(line, "text", "") or "").strip()
            box = parse_box(getattr(line, "box", None))
            if text and box and box[2] > box[0] and box[3] > box[1]:
                records.append((text, *box))
    return records


def make_layout(lines, fallback: str, image_size, columns: int = 120):
    """Render OCR lines as fixed-width text while preserving page geometry.

    OCR detectors return one box per visual line (and often one box per table
    cell). Lines with the same vertical band are therefore rendered on the
    same output row, while their horizontal box positions become fixed-width
    column positions. This keeps headers, paragraphs, and multi-column text
    readable in a plain TXT file without adding table borders or HTML.
    """
    if not lines:
        LOG.warning("No bounding boxes returned: retaining CLI line breaks")
        return fallback
    width, height = image_size
    if max(line[3] for line in lines) <= 1.01 and max(line[4] for line in lines) <= 1.01:
        width, height = 1.0, 1.0
    positioned = []
    for text, x1, y1, x2, y2 in lines:
        content = re.sub(r"[\t\r\n]+", " ", str(text)).strip()
        if not content or x2 <= x1 or y2 <= y1:
            continue
        positioned.append((content, x1 / width, y1 / height,
                           x2 / width, y2 / height))
    if not positioned:
        LOG.warning("No usable positioned lines: retaining CLI line breaks")
        return fallback

    # Estimate a line's vertical spacing from OCR boxes rather than using a fixed canvas.
    heights = [max(0.001, y2-y1) for _, x1, y1, x2, y2 in positioned]
    typical_height = statistics.median(heights)
    row_tolerance = max(typical_height * 0.55, 0.004)

    # Group boxes that share a visual baseline so multi-column text on one row
    # stays together instead of colliding or being pushed onto different rows.
    grouped = []
    for item in sorted(positioned, key=lambda entry: ((entry[2] + entry[4]) / 2, entry[1])):
        _, x1, y1, x2, y2 = item
        center = (y1 + y2) / 2
        height_now = max(0.001, y2 - y1)
        match = None
        for group in reversed(grouped[-6:]):
            overlap = min(y2, group["bottom"]) - max(y1, group["top"])
            close_center = abs(center - group["center"]) <= max(
                row_tolerance, 0.55 * max(height_now, group["height"])
            )
            if overlap >= 0.2 * min(height_now, group["height"]) or close_center:
                match = group
                break
        if match is None:
            grouped.append({
                "center": center,
                "top": y1,
                "bottom": y2,
                "height": height_now,
                "items": [item],
            })
        else:
            match["items"].append(item)
            match["top"] = min(match["top"], y1)
            match["bottom"] = max(match["bottom"], y2)
            match["center"] = (match["top"] + match["bottom"]) / 2
            match["height"] = max(match["height"], height_now)

    grouped.sort(key=lambda group: group["center"])
    multi_cell_rows = sum(1 for group in grouped if len(group["items"]) >= 2)
    if multi_cell_rows:
        LOG.info("Layout preserved %d multi-cell row(s) across %d OCR row(s)",
                 multi_cell_rows, len(grouped))

    rows = max(35, min(240, round(1.0 / max(typical_height * 1.35, 0.006))))
    canvas = [[" "] * columns for _ in range(rows)]
    collisions = 0
    last_target = -1

    def ensure_width(required: int) -> None:
        if required <= len(canvas[0]):
            return
        extra = required - len(canvas[0])
        for row in canvas:
            row.extend([" "] * extra)

    for group in grouped:
        target = max(0, min(rows - 1, round(group["center"] * (rows - 1))))
        if target <= last_target:
            target = min(rows - 1, last_target + 1)
        last_target = target
        cursor = -1
        for text, x1, y1, x2, y2 in sorted(group["items"], key=lambda item: item[1]):
            is_arabic = any("\u0600" <= ch <= "\u08ff" for ch in text)
            left = round(x1 * (columns - 1))
            right = round(x2 * (columns - 1))
            start = right - len(text) + 1 if is_arabic else left
            start = max(0, start)
            if start <= cursor:
                collisions += 1
                start = cursor + 2
            needed = start + len(text)
            ensure_width(needed)
            canvas[target][start:needed] = list(text)
            cursor = needed
    if collisions:
        LOG.warning("%d layout collisions; text was shifted right to preserve it", collisions)
    output = "\n".join("".join(row).rstrip() for row in canvas).rstrip()
    return output if output.strip() else fallback


def _run(source: str, output: str | None, dpi: int, threshold: int,
         timeout: int, keep_images: bool, columns: int = 120) -> Path:
    parsed = urlparse(source)
    name = Path(unquote(parsed.path)).stem if parsed.scheme in {"http", "https"} else Path(source).stem
    name = re.sub(r'[^\w.\-]+', '_', name, flags=re.UNICODE) or "document"
    destination = Path(output).expanduser().resolve() if output else Path.cwd() / name
    destination.mkdir(parents=True, exist_ok=True)

    LOG.setLevel(logging.INFO)
    for handler in LOG.handlers[:]:
        LOG.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    for handler in (logging.StreamHandler(sys.stdout),
                    logging.FileHandler(destination / "ocr.log", encoding="utf-8")):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)

    document = fetch_source(source, destination)
    LOG.info("Source saved: %s", document)
    selected_dir = destination / "preprocessed" if keep_images else None
    if selected_dir:
        selected_dir.mkdir(exist_ok=True)

    pages_text = []
    ocr_model = None
    with tempfile.TemporaryDirectory(prefix="indra_candidates_") as temp:
        work = Path(temp)
        for page_number, image in page_images(document, dpi):
            text, description, score = process_page(page_number, image, work,
                                                     threshold, timeout)
            if not text:
                LOG.warning("Page %s: no OCR text recognized", page_number)
            # Get geometry only for the winning rotation. CLI result remains the
            # authoritative fallback if the Python API cannot return line boxes.
            selected = work / f"page_{page_number:03d}_selected.png"
            structured = text
            if text:
                try:
                    if ocr_model is None:
                        import indra_ocr
                        ocr_model = indra_ocr.load(threads=4, mode="ocr")
                    records = extract_positioned_lines(ocr_model, selected)
                    LOG.info("Page %s positioned lines: %d", page_number, len(records))
                    with Image.open(selected) as selected_image:
                        structured = make_layout(records, text, selected_image.size,
                                                 columns=columns)
                except Exception as exc:
                    LOG.warning("Page %s layout extraction unavailable (%s); using CLI text",
                                page_number, exc)
            pages_text.append(structured)
            if selected_dir:
                shutil.copy2(work / f"page_{page_number:03d}_selected.png",
                             selected_dir / f"page_{page_number:03d}_selected.png")

    # One TXT only. Keep line breaks returned by Indra OCR.
    result = destination / f"{name}.txt"
    result.write_text("\n\n\f\n\n".join(pages_text).strip() + "\n", encoding="utf-8-sig")
    LOG.info("TXT saved: %s | total characters=%s", result,
             sum(len(item) for item in pages_text))
    for handler in LOG.handlers[:]:
        handler.flush()
        LOG.removeHandler(handler)
        handler.close()
    return result


def _close_log_handlers() -> None:
    for handler in LOG.handlers[:]:
        try:
            handler.flush()
        finally:
            LOG.removeHandler(handler)
            handler.close()


def run(*args, **kwargs) -> Path:
    """Run OCR and always release the Windows log file handle."""
    try:
        return _run(*args, **kwargs)
    finally:
        _close_log_handlers()


import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import ImageTk

ARABIC_RE = re.compile(r"[\u0600-\u06ff]")


class ClearReadDesktop(tk.Tk):
    BG = "#eef0f3"
    PANEL = "#ffffff"
    INK = "#202124"
    MUTED = "#70757a"
    LINE = "#e1e4e8"
    BLUE = "#1a73e8"
    BLUE_SOFT = "#e8f0fe"
    TEAL = "#168277"
    OUTPUT_BG = "#f7fbfa"
    OUTPUT_LINE = "#d8e7e4"
    INPUT_BG = "#eef0f3"
    TEXT_BG = "#ffffff"

    def __init__(self) -> None:
        super().__init__()
        self.title("SinLens")
        self.geometry("1420x900")
        self.minsize(1050, 680)
        self.configure(bg=self.BG)
        self.app_icon = self._create_lens_icon()
        self.iconphoto(True, self.app_icon)
        self.protocol("WM_DELETE_WINDOW", self.close_app)

        self.document_name = "document"
        self.source_path: Path | None = None
        self.remote_dir: Path | None = None
        self.page_image: Image.Image | None = None
        self.tk_image: ImageTk.PhotoImage | None = None
        self.display_bounds = (0, 0, 0, 0)
        self.zoom = 1.0
        self.rotation = 0
        self.is_converting = False
        self.progress_window: tk.Toplevel | None = None
        self.lines: list[dict[str, object]] = []
        self.active_index: int | None = None
        self.ocr_raw = ""
        self.ocr_lines: list[str] = []
        self.last_result_path: Path | None = None
        self.drawing: tuple[float, float] | None = None

        self._build_style()
        self._build_header()
        self._build_body()
        self._apply_dark_widget_theme()
        self.after(100, self.render_document)

    def _create_lens_icon(self) -> ImageTk.PhotoImage:
        """Create the SinLens app icon: a lens with a small crosshair."""
        image = Image.new("RGBA", (48, 48), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((6, 6, 36, 36), outline=(26, 115, 232, 255), width=5)
        draw.line((31, 31, 43, 43), fill=(26, 115, 232, 255), width=5)
        draw.line((14, 21, 28, 21), fill=(26, 115, 232, 255), width=2)
        draw.line((21, 14, 21, 28), fill=(26, 115, 232, 255), width=2)
        return ImageTk.PhotoImage(image)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TButton", padding=(10, 7), font=("Segoe UI", 9))
        style.configure("Lens.TButton", padding=(10, 7), font=("Segoe UI", 9))
        style.configure("Vertical.TScrollbar", background=self.PANEL,
                        troughcolor=self.BG, bordercolor=self.LINE,
                        arrowcolor=self.MUTED)
        style.configure("Horizontal.TScrollbar", background=self.PANEL,
                        troughcolor=self.BG, bordercolor=self.LINE,
                        arrowcolor=self.MUTED)
        style.configure("Horizontal.TProgressbar", background=self.BLUE,
                        troughcolor=self.LINE, bordercolor=self.LINE)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("Canvas.TFrame", background=self.BG)
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED,
                        font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=self.PANEL, foreground=self.INK,
                        font=("Segoe UI", 16, "bold"))
        style.configure("Heading.TLabel", background=self.PANEL, foreground=self.INK,
                        font=("Segoe UI", 12, "bold"))

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=self.PANEL, height=68, highlightbackground=self.LINE,
                          highlightthickness=1)
        header.pack(fill="x")
        header.pack_propagate(False)

        brand = tk.Frame(header, bg=self.PANEL)
        brand.pack(side="left", padx=25)
        tk.Label(brand, image=self.app_icon, bg=self.PANEL).pack(side="left", padx=(0, 8))
        tk.Label(brand, text="SinLens", bg=self.PANEL, fg=self.BLUE,
                 font=("Segoe UI", 18, "bold")).pack(side="left")
        tk.Label(brand, text="  DESKTOP OCR", bg=self.PANEL, fg="#9aa0a6",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(5, 0))

        actions = tk.Frame(header, bg=self.PANEL)
        actions.pack(side="right", padx=25)
        self.import_button = ttk.Button(actions, text="+  Import local / تحميل محلي", style="Lens.TButton",
                                        command=self.import_document)
        self.import_button.pack(side="left", padx=4)
        self.import_button.configure(text="Import document")
        self.remote_button = ttk.Button(actions, text="Import URL / رابط", style="Lens.TButton",
                                        command=self.import_remote)
        self.remote_button.pack(side="left", padx=4)
        self.remote_button.configure(text="Import URL")
        self.clear_button = ttk.Button(actions, text="Clear zones / مسح", style="Lens.TButton",
                                       state=tk.DISABLED, command=self.clear_lines)
        self.clear_button.pack(side="left", padx=4)
        self.clear_button.configure(text="Clear zones")
        self.remove_button = ttk.Button(actions, text="Remove / إزالة", style="Lens.TButton",
                                        state=tk.DISABLED, command=self.remove_document)
        self.remove_button.pack(side="left", padx=4)
        self.remove_button.configure(text="Remove document")
        self.header_convert_button = ttk.Button(
            actions, text="Convert to TXT / تحويل", style="Blue.TButton",
            state=tk.DISABLED, command=self.convert_to_txt,
        )
        self.header_convert_button.pack(side="left", padx=4)
        self.header_convert_button.configure(style="Lens.TButton")
        self.header_convert_button.configure(text="Convert to TXT")
        tk.Label(actions, text="● Local / محلي", bg=self.PANEL, fg="#188038",
                 font=("Segoe UI", 9)).pack(side="left", padx=(15, 0))

    def _build_body(self) -> None:
        body = tk.Frame(self, bg=self.BG)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1, minsize=420)
        body.grid_rowconfigure(0, weight=1)

        viewer = tk.Frame(body, bg=self.BG)
        viewer.grid(row=0, column=0, sticky="nsew")
        viewer.grid_rowconfigure(1, weight=1)
        viewer.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(viewer, bg=self.PANEL, height=78,
                           highlightbackground=self.LINE, highlightthickness=1)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        heading = tk.Frame(toolbar, bg=self.PANEL)
        heading.pack(side="left", padx=23, pady=11)
        tk.Label(heading, text="INPUT / الإدخال", bg=self.PANEL, fg="#9aa0a6",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.title_label = tk.Label(heading, text="Choose an image to begin",
                                    bg=self.PANEL, fg=self.INK,
                                    font=("Segoe UI", 14, "bold"))
        self.title_label.pack(anchor="w", pady=(3, 0))
        self.meta_label = tk.Label(heading, text="Import a file or URL to begin / حمّل ملفاً أو رابطاً للبدء",
                                   bg=self.PANEL, fg=self.MUTED,
                                   font=("Segoe UI", 9))
        self.meta_label.pack(anchor="w")
        heading.winfo_children()[0].configure(text="SOURCE DOCUMENT")
        self.meta_label.configure(text="Import a local file or paste a remote URL.")

        toolbar_actions = tk.Frame(toolbar, bg=self.PANEL)
        toolbar_actions.pack(side="right", padx=22)
        self.status_label = tk.Label(toolbar_actions, text="0 selected / 0 محددة",
                                     bg=self.PANEL, fg=self.TEAL,
                                     font=("Segoe UI", 9, "bold"))
        self.status_label.pack(side="right", padx=(14, 0))

        canvas_frame = tk.Frame(viewer, bg=self.BG)
        canvas_frame.grid(row=1, column=0, sticky="nsew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(canvas_frame, bg=self.BG, highlightthickness=0,
                                cursor="crosshair")
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=18, pady=(18, 0))
        yscroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        xscroll = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        yscroll.grid(row=0, column=1, sticky="ns", pady=(18, 0))
        xscroll.grid(row=1, column=0, sticky="ew", padx=18)
        self.canvas.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.canvas.bind("<ButtonPress-1>", self.start_selection)
        self.canvas.bind("<B1-Motion>", self.update_selection)
        self.canvas.bind("<ButtonRelease-1>", self.finish_selection)
        self.canvas.bind("<Configure>", lambda _event: self.render_document())

        controls = tk.Frame(viewer, bg=self.PANEL, height=60,
                            highlightbackground=self.LINE, highlightthickness=1)
        controls.grid(row=2, column=0, sticky="ew")
        controls.grid_propagate(False)
        tk.Label(controls, text="TEXT / النص", bg=self.BLUE_SOFT, fg="#185abc",
                 padx=13, pady=7, font=("Segoe UI", 9, "bold")).pack(side="left", padx=20, pady=12)
        tk.Label(controls, text="1) Import  →  2) Select a zone  →  3) Convert to TXT",
                 bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9)).pack(side="left")
        zoom = tk.Frame(controls, bg=self.PANEL)
        zoom.pack(side="right", padx=18)
        controls.winfo_children()[0].configure(text="DOCUMENT")
        controls.winfo_children()[1].configure(
            text="1. Import document  →  2. Draw readable area  →  3. Convert to TXT"
        )
        ttk.Button(zoom, text="↺", width=4, style="Lens.TButton",
                   command=lambda: self.rotate_document(90)).pack(side="left", padx=2)
        ttk.Button(zoom, text="↻", width=4, style="Lens.TButton",
                   command=lambda: self.rotate_document(-90)).pack(side="left", padx=2)
        ttk.Button(zoom, text="-", width=3, style="Lens.TButton",
                   command=lambda: self.set_zoom(self.zoom - .1)).pack(side="left", padx=2)
        self.zoom_label = tk.Label(zoom, text="100%", width=6, bg=self.PANEL,
                                   fg=self.MUTED, font=("Segoe UI", 9))
        self.zoom_label.pack(side="left")
        ttk.Button(zoom, text="+", width=3, style="Lens.TButton",
                   command=lambda: self.set_zoom(self.zoom + .1)).pack(side="left", padx=2)

        self._build_results(body)

    def _apply_dark_widget_theme(self, root: tk.Misc | None = None) -> None:
        """Apply dark colors to classic Tk widgets after the layout is built."""
        background_map = {
            self.BG: self.BG,
            self.PANEL: self.PANEL,
            self.BLUE_SOFT: self.BLUE_SOFT,
            self.INPUT_BG: self.INPUT_BG,
            self.OUTPUT_BG: self.OUTPUT_BG,
            self.TEXT_BG: self.TEXT_BG,
            "#ffffff": self.PANEL,
            "#f8fafd": "#f8fafd",
            "#f7fbfa": self.OUTPUT_BG,
            "#fafbfc": "#fafbfc",
        }
        foreground_map = {
            "#9aa0a6": self.MUTED,
            "#3c4043": self.INK,
            "#5f6368": self.MUTED,
            "#185abc": "#185abc",
            "#188038": "#188038",
            self.MUTED: self.MUTED,
            self.INK: self.INK,
            self.TEAL: self.TEAL,
        }

        def visit(parent: tk.Misc) -> None:
            for child in parent.winfo_children():
                try:
                    widget_class = child.winfo_class()
                    if widget_class in {"Frame", "Labelframe"}:
                        old_bg = str(child.cget("background"))
                        child.configure(
                            background=background_map.get(old_bg, self.PANEL),
                            highlightbackground=(self.OUTPUT_LINE if old_bg == "#f7fbfa" else self.LINE),
                        )
                    elif widget_class == "Label":
                        old_bg = str(child.cget("background"))
                        old_fg = str(child.cget("foreground"))
                        child.configure(
                            background=background_map.get(old_bg, self.PANEL),
                            foreground=foreground_map.get(old_fg, self.INK),
                        )
                    elif widget_class == "Text":
                        child.configure(
                            background=self.TEXT_BG, foreground=self.INK,
                            insertbackground=self.INK,
                            selectbackground="#c6daf7",
                            selectforeground=self.INK,
                        )
                    elif widget_class == "Listbox":
                        child.configure(
                            background=self.PANEL, foreground=self.INK,
                            selectbackground="#c6daf7",
                            selectforeground=self.INK,
                        )
                    elif widget_class == "Entry":
                        child.configure(
                            background=self.TEXT_BG, foreground=self.INK,
                            insertbackground=self.INK,
                            selectbackground="#c6daf7",
                            selectforeground=self.INK,
                        )
                    elif widget_class == "Canvas":
                        child.configure(background=self.INPUT_BG if child is self.canvas else self.PANEL)
                except tk.TclError:
                    pass
                visit(child)

        visit(root if root is not None else self)

    def _build_results(self, body: tk.Frame) -> None:
        outer = tk.Frame(body, bg=self.PANEL,
                         highlightbackground=self.LINE, highlightthickness=1)
        outer.grid(row=0, column=1, sticky="nsew")
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)
        scroll_canvas = tk.Canvas(outer, bg=self.PANEL, highlightthickness=0, borderwidth=0)
        scroll_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=scroll_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        scroll_canvas.configure(yscrollcommand=scrollbar.set)
        panel = tk.Frame(scroll_canvas, bg=self.PANEL, width=390)
        window_id = scroll_canvas.create_window((0, 0), window=panel, anchor="nw")
        panel.bind("<Configure>", lambda _event: scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all")))
        scroll_canvas.bind(
            "<Configure>",
            lambda event: scroll_canvas.itemconfigure(
                window_id, width=event.width, height=event.height
            ),
        )
        scroll_canvas.bind_all("<MouseWheel>", lambda event: scroll_canvas.yview_scroll(-int(event.delta / 120), "units"))
        panel.grid_columnconfigure(0, weight=1)
        # Keep the output card as tall as the visible input canvas. The result
        # text then receives the remaining vertical space in the right pane.
        panel.grid_rowconfigure(2, weight=1)

        top = tk.Frame(panel, bg=self.PANEL)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(22, 10))
        tk.Label(top, text="OCR OUTPUT / النص المستخرج", bg=self.PANEL, fg="#9aa0a6",
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        tk.Label(top, text="Readable zones / المناطق المقروءة", bg=self.PANEL, fg="#3c4043",
                 font=("Segoe UI", 16)).pack(anchor="w", pady=(4, 0))

        self.hint_label = tk.Label(panel,
                                   text="اسحب فوق المنطقة المقروءة ثم اضغط Convert to TXT.\nDrag over a readable zone, then convert it to TXT.",
                                   bg="#f8fafd", fg="#5f6368", justify="left", anchor="w",
                                   wraplength=345, padx=10, pady=9,
                                   font=("Segoe UI", 8))
        self.hint_label.grid(row=1, column=0, sticky="ew", padx=20, pady=5)
        top.winfo_children()[0].configure(text="OUTPUT")
        top.winfo_children()[1].configure(text="Converted text")
        self.clear_result_button = ttk.Button(
            top, text="✕", width=3, style="Lens.TButton",
            state=tk.DISABLED, command=self.clear_result,
        )
        self.clear_result_button.pack(side="right", anchor="n")
        self.hint_label.configure(
            text="Import a document, draw a readable area, then click Convert to TXT."
        )

        output = tk.Frame(panel, bg="#f7fbfa", highlightbackground="#d8e7e4",
                          highlightthickness=1)
        output.grid(row=2, column=0, sticky="nsew", padx=20, pady=(8, 10))
        output.grid_rowconfigure(1, weight=1)
        output.grid_columnconfigure(0, weight=1)
        tk.Label(output, text="RESULT FROM MAIN.PY / نتيجة التحويل", bg="#f7fbfa", fg=self.TEAL,
                 font=("Segoe UI", 8, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(9, 0))
        result_frame = tk.Frame(output, bg="#f7fbfa")
        result_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        result_frame.grid_rowconfigure(0, weight=1)
        result_frame.grid_columnconfigure(0, weight=1)
        self.result_text = tk.Text(result_frame, height=1, wrap="none", relief="flat",
                                   bg="#ffffff", fg=self.INK, font=("Consolas", 9),
                                   state=tk.DISABLED)
        self.result_text.grid(row=0, column=0, sticky="nsew")
        result_xscroll = ttk.Scrollbar(result_frame, orient="horizontal",
                                       command=self.result_text.xview)
        result_xscroll.grid(row=1, column=0, sticky="ew")
        self.result_text.configure(xscrollcommand=result_xscroll.set)

        divider = tk.Frame(panel, bg=self.PANEL)
        divider.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 5))
        tk.Label(divider, text="SELECTED ZONES / المناطق المحددة", bg=self.PANEL, fg="#5f6368",
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self.count_label = tk.Label(divider, text="0 zones / 0 مناطق", bg=self.PANEL, fg=self.TEAL,
                                    font=("Segoe UI", 8, "bold"))
        self.count_label.pack(side="right")
        divider.winfo_children()[0].configure(text="SELECTED ZONES")

        list_frame = tk.Frame(panel, bg=self.PANEL)
        list_frame.grid(row=4, column=0, sticky="ew", padx=20)
        list_frame.configure(height=92)
        list_frame.grid_propagate(False)
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)
        self.line_list = tk.Listbox(list_frame, activestyle="none", height=4, relief="flat",
                                    highlightthickness=0, bg=self.PANEL, fg=self.INK,
                                    selectbackground="#e8f0fe", selectforeground=self.INK,
                                    font=("Segoe UI", 9))
        self.line_list.grid(row=0, column=0, sticky="nsew")
        self.line_list.bind("<<ListboxSelect>>", self.select_line)
        line_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.line_list.yview)
        line_scroll.grid(row=0, column=1, sticky="ns")
        line_xscroll = ttk.Scrollbar(list_frame, orient="horizontal", command=self.line_list.xview)
        line_xscroll.grid(row=1, column=0, sticky="ew")
        self.line_list.configure(yscrollcommand=line_scroll.set, xscrollcommand=line_xscroll.set)

        log_frame = tk.Frame(panel, bg="#fafbfc", highlightbackground=self.LINE,
                             highlightthickness=1)
        log_frame.grid(row=5, column=0, sticky="ew", padx=20, pady=(12, 0))
        log_frame.grid_columnconfigure(0, weight=1)
        tk.Label(log_frame, text="ACTION LOG", bg="#fafbfc", fg="#5f6368",
                 font=("Segoe UI", 8, "bold")).grid(row=0, column=0, sticky="w", padx=9, pady=(8, 2))
        self.log_text = tk.Text(log_frame, height=5, wrap="word", relief="flat",
                                bg="#ffffff", fg="#5f6368", font=("Consolas", 8),
                                state=tk.DISABLED)
        self.log_text.grid(row=1, column=0, sticky="ew", padx=7, pady=(0, 7))

        self.save_result_button = ttk.Button(
            panel, text="Save result / حفظ النتيجة", style="Blue.TButton",
            state=tk.DISABLED, command=self.save_result,
        )
        self.save_result_button.grid(row=6, column=0, sticky="ew", padx=20, pady=(12, 0))
        self.save_result_button.configure(style="Lens.TButton")
        self.save_result_button.configure(text="Save result")

        bottom = tk.Label(panel, text="Select a zone, then press Delete to remove it.\nحدد منطقة واضغط Delete لحذفها.",
                          bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8))
        bottom.grid(row=7, column=0, sticky="ew", padx=20, pady=14)
        bottom.configure(text="Draw a zone on the image. Select it here, then press Delete to remove it.")
        self.line_list.bind("<Delete>", lambda _event: self.delete_line())
        self.log_action("SinLens ready")

    def log_action(self, message: str) -> None:
        if not hasattr(self, "log_text"):
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{stamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def import_document(self) -> None:
        path = filedialog.askopenfilename(
            title="Import document",
            filetypes=[("Documents", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        self.load_document_path(Path(path))

    def import_remote(self) -> None:
        url = self.ask_remote_url()
        if not url:
            return
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() not in {"http", "https"}:
            messagebox.showerror("Invalid URL", "Use an HTTP or HTTPS URL.")
            return
        remote_dir = Path(tempfile.mkdtemp(prefix="sinlens_remote_"))
        try:
            source = fetch_source(url.strip(), remote_dir)
            self._cleanup_remote_dir()
            self.remote_dir = remote_dir
            self.load_document_path(source, display_name=url.strip())
            self.log_action(f"Imported remote document: {url.strip()}")
        except Exception as exc:
            shutil.rmtree(remote_dir, ignore_errors=True)
            self.log_action(f"Remote import failed: {exc}")
            messagebox.showerror("Remote import failed", str(exc))

    def load_document_path(self, source: Path, display_name: str | None = None) -> None:
        try:
            if source.suffix.lower() == ".pdf":
                with pymupdf.open(str(source)) as pdf:
                    if not pdf.page_count:
                        raise ValueError("The PDF has no pages")
                    page = pdf[0]
                    pix = page.get_pixmap(dpi=180, colorspace=pymupdf.csRGB, alpha=False)
                    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            else:
                with Image.open(source) as opened:
                    image = ImageOps.exif_transpose(opened).convert("RGB").copy()
        except Exception as exc:
            self.log_action(f"Import failed: {exc}")
            messagebox.showerror("Import failed", str(exc))
            return
        # Keep the downloaded file only when this is the same remote document.
        # A local import must release the previous remote temp directory.
        if self.remote_dir is not None and source.parent != self.remote_dir:
            self._cleanup_remote_dir()
        self.document_name = source.stem or "document"
        self.source_path = source
        self.page_image = image
        self.lines.clear()
        self.active_index = None
        self.ocr_raw = ""
        self.ocr_lines = []
        self.last_result_path = None
        self.zoom = 1.0
        self.rotation = 0
        label = display_name or source.name
        self.title_label.configure(text=source.name)
        self.meta_label.configure(text=f"{label}  |  {image.width} x {image.height}")
        self.header_convert_button.configure(state=tk.NORMAL)
        self.clear_button.configure(state=tk.NORMAL)
        self.remove_button.configure(state=tk.NORMAL)
        self.zoom_label.configure(text="100%")
        self.render_document()
        self.refresh_lines()
        self.result_text.configure(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.configure(state=tk.DISABLED)
        self.clear_result_button.configure(state=tk.DISABLED)
        self.save_result_button.configure(state=tk.DISABLED)
        self.log_action(f"Imported document: {source.name}")

    def ask_remote_url(self) -> str | None:
        dialog = tk.Toplevel(self)
        dialog.configure(bg=self.PANEL)
        dialog.title("Import URL / استيراد رابط")
        dialog.geometry("820x155")
        dialog.minsize(620, 155)
        dialog.resizable(True, False)
        dialog.transient(self)
        dialog.grab_set()

        tk.Label(dialog, text="Remote document URL / رابط المستند", font=("Segoe UI", 11, "bold")).pack(
            anchor="w", padx=18, pady=(16, 4)
        )
        tk.Label(dialog, text="Paste a long HTTP(S) link to an image or PDF.\nألصق رابط HTTP(S) طويلاً لصورة أو PDF.",
                 fg=self.MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=18)
        entry = tk.Entry(dialog, width=105, font=("Segoe UI", 10))
        entry.pack(fill="x", padx=18, pady=9)
        entry.focus_set()
        result: dict[str, str | None] = {"value": None}

        buttons = tk.Frame(dialog)
        buttons.pack(anchor="e", padx=18)
        ttk.Button(buttons, text="Cancel / إلغاء", command=dialog.destroy).pack(side="left", padx=4)

        def submit() -> None:
            result["value"] = entry.get().strip()
            dialog.destroy()

        ttk.Button(buttons, text="Import URL / استيراد", style="Blue.TButton",
                   command=submit).pack(side="left", padx=4)
        self._apply_dark_widget_theme(dialog)
        dialog.bind("<Return>", lambda _event: submit())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self.wait_window(dialog)
        return result["value"]

    def _cleanup_remote_dir(self, except_dir: Path | None = None) -> None:
        if self.remote_dir is not None and self.remote_dir != except_dir:
            shutil.rmtree(self.remote_dir, ignore_errors=True)
            self.remote_dir = None

    def render_document(self) -> None:
        self.canvas.delete("all")
        if self.page_image is None:
            width = max(500, self.canvas.winfo_width())
            height = max(400, self.canvas.winfo_height())
            self.canvas.configure(scrollregion=(0, 0, width, height))
            center_x, center_y = width // 2, height // 2
            self.canvas.create_oval(center_x - 36, center_y - 84, center_x + 36, center_y - 12,
                                    outline=self.BLUE, width=3)
            self.canvas.create_text(center_x, center_y + 18, text="SinLens",
                                    fill=self.BLUE, font=("Segoe UI", 22, "bold"))
            self.canvas.create_text(
                center_x, center_y + 55,
                text="Import local file or URL\nحمّل ملفاً محلياً أو رابطاً بعيداً",
                fill=self.MUTED, justify="center", font=("Segoe UI", 10),
            )
            return
        available_w = max(300, self.canvas.winfo_width() - 55)
        available_h = max(300, self.canvas.winfo_height() - 55)
        base = min(available_w / self.page_image.width, available_h / self.page_image.height)
        scale = max(.08, base * self.zoom)
        width = max(1, round(self.page_image.width * scale))
        height = max(1, round(self.page_image.height * scale))
        shown = self.page_image.resize((width, height), Image.Resampling.LANCZOS)
        self.tk_image = ImageTk.PhotoImage(shown)
        origin_x = max(20, (self.canvas.winfo_width() - width) // 2)
        origin_y = max(20, (self.canvas.winfo_height() - height) // 2)
        self.display_bounds = origin_x, origin_y, width, height
        self.canvas.configure(scrollregion=(0, 0, max(self.canvas.winfo_width(), origin_x + width + 20),
                                            max(self.canvas.winfo_height(), origin_y + height + 20)))
        self.canvas.create_image(origin_x, origin_y, image=self.tk_image, anchor="nw", tags="document")
        self.draw_boxes()

    def draw_boxes(self) -> None:
        self.canvas.delete("selection")
        ox, oy, width, height = self.display_bounds
        for index, line in enumerate(self.lines):
            x1 = ox + float(line["x"]) * width
            y1 = oy + float(line["y"]) * height
            x2 = x1 + float(line["width"]) * width
            y2 = y1 + float(line["height"]) * height
            color = self.BLUE if line["active"] else "#9aa0a6"
            self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=2,
                                         fill="#1a73e8", stipple="gray25" if line["active"] else "gray50",
                                         tags="selection")
            self.canvas.create_rectangle(x1, max(oy, y1 - 18), x1 + 23, y1,
                                         fill=color, outline=color, tags="selection")
            self.canvas.create_text(x1 + 11, max(oy - 9, y1 - 9), text=str(index + 1),
                                    fill="#ffffff", font=("Segoe UI", 8, "bold"), tags="selection")

    def point_on_document(self, event: tk.Event) -> tuple[float, float] | None:
        ox, oy, width, height = self.display_bounds
        if not width or not height:
            return None
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        nx = (x - ox) / width
        ny = (y - oy) / height
        if not (0 <= nx <= 1 and 0 <= ny <= 1):
            return None
        return nx, ny

    def start_selection(self, event: tk.Event) -> None:
        point = self.point_on_document(event)
        if point is None:
            return
        self.drawing = point
        self.canvas.delete("ghost")

    def update_selection(self, event: tk.Event) -> None:
        if self.drawing is None:
            return
        point = self.point_on_document(event)
        if point is None:
            return
        x1, y1 = self.drawing
        x2, y2 = point
        ox, oy, width, height = self.display_bounds
        self.canvas.delete("ghost")
        self.canvas.create_rectangle(ox + min(x1, x2) * width, oy + min(y1, y2) * height,
                                     ox + max(x1, x2) * width, oy + max(y1, y2) * height,
                                     outline=self.TEAL, width=2, dash=(5, 3),
                                     fill="#168277", stipple="gray25", tags="ghost")

    def finish_selection(self, event: tk.Event) -> None:
        if self.drawing is None:
            return
        start = self.drawing
        self.drawing = None
        point = self.point_on_document(event)
        self.canvas.delete("ghost")
        if point is None:
            return
        x1, y1 = start
        x2, y2 = point
        x, y = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        if width < .012 or height < .008:
            return
        self.lines.append({"x": x, "y": y, "width": width, "height": height,
                           "text": "", "active": True})
        self.active_index = len(self.lines) - 1
        self.refresh_lines()
        self.log_action(f"Selected zone {self.active_index + 1}")

    def select_line(self, _event: tk.Event | None = None) -> None:
        selected = self.line_list.curselection()
        if not selected:
            return
        self.active_index = selected[0]
        self.draw_boxes()

    def refresh_lines(self) -> None:
        self.line_list.delete(0, tk.END)
        for index, line in enumerate(self.lines):
            marker = "[x]" if line["active"] else "[ ]"
            text = str(line["text"]).replace("\n", " ").strip() or "Type readable text..."
            self.line_list.insert(tk.END, f"{marker}  {index + 1:02d}  {text[:64]}")
        if self.lines:
            if self.active_index is None or self.active_index >= len(self.lines):
                self.active_index = len(self.lines) - 1
            self.line_list.selection_set(self.active_index)
        else:
            self.active_index = None
        self.draw_boxes()
        self.update_status()
        self._set_clear_status_text()

    def _set_clear_status_text(self) -> None:
        """Keep selection instructions short and readable in the input flow."""
        active = sum(1 for line in self.lines if line["active"])
        self.status_label.configure(text=f"{active} selected")
        self.count_label.configure(
            text=f"{active} readable zone{'s' if active != 1 else ''}"
        )
        if not self.is_converting:
            if active:
                self.hint_label.configure(
                    text="A readable zone is selected. Convert to TXT will process only that area."
                )
            else:
                self.hint_label.configure(
                    text="No zone selected. Convert to TXT will process the full document."
                )

    def update_status(self) -> None:
        active = sum(1 for line in self.lines if line["active"])
        self.status_label.configure(text=f"{active} selected / {active} محددة")
        self.count_label.configure(text=f"{active} zone{'s' if active != 1 else ''} / {active} مناطق")
        if not self.is_converting:
            self.hint_label.configure(
                text=("A zone is selected: Convert to TXT will process only this zone.\nمنطقة محددة: سيتم تحويلها فقط."
                      if active else "No zone selected: Convert to TXT will process the full document.\nبدون تحديد: سيتم تحويل المستند كاملاً.")
            )

    def delete_line(self) -> None:
        if self.active_index is None or not self.lines:
            return
        deleted_number = self.active_index + 1
        self.lines.pop(self.active_index)
        self.active_index = min(self.active_index, len(self.lines) - 1) if self.lines else None
        self.refresh_lines()
        self.log_action(f"Deleted zone {deleted_number}")

    def clear_lines(self) -> None:
        cleared = len(self.lines)
        self.lines.clear()
        self.active_index = None
        self.refresh_lines()
        if cleared:
            self.log_action(f"Cleared {cleared} selected zone(s)")

    def clear_result(self) -> None:
        """Clear the displayed OCR result without deleting the saved TXT file."""
        self.ocr_raw = ""
        self.ocr_lines = []
        self.last_result_path = None
        self.result_text.configure(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.configure(state=tk.DISABLED)
        self.clear_result_button.configure(state=tk.DISABLED)
        self.save_result_button.configure(state=tk.DISABLED)
        self.hint_label.configure(text="Result cleared. Select a zone or convert the full document.")
        self.log_action("Cleared OCR result")

    def remove_document(self) -> None:
        if self.page_image is None and self.source_path is None:
            return
        if not messagebox.askyesno("Remove document", "Remove the document from the workspace?\nThe original file will not be deleted."):
            return
        self.source_path = None
        self._cleanup_remote_dir()
        self.page_image = None
        self.tk_image = None
        self.document_name = "document"
        self.rotation = 0
        self.zoom = 1.0
        self.lines.clear()
        self.active_index = None
        self.ocr_raw = ""
        self.ocr_lines = []
        self.last_result_path = None
        self.title_label.configure(text="Choose an image to begin")
        self.meta_label.configure(text="Import a file or URL to begin / حمّل ملفاً أو رابطاً للبدء")
        self.zoom_label.configure(text="100%")
        self.header_convert_button.configure(state=tk.DISABLED)
        self.clear_button.configure(state=tk.DISABLED)
        self.remove_button.configure(state=tk.DISABLED)
        self.result_text.configure(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.configure(state=tk.DISABLED)
        self.clear_result_button.configure(state=tk.DISABLED)
        self.save_result_button.configure(state=tk.DISABLED)
        self.canvas.delete("all")
        self.display_bounds = (0, 0, 0, 0)
        self.refresh_lines()
        self.hint_label.configure(text="No document loaded. Import a document to begin.\nلا يوجد مستند. حمّل مستنداً للبدء.")
        self.log_action("Removed document from workspace")

    def close_app(self) -> None:
        self._cleanup_remote_dir()
        self.destroy()

    def set_zoom(self, value: float) -> None:
        self.zoom = max(.5, min(2.5, round(value, 2)))
        self.zoom_label.configure(text=f"{round(self.zoom * 100)}%")
        self.render_document()

    def rotate_document(self, degrees: int) -> None:
        if self.page_image is None:
            self.hint_label.configure(text="Import a document before rotating it.")
            return
        self.page_image = self.page_image.rotate(degrees, expand=True)
        self.rotation = (self.rotation + degrees) % 360
        self.lines.clear()
        self.active_index = None
        self.meta_label.configure(
            text=f"{self.source_path.name if self.source_path else self.document_name}  |  "
                 f"{self.page_image.width} x {self.page_image.height}  |  rotation {self.rotation} degrees"
        )
        self.render_document()
        self.refresh_lines()
        self.hint_label.configure(text="Page rotated. Draw a zone, then convert it.\nتم تدوير الصفحة. حدد منطقة ثم حوّلها.")
        self.log_action(f"Rotated document to {self.rotation} degrees")

    def focus_canvas(self) -> None:
        self.canvas.focus_set()

    def save_text(self, text: str, suffix: str) -> None:
        if not text.strip():
            messagebox.showinfo("Nothing to export", "Add or import text before converting to TXT.")
            return
        target = self.default_output_path(suffix)
        Path(target).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8-sig")
        self.last_result_path = target
        self.save_result_button.configure(state=tk.NORMAL)
        self.hint_label.configure(text=f"Saved TXT: {target}")

    def default_output_path(self, suffix: str) -> Path:
        """Return a predictable TXT destination without opening a save dialog."""
        if self.source_path is not None and self.remote_dir is None:
            output_dir = self.source_path.parent
        else:
            output_dir = Path.home() / "Downloads"
        output_dir.mkdir(parents=True, exist_ok=True)
        base = output_dir / f"{self.document_name}-{suffix}.txt"
        if not base.exists():
            return base
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = output_dir / f"{self.document_name}-{suffix}_{stamp}.txt"
        counter = 2
        while candidate.exists():
            candidate = output_dir / f"{self.document_name}-{suffix}_{stamp}_{counter}.txt"
            counter += 1
        return candidate

    def save_result(self) -> None:
        """Let the user choose where to save the latest OCR result."""
        if not self.ocr_raw.strip():
            messagebox.showinfo("No result", "Convert a document first.")
            return
        initial_dir = self.last_result_path.parent if self.last_result_path else (
            self.source_path.parent if self.source_path is not None and self.remote_dir is None
            else Path.home() / "Downloads"
        )
        target = filedialog.asksaveasfilename(
            title="Save OCR result",
            initialdir=str(initial_dir),
            initialfile=f"{self.document_name}-result.txt",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            target_path = Path(target)
            target_path.write_text(
                self.ocr_raw if self.ocr_raw.endswith("\n") else self.ocr_raw + "\n",
                encoding="utf-8-sig",
            )
            self.last_result_path = target_path
            self.save_result_button.configure(state=tk.NORMAL)
            self.hint_label.configure(text=f"Saved TXT: {target_path}")
            self.log_action(f"Saved result: {target_path}")
        except Exception as exc:
            self.log_action(f"Save failed: {exc}")
            messagebox.showerror("Save failed", str(exc))

    def _show_progress(self) -> None:
        if self.progress_window is not None and self.progress_window.winfo_exists():
            return
        window = tk.Toplevel(self)
        window.configure(bg=self.PANEL)
        window.title("Converting document")
        window.geometry("360x145")
        window.resizable(False, False)
        window.transient(self)
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        tk.Label(window, text="Converting to TXT / جارٍ التحويل", font=("Segoe UI", 13, "bold"),
                 pady=12).pack()
        tk.Label(window, text="SinLens is reading the document. Please wait...\nيرجى الانتظار حتى انتهاء التحويل.",
                 font=("Segoe UI", 9)).pack()
        progress = ttk.Progressbar(window, mode="indeterminate", length=285)
        progress.pack(pady=16)
        self._apply_dark_widget_theme(window)
        progress.start(12)
        window.grab_set()
        self.progress_window = window

    def _hide_progress(self) -> None:
        if self.progress_window is None:
            return
        try:
            self.progress_window.grab_release()
            self.progress_window.destroy()
        finally:
            self.progress_window = None

    def _set_conversion_busy(self, busy: bool) -> None:
        self.is_converting = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.header_convert_button.configure(state=state)
        self.import_button.configure(state=state)
        self.remote_button.configure(state=state)
        self.clear_button.configure(state=state if self.page_image is not None else tk.DISABLED)
        self.remove_button.configure(state=state if self.page_image is not None else tk.DISABLED)
        self.save_result_button.configure(
            state=tk.DISABLED if busy or self.last_result_path is None else tk.NORMAL
        )
        if busy:
            self._show_progress()
        else:
            self._hide_progress()
        self.hint_label.configure(
            text="Running main.py OCR..." if busy else self.hint_label.cget("text")
        )

    def _start_main_conversion(self, mode: str, target: str) -> None:
        if self.is_converting:
            return
        self.log_action(
            f"Started main.py conversion: {'selected zones' if mode == 'selected' else 'full document'}"
        )
        self._set_conversion_busy(True)
        worker = threading.Thread(
            target=self._run_main_conversion,
            args=(mode, target),
            daemon=True,
        )
        worker.start()

    def _run_main_conversion(self, mode: str, target: str) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="clearread_main_") as work:
                work_dir = Path(work)
                if self.page_image is None:
                    raise ValueError("No document is loaded")
                # Always OCR the currently displayed image. This preserves the
                # user's manual rotation instead of reopening the original PDF.
                source = work_dir / "selected_document.png"
                image = self.page_image.copy()
                if mode == "selected":
                    active = [line for line in self.lines if line["active"]]
                    if not active:
                        raise ValueError("Select at least one readable zone")
                    left = min(float(line["x"]) for line in active)
                    top = min(float(line["y"]) for line in active)
                    right = max(float(line["x"]) + float(line["width"]) for line in active)
                    bottom = max(float(line["y"]) + float(line["height"]) for line in active)
                    margin_x = min(.02, (right - left) * .08)
                    margin_y = min(.02, (bottom - top) * .08)
                    left = max(0.0, left - margin_x)
                    top = max(0.0, top - margin_y)
                    right = min(1.0, right + margin_x)
                    bottom = min(1.0, bottom + margin_y)
                    image = image.crop((
                        round(left * image.width), round(top * image.height),
                        round(right * image.width), round(bottom * image.height),
                    ))
                image.save(source)
                result = run(
                    source=str(source),
                    output=str(work_dir),
                    dpi=250,
                    threshold=80,
                    timeout=300,
                    keep_images=False,
                    columns=120,
                )
                text = result.read_text(encoding="utf-8-sig")
            self.after(0, lambda: self._main_conversion_done(mode, target, text))
        except Exception as exc:
            error_text = str(exc)
            self.after(0, lambda error=error_text: self._main_conversion_failed(error))

    def _main_conversion_done(self, mode: str, target: str, text: str) -> None:
        self._set_conversion_busy(False)
        target_path = Path(target)
        try:
            target_path.write_text(
                text if text.endswith("\n") else text + "\n", encoding="utf-8-sig"
            )
        except Exception as exc:
            self._main_conversion_failed(f"Could not save TXT to {target_path}: {exc}")
            return
        self.last_result_path = target_path
        self.save_result_button.configure(state=tk.NORMAL)
        self.clear_result_button.configure(state=tk.NORMAL)
        self.ocr_raw = text
        self.ocr_lines = [line for line in text.splitlines() if line.strip()]
        self.result_text.configure(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        self.result_text.insert("1.0", text)
        self.result_text.configure(state=tk.DISABLED)
        if mode == "selected" and self.lines:
            active = sorted(
                (line for line in self.lines if line["active"]),
                key=lambda line: (float(line["y"]), float(line["x"])),
            )
            for index, line in enumerate(active):
                if index < len(self.ocr_lines):
                    line["text"] = self.ocr_lines[index]
            self.refresh_lines()
        self.hint_label.configure(text=f"Saved TXT: {target_path}")
        self.log_action(f"Conversion complete: {target_path}")
        messagebox.showinfo("Conversion complete", f"TXT saved to:\n{target_path}")

    def _main_conversion_failed(self, error: str) -> None:
        self._set_conversion_busy(False)
        self.hint_label.configure(text="main.py conversion failed")
        self.log_action(f"Conversion failed: {error}")
        messagebox.showerror("Conversion failed", str(error))

    def convert_full(self) -> None:
        if self.page_image is None and self.source_path is None:
            messagebox.showinfo("Document required", "Import a document before converting it.")
            return
        target = self.default_output_path("full")
        self._start_main_conversion("full", str(target))

    def build_selected_text(self) -> str:
        selected = [line for line in self.lines if line["active"] and str(line["text"]).strip()]
        if not selected:
            return ""
        columns = max(120, *(round(float(line["x"]) * 120) + len(str(line["text"]).strip()) + 4 for line in selected))
        rows = 100
        canvas = [[" "] * columns for _ in range(rows)]
        for line in sorted(selected, key=lambda item: (float(item["y"]), float(item["x"]))):
            content = re.sub(r"[\r\n]+", " ", str(line["text"])).strip()
            row = min(rows - 1, max(0, round(float(line["y"]) * (rows - 1))))
            arabic = bool(ARABIC_RE.search(content))
            start = round((float(line["x"]) + float(line["width"])) * columns) - len(content) if arabic else round(float(line["x"]) * columns)
            start = max(0, min(start, max(0, columns - len(content))))
            target = row
            while target < rows and any(canvas[target][offset] != " " for offset in range(start, min(columns, start + len(content)))):
                target += 1
            target = min(rows - 1, target)
            for offset, char in enumerate(content):
                if start + offset < columns:
                    canvas[target][start + offset] = char
        return "\n".join("".join(row).rstrip() for row in canvas).rstrip() + "\n"

    def convert_selected(self) -> None:
        if not any(line["active"] for line in self.lines):
            messagebox.showinfo("No selected zone", "Draw and select at least one readable zone first.")
            return
        target = self.default_output_path("selected")
        self._start_main_conversion("selected", str(target))

    def convert_to_txt(self) -> None:
        """Use one button: full document when no zone is selected, otherwise selected zones."""
        if any(line["active"] for line in self.lines):
            self.convert_selected()
        else:
            self.convert_full()



if __name__ == "__main__":
    ClearReadDesktop().mainloop()

