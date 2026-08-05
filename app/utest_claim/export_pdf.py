"""
Gộp toàn bộ testcase trong utest_claim_testcase/ thành một file PDF.

Mỗi testcase gồm Step 1 (tất cả ảnh) và Step 2 (Feedback.txt).

Chạy:
  python -m app.utest_claim.export_pdf
  python -m app.utest_claim.export_pdf --output utest_claim_testcase/all_testcases.pdf
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

try:
    from PIL import Image as PILImage
except ModuleNotFoundError:
    print(
        "Thiếu thư viện xử lý ảnh. Cài bằng: pip install Pillow",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Image as RLImage
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer
except ModuleNotFoundError:
    print(
        "Thiếu thư viện PDF. Cài bằng: pip install reportlab",
        file=sys.stderr,
    )
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "utest_claim_testcase"
DEFAULT_OUTPUT = DEFAULT_INPUT / "all_testcases.pdf"

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".gif",
}

PAGE_WIDTH, PAGE_HEIGHT = A4
CONTENT_WIDTH = PAGE_WIDTH - 2 * inch
MAX_IMAGE_HEIGHT = PAGE_HEIGHT - 2.5 * inch


def list_testcase_dirs(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and path.name.isdigit()),
        key=lambda path: path.name,
    )


def list_images(step1_dir: Path) -> list[Path]:
    if not step1_dir.is_dir():
        return []
    return sorted(
        path
        for path in step1_dir.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def read_feedback(step2_dir: Path) -> str:
    feedback_path = step2_dir / "Feedback.txt"
    if feedback_path.is_file():
        return feedback_path.read_text(encoding="utf-8").strip()
    return "(Không có Feedback.txt)"


def _prepare_rgb_image(path: Path) -> PILImage.Image:
    with PILImage.open(path) as image:
        if image.mode in ("RGBA", "LA"):
            background = PILImage.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.split()[-1])
            return background.copy()
        if image.mode == "P":
            converted = image.convert("RGBA")
            background = PILImage.new("RGB", converted.size, (255, 255, 255))
            background.paste(converted, mask=converted.split()[-1])
            return background.copy()
        if image.mode != "RGB":
            return image.convert("RGB")
        return image.copy()


def make_reportlab_image(path: Path) -> RLImage:
    image = _prepare_rgb_image(path)
    width, height = image.size
    scale = min(CONTENT_WIDTH / width, MAX_IMAGE_HEIGHT / height, 1.0)
    display_width = width * scale
    display_height = height * scale

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    buffer.seek(0)
    return RLImage(buffer, width=display_width, height=display_height)


def _feedback_paragraphs(feedback_text: str, body_style: ParagraphStyle) -> list:
    elements = []
    for line in feedback_text.splitlines():
        if not line.strip():
            elements.append(Spacer(1, 6))
            continue
        safe_line = escape(line).replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;")
        elements.append(Paragraph(safe_line, body_style))
    return elements


def build_pdf(input_root: Path, output_path: Path) -> tuple[Path, int, int]:
    testcase_dirs = list_testcase_dirs(input_root)
    if not testcase_dirs:
        raise ValueError(f"Không tìm thấy testcase nào trong {input_root}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TestcaseTitle",
        parent=styles["Heading1"],
        fontSize=18,
        spaceAfter=12,
        alignment=TA_LEFT,
    )
    section_style = ParagraphStyle(
        "SectionTitle",
        parent=styles["Heading2"],
        fontSize=14,
        spaceBefore=8,
        spaceAfter=10,
        alignment=TA_LEFT,
    )
    body_style = ParagraphStyle(
        "FeedbackBody",
        parent=styles["Normal"],
        fontSize=9,
        leading=12,
        alignment=TA_LEFT,
    )
    caption_style = ParagraphStyle(
        "ImageCaption",
        parent=styles["Normal"],
        fontSize=8,
        textColor="#444444",
        spaceAfter=8,
        alignment=TA_LEFT,
    )

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=inch,
        rightMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
        title="uTest Claim Testcases",
    )

    story: list = []
    total_images = 0

    for index, case_dir in enumerate(testcase_dirs):
        testcase_id = case_dir.name
        step1_dir = case_dir / "Step1"
        step2_dir = case_dir / "Step2"
        images = list_images(step1_dir)
        feedback_text = read_feedback(step2_dir)

        if index > 0:
            story.append(PageBreak())

        story.append(Paragraph(f"Test Case ID: {escape(testcase_id)}", title_style))
        story.append(Spacer(1, 8))
        story.append(Paragraph("Step 1 — Attachments", section_style))

        if images:
            for image_index, image_path in enumerate(images, start=1):
                story.append(
                    Paragraph(
                        f"Image {image_index}/{len(images)}: {escape(image_path.name)}",
                        caption_style,
                    )
                )
                story.append(make_reportlab_image(image_path))
                story.append(Spacer(1, 12))
                total_images += 1
        else:
            story.append(Paragraph("(Không có ảnh Step 1)", body_style))
            story.append(Spacer(1, 12))

        story.append(Paragraph("Step 2 — UX Feedback", section_style))
        story.extend(_feedback_paragraphs(feedback_text, body_style))

    doc.build(story)
    return output_path, len(testcase_dirs), total_images


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gộp tất cả testcase uTest (Step1 ảnh + Step2 feedback) thành một PDF."
    )
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT),
        help=f"Thư mục gốc chứa các testcase (mặc định: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Đường dẫn file PDF đầu ra (mặc định: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    input_root = Path(args.input_dir).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    if not input_root.is_dir():
        print(f"Thư mục không tồn tại: {input_root}", file=sys.stderr)
        raise SystemExit(1)

    started = datetime.now()
    print(f"Input : {input_root}")
    print(f"Output: {output_path}")

    try:
        pdf_path, case_count, image_count = build_pdf(input_root, output_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    elapsed = (datetime.now() - started).total_seconds()
    size_mb = pdf_path.stat().st_size / (1024 * 1024)
    print(
        f"\nHoàn tất: {case_count} testcase, {image_count} ảnh "
        f"→ {pdf_path} ({size_mb:.1f} MB, {elapsed:.1f}s)"
    )


if __name__ == "__main__":
    main()
