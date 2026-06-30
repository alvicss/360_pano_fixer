# -*- coding: utf-8 -*-
"""
360 全景圖工具
---------------------------------
功能：
1. 讀取單張或多張 GPT/AI 生成的全景圖
2. 批次檢查是否為 2:1 比例（equirectangular 投影必須條件）
3. 依設定自動裁切或保留原圖
4. 寫入 Google Photo Sphere (GPano) XMP metadata
5. 輸出新檔案，讓 Facebook / 手機相簿能正確辨識為 360 度照片

使用方式：
- 直接雙擊開啟，使用桌面 UI 批次處理
- 把圖片拖曳到本程式 (exe) 上，會自動加入清單
- 如需命令列模式：python pano_fixer.py --cli 檔案1 檔案2
"""

from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from xml.sax.saxutils import escape

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageColor, UnidentifiedImageError


SUPPORTED_INPUT_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif")
NON_2TO1_ACTIONS = {
    "crop": "自動裁切成 2:1",
    "skip": "跳過這張圖",
    "keep": "照原圖輸出",
}
OUTPUT_MODES = {
    "same_folder": "輸出到原圖旁",
    "custom_folder": "輸出到指定資料夾",
}


@dataclass
class PanoSettings:
    non_2to1_action: str = "crop"
    ratio_tolerance: float = 0.01
    crop_anchor_x: float = 50.0
    crop_anchor_y: float = 50.0
    output_mode: str = "same_folder"
    output_dir: str = ""
    filename_suffix: str = "_360"
    overwrite_existing: bool = False
    jpeg_quality: int = 95
    background_color: str = "#FFFFFF"
    projection_type: str = "equirectangular"
    use_panorama_viewer: bool = True
    source_photos_count: int | None = 1
    exposure_lock_used: bool = False
    pose_heading_degrees: float | None = None
    pose_pitch_degrees: float | None = None
    pose_roll_degrees: float | None = None
    initial_view_heading_degrees: float | None = None
    initial_view_pitch_degrees: float | None = None
    initial_view_roll_degrees: float | None = None
    initial_horizontal_fov_degrees: float | None = None
    initial_camera_dolly: float | None = None
    full_pano_override: bool = False
    full_pano_width: int | None = None
    full_pano_height: int | None = None
    cropped_area_override: bool = False
    cropped_area_left: int = 0
    cropped_area_top: int = 0
    largest_rect_override: bool = False
    largest_rect_left: int = 0
    largest_rect_top: int = 0
    largest_rect_width: int | None = None
    largest_rect_height: int | None = None


@dataclass
class ProcessResult:
    input_path: str
    output_path: str
    width: int
    height: int
    was_cropped: bool
    converted_to_jpeg: bool


class HoverToolTip:
    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 1000) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.after_id: str | None = None
        self.tip_window: tk.Toplevel | None = None

        self.widget.bind("<Enter>", self._schedule, add="+")
        self.widget.bind("<Leave>", self._hide, add="+")
        self.widget.bind("<ButtonPress>", self._hide, add="+")
        self.widget.bind("<FocusOut>", self._hide, add="+")

    def _schedule(self, _event: tk.Event) -> None:
        self._cancel_schedule()
        self.after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel_schedule(self) -> None:
        if self.after_id is not None:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def _show(self) -> None:
        self._cancel_schedule()
        if self.tip_window is not None or not self.text:
            return

        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 10
        self.tip_window = tk.Toplevel(self.widget)
        self.tip_window.wm_overrideredirect(True)
        self.tip_window.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            self.tip_window,
            text=self.text,
            justify="left",
            relief="solid",
            borderwidth=1,
            background="#fff8dc",
            padx=8,
            pady=6,
            wraplength=420,
        )
        label.pack()

    def _hide(self, _event: tk.Event | None = None) -> None:
        self._cancel_schedule()
        if self.tip_window is not None:
            self.tip_window.destroy()
            self.tip_window = None


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def parse_optional_float(raw: str, field_name: str) -> float | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必須是數字") from exc


def parse_optional_int(raw: str, field_name: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必須是整數") from exc


def _format_bound(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}"


def validate_numeric_range(
    value: float | int,
    field_name: str,
    min_value: float | int | None = None,
    max_value: float | int | None = None,
) -> float | int:
    if min_value is not None and value < min_value:
        if max_value is not None:
            raise ValueError(f"{field_name} 必須介於 {_format_bound(min_value)} 到 {_format_bound(max_value)}")
        raise ValueError(f"{field_name} 必須大於或等於 {_format_bound(min_value)}")
    if max_value is not None and value > max_value:
        if min_value is not None:
            raise ValueError(f"{field_name} 必須介於 {_format_bound(min_value)} 到 {_format_bound(max_value)}")
        raise ValueError(f"{field_name} 必須小於或等於 {_format_bound(max_value)}")
    return value


def parse_float_in_range(
    raw: str,
    field_name: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    value = raw.strip()
    if not value:
        raise ValueError(f"{field_name} 不能留白")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必須是數字") from exc
    return float(validate_numeric_range(parsed, field_name, min_value, max_value))


def parse_int_in_range(
    raw: str,
    field_name: str,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    value = raw.strip()
    if not value:
        raise ValueError(f"{field_name} 不能留白")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必須是整數") from exc
    return int(validate_numeric_range(parsed, field_name, min_value, max_value))


def parse_optional_float_in_range(
    raw: str,
    field_name: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float | None:
    parsed = parse_optional_float(raw, field_name)
    if parsed is None:
        return None
    return float(validate_numeric_range(parsed, field_name, min_value, max_value))


def parse_optional_int_in_range(
    raw: str,
    field_name: str,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | None:
    parsed = parse_optional_int(raw, field_name)
    if parsed is None:
        return None
    return int(validate_numeric_range(parsed, field_name, min_value, max_value))


def numeric_tooltip(description: str, range_text: str, allow_blank: bool = False) -> str:
    suffix = f"數值範圍：{range_text}。"
    if allow_blank:
        suffix += " 留白代表不寫入。"
    return f"{description} {suffix}"


def calculate_anchor_offset(current_size: int, target_size: int, anchor_percent: float) -> int:
    available = max(current_size - target_size, 0)
    ratio = clamp(anchor_percent, 0.0, 100.0) / 100.0
    return int(round(available * ratio))


def make_2to1(img: Image.Image, anchor_x: float = 50.0, anchor_y: float = 50.0) -> tuple[Image.Image, bool]:
    """把圖片裁切成最接近的 2:1 比例，可調整保留畫面的偏移。"""
    w, h = img.size
    ratio = w / h
    if abs(ratio - 2.0) < 1e-9:
        return img, False

    if ratio > 2:
        new_w = h * 2
        left = calculate_anchor_offset(w, new_w, anchor_x)
        return img.crop((left, 0, left + new_w, h)), True

    new_h = w // 2
    top = calculate_anchor_offset(h, new_h, anchor_y)
    return img.crop((0, top, w, top + new_h)), True


def ensure_rgb_image(img: Image.Image, background_color: str) -> Image.Image:
    """把圖片轉成適合輸出 JPEG 的 RGB；若有透明度就先鋪底色。"""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba_img = img.convert("RGBA")
        background = Image.new("RGBA", rgba_img.size, background_color)
        return Image.alpha_composite(background, rgba_img).convert("RGB")
    return img.convert("RGB")


def build_gpano_xmp(settings: PanoSettings, width: int, height: int) -> str:
    full_width = settings.full_pano_width if settings.full_pano_override and settings.full_pano_width else width
    full_height = settings.full_pano_height if settings.full_pano_override and settings.full_pano_height else height
    cropped_left = settings.cropped_area_left if settings.cropped_area_override else 0
    cropped_top = settings.cropped_area_top if settings.cropped_area_override else 0
    largest_left = settings.largest_rect_left if settings.largest_rect_override else 0
    largest_top = settings.largest_rect_top if settings.largest_rect_override else 0
    largest_width = (
        settings.largest_rect_width if settings.largest_rect_override and settings.largest_rect_width else width
    )
    largest_height = (
        settings.largest_rect_height if settings.largest_rect_override and settings.largest_rect_height else height
    )

    tags: list[tuple[str, str]] = [
        ("ProjectionType", escape(settings.projection_type)),
        ("UsePanoramaViewer", "True" if settings.use_panorama_viewer else "False"),
        ("CroppedAreaImageWidthPixels", str(width)),
        ("CroppedAreaImageHeightPixels", str(height)),
        ("FullPanoWidthPixels", str(full_width)),
        ("FullPanoHeightPixels", str(full_height)),
        ("CroppedAreaLeftPixels", str(cropped_left)),
        ("CroppedAreaTopPixels", str(cropped_top)),
        ("LargestValidInteriorRectLeft", str(largest_left)),
        ("LargestValidInteriorRectTop", str(largest_top)),
        ("LargestValidInteriorRectWidth", str(largest_width)),
        ("LargestValidInteriorRectHeight", str(largest_height)),
    ]

    optional_tags = [
        ("SourcePhotosCount", settings.source_photos_count),
        ("ExposureLockUsed", "True" if settings.exposure_lock_used else None),
        ("PoseHeadingDegrees", settings.pose_heading_degrees),
        ("PosePitchDegrees", settings.pose_pitch_degrees),
        ("PoseRollDegrees", settings.pose_roll_degrees),
        ("InitialViewHeadingDegrees", settings.initial_view_heading_degrees),
        ("InitialViewPitchDegrees", settings.initial_view_pitch_degrees),
        ("InitialViewRollDegrees", settings.initial_view_roll_degrees),
        ("InitialHorizontalFOVDegrees", settings.initial_horizontal_fov_degrees),
        ("InitialCameraDolly", settings.initial_camera_dolly),
    ]

    for tag_name, tag_value in optional_tags:
        if tag_value is None:
            continue
        tags.append((tag_name, str(tag_value)))

    tag_lines = "\n".join(f"   <GPano:{name}>{value}</GPano:{name}>" for name, value in tags)
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.6-c011">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:GPano="http://ns.google.com/photos/1.0/panorama/">\n'
        f"{tag_lines}\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>'
    )


def insert_xmp_jpeg(jpeg_path: str, xmp_str: str) -> None:
    """
    將 XMP 字串以標準 APP1 segment 插入 JPEG 檔案中。
    """
    xmp_header = b"http://ns.adobe.com/xap/1.0/\x00"
    xmp_bytes = xmp_header + xmp_str.encode("utf-8")

    with open(jpeg_path, "rb") as file_obj:
        data = file_obj.read()

    if data[0:2] != b"\xff\xd8":
        raise ValueError("不是合法的 JPEG 檔案")

    segment_length = len(xmp_bytes) + 2
    if segment_length > 65535:
        raise ValueError("XMP 資料太大，超過單一 JPEG segment 上限")

    app1_segment = b"\xff\xe1" + struct.pack(">H", segment_length) + xmp_bytes
    new_data = data[:2] + app1_segment + data[2:]

    with open(jpeg_path, "wb") as file_obj:
        file_obj.write(new_data)


def resolve_output_path(input_path: str, settings: PanoSettings) -> str:
    source = Path(input_path)
    if settings.output_mode == "custom_folder":
        if not settings.output_dir.strip():
            raise ValueError("已選擇指定輸出資料夾，但尚未填入路徑")
        output_dir = Path(settings.output_dir).expanduser()
    else:
        output_dir = source.parent

    output_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{source.stem}{settings.filename_suffix}.jpg"
    output_path = output_dir / output_name

    if output_path.exists() and not settings.overwrite_existing:
        raise FileExistsError(f"輸出檔已存在：{output_path}")

    return str(output_path)


def process_image(path: str, settings: PanoSettings, log: Callable[[str], None] | None = None) -> ProcessResult:
    logger = log or (lambda _message: None)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"找不到檔案: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_INPUT_EXT:
        raise ValueError(f"不支援的格式: {ext}")

    converted_to_jpeg = ext not in (".jpg", ".jpeg")
    if converted_to_jpeg:
        logger(
            f"偵測到 {ext}，會先轉成 JPG 再寫入 360 metadata。若原圖有透明背景，將以 {settings.background_color} 補底。"
        )

    try:
        with Image.open(path) as source_img:
            img = ensure_rgb_image(source_img, settings.background_color)
    except UnidentifiedImageError as exc:
        raise ValueError("無法辨識的圖片格式") from exc
    w, h = img.size
    ratio = w / h
    logger(f"原始尺寸: {w} x {h} (比例 {ratio:.3f})")

    was_cropped = False
    if abs(ratio - 2.0) > settings.ratio_tolerance:
        action = settings.non_2to1_action
        if action == "crop":
            img, was_cropped = make_2to1(img, settings.crop_anchor_x, settings.crop_anchor_y)
            w, h = img.size
            logger(f"已裁切成 2:1，輸出尺寸: {w} x {h}")
        elif action == "skip":
            raise ValueError("圖片比例不是 2:1，已依設定跳過")
        else:
            logger("圖片比例不是 2:1，但依設定保留原圖尺寸輸出")
    else:
        logger("比例已經接近 2:1，不需裁切")

    output_path = resolve_output_path(path, settings)
    img.save(output_path, "JPEG", quality=settings.jpeg_quality)
    xmp_str = build_gpano_xmp(settings, w, h)
    insert_xmp_jpeg(output_path, xmp_str)

    return ProcessResult(
        input_path=path,
        output_path=output_path,
        width=w,
        height=h,
        was_cropped=was_cropped,
        converted_to_jpeg=converted_to_jpeg,
    )


def gather_images_from_folder(folder: str, recursive: bool) -> list[str]:
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"不是有效資料夾: {folder}")

    image_paths: list[str] = []
    iterator: Iterable[Path] = root.rglob("*") if recursive else root.iterdir()
    for item in iterator:
        if item.is_file() and item.suffix.lower() in SUPPORTED_INPUT_EXT:
            image_paths.append(str(item))
    return sorted(image_paths)


class PanoFixerApp(tk.Tk):
    def __init__(self, startup_paths: list[str] | None = None) -> None:
        super().__init__()
        self.title("360 全景圖工具")
        self.geometry("1200x800")
        self.minsize(980, 760)

        style = ttk.Style(self)
        default_font = ("Microsoft JhengHei", 10)
        style.configure(".", font=default_font)
        style.configure("Treeview.Heading", font=("Microsoft JhengHei", 10, "bold"))
        style.configure("TLabelframe.Label", font=("Microsoft JhengHei", 10, "bold"), foreground="#005A9E")

        self.file_items: dict[str, str] = {}
        self.processing_queue: list[str] = []
        self.batch_settings: PanoSettings | None = None
        self.is_processing = False
        self.tooltips: list[HoverToolTip] = []

        self._build_variables()
        self._build_layout()

        if startup_paths:
            self.add_paths(startup_paths)
            self.after(250, self.start_batch)

    def _build_variables(self) -> None:
        self.recursive_scan_var = tk.BooleanVar(value=False)
        self.non_2to1_action_var = tk.StringVar(value="crop")
        self.ratio_tolerance_var = tk.StringVar(value="0.01")
        self.crop_anchor_x_var = tk.StringVar(value="50")
        self.crop_anchor_y_var = tk.StringVar(value="50")
        self.output_mode_var = tk.StringVar(value="same_folder")
        self.output_dir_var = tk.StringVar(value="")
        self.filename_suffix_var = tk.StringVar(value="_360")
        self.overwrite_existing_var = tk.BooleanVar(value=False)
        self.jpeg_quality_var = tk.StringVar(value="95")
        self.background_color_var = tk.StringVar(value="#FFFFFF")
        self.projection_type_var = tk.StringVar(value="equirectangular")
        self.use_panorama_viewer_var = tk.BooleanVar(value=True)
        self.source_photos_count_var = tk.StringVar(value="1")
        self.exposure_lock_used_var = tk.BooleanVar(value=False)
        self.pose_heading_var = tk.StringVar(value="")
        self.pose_pitch_var = tk.StringVar(value="")
        self.pose_roll_var = tk.StringVar(value="")
        self.initial_heading_var = tk.StringVar(value="")
        self.initial_pitch_var = tk.StringVar(value="")
        self.initial_roll_var = tk.StringVar(value="")
        self.initial_fov_var = tk.StringVar(value="")
        self.initial_dolly_var = tk.StringVar(value="")
        self.full_pano_override_var = tk.BooleanVar(value=False)
        self.full_pano_width_var = tk.StringVar(value="")
        self.full_pano_height_var = tk.StringVar(value="")
        self.cropped_area_override_var = tk.BooleanVar(value=False)
        self.cropped_area_left_var = tk.StringVar(value="0")
        self.cropped_area_top_var = tk.StringVar(value="0")
        self.largest_rect_override_var = tk.BooleanVar(value=False)
        self.largest_rect_left_var = tk.StringVar(value="0")
        self.largest_rect_top_var = tk.StringVar(value="0")
        self.largest_rect_width_var = tk.StringVar(value="")
        self.largest_rect_height_var = tk.StringVar(value="")

    def _bind_variable_traces(self) -> None:
        self.output_mode_var.trace_add("write", lambda *_args: self._toggle_output_dir_state())
        self.full_pano_override_var.trace_add("write", lambda *_args: self._toggle_metadata_override_states())
        self.cropped_area_override_var.trace_add("write", lambda *_args: self._toggle_metadata_override_states())
        self.largest_rect_override_var.trace_add("write", lambda *_args: self._toggle_metadata_override_states())

    def _attach_tooltip(self, text: str, *widgets: tk.Widget) -> None:
        for widget in widgets:
            self.tooltips.append(HoverToolTip(widget, text))

    def _create_scrollable_tab(self, notebook: ttk.Notebook, title: str) -> ttk.Frame:
        container = ttk.Frame(notebook)
        notebook.add(container, text=title)

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=12)

        inner.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )

        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def resize_inner(_event: tk.Event) -> None:
            canvas.itemconfigure(canvas_window, width=_event.width)

        canvas.bind("<Configure>", resize_inner)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _bind_mousewheel(widget: tk.Widget) -> None:
            widget.bind(
                "<MouseWheel>",
                lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"),
                add="+",
            )

        def _bind_all_children(widget: tk.Widget = inner) -> None:
            _bind_mousewheel(widget)
            for child in widget.winfo_children():
                _bind_all_children(child)
        
        inner.bind_children_scroll = _bind_all_children

        _bind_mousewheel(inner)
        _bind_mousewheel(canvas)
        return inner

    def _build_layout(self) -> None:
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(fill="x")
        add_files_button = ttk.Button(toolbar, text="加入圖片", command=self.pick_files)
        add_files_button.pack(side="left", padx=(0, 8))
        add_folder_button = ttk.Button(toolbar, text="加入資料夾", command=self.pick_folder)
        add_folder_button.pack(side="left", padx=(0, 8))
        remove_button = ttk.Button(toolbar, text="移除選取", command=self.remove_selected)
        remove_button.pack(side="left", padx=(0, 8))
        clear_button = ttk.Button(toolbar, text="清空清單", command=self.clear_files)
        clear_button.pack(side="left", padx=(0, 16))
        recursive_button = ttk.Checkbutton(toolbar, text="加入資料夾時含子資料夾", variable=self.recursive_scan_var)
        recursive_button.pack(side="left")
        self._attach_tooltip("一次加入多張圖片到待處理清單。", add_files_button)
        self._attach_tooltip("把整個資料夾內的圖片加入清單，可配合右側勾選一併掃描子資料夾。", add_folder_button, recursive_button)
        self._attach_tooltip("把目前清單中選取的項目移除，不會刪除硬碟上的原始圖片。", remove_button)
        self._attach_tooltip("清空待處理清單，不會刪除硬碟上的原始圖片。", clear_button)

        content_frame = ttk.Frame(self, padding=(12, 0, 12, 12))
        content_frame.pack(fill="both", expand=True)
        content_frame.columnconfigure(0, weight=1)
        content_frame.rowconfigure(0, weight=1, minsize=170)
        content_frame.rowconfigure(1, weight=4, minsize=420)

        files_frame = ttk.Frame(content_frame, padding=(8, 8, 8, 4))
        settings_frame = ttk.Frame(content_frame, padding=(8, 4, 8, 8))
        files_frame.grid(row=0, column=0, sticky="nsew")
        settings_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        files_frame.rowconfigure(1, weight=1)
        files_frame.columnconfigure(0, weight=1)
        settings_frame.rowconfigure(0, weight=1)
        settings_frame.columnconfigure(0, weight=1)

        files_label = ttk.Label(files_frame, text="待處理檔案")
        files_label.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self._attach_tooltip("上方清單會顯示待處理檔案、目前狀態與輸出結果。處理完成後可雙擊該列直接開啟輸出檔。", files_label)
        self.file_tree = ttk.Treeview(
            files_frame,
            columns=("status", "output"),
            show="tree headings",
            selectmode="extended",
            height=6,
        )
        self.file_tree.heading("#0", text="檔案")
        self.file_tree.heading("status", text="狀態")
        self.file_tree.heading("output", text="輸出")
        self.file_tree.column("#0", width=520, stretch=True)
        self.file_tree.column("status", width=90, stretch=False)
        self.file_tree.column("output", width=340, stretch=True)

        tree_scroll_y = ttk.Scrollbar(files_frame, orient="vertical", command=self.file_tree.yview)
        tree_scroll_x = ttk.Scrollbar(files_frame, orient="horizontal", command=self.file_tree.xview)
        self.file_tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)
        self.file_tree.grid(row=1, column=0, sticky="nsew")
        self.file_tree.bind("<Double-1>", self.open_selected_output)
        tree_scroll_y.grid(row=1, column=1, sticky="ns")
        tree_scroll_x.grid(row=2, column=0, sticky="ew")

        notebook = ttk.Notebook(settings_frame)
        notebook.grid(row=0, column=0, sticky="nsew")

        basic_tab = self._create_scrollable_tab(notebook, "基本設定")
        metadata_tab = self._create_scrollable_tab(notebook, "進階 Metadata")
        log_tab = ttk.Frame(notebook, padding=12)
        notebook.add(log_tab, text="處理紀錄")

        self._build_basic_tab(basic_tab)
        self._build_metadata_tab(metadata_tab)
        self._build_log_tab(log_tab)

        if hasattr(basic_tab, "bind_children_scroll"):
            basic_tab.bind_children_scroll()
        if hasattr(metadata_tab, "bind_children_scroll"):
            metadata_tab.bind_children_scroll()

        footer = ttk.Frame(self, padding=(12, 0, 12, 12))
        footer.pack(fill="x")
        self.summary_var = tk.StringVar(value="尚未加入任何圖片")
        ttk.Label(footer, textvariable=self.summary_var).pack(side="left")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(footer, variable=self.progress_var, maximum=1.0, length=220, mode="determinate")
        self.progress.pack(side="right", padx=(12, 0))
        reset_button = ttk.Button(footer, text="還原預設值", command=self.reset_defaults)
        reset_button.pack(side="right", padx=(8, 0))
        self.start_button = ttk.Button(footer, text="開始批次處理", command=self.start_batch)
        self.start_button.pack(side="right")
        self._attach_tooltip("把所有設定恢復成最適合新手直接使用的預設值。", reset_button)
        self._attach_tooltip("依照目前上方清單與下方設定開始批次處理。", self.start_button, self.progress)

        self._bind_variable_traces()
        self._toggle_output_dir_state()
        self._toggle_metadata_override_states()

    def _build_basic_tab(self, parent: ttk.Frame) -> None:
        def add_tip(text: str, *widgets: tk.Widget) -> None:
            self._attach_tooltip(text, *widgets)

        # 裁切與比例設定區塊
        group_crop = ttk.LabelFrame(parent, text="裁切與比例設定", padding=(16, 12))
        group_crop.pack(fill="x", padx=12, pady=(12, 6))
        group_crop.columnconfigure(0, weight=0, minsize=140)
        group_crop.columnconfigure(1, weight=0, minsize=260)
        group_crop.columnconfigure(2, weight=1)

        row = 0
        ratio_label = ttk.Label(group_crop, text="非 2:1 圖片")
        ratio_label.grid(row=row, column=0, sticky="nw", pady=6)
        action_frame = ttk.Frame(group_crop)
        action_frame.grid(row=row, column=1, columnspan=2, sticky="w", pady=6)
        for action_key, action_label in NON_2TO1_ACTIONS.items():
            action_button = ttk.Radiobutton(
                action_frame,
                text=action_label,
                variable=self.non_2to1_action_var,
                value=action_key,
            )
            action_button.pack(anchor="w", pady=2)
            add_tip(
                "決定圖片不是標準 2:1 時怎麼處理。建議保留預設的自動裁切，最容易讓平台正確辨識成 360 圖。",
                action_button,
            )
        add_tip(
            "決定圖片不是標準 2:1 時怎麼處理。建議保留預設的自動裁切，最容易讓平台正確辨識成 360 圖。",
            ratio_label,
        )

        row += 1
        tolerance_label = ttk.Label(group_crop, text="比例容差")
        tolerance_label.grid(row=row, column=0, sticky="w", pady=6)
        tolerance_entry = ttk.Entry(group_crop, textvariable=self.ratio_tolerance_var, width=12)
        tolerance_entry.grid(row=row, column=1, sticky="w", pady=6)
        tolerance_hint = ttk.Label(group_crop, text="預設 0.01", foreground="#666666")
        tolerance_hint.grid(row=row, column=2, sticky="w", pady=6, padx=(12, 0))
        add_tip(
            numeric_tooltip("接受圖片比例偏離 2:1 的容許值。數字越小越嚴格，0.01 代表相差 1% 以內視為可接受。", "0 以上"),
            tolerance_label,
            tolerance_entry,
            tolerance_hint,
        )

        row += 1
        anchor_x_label = ttk.Label(group_crop, text="裁切保留位置 X (%)")
        anchor_x_label.grid(row=row, column=0, sticky="w", pady=6)
        anchor_x_entry = ttk.Entry(group_crop, textvariable=self.crop_anchor_x_var, width=12)
        anchor_x_entry.grid(row=row, column=1, sticky="w", pady=6)
        anchor_x_hint = ttk.Label(group_crop, text="0=靠左，50=置中，100=靠右", foreground="#666666")
        anchor_x_hint.grid(row=row, column=2, sticky="w", pady=6, padx=(12, 0))
        add_tip(
            numeric_tooltip("當圖片太寬需要左右裁切時，控制保留畫面的水平位置。50 代表從正中央裁。", "0 到 100"),
            anchor_x_label,
            anchor_x_entry,
            anchor_x_hint,
        )

        row += 1
        anchor_y_label = ttk.Label(group_crop, text="裁切保留位置 Y (%)")
        anchor_y_label.grid(row=row, column=0, sticky="w", pady=6)
        anchor_y_entry = ttk.Entry(group_crop, textvariable=self.crop_anchor_y_var, width=12)
        anchor_y_entry.grid(row=row, column=1, sticky="w", pady=6)
        anchor_y_hint = ttk.Label(group_crop, text="0=靠上，50=置中，100=靠下", foreground="#666666")
        anchor_y_hint.grid(row=row, column=2, sticky="w", pady=6, padx=(12, 0))
        add_tip(
            numeric_tooltip("當圖片太高需要上下裁切時，控制保留畫面的垂直位置。50 代表從正中央裁。", "0 到 100"),
            anchor_y_label,
            anchor_y_entry,
            anchor_y_hint,
        )

        # 輸出設定區塊
        group_out = ttk.LabelFrame(parent, text="輸出設定", padding=(16, 12))
        group_out.pack(fill="x", padx=12, pady=6)
        group_out.columnconfigure(0, weight=0, minsize=140)
        group_out.columnconfigure(1, weight=0, minsize=260)
        group_out.columnconfigure(2, weight=1)

        row = 0
        output_mode_label = ttk.Label(group_out, text="輸出位置")
        output_mode_label.grid(row=row, column=0, sticky="w", pady=6)
        output_mode_frame = ttk.Frame(group_out)
        output_mode_frame.grid(row=row, column=1, columnspan=2, sticky="w", pady=6)
        same_folder_button = ttk.Radiobutton(
            output_mode_frame,
            text=OUTPUT_MODES["same_folder"],
            variable=self.output_mode_var,
            value="same_folder",
        )
        same_folder_button.pack(side="left")
        custom_folder_button = ttk.Radiobutton(
            output_mode_frame,
            text=OUTPUT_MODES["custom_folder"],
            variable=self.output_mode_var,
            value="custom_folder",
        )
        custom_folder_button.pack(side="left", padx=(24, 0))
        add_tip("選擇輸出檔要存回原圖旁邊，還是統一存到指定資料夾。批次整理時常用指定資料夾。", output_mode_label, same_folder_button, custom_folder_button)

        row += 1
        output_dir_label = ttk.Label(group_out, text="指定輸出資料夾")
        output_dir_label.grid(row=row, column=0, sticky="w", pady=6)
        output_dir_frame = ttk.Frame(group_out)
        output_dir_frame.grid(row=row, column=1, columnspan=2, sticky="w", pady=6)
        self.output_dir_entry = ttk.Entry(output_dir_frame, textvariable=self.output_dir_var, width=40)
        self.output_dir_entry.pack(side="left")
        output_dir_button = ttk.Button(output_dir_frame, text="瀏覽", command=self.pick_output_dir)
        output_dir_button.pack(side="left", padx=(10, 0))
        add_tip("只有在選擇『輸出到指定資料夾』時會使用這個路徑。程式會自動建立不存在的資料夾。", output_dir_label, self.output_dir_entry, output_dir_button)

        row += 1
        suffix_label = ttk.Label(group_out, text="檔名後綴")
        suffix_label.grid(row=row, column=0, sticky="w", pady=6)
        suffix_entry = ttk.Entry(group_out, textvariable=self.filename_suffix_var, width=18)
        suffix_entry.grid(row=row, column=1, sticky="w", pady=6)
        suffix_hint = ttk.Label(group_out, text="預設 _360", foreground="#666666")
        suffix_hint.grid(row=row, column=2, sticky="w", pady=6, padx=(12, 0))
        add_tip("輸出檔名會接在原始檔名後面，例如 photo 變成 photo_360.jpg。", suffix_label, suffix_entry, suffix_hint)

        row += 1
        quality_label = ttk.Label(group_out, text="JPEG 品質")
        quality_label.grid(row=row, column=0, sticky="w", pady=6)
        quality_entry = ttk.Entry(group_out, textvariable=self.jpeg_quality_var, width=12)
        quality_entry.grid(row=row, column=1, sticky="w", pady=6)
        quality_hint = ttk.Label(group_out, text="1 到 100，預設 95", foreground="#666666")
        quality_hint.grid(row=row, column=2, sticky="w", pady=6, padx=(12, 0))
        add_tip(
            numeric_tooltip("JPEG 壓縮品質。數字越高畫質越好、檔案越大。360 圖通常建議 90 到 95。", "1 到 100"),
            quality_label,
            quality_entry,
            quality_hint,
        )

        row += 1
        overwrite_button = ttk.Checkbutton(group_out, text="若輸出檔已存在，直接覆蓋", variable=self.overwrite_existing_var)
        overwrite_button.grid(row=row, column=0, columnspan=3, sticky="w", pady=(12, 6))
        add_tip("開啟後，同名輸出檔會直接被新結果覆蓋。關閉時若檔案已存在，該筆會標記失敗避免誤蓋。", overwrite_button)

        # 進階與其他區塊
        group_adv = ttk.LabelFrame(parent, text="進階處理", padding=(16, 12))
        group_adv.pack(fill="x", padx=12, pady=(6, 12))
        group_adv.columnconfigure(0, weight=0, minsize=140)
        group_adv.columnconfigure(1, weight=0, minsize=260)
        group_adv.columnconfigure(2, weight=1)

        row = 0
        bg_label = ttk.Label(group_adv, text="透明背景補色")
        bg_label.grid(row=row, column=0, sticky="w", pady=6)
        bg_action_frame = ttk.Frame(group_adv)
        bg_action_frame.grid(row=row, column=1, columnspan=2, sticky="w", pady=6)
        bg_entry = ttk.Entry(bg_action_frame, textvariable=self.background_color_var, width=14)
        bg_entry.pack(side="left")
        bg_hint = ttk.Label(bg_action_frame, text="預設 #FFFFFF", foreground="#666666")
        bg_hint.pack(side="left", padx=(12, 0))
        bg_pick_button = ttk.Button(bg_action_frame, text="選色", command=self.pick_background_color)
        bg_pick_button.pack(side="left", padx=(10, 0))
        add_tip("PNG/WebP 等含透明背景時，轉 JPG 之前會先用這個顏色補底。請填十六進位色碼，例如 #FFFFFF。", bg_label, bg_entry, bg_hint, bg_pick_button)

        hint = (
            "💡 提示：預設值設計給不熟悉 360 metadata 的使用者：\n"
            "1. 非 2:1 直接自動裁切\n"
            "2. 輸出到原圖旁邊\n"
            "3. JPEG 品質 95\n"
            "4. metadata 只填安全且常用的欄位"
        )
        hint_label = ttk.Label(parent, text=hint, justify="left", foreground="#005A9E")
        hint_label.pack(fill="x", padx=16, pady=(0, 12))

    def _build_metadata_tab(self, parent: ttk.Frame) -> None:
        def add_tip(text: str, *widgets: tk.Widget) -> None:
            self._attach_tooltip(text, *widgets)

        # 基本投影資訊區塊
        group_basic = ttk.LabelFrame(parent, text="基本投影資訊", padding=(16, 12))
        group_basic.pack(fill="x", padx=12, pady=(12, 6))
        group_basic.columnconfigure(0, weight=0, minsize=240)
        group_basic.columnconfigure(1, weight=0, minsize=180)
        group_basic.columnconfigure(2, weight=1)

        row = 0
        projection_label = ttk.Label(group_basic, text="ProjectionType（投影類型）")
        projection_label.grid(row=row, column=0, sticky="w", pady=6)
        projection_entry = ttk.Entry(group_basic, textvariable=self.projection_type_var, width=22)
        projection_entry.grid(row=row, column=1, sticky="w", pady=6)
        projection_hint = ttk.Label(group_basic, text="一般保持 equirectangular", foreground="#666666")
        projection_hint.grid(row=row, column=2, sticky="w", pady=6, padx=(18, 0))
        add_tip("定義全景投影方式。大多數 360 平面展開圖都應保持 equirectangular，不建議隨意修改。", projection_label, projection_entry, projection_hint)

        row += 1
        use_viewer_button = ttk.Checkbutton(group_basic, text="UsePanoramaViewer（全景檢視）", variable=self.use_panorama_viewer_var)
        use_viewer_button.grid(row=row, column=0, columnspan=2, sticky="w", pady=6)
        exposure_lock_button = ttk.Checkbutton(group_basic, text="ExposureLockUsed（曝光鎖定）", variable=self.exposure_lock_used_var)
        exposure_lock_button.grid(row=row, column=2, sticky="w", pady=6, padx=(18, 0))
        add_tip("告訴支援平台這張圖應該用全景檢視器開啟。一般 360 圖建議維持勾選。", use_viewer_button)
        add_tip("表示拍攝來源是否鎖定曝光。對 AI 生成圖通常沒有明顯影響，可維持未勾選。", exposure_lock_button)

        row += 1
        source_count_label = ttk.Label(group_basic, text="SourcePhotosCount（來源張數）")
        source_count_label.grid(row=row, column=0, sticky="w", pady=6)
        source_count_entry = ttk.Entry(group_basic, textvariable=self.source_photos_count_var, width=14)
        source_count_entry.grid(row=row, column=1, sticky="w", pady=6)
        add_tip(
            numeric_tooltip("原始來源照片數量。單張 AI 圖通常填 1；若來自多張拼接，可填實際數量。", "1 以上整數", allow_blank=True),
            source_count_label,
            source_count_entry,
        )

        # 全景視角與姿態區塊
        group_pose = ttk.LabelFrame(parent, text="全景視角與姿態 (Pose & View)", padding=(16, 12))
        group_pose.pack(fill="x", padx=12, pady=6)
        group_pose.columnconfigure(0, weight=0, minsize=240)
        group_pose.columnconfigure(1, weight=0, minsize=140)
        group_pose.columnconfigure(2, weight=0, minsize=280)
        group_pose.columnconfigure(3, weight=1)

        row = 0
        pose_heading_label = ttk.Label(group_pose, text="PoseHeadingDegrees（全景朝向）")
        pose_heading_label.grid(row=row, column=0, sticky="w", pady=6)
        pose_heading_entry = ttk.Entry(group_pose, textvariable=self.pose_heading_var, width=14)
        pose_heading_entry.grid(row=row, column=1, sticky="w", pady=6)
        initial_heading_label = ttk.Label(group_pose, text="InitialViewHeadingDegrees（初始水平視角）")
        initial_heading_label.grid(row=row, column=2, sticky="w", pady=6, padx=(18, 0))
        initial_heading_entry = ttk.Entry(group_pose, textvariable=self.initial_heading_var, width=14)
        initial_heading_entry.grid(row=row, column=3, sticky="w", pady=6)
        add_tip(
            numeric_tooltip("整張全景的朝向角度，單位是度數。通常留白即可，只有想手動控制北向或主視角時才需要填。", "0 到 360", allow_blank=True),
            pose_heading_label,
            pose_heading_entry,
        )
        add_tip(
            numeric_tooltip("開啟全景時初始水平視角。平台若支援，會優先看向這個方向。", "0 到 360", allow_blank=True),
            initial_heading_label,
            initial_heading_entry,
        )

        row += 1
        pose_pitch_label = ttk.Label(group_pose, text="PosePitchDegrees（全景俯仰）")
        pose_pitch_label.grid(row=row, column=0, sticky="w", pady=6)
        pose_pitch_entry = ttk.Entry(group_pose, textvariable=self.pose_pitch_var, width=14)
        pose_pitch_entry.grid(row=row, column=1, sticky="w", pady=6)
        initial_pitch_label = ttk.Label(group_pose, text="InitialViewPitchDegrees（初始上下視角）")
        initial_pitch_label.grid(row=row, column=2, sticky="w", pady=6, padx=(18, 0))
        initial_pitch_entry = ttk.Entry(group_pose, textvariable=self.initial_pitch_var, width=14)
        initial_pitch_entry.grid(row=row, column=3, sticky="w", pady=6)
        add_tip(
            numeric_tooltip("整張全景的俯仰角。多數情況留白即可。", "-180 到 180", allow_blank=True),
            pose_pitch_label,
            pose_pitch_entry,
        )
        add_tip(
            numeric_tooltip("開啟全景時初始往上或往下看的角度。正值向上、負值向下。", "-180 到 180", allow_blank=True),
            initial_pitch_label,
            initial_pitch_entry,
        )

        row += 1
        pose_roll_label = ttk.Label(group_pose, text="PoseRollDegrees（全景傾斜）")
        pose_roll_label.grid(row=row, column=0, sticky="w", pady=6)
        pose_roll_entry = ttk.Entry(group_pose, textvariable=self.pose_roll_var, width=14)
        pose_roll_entry.grid(row=row, column=1, sticky="w", pady=6)
        initial_roll_label = ttk.Label(group_pose, text="InitialViewRollDegrees（初始畫面傾斜）")
        initial_roll_label.grid(row=row, column=2, sticky="w", pady=6, padx=(18, 0))
        initial_roll_entry = ttk.Entry(group_pose, textvariable=self.initial_roll_var, width=14)
        initial_roll_entry.grid(row=row, column=3, sticky="w", pady=6)
        add_tip(
            numeric_tooltip("整張全景的滾轉角度，用來校正水平線傾斜。多數情況留白即可。", "-180 到 180", allow_blank=True),
            pose_roll_label,
            pose_roll_entry,
        )
        add_tip(
            numeric_tooltip("開啟全景時初始畫面的傾斜角。通常不需要填。", "-180 到 180", allow_blank=True),
            initial_roll_label,
            initial_roll_entry,
        )

        row += 1
        initial_fov_label = ttk.Label(group_pose, text="InitialHorizontalFOVDegrees（初始視野）")
        initial_fov_label.grid(row=row, column=0, sticky="w", pady=6)
        initial_fov_entry = ttk.Entry(group_pose, textvariable=self.initial_fov_var, width=14)
        initial_fov_entry.grid(row=row, column=1, sticky="w", pady=6)
        initial_dolly_label = ttk.Label(group_pose, text="InitialCameraDolly（鏡頭推進）")
        initial_dolly_label.grid(row=row, column=2, sticky="w", pady=6, padx=(18, 0))
        initial_dolly_entry = ttk.Entry(group_pose, textvariable=self.initial_dolly_var, width=14)
        initial_dolly_entry.grid(row=row, column=3, sticky="w", pady=6)
        add_tip(
            numeric_tooltip("開啟全景時的水平視角大小。數字越大視野越廣，越小越像放大。", "大於 0 且小於或等於 360", allow_blank=True),
            initial_fov_label,
            initial_fov_entry,
        )
        add_tip(
            numeric_tooltip("控制初始視角的前後推進感，支援度不一定一致。通常可留白。", "任意數字", allow_blank=True),
            initial_dolly_label,
            initial_dolly_entry,
        )

        # 手動覆蓋尺寸與裁切 (Override) 區塊
        group_override = ttk.LabelFrame(parent, text="手動覆蓋尺寸與裁切 (Override)", padding=(16, 12))
        group_override.pack(fill="x", padx=12, pady=(6, 12))
        group_override.columnconfigure(0, weight=0, minsize=80)
        group_override.columnconfigure(1, weight=0, minsize=140)
        group_override.columnconfigure(2, weight=0, minsize=80)
        group_override.columnconfigure(3, weight=1)

        row = 0
        full_override_button = ttk.Checkbutton(group_override, text="手動指定 FullPanoWidth/Height（完整全景尺寸）", variable=self.full_pano_override_var)
        full_override_button.grid(row=row, column=0, columnspan=4, sticky="w", pady=6)

        row += 1
        full_width_label = ttk.Label(group_override, text="Width", foreground="#555555")
        full_width_label.grid(row=row, column=0, sticky="w", pady=6, padx=(24, 12))
        self.full_pano_width_entry = ttk.Entry(group_override, textvariable=self.full_pano_width_var, width=14)
        self.full_pano_width_entry.grid(row=row, column=1, sticky="w", pady=6)
        full_height_label = ttk.Label(group_override, text="Height", foreground="#555555")
        full_height_label.grid(row=row, column=2, sticky="w", pady=6, padx=(12, 12))
        self.full_pano_height_entry = ttk.Entry(group_override, textvariable=self.full_pano_height_var, width=14)
        self.full_pano_height_entry.grid(row=row, column=3, sticky="w", pady=6)
        add_tip(
            numeric_tooltip("覆蓋整張完整全景的寬高。預設會直接使用輸出圖片尺寸，只有處理裁切來源時才較常需要改。", "1 以上整數", allow_blank=True),
            full_override_button,
            full_width_label,
            self.full_pano_width_entry,
            full_height_label,
            self.full_pano_height_entry,
        )

        row += 1
        ttk.Separator(group_override, orient="horizontal").grid(row=row, column=0, columnspan=4, sticky="ew", pady=8)

        row += 1
        cropped_override_button = ttk.Checkbutton(group_override, text="手動指定 CroppedAreaLeft/Top（裁切起點）", variable=self.cropped_area_override_var)
        cropped_override_button.grid(row=row, column=0, columnspan=4, sticky="w", pady=6)

        row += 1
        cropped_left_label = ttk.Label(group_override, text="Left", foreground="#555555")
        cropped_left_label.grid(row=row, column=0, sticky="w", pady=6, padx=(24, 12))
        self.cropped_area_left_entry = ttk.Entry(group_override, textvariable=self.cropped_area_left_var, width=14)
        self.cropped_area_left_entry.grid(row=row, column=1, sticky="w", pady=6)
        cropped_top_label = ttk.Label(group_override, text="Top", foreground="#555555")
        cropped_top_label.grid(row=row, column=2, sticky="w", pady=6, padx=(12, 12))
        self.cropped_area_top_entry = ttk.Entry(group_override, textvariable=self.cropped_area_top_var, width=14)
        self.cropped_area_top_entry.grid(row=row, column=3, sticky="w", pady=6)
        add_tip(
            numeric_tooltip("手動指定裁切區塊在完整全景中的左上角座標。一般單張輸出預設 0,0 就夠了。", "0 以上整數"),
            cropped_override_button,
            cropped_left_label,
            self.cropped_area_left_entry,
            cropped_top_label,
            self.cropped_area_top_entry,
        )

        row += 1
        ttk.Separator(group_override, orient="horizontal").grid(row=row, column=0, columnspan=4, sticky="ew", pady=8)

        row += 1
        largest_override_button = ttk.Checkbutton(
            group_override, text="手動指定 LargestValidInteriorRect（有效區域）", variable=self.largest_rect_override_var
        )
        largest_override_button.grid(row=row, column=0, columnspan=4, sticky="w", pady=6)

        row += 1
        largest_left_label = ttk.Label(group_override, text="Left", foreground="#555555")
        largest_left_label.grid(row=row, column=0, sticky="w", pady=6, padx=(24, 12))
        self.largest_rect_left_entry = ttk.Entry(group_override, textvariable=self.largest_rect_left_var, width=14)
        self.largest_rect_left_entry.grid(row=row, column=1, sticky="w", pady=6)
        largest_top_label = ttk.Label(group_override, text="Top", foreground="#555555")
        largest_top_label.grid(row=row, column=2, sticky="w", pady=6, padx=(12, 12))
        self.largest_rect_top_entry = ttk.Entry(group_override, textvariable=self.largest_rect_top_var, width=14)
        self.largest_rect_top_entry.grid(row=row, column=3, sticky="w", pady=6)
        add_tip(
            numeric_tooltip("標示畫面中有效矩形區域的位置與大小。若圖片沒有黑邊或遮罩，通常不必手動指定。", "0 以上整數"),
            largest_override_button,
            largest_left_label,
            self.largest_rect_left_entry,
            largest_top_label,
            self.largest_rect_top_entry,
        )

        row += 1
        largest_width_label = ttk.Label(group_override, text="Width", foreground="#555555")
        largest_width_label.grid(row=row, column=0, sticky="w", pady=6, padx=(24, 12))
        self.largest_rect_width_entry = ttk.Entry(group_override, textvariable=self.largest_rect_width_var, width=14)
        self.largest_rect_width_entry.grid(row=row, column=1, sticky="w", pady=6)
        largest_height_label = ttk.Label(group_override, text="Height", foreground="#555555")
        largest_height_label.grid(row=row, column=2, sticky="w", pady=6, padx=(12, 12))
        self.largest_rect_height_entry = ttk.Entry(group_override, textvariable=self.largest_rect_height_var, width=14)
        self.largest_rect_height_entry.grid(row=row, column=3, sticky="w", pady=6)
        add_tip(
            numeric_tooltip("有效矩形區域的上方偏移量。通常保持自動值即可。", "0 以上整數"),
            largest_top_label,
            self.largest_rect_top_entry,
        )
        add_tip(
            numeric_tooltip("有效矩形區域的寬度。未勾選 override 時會自動使用輸出圖片寬度。", "1 以上整數", allow_blank=True),
            largest_width_label,
            self.largest_rect_width_entry,
            largest_height_label,
            self.largest_rect_height_entry,
        )

        note = (
            "💡 進階欄位全部都有預設狀態：\n"
            "1. 空白代表不額外寫入，避免給新手多餘負擔\n"
            "2. 未勾選 override 時，自動用輸出圖片尺寸與左上角 0,0\n"
            "3. 除非你知道自己在做什麼，否則建議保持預設"
        )
        note_label = ttk.Label(parent, text=note, justify="left", foreground="#005A9E")
        note_label.pack(fill="x", padx=16, pady=(0, 12))

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        self.log_text = tk.Text(parent, wrap="word", height=24)
        log_scroll = ttk.Scrollbar(parent, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(fill="both", expand=True, side="left")
        log_scroll.pack(fill="y", side="right")

    def _toggle_output_dir_state(self) -> None:
        state = "normal" if self.output_mode_var.get() == "custom_folder" else "disabled"
        self.output_dir_entry.configure(state=state)

    def _toggle_metadata_override_states(self) -> None:
        full_state = "normal" if self.full_pano_override_var.get() else "disabled"
        cropped_state = "normal" if self.cropped_area_override_var.get() else "disabled"
        largest_state = "normal" if self.largest_rect_override_var.get() else "disabled"

        for widget in (self.full_pano_width_entry, self.full_pano_height_entry):
            widget.configure(state=full_state)
        for widget in (self.cropped_area_left_entry, self.cropped_area_top_entry):
            widget.configure(state=cropped_state)
        for widget in (
            self.largest_rect_left_entry,
            self.largest_rect_top_entry,
            self.largest_rect_width_entry,
            self.largest_rect_height_entry,
        ):
            widget.configure(state=largest_state)

    def pick_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="選擇圖片",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.webp *.bmp *.tiff *.tif"), ("All files", "*.*")],
        )
        if paths:
            self.add_paths(list(paths))

    def pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="選擇資料夾")
        if not folder:
            return
        try:
            paths = gather_images_from_folder(folder, self.recursive_scan_var.get())
        except Exception as exc:
            messagebox.showerror("加入資料夾失敗", str(exc))
            return
        if not paths:
            messagebox.showinfo("沒有找到圖片", "這個資料夾內沒有可支援的圖片格式。")
            return
        self.add_paths(paths)

    def pick_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="選擇輸出資料夾")
        if folder:
            self.output_dir_var.set(folder)

    def pick_background_color(self) -> None:
        _, color = colorchooser.askcolor(color=self.background_color_var.get() or "#FFFFFF", title="選擇透明背景補色")
        if color:
            self.background_color_var.set(color.upper())

    def open_selected_output(self, _event: tk.Event | None = None) -> None:
        selection = self.file_tree.selection()
        if not selection:
            return

        item_id = selection[0]
        output_path = self.file_tree.set(item_id, "output")
        if not output_path or not os.path.isfile(output_path):
            return

        try:
            os.startfile(output_path)
        except OSError as exc:
            messagebox.showerror("開啟輸出檔失敗", str(exc))

    def add_paths(self, paths: list[str]) -> None:
        added = 0
        for raw_path in paths:
            path = os.path.abspath(raw_path)
            if os.path.isdir(path):
                sub_paths = gather_images_from_folder(path, self.recursive_scan_var.get())
                self.add_paths(sub_paths)
                continue
            if not os.path.isfile(path):
                continue
            if Path(path).suffix.lower() not in SUPPORTED_INPUT_EXT:
                continue
            if path in self.file_items.values():
                continue
            item_id = self.file_tree.insert("", "end", text=path, values=("待處理", ""))
            self.file_items[item_id] = path
            added += 1

        if added:
            self._refresh_summary()

    def remove_selected(self) -> None:
        for item_id in self.file_tree.selection():
            self.file_tree.delete(item_id)
            self.file_items.pop(item_id, None)
        self._refresh_summary()

    def clear_files(self) -> None:
        for item_id in list(self.file_items):
            self.file_tree.delete(item_id)
            self.file_items.pop(item_id, None)
        self._refresh_summary()

    def reset_defaults(self) -> None:
        self.recursive_scan_var.set(False)
        self.non_2to1_action_var.set("crop")
        self.ratio_tolerance_var.set("0.01")
        self.crop_anchor_x_var.set("50")
        self.crop_anchor_y_var.set("50")
        self.output_mode_var.set("same_folder")
        self.output_dir_var.set("")
        self.filename_suffix_var.set("_360")
        self.overwrite_existing_var.set(False)
        self.jpeg_quality_var.set("95")
        self.background_color_var.set("#FFFFFF")
        self.projection_type_var.set("equirectangular")
        self.use_panorama_viewer_var.set(True)
        self.source_photos_count_var.set("1")
        self.exposure_lock_used_var.set(False)
        self.pose_heading_var.set("")
        self.pose_pitch_var.set("")
        self.pose_roll_var.set("")
        self.initial_heading_var.set("")
        self.initial_pitch_var.set("")
        self.initial_roll_var.set("")
        self.initial_fov_var.set("")
        self.initial_dolly_var.set("")
        self.full_pano_override_var.set(False)
        self.full_pano_width_var.set("")
        self.full_pano_height_var.set("")
        self.cropped_area_override_var.set(False)
        self.cropped_area_left_var.set("0")
        self.cropped_area_top_var.set("0")
        self.largest_rect_override_var.set(False)
        self.largest_rect_left_var.set("0")
        self.largest_rect_top_var.set("0")
        self.largest_rect_width_var.set("")
        self.largest_rect_height_var.set("")
        self._toggle_output_dir_state()
        self._toggle_metadata_override_states()
        self._append_log("已還原所有預設值")

    def _refresh_summary(self) -> None:
        count = len(self.file_items)
        if count == 0:
            self.summary_var.set("尚未加入任何圖片")
            return

        status_counts = {"待處理": 0, "處理中": 0, "完成": 0, "失敗": 0}
        for item_id in self.file_items:
            status = self.file_tree.set(item_id, "status") or "待處理"
            if status not in status_counts:
                status_counts["待處理"] += 1
            else:
                status_counts[status] += 1

        self.summary_var.set(
            f"共 {count} 張 | 待處理 {status_counts['待處理']} | 處理中 {status_counts['處理中']} | 完成 {status_counts['完成']} | 失敗 {status_counts['失敗']}"
        )

    def _reset_batch_statuses(self) -> None:
        for item_id in self.file_items:
            self.file_tree.set(item_id, "status", "待處理")
            self.file_tree.set(item_id, "output", "")
        self.progress.configure(maximum=max(len(self.file_items), 1))
        self.progress_var.set(0.0)
        self._refresh_summary()

    def _append_log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.update_idletasks()

    def collect_settings(self) -> PanoSettings:
        ratio_tolerance = parse_float_in_range(self.ratio_tolerance_var.get(), "比例容差", min_value=0.0)
        crop_anchor_x = parse_float_in_range(self.crop_anchor_x_var.get(), "裁切保留位置 X (%)", min_value=0.0, max_value=100.0)
        crop_anchor_y = parse_float_in_range(self.crop_anchor_y_var.get(), "裁切保留位置 Y (%)", min_value=0.0, max_value=100.0)
        jpeg_quality = parse_int_in_range(self.jpeg_quality_var.get(), "JPEG 品質", min_value=1, max_value=100)
        background_color = self.background_color_var.get().strip() or "#FFFFFF"
        try:
            ImageColor.getrgb(background_color)
        except ValueError as exc:
            raise ValueError("透明背景補色格式無效，請輸入像 #FFFFFF 這樣的色碼") from exc

        return PanoSettings(
            non_2to1_action=self.non_2to1_action_var.get(),
            ratio_tolerance=ratio_tolerance,
            crop_anchor_x=crop_anchor_x,
            crop_anchor_y=crop_anchor_y,
            output_mode=self.output_mode_var.get(),
            output_dir=self.output_dir_var.get().strip(),
            filename_suffix=self.filename_suffix_var.get(),
            overwrite_existing=self.overwrite_existing_var.get(),
            jpeg_quality=jpeg_quality,
            background_color=background_color,
            projection_type=self.projection_type_var.get().strip() or "equirectangular",
            use_panorama_viewer=self.use_panorama_viewer_var.get(),
            source_photos_count=parse_optional_int_in_range(self.source_photos_count_var.get(), "SourcePhotosCount", min_value=1),
            exposure_lock_used=self.exposure_lock_used_var.get(),
            pose_heading_degrees=parse_optional_float_in_range(
                self.pose_heading_var.get(), "PoseHeadingDegrees", min_value=0.0, max_value=360.0
            ),
            pose_pitch_degrees=parse_optional_float_in_range(
                self.pose_pitch_var.get(), "PosePitchDegrees", min_value=-180.0, max_value=180.0
            ),
            pose_roll_degrees=parse_optional_float_in_range(
                self.pose_roll_var.get(), "PoseRollDegrees", min_value=-180.0, max_value=180.0
            ),
            initial_view_heading_degrees=parse_optional_float_in_range(
                self.initial_heading_var.get(), "InitialViewHeadingDegrees", min_value=0.0, max_value=360.0
            ),
            initial_view_pitch_degrees=parse_optional_float_in_range(
                self.initial_pitch_var.get(), "InitialViewPitchDegrees", min_value=-180.0, max_value=180.0
            ),
            initial_view_roll_degrees=parse_optional_float_in_range(
                self.initial_roll_var.get(), "InitialViewRollDegrees", min_value=-180.0, max_value=180.0
            ),
            initial_horizontal_fov_degrees=parse_optional_float_in_range(
                self.initial_fov_var.get(), "InitialHorizontalFOVDegrees", min_value=0.000001, max_value=360.0
            ),
            initial_camera_dolly=parse_optional_float(self.initial_dolly_var.get(), "InitialCameraDolly"),
            full_pano_override=self.full_pano_override_var.get(),
            full_pano_width=parse_optional_int_in_range(self.full_pano_width_var.get(), "FullPanoWidthPixels", min_value=1),
            full_pano_height=parse_optional_int_in_range(self.full_pano_height_var.get(), "FullPanoHeightPixels", min_value=1),
            cropped_area_override=self.cropped_area_override_var.get(),
            cropped_area_left=parse_int_in_range(self.cropped_area_left_var.get() or "0", "CroppedAreaLeftPixels", min_value=0),
            cropped_area_top=parse_int_in_range(self.cropped_area_top_var.get() or "0", "CroppedAreaTopPixels", min_value=0),
            largest_rect_override=self.largest_rect_override_var.get(),
            largest_rect_left=parse_int_in_range(self.largest_rect_left_var.get() or "0", "LargestValidInteriorRectLeft", min_value=0),
            largest_rect_top=parse_int_in_range(self.largest_rect_top_var.get() or "0", "LargestValidInteriorRectTop", min_value=0),
            largest_rect_width=parse_optional_int_in_range(
                self.largest_rect_width_var.get(), "LargestValidInteriorRectWidth", min_value=1
            ),
            largest_rect_height=parse_optional_int_in_range(
                self.largest_rect_height_var.get(), "LargestValidInteriorRectHeight", min_value=1
            ),
        )

    def start_batch(self) -> None:
        if self.is_processing:
            return
        if not self.file_items:
            messagebox.showinfo("沒有檔案", "請先加入至少一張圖片。")
            return

        try:
            self.batch_settings = self.collect_settings()
        except Exception as exc:
            messagebox.showerror("設定有誤", str(exc))
            return

        self._reset_batch_statuses()
        self.processing_queue = list(self.file_items.keys())
        self.is_processing = True
        self.start_button.configure(state="disabled")
        self._append_log("=" * 60)
        self._append_log("開始批次處理")
        self.after(10, self._process_next)

    def _process_next(self) -> None:
        if not self.processing_queue:
            self.is_processing = False
            self.start_button.configure(state="normal")
            self._append_log("全部處理完成")
            self._refresh_summary()
            return

        item_id = self.processing_queue.pop(0)
        input_path = self.file_items[item_id]
        self.file_tree.set(item_id, "status", "處理中")
        self._refresh_summary()
        self._append_log(f"[處理] {input_path}")

        def log(message: str) -> None:
            self._append_log(f"  - {message}")

        try:
            result = process_image(input_path, self.batch_settings, log)
            self.file_tree.set(item_id, "status", "完成")
            self.file_tree.set(item_id, "output", result.output_path)
            self._append_log(f"  - 完成，輸出：{result.output_path}")
        except Exception as exc:
            self.file_tree.set(item_id, "status", "失敗")
            self.file_tree.set(item_id, "output", str(exc))
            self._append_log(f"  - 失敗：{exc}")

        processed_count = len(self.file_items) - len(self.processing_queue)
        self.progress_var.set(float(processed_count))
        self._refresh_summary()
        self.after(10, self._process_next)


def process_cli(paths: list[str]) -> int:
    settings = PanoSettings()
    if not paths:
        print("請提供至少一個檔案路徑")
        return 1

    exit_code = 0
    for path in paths:
        print(f"\n處理檔案: {path}")
        try:
            result = process_image(path, settings, print)
            print(f"完成：{result.output_path}")
        except Exception as exc:
            exit_code = 1
            print(f"失敗：{exc}")
    return exit_code


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--cli":
        return process_cli(args[1:])

    app = PanoFixerApp(startup_paths=args)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
