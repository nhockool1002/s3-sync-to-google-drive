from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "hardsub" / "source"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "hardsub" / "output"
DEFAULT_MARK_PATH = PROJECT_ROOT / "app" / "watermark" / "mark.jpeg"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
DOWNLOAD_SUFFIX = ".crdownload"


@dataclass(frozen=True)
class Job:
    video_path: Path
    srt_path: Path
    output_path: Path


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(text: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in text)
    return safe.strip("_") or "hardsub"


def _which_or_exit(cmd: str) -> None:
    if not shutil.which(cmd):
        print(
            f"Không tìm thấy `{cmd}` trong PATH. Cài ffmpeg (vd: brew install ffmpeg).",
            file=sys.stderr,
        )
        sys.exit(1)


def is_supported_video(path: Path) -> bool:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if not suffixes:
        return False

    if suffixes[-1] in VIDEO_EXTENSIONS:
        return True

    return (
        suffixes[-1] == DOWNLOAD_SUFFIX
        and len(suffixes) >= 2
        and suffixes[-2] in VIDEO_EXTENSIONS
    )


def iter_videos(source_dir: Path, recursive: bool) -> Iterable[Path]:
    pattern = "**/*" if recursive else "*"
    for path in sorted(source_dir.glob(pattern)):
        if path.is_file() and is_supported_video(path):
            yield path


def find_srt_for(video_path: Path) -> Optional[Path]:
    exact = video_path.with_suffix(".srt")
    if exact.is_file():
        return exact

    candidates = sorted(video_path.parent.glob(f"{video_path.stem}*.srt"))
    return candidates[0] if candidates else None


def output_path_for(video_path: Path, source_dir: Path, output_dir: Path, recursive: bool) -> Path:
    relative = video_path.relative_to(source_dir) if recursive else Path(video_path.name)
    if relative.suffix.lower() == DOWNLOAD_SUFFIX and len(relative.suffixes) >= 2:
        relative = relative.with_suffix("")

    return output_dir / relative.with_name(f"{relative.stem}.hardsub.mp4")


def discover_jobs(source_dir: Path, output_dir: Path, recursive: bool) -> list[Job]:
    jobs: list[Job] = []
    for video_path in iter_videos(source_dir, recursive):
        srt_path = find_srt_for(video_path)
        if not srt_path:
            print(f"{_ts()} | [skip] Không tìm thấy SRT cho video: {video_path.name}")
            continue
        jobs.append(
            Job(
                video_path=video_path,
                srt_path=srt_path,
                output_path=output_path_for(video_path, source_dir, output_dir, recursive),
            )
        )
    return jobs


def _escape_filter_path(path: Path) -> str:
    text = path.as_posix()
    return (
        text.replace("\\", "\\\\")
        .replace("'", r"\'")
        .replace(":", r"\:")
        .replace(",", r"\,")
    )


def _build_filter(args: argparse.Namespace, temp_srt_path: Path) -> str:
    srt = _escape_filter_path(temp_srt_path)
    style = args.subtitle_style.replace("'", r"\'")
    margin = max(0, args.watermark_margin)
    watermark_opacity = min(max(args.watermark_opacity, 0.0), 1.0)
    watermark_width_ratio = max(0.01, args.watermark_width_ratio)

    return (
        f"[1:v][0:v]scale2ref=w=main_w*{watermark_width_ratio}:h=-1[wmraw][base];"
        f"[wmraw]format=rgba,colorchannelmixer=aa={watermark_opacity}[wm];"
        f"[base][wm]overlay=W-w-{margin}:{margin}[wmv];"
        f"[wmv]subtitles={srt}:force_style='{style}'[vout]"
    )


def _copy_srt_to_temp(srt_path: Path, temp_dir: Path) -> Path:
    temp_srt = temp_dir / "subtitle.srt"
    shutil.copyfile(srt_path, temp_srt)
    return temp_srt


def _run_ffmpeg(job: Job, mark_path: Path, args: argparse.Namespace) -> None:
    _which_or_exit("ffmpeg")
    job.output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hardsub_") as temp_name:
        temp_srt = _copy_srt_to_temp(job.srt_path, Path(temp_name))
        filter_complex = _build_filter(args, temp_srt)

        command = [
            "ffmpeg",
            "-hide_banner",
            "-y" if args.overwrite else "-n",
            "-i",
            str(job.video_path),
            "-loop",
            "1",
            "-i",
            str(mark_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            "-c:v",
            args.video_codec,
            "-preset",
            args.preset,
            "-crf",
            str(args.crf),
            "-c:a",
            "aac",
            "-b:a",
            args.audio_bitrate,
            "-movflags",
            "+faststart",
            "-shortest",
            str(job.output_path),
        ]

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        assert process.stdout is not None
        for line in process.stdout:
            print(line.rstrip(), flush=True)

        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"ffmpeg kết thúc với mã lỗi {return_code}")


def _progress_log_path(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{stamp}_hardsub.progress.log"


def _pid_path_for(progress_log_path: Path) -> Path:
    return progress_log_path.with_suffix(".pid")


def _worker_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "app.hardsub.batch_hardsub",
        "--foreground",
        "--source",
        str(args.source),
        "--output",
        str(args.output),
        "--mark",
        str(args.mark),
        "--crf",
        str(args.crf),
        "--preset",
        args.preset,
        "--video-codec",
        args.video_codec,
        "--audio-bitrate",
        args.audio_bitrate,
        "--watermark-opacity",
        str(args.watermark_opacity),
        "--watermark-width-ratio",
        str(args.watermark_width_ratio),
        "--watermark-margin",
        str(args.watermark_margin),
        "--subtitle-style",
        args.subtitle_style,
    ]
    if args.recursive:
        command.append("--recursive")
    if args.overwrite:
        command.append("--overwrite")
    return command


def start_background(args: argparse.Namespace) -> tuple[int, Path, Path]:
    output_dir = args.output.expanduser().resolve()
    progress_log_path = _progress_log_path(output_dir)
    pid_path = _pid_path_for(progress_log_path)

    with open(progress_log_path, "a", encoding="utf-8", buffering=1) as log_file:
        log_file.write(f"{_ts()} | === Hardsub + nén video nền ===\n")
        log_file.write(f"{_ts()} | Source: {args.source}\n")
        log_file.write(f"{_ts()} | Output: {args.output}\n")
        log_file.write(f"{_ts()} | Watermark: {args.mark}\n")
        log_file.flush()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            _worker_command(args),
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.write(f"{_ts()} | PID: {process.pid}\n")
        log_file.write(f"{_ts()} | Worker đã chạy nền.\n\n")

    with open(pid_path, "w", encoding="utf-8") as pid_file:
        pid_file.write(f"{process.pid}\n")

    return process.pid, progress_log_path, pid_path


def run_foreground(args: argparse.Namespace) -> None:
    source_dir = args.source.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    mark_path = args.mark.expanduser().resolve()

    if not source_dir.is_dir():
        print(f"Không tìm thấy thư mục source: {source_dir}", file=sys.stderr)
        sys.exit(1)
    if not mark_path.is_file():
        print(f"Không tìm thấy watermark: {mark_path}", file=sys.stderr)
        sys.exit(1)

    jobs = discover_jobs(source_dir, output_dir, args.recursive)
    if not jobs:
        print(f"{_ts()} | Không có job hardsub nào trong: {source_dir}")
        return

    success_count = 0
    skipped_count = 0
    failed_count = 0

    print(f"{_ts()} | Tìm thấy {len(jobs)} video có SRT tương ứng.")
    for index, job in enumerate(jobs, start=1):
        print(f"\n{_ts()} | [{index}/{len(jobs)}] Bắt đầu: {job.video_path.name}")
        print(f"{_ts()} | SRT: {job.srt_path.name}")
        print(f"{_ts()} | Output: {job.output_path}")

        if job.output_path.exists() and not args.overwrite:
            skipped_count += 1
            print(f"{_ts()} | [skip] Output đã tồn tại: {job.output_path}")
            continue

        try:
            _run_ffmpeg(job, mark_path, args)
        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            print(f"{_ts()} | [fail] {job.video_path.name}: {exc}", file=sys.stderr)
            continue

        success_count += 1
        print(f"{_ts()} | [ok] Hoàn tất: {job.output_path.name}")

    print(
        f"\n{_ts()} | Hoàn tất hardsub: "
        f"{success_count} thành công, {skipped_count} bỏ qua, {failed_count} lỗi."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hardsub SRT, nén video và chèn watermark mờ góc phải trên."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"Thư mục source chứa video + SRT, default: {DEFAULT_SOURCE_DIR}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Thư mục output, default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--mark",
        type=Path,
        default=DEFAULT_MARK_PATH,
        help=f"Watermark ảnh, default: {DEFAULT_MARK_PATH}",
    )
    parser.add_argument("--recursive", action="store_true", help="Quét cả thư mục con.")
    parser.add_argument("--overwrite", action="store_true", help="Ghi đè output nếu đã có.")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Chạy trực tiếp thay vì spawn process nền.",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=28,
        help="Mức nén video CRF; cao hơn = nhẹ hơn nhưng xấu hơn, default: 28.",
    )
    parser.add_argument(
        "--preset",
        default="medium",
        help="Preset encoder ffmpeg: veryslow, slow, medium, fast..., default: medium.",
    )
    parser.add_argument(
        "--video-codec",
        choices=["libx264", "libx265"],
        default="libx264",
        help="Codec video output, default: libx264.",
    )
    parser.add_argument(
        "--audio-bitrate",
        default="128k",
        help="Bitrate audio AAC output, default: 128k.",
    )
    parser.add_argument(
        "--watermark-opacity",
        type=float,
        default=0.22,
        help="Độ mờ watermark 0..1, default: 0.22.",
    )
    parser.add_argument(
        "--watermark-width-ratio",
        type=float,
        default=0.42,
        help="Chiều rộng watermark theo chiều rộng video, default: 0.22.",
    )
    parser.add_argument(
        "--watermark-margin",
        type=int,
        default=24,
        help="Khoảng cách watermark tới mép trên/phải, default: 24.",
    )
    parser.add_argument(
        "--subtitle-style",
        default="FontName=Arial,FontSize=18,Outline=2,Shadow=1,Alignment=2,MarginV=36",
        help="ASS force_style cho phụ đề hardsub.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.foreground:
        run_foreground(args)
        return

    pid, progress_log_path, pid_path = start_background(args)
    print("Đã chuyển hardsub + nén video sang chạy nền.")
    print(f"PID: {pid}")
    print(f"File log tiến trình: {progress_log_path}")
    print(f"File PID: {pid_path}")
    print(f"Theo dõi log: tail -f {progress_log_path}")


if __name__ == "__main__":
    main()
