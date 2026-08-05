from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image
except ModuleNotFoundError:
    print(
        "Thiếu thư viện xử lý ảnh. Cài bằng: pip install Pillow pillow-heif",
        file=sys.stderr,
    )
    sys.exit(1)

from ..compress.batch_compress import (
    _human_size,
    _prepare_image,
    _register_avif,
    _save_kwargs,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "heic" / "source"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "heic" / "output"

HEIC_EXTENSIONS = {".heic", ".heif"}
DOWNLOAD_SUFFIX = ".crdownload"


@dataclass(frozen=True)
class ConvertResult:
    input_path: Path
    output_path: Path
    original_size: int
    output_size: int
    saved_size: int
    saved_ratio: float


def _register_heif() -> bool:
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except ModuleNotFoundError:
        return False
    return True


def is_heic_image(path: Path) -> bool:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if not suffixes:
        return False

    if suffixes[-1] in HEIC_EXTENSIONS:
        return True

    return (
        suffixes[-1] == DOWNLOAD_SUFFIX
        and len(suffixes) >= 2
        and suffixes[-2] in HEIC_EXTENSIONS
    )


def iter_heic_images(source_dir: Path, recursive: bool) -> Iterable[Path]:
    pattern = "**/*" if recursive else "*"
    for path in sorted(source_dir.glob(pattern)):
        if path.is_file() and is_heic_image(path):
            yield path


def output_path_for(
    input_path: Path,
    source_dir: Path,
    output_dir: Path,
    recursive: bool,
    output_format: str,
    timestamp: str,
) -> Path:
    relative = input_path.relative_to(source_dir) if recursive else Path(input_path.name)
    if relative.suffix.lower() == DOWNLOAD_SUFFIX and len(relative.suffixes) >= 2:
        relative = relative.with_suffix("")

    suffix = f".{output_format.lower()}"
    if suffix == ".jpeg":
        suffix = ".jpg"
    relative = relative.with_suffix(suffix)
    relative = relative.with_name(f"{relative.stem}_{timestamp}{relative.suffix}")
    return output_dir / relative


def _pil_format(output_format: str) -> str:
    fmt = output_format.upper()
    if fmt == "JPG":
        return "JPEG"
    return fmt


def convert_heic_image(
    input_path: Path,
    output_path: Path,
    args: argparse.Namespace,
) -> ConvertResult:
    if not _register_heif():
        raise RuntimeError(
            "Cần cài pillow-heif để đọc HEIC/HEIF: pip install pillow-heif"
        )

    output_format = _pil_format(args.format)
    if output_format == "AVIF" and not _register_avif():
        raise RuntimeError(
            "Muốn xuất AVIF cần cài pillow-avif-plugin: pip install pillow-avif-plugin"
        )

    with Image.open(input_path) as img:
        image = _prepare_image(img, output_format)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path, format=output_format, **_save_kwargs(output_format, args))

    original_size = input_path.stat().st_size
    output_size = output_path.stat().st_size

    saved_size = max(0, original_size - output_size)
    saved_ratio = saved_size / original_size if original_size else 0.0
    return ConvertResult(
        input_path=input_path,
        output_path=output_path,
        original_size=original_size,
        output_size=output_size,
        saved_size=saved_size,
        saved_ratio=saved_ratio,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert HEIC/HEIF sang JPEG/PNG/WebP/AVIF và nén hàng loạt."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"Thư mục ảnh HEIC nguồn, default: {DEFAULT_SOURCE_DIR}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Thư mục output, default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--format",
        choices=["jpeg", "jpg", "png", "webp", "avif"],
        default="jpeg",
        help="Định dạng output, default: jpeg.",
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
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    source_dir = args.source.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()

    if not source_dir.is_dir():
        print(f"Không tìm thấy thư mục source: {source_dir}", file=sys.stderr)
        sys.exit(1)

    images = list(iter_heic_images(source_dir, args.recursive))
    if not images:
        print(f"Không tìm thấy file HEIC/HEIF trong: {source_dir}")
        return

    success_count = 0
    skipped_count = 0
    failed_count = 0
    dry_run_count = 0
    total_original = 0
    total_output = 0
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_format = args.format

    for input_path in images:
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
            result = convert_heic_image(input_path, output_path, args)
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
