"""
Chia file video thành 2 phần, mỗi phần không dài hơn --max-minutes (mặc định 59).

Cần ffmpeg và ffprobe trên PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Optional, Tuple


def _which_or_exit(cmd: str) -> None:
    from shutil import which

    if not which(cmd):
        print(
            f"Không tìm thấy `{cmd}` trong PATH. Cài ffmpeg (vd: brew install ffmpeg).",
            file=sys.stderr,
        )
        sys.exit(1)


def get_duration_seconds(media_path: str) -> float:
    _which_or_exit("ffprobe")
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            media_path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(r.stdout)
    return float(data["format"]["duration"])


def split_two_parts(
    input_path: str,
    output_part1: str,
    output_part2: str,
    cut_at_seconds: float,
) -> None:
    _which_or_exit("ffmpeg")
    cut_at_seconds = max(0.1, cut_at_seconds)

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-t",
            str(cut_at_seconds),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output_part1,
        ],
        check=True,
    )

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            str(cut_at_seconds),
            "-i",
            input_path,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output_part2,
        ],
        check=True,
    )


def compute_cut_point(duration_sec: float, max_part_sec: float) -> Tuple[float, Optional[str]]:
    """
    Trả về thời điểm cắt (giây) để có 2 phần, mỗi phần <= max_part_sec.
    Nếu không khả thi với đúng 2 file, trả về (0, thông báo lỗi).
    """
    if duration_sec <= 0:
        return 0.0, "Không đọc được độ dài video hoặc file rỗng."

    if duration_sec <= max_part_sec:
        return duration_sec / 2.0, None

    # Cần cả 2 phần <= max: tổng tối đa là 2 * max_part_sec
    if duration_sec > 2 * max_part_sec:
        return 0.0, (
            f"Video dài {duration_sec / 60:.1f} phút — không thể chia thành đúng 2 phần "
            f"mà mỗi phần ≤ {max_part_sec / 60:.0f} phút "
            f"(tổng tối đa ~{2 * max_part_sec / 60:.0f} phút). "
            "Hãy nén/cắt bớt hoặc dùng công cụ chia nhiều hơn 2 đoạn."
        )

    # duration <= 2 * max_part_sec: chia đôi, mỗi nửa không vượt max
    return duration_sec / 2.0, None


def _default_outputs(input_path: str) -> Tuple[str, str]:
    base, ext = os.path.splitext(os.path.abspath(input_path))
    ext = ext if ext else ".mp4"
    return (f"{base}_part1{ext}", f"{base}_part2{ext}")


def _prompt_path(prompt: str) -> str:
    raw = input(prompt).strip().strip('"')
    if not raw:
        print("Cần có đường dẫn.", file=sys.stderr)
        sys.exit(1)
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chia video thành 2 phần, mỗi phần không quá N phút (mặc định 59)."
    )
    parser.add_argument("input", nargs="?", help="File video đầu vào")
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=59.0,
        help="Độ dài tối đa mỗi phần (phút), default: 59",
    )
    parser.add_argument(
        "-1",
        "--part1",
        metavar="PATH",
        help="Đường dẫn file phần 1 (mặc định: <tên>_part1.<ext>)",
    )
    parser.add_argument(
        "-2",
        "--part2",
        metavar="PATH",
        help="Đường dẫn file phần 2 (mặc định: <tên>_part2.<ext>)",
    )
    args = parser.parse_args()

    inp = args.input.strip() if args.input else None
    if not inp:
        inp = _prompt_path("Đường dẫn file video: ")

    if not os.path.isfile(inp):
        print(f"Không tìm thấy file: {inp}", file=sys.stderr)
        sys.exit(1)

    max_sec = max(1.0, float(args.max_minutes) * 60.0)

    try:
        duration = get_duration_seconds(inp)
    except subprocess.CalledProcessError as e:
        print(f"ffprobe lỗi: {e}", file=sys.stderr)
        sys.exit(1)

    cut, err = compute_cut_point(duration, max_sec)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    if args.part1 and args.part2:
        out1, out2 = args.part1, args.part2
    elif args.part1 or args.part2:
        print("Cần cả --part1 và --part2 hoặc để mặc định.", file=sys.stderr)
        sys.exit(1)
    else:
        out1, out2 = _default_outputs(inp)

    for d in (os.path.dirname(out1), os.path.dirname(out2)):
        if d:
            os.makedirs(d, exist_ok=True)

    print(f"Độ dài video: {duration / 60:.2f} phút — cắt tại {cut / 60:.2f} phút.")
    try:
        split_two_parts(inp, out1, out2, cut)
    except subprocess.CalledProcessError as e:
        print(f"ffmpeg lỗi: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Đã tạo:\n  {out1}\n  {out2}")


if __name__ == "__main__":
    main()
