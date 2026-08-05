from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Tuple

try:
    import cv2
    import numpy as np
except ModuleNotFoundError:
    print(
        "Thiếu thư viện xử lý ảnh. Cài bằng: pip install opencv-python",
        file=sys.stderr,
    )
    sys.exit(1)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_DIR = BASE_DIR / "source"
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_MARK_PATH = BASE_DIR / "mark.jpeg"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
DOWNLOAD_SUFFIX = ".crdownload"


Box = Tuple[int, int, int, int]
RatioBox = Tuple[float, float, float, float]


def _parse_csv_numbers(raw: str, expected_len: int, value_type: type) -> tuple:
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if len(values) != expected_len:
        raise argparse.ArgumentTypeError(
            f"Cần {expected_len} giá trị phân tách bằng dấu phẩy."
        )

    try:
        return tuple(value_type(value) for value in values)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Giá trị không hợp lệ: {raw}") from exc


def parse_box(raw: str) -> Box:
    return _parse_csv_numbers(raw, 4, int)


def parse_ratio_box(raw: str) -> RatioBox:
    values = _parse_csv_numbers(raw, 4, float)
    if any(value < 0 or value > 1 for value in values):
        raise argparse.ArgumentTypeError("Tỉ lệ phải nằm trong khoảng 0..1.")
    return values


def is_supported_image(path: Path) -> bool:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if not suffixes:
        return False

    if suffixes[-1] in IMAGE_EXTENSIONS:
        return True

    return (
        suffixes[-1] == DOWNLOAD_SUFFIX
        and len(suffixes) >= 2
        and suffixes[-2] in IMAGE_EXTENSIONS
    )


def iter_images(source_dir: Path, recursive: bool) -> Iterable[Path]:
    pattern = "**/*" if recursive else "*"
    for path in sorted(source_dir.glob(pattern)):
        if path.is_file() and is_supported_image(path):
            yield path


def output_name_for(input_path: Path, timestamp: str) -> str:
    if input_path.suffix.lower() == DOWNLOAD_SUFFIX and len(input_path.suffixes) >= 2:
        normalized = input_path.with_suffix("")
    else:
        normalized = input_path
    return f"{normalized.stem}_{timestamp}{normalized.suffix}"


def read_image(path: Path, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def write_image(path: Path, image: np.ndarray) -> None:
    ext = path.suffix.lower() or ".png"
    success, data = cv2.imencode(ext, image)
    if not success:
        raise ValueError(f"Không thể ghi định dạng ảnh: {ext}")
    data.tofile(str(path))


def clamp_box(box: Box, width: int, height: int) -> Box:
    x1, y1, x2, y2 = box
    x1 = min(max(x1, 0), width)
    x2 = min(max(x2, 0), width)
    y1 = min(max(y1, 0), height)
    y2 = min(max(y2, 0), height)
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def ratio_box_to_pixels(box: RatioBox, width: int, height: int) -> Box:
    x1, y1, x2, y2 = box
    return clamp_box(
        (
            int(round(x1 * width)),
            int(round(y1 * height)),
            int(round(x2 * width)),
            int(round(y2 * height)),
        ),
        width,
        height,
    )


def remove_watermark(
    image: np.ndarray,
    box: Box,
    inpaint_radius: int,
    padding: int,
) -> np.ndarray:
    height, width = image.shape[:2]
    x1, y1, x2, y2 = clamp_box(box, width, height)
    if x1 == x2 or y1 == y2:
        return image

    mask = np.zeros((height, width), dtype=np.uint8)
    mask[y1:y2, x1:x2] = 255

    if padding > 0:
        kernel = np.ones((padding, padding), dtype=np.uint8)
        mask = cv2.dilate(mask, kernel, iterations=1)

    return cv2.inpaint(image, mask, inpaint_radius, cv2.INPAINT_TELEA)


def load_mark(
    mark_path: Path,
    target_width: int,
    opacity: float,
    opaque_mark: bool,
    dark_threshold: int,
    target_height: Optional[int] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    mark = read_image(mark_path, cv2.IMREAD_UNCHANGED)
    if mark is None:
        raise ValueError(f"Không đọc được watermark: {mark_path}")

    if target_width <= 0:
        raise ValueError("Chiều rộng watermark phải lớn hơn 0.")

    if target_height is None:
        scale = target_width / mark.shape[1]
        target_height = max(1, int(round(mark.shape[0] * scale)))
    elif target_height <= 0:
        raise ValueError("Chiều cao watermark phải lớn hơn 0.")

    mark = cv2.resize(mark, (target_width, target_height), interpolation=cv2.INTER_AREA)

    if mark.ndim == 2:
        bgr = cv2.cvtColor(mark, cv2.COLOR_GRAY2BGR)
        base_alpha = np.ones(mark.shape, dtype=np.float32)
    elif mark.shape[2] == 4:
        bgr = mark[:, :, :3]
        if opaque_mark:
            base_alpha = np.ones(bgr.shape[:2], dtype=np.float32)
        else:
            base_alpha = mark[:, :, 3].astype(np.float32) / 255.0
    else:
        bgr = mark[:, :, :3]
        if opaque_mark:
            base_alpha = np.ones(bgr.shape[:2], dtype=np.float32)
        else:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
            base_alpha = np.clip(
                (gray - dark_threshold) / max(1, 255 - dark_threshold),
                0.0,
                1.0,
            )

    alpha = np.clip(base_alpha * opacity, 0.0, 1.0)
    return bgr, alpha


def get_mark_position(
    position: str,
    image_width: int,
    image_height: int,
    mark_width: int,
    mark_height: int,
    margin: int,
) -> Tuple[int, int]:
    center_x = (image_width - mark_width) // 2
    center_y = (image_height - mark_height) // 2

    positions = {
        "center": (center_x, center_y),
        "top-left": (margin, margin),
        "top": (center_x, margin),
        "top-right": (image_width - mark_width - margin, margin),
        "bottom-left": (margin, image_height - mark_height - margin),
        "bottom": (center_x, image_height - mark_height - margin),
        "bottom-right": (
            image_width - mark_width - margin,
            image_height - mark_height - margin,
        ),
    }
    return positions[position]


def overlay_mark(
    image: np.ndarray,
    mark_bgr: np.ndarray,
    mark_alpha: np.ndarray,
    position: str,
    margin: int,
) -> np.ndarray:
    result = image.copy()
    image_height, image_width = result.shape[:2]
    mark_height, mark_width = mark_bgr.shape[:2]
    x, y = get_mark_position(
        position, image_width, image_height, mark_width, mark_height, margin
    )

    src_x1 = max(0, -x)
    src_y1 = max(0, -y)
    dst_x1 = max(0, x)
    dst_y1 = max(0, y)
    dst_x2 = min(image_width, x + mark_width)
    dst_y2 = min(image_height, y + mark_height)

    if dst_x1 >= dst_x2 or dst_y1 >= dst_y2:
        return result

    src_x2 = src_x1 + (dst_x2 - dst_x1)
    src_y2 = src_y1 + (dst_y2 - dst_y1)

    roi = result[dst_y1:dst_y2, dst_x1:dst_x2].astype(np.float32)
    mark_roi = mark_bgr[src_y1:src_y2, src_x1:src_x2].astype(np.float32)
    alpha = mark_alpha[src_y1:src_y2, src_x1:src_x2, None]

    blended = mark_roi * alpha + roi * (1.0 - alpha)
    result[dst_y1:dst_y2, dst_x1:dst_x2] = np.clip(blended, 0, 255).astype(np.uint8)
    return result


def overlay_mark_at(
    image: np.ndarray,
    mark_bgr: np.ndarray,
    mark_alpha: np.ndarray,
    x: int,
    y: int,
) -> np.ndarray:
    result = image.copy()
    image_height, image_width = result.shape[:2]
    mark_height, mark_width = mark_bgr.shape[:2]

    src_x1 = max(0, -x)
    src_y1 = max(0, -y)
    dst_x1 = max(0, x)
    dst_y1 = max(0, y)
    dst_x2 = min(image_width, x + mark_width)
    dst_y2 = min(image_height, y + mark_height)

    if dst_x1 >= dst_x2 or dst_y1 >= dst_y2:
        return result

    src_x2 = src_x1 + (dst_x2 - dst_x1)
    src_y2 = src_y1 + (dst_y2 - dst_y1)

    roi = result[dst_y1:dst_y2, dst_x1:dst_x2].astype(np.float32)
    mark_roi = mark_bgr[src_y1:src_y2, src_x1:src_x2].astype(np.float32)
    alpha = mark_alpha[src_y1:src_y2, src_x1:src_x2, None]

    blended = mark_roi * alpha + roi * (1.0 - alpha)
    result[dst_y1:dst_y2, dst_x1:dst_x2] = np.clip(blended, 0, 255).astype(np.uint8)
    return result


def fit_size_keep_ratio(
    source_width: int,
    source_height: int,
    max_width: int,
    max_height: int,
) -> Tuple[int, int]:
    if source_width <= 0 or source_height <= 0:
        raise ValueError("Kích thước watermark không hợp lệ.")
    if max_width <= 0 or max_height <= 0:
        raise ValueError("Vùng chèn watermark không hợp lệ.")

    scale = min(max_width / source_width, max_height / source_height)
    return (
        max(1, int(round(source_width * scale))),
        max(1, int(round(source_height * scale))),
    )


def load_mark_fit_box(
    mark_path: Path,
    max_width: int,
    max_height: int,
    opacity: float,
    opaque_mark: bool,
    dark_threshold: int,
) -> Tuple[np.ndarray, np.ndarray]:
    mark = read_image(mark_path, cv2.IMREAD_UNCHANGED)
    if mark is None:
        raise ValueError(f"Không đọc được watermark: {mark_path}")

    mark_height, mark_width = mark.shape[:2]
    target_width, target_height = fit_size_keep_ratio(
        mark_width,
        mark_height,
        max_width,
        max_height,
    )
    return load_mark(
        mark_path,
        target_width,
        opacity,
        opaque_mark,
        dark_threshold,
        target_height,
    )


def process_image(
    input_path: Path,
    output_path: Path,
    mark_path: Path,
    args: argparse.Namespace,
) -> None:
    image = read_image(input_path)
    if image is None:
        raise ValueError("Không đọc được ảnh hoặc file chưa tải xong.")

    height, width = image.shape[:2]
    default_watermark_box = args.remove_box or ratio_box_to_pixels(
        args.remove_box_ratio, width, height
    )

    if not args.no_remove:
        image = remove_watermark(
            image,
            default_watermark_box,
            args.inpaint_radius,
            args.remove_padding,
        )

    if not args.no_add_mark:
        if args.add_mode == "old-box":
            add_box = args.add_box or (
                ratio_box_to_pixels(args.add_box_ratio, width, height)
                if args.add_box_ratio
                else default_watermark_box
            )
            x1, y1, x2, y2 = clamp_box(add_box, width, height)
            box_width = max(1, x2 - x1)
            box_height = max(1, y2 - y1)
            mark_bgr, mark_alpha = load_mark_fit_box(
                mark_path,
                box_width,
                box_height,
                args.opacity,
                args.opaque_mark,
                args.dark_threshold,
            )
            mark_height, mark_width = mark_bgr.shape[:2]
            paste_x = x1 + (box_width - mark_width) // 2
            paste_y = y1 + (box_height - mark_height) // 2
            image = overlay_mark_at(image, mark_bgr, mark_alpha, paste_x, paste_y)
        else:
            mark_width = max(1, int(round(width * args.mark_width_ratio)))
            mark_bgr, mark_alpha = load_mark(
                mark_path,
                mark_width,
                args.opacity,
                args.opaque_mark,
                args.dark_threshold,
            )
            image = overlay_mark(image, mark_bgr, mark_alpha, args.position, args.margin)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_image(output_path, image)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Xoá watermark cũ và chèn watermark mới cho toàn bộ ảnh trong thư mục."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"Thư mục ảnh nguồn, default: {DEFAULT_SOURCE_DIR}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Thư mục ảnh output, default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--mark",
        type=Path,
        default=DEFAULT_MARK_PATH,
        help=f"File watermark mới, default: {DEFAULT_MARK_PATH}",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Quét cả thư mục con trong source.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Ghi đè file output nếu đã tồn tại.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chỉ in danh sách file sẽ xử lý, không ghi ảnh.",
    )
    parser.add_argument(
        "--no-remove",
        action="store_true",
        help="Không xoá watermark cũ.",
    )
    parser.add_argument(
        "--remove-box",
        type=parse_box,
        help="Vùng xoá theo pixel: x1,y1,x2,y2. Ví dụ: 720,484,1014,576",
    )
    parser.add_argument(
        "--remove-box-ratio",
        type=parse_ratio_box,
        default=parse_ratio_box("0.70,0.84,0.99,1"),
        help="Vùng xoá theo tỉ lệ ảnh: x1,y1,x2,y2. Default: 0.70,0.84,0.99,1",
    )
    parser.add_argument(
        "--remove-padding",
        type=int,
        default=8,
        help="Nới vùng mask trước khi inpaint, default: 8",
    )
    parser.add_argument(
        "--inpaint-radius",
        type=int,
        default=7,
        help="Bán kính inpaint, default: 7",
    )
    parser.add_argument(
        "--no-add-mark",
        action="store_true",
        help="Không chèn watermark mới.",
    )
    parser.add_argument(
        "--add-mode",
        choices=["old-box", "position"],
        default="old-box",
        help="Cách chèn watermark mới: old-box = đúng vùng watermark cũ, position = theo vị trí. Default: old-box",
    )
    parser.add_argument(
        "--add-box",
        type=parse_box,
        help="Vùng chèn watermark mới theo pixel: x1,y1,x2,y2. Mặc định dùng vùng xoá watermark cũ.",
    )
    parser.add_argument(
        "--add-box-ratio",
        type=parse_ratio_box,
        help="Vùng chèn watermark mới theo tỉ lệ ảnh: x1,y1,x2,y2. Mặc định dùng vùng xoá watermark cũ.",
    )
    parser.add_argument(
        "--position",
        choices=[
            "center",
            "top-left",
            "top",
            "top-right",
            "bottom-left",
            "bottom",
            "bottom-right",
        ],
        default="center",
        help="Vị trí chèn watermark mới khi --add-mode position, default: center",
    )
    parser.add_argument(
        "--mark-width-ratio",
        type=float,
        default=0.65,
        help="Chiều rộng watermark mới khi --add-mode position, default: 0.65",
    )
    parser.add_argument(
        "--opacity",
        type=float,
        default=1.0,
        help="Độ đậm watermark mới 0..1, default: 1.0",
    )
    parser.add_argument(
        "--margin",
        type=int,
        default=24,
        help="Khoảng cách mép khi position không phải center, default: 24",
    )
    parser.add_argument(
        "--opaque-mark",
        action="store_true",
        default=True,
        help="Giữ nguyên nền của file watermark. Đây là mặc định.",
    )
    parser.add_argument(
        "--transparent-mark",
        action="store_false",
        dest="opaque_mark",
        help="Tự bỏ nền tối của watermark mới, chỉ giữ chữ/logo sáng.",
    )
    parser.add_argument(
        "--dark-threshold",
        type=int,
        default=35,
        help="Ngưỡng bỏ nền tối cho watermark không có alpha, default: 35",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    source_dir = args.source.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    mark_path = args.mark.expanduser().resolve()

    if not source_dir.is_dir():
        print(f"Không tìm thấy thư mục source: {source_dir}", file=sys.stderr)
        sys.exit(1)

    if not mark_path.is_file():
        print(f"Không tìm thấy file watermark: {mark_path}", file=sys.stderr)
        sys.exit(1)

    images = list(iter_images(source_dir, args.recursive))
    if not images:
        print(f"Không tìm thấy ảnh trong: {source_dir}")
        return

    success_count = 0
    skipped_count = 0
    failed_count = 0
    dry_run_count = 0
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for input_path in images:
        output_name = output_name_for(input_path, timestamp)
        output_path = output_dir / output_name

        if args.dry_run:
            dry_run_count += 1
            print(f"[dry-run] {input_path} -> {output_path}")
            continue

        if output_path.exists() and not args.overwrite:
            skipped_count += 1
            print(f"[skip] Đã tồn tại: {output_path}")
            continue

        try:
            process_image(input_path, output_path, mark_path, args)
        except Exception as exc:
            failed_count += 1
            print(f"[fail] {input_path}: {exc}", file=sys.stderr)
            continue

        success_count += 1
        print(f"[ok] {input_path.name} -> {output_path.name}")

    if args.dry_run:
        print(f"Hoàn tất dry-run: {dry_run_count} file sẽ được xử lý.")
    else:
        print(
            "Hoàn tất: "
            f"{success_count} thành công, {skipped_count} bỏ qua, {failed_count} lỗi."
        )


if __name__ == "__main__":
    main()
