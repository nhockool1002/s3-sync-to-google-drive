from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

try:
    from PIL import Image, ImageOps
except ModuleNotFoundError:
    print(
        "Thiếu thư viện nén ảnh. Cài bằng: pip install Pillow pillow-avif-plugin",
        file=sys.stderr,
    )
    sys.exit(1)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "compress" / "source"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "compress" / "output"

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".avif",
    ".bmp",
    ".tif",
    ".tiff",
}
DOWNLOAD_SUFFIX = ".crdownload"


@dataclass(frozen=True)
class CompressResult:
    input_path: Path
    output_path: Path
    original_size: int
    output_size: int
    saved_size: int
    saved_ratio: float


def _register_avif() -> bool:
    try:
        import pillow_avif  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


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


def output_path_for(
    input_path: Path,
    source_dir: Path,
    output_dir: Path,
    recursive: bool,
    output_format: Optional[str],
    timestamp: str,
) -> Path:
    relative = input_path.relative_to(source_dir) if recursive else Path(input_path.name)
    if relative.suffix.lower() == DOWNLOAD_SUFFIX and len(relative.suffixes) >= 2:
        relative = relative.with_suffix("")

    if output_format:
        relative = relative.with_suffix(f".{output_format.lower()}")
    elif relative.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".avif"):
        relative = relative.with_suffix(".jpg")
    relative = relative.with_name(f"{relative.stem}_{timestamp}{relative.suffix}")
    return output_dir / relative


def _needs_rgb(image: Image.Image, output_format: str) -> bool:
    return output_format in {"JPEG", "AVIF"} and image.mode not in ("RGB", "L")


def _prepare_image(image: Image.Image, output_format: str) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if _needs_rgb(image, output_format):
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode in ("RGBA", "LA"):
            alpha = image.getchannel("A")
            background.paste(image.convert("RGBA"), mask=alpha)
            return background
        return image.convert("RGB")
    return image


def _format_from_path(path: Path, force_avif: bool) -> str:
    if force_avif:
        return "AVIF"

    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "JPEG"
    if suffix == ".png":
        return "PNG"
    if suffix == ".webp":
        return "WEBP"
    if suffix == ".avif":
        return "AVIF"

    return "JPEG"


def _save_kwargs(output_format: str, args: argparse.Namespace) -> dict:
    if output_format == "JPEG":
        return {
            "quality": args.jpeg_quality,
            "optimize": True,
            "progressive": True,
            "subsampling": "4:2:0",
        }

    if output_format == "WEBP":
        return {
            "quality": args.webp_quality,
            "method": 6,
            "lossless": False,
        }

    if output_format == "AVIF":
        return {
            "quality": args.avif_quality,
            "speed": args.avif_speed,
        }

    if output_format == "PNG":
        return {
            "optimize": True,
            "compress_level": 9,
        }

    return {}


def compress_image(
    input_path: Path,
    output_path: Path,
    args: argparse.Namespace,
) -> CompressResult:
    output_format = _format_from_path(input_path, args.avif)
    input_is_avif = input_path.suffix.lower() == ".avif"
    if (output_format == "AVIF" or input_is_avif) and not _register_avif():
        raise RuntimeError(
            "Muốn xuất AVIF cần cài pillow-avif-plugin: pip install pillow-avif-plugin"
        )

    with Image.open(input_path) as img:
        image = _prepare_image(img, output_format)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path, format=output_format, **_save_kwargs(output_format, args))

    original_size = input_path.stat().st_size
    output_size = output_path.stat().st_size

    can_keep_original = input_path.suffix.lower() == output_path.suffix.lower()
    if args.keep_smaller and output_size > original_size and not args.avif and can_keep_original:
        shutil.copy2(input_path, output_path)
        output_size = output_path.stat().st_size

    saved_size = max(0, original_size - output_size)
    saved_ratio = saved_size / original_size if original_size else 0.0
    return CompressResult(
        input_path=input_path,
        output_path=output_path,
        original_size=original_size,
        output_size=output_size,
        saved_size=saved_size,
        saved_ratio=saved_ratio,
    )


def _human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Nén ảnh hàng loạt từ compress/source sang compress/output."
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
        help=f"Thư mục output, default: {DEFAULT_OUTPUT_DIR}",
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
        "--avif",
        action="store_true",
        help="Chuyển toàn bộ ảnh output sang AVIF để giảm dung lượng mạnh hơn.",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=72,
        help="Chất lượng JPEG 1..95, default: 72.",
    )
    parser.add_argument(
        "--webp-quality",
        type=int,
        default=68,
        help="Chất lượng WebP 1..100, default: 68.",
    )
    parser.add_argument(
        "--avif-quality",
        type=int,
        default=45,
        help="Chất lượng AVIF 1..100, default: 45.",
    )
    parser.add_argument(
        "--avif-speed",
        type=int,
        default=4,
        help="Tốc độ AVIF 0..10; thấp hơn = nén tốt hơn nhưng chậm hơn, default: 4.",
    )
    parser.add_argument(
        "--no-keep-smaller",
        action="store_false",
        dest="keep_smaller",
        help="Không copy lại file gốc nếu bản nén không nhỏ hơn. Không áp dụng khi --avif.",
    )
    parser.set_defaults(keep_smaller=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    source_dir = args.source.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()

    if not source_dir.is_dir():
        print(f"Không tìm thấy thư mục source: {source_dir}", file=sys.stderr)
        sys.exit(1)

    images = list(iter_images(source_dir, args.recursive))
    if not images:
        print(f"Không tìm thấy ảnh trong: {source_dir}")
        return

    success_count = 0
    skipped_count = 0
    failed_count = 0
    dry_run_count = 0
    total_original = 0
    total_output = 0
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for input_path in images:
        output_format = "avif" if args.avif else None
        output_path = output_path_for(
            input_path,
            source_dir,
            output_dir,
            args.recursive,
            output_format,
            timestamp,
        )

        if args.dry_run:
            dry_run_count += 1
            print(f"[dry-run] {input_path} -> {output_path}")
            continue

        if output_path.exists() and not args.overwrite:
            skipped_count += 1
            print(f"[skip] Đã tồn tại: {output_path}")
            continue

        try:
            result = compress_image(input_path, output_path, args)
        except Exception as exc:
            failed_count += 1
            print(f"[fail] {input_path}: {exc}", file=sys.stderr)
            continue

        success_count += 1
        total_original += result.original_size
        total_output += result.output_size
        print(
            f"[ok] {input_path.name} -> {output_path.name} | "
            f"{_human_size(result.original_size)} -> {_human_size(result.output_size)} "
            f"(-{result.saved_ratio:.1%})"
        )

    if args.dry_run:
        print(f"Hoàn tất dry-run: {dry_run_count} file sẽ được xử lý.")
        return

    saved = max(0, total_original - total_output)
    ratio = saved / total_original if total_original else 0.0
    print(
        "Hoàn tất: "
        f"{success_count} thành công, {skipped_count} bỏ qua, {failed_count} lỗi. "
        f"Tổng: {_human_size(total_original)} -> {_human_size(total_output)} "
        f"(-{ratio:.1%})."
    )


if __name__ == "__main__":
    main()
