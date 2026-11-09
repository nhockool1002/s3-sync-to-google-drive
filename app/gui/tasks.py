"""Hàm cầu nối giữa GUI và logic xử lý sẵn có trong app/.

Mỗi hàm nhận tham số đơn giản (đường dẫn, option) và trả về 1 chuỗi mô tả
kết quả; log chi tiết được in ra stdout (Worker sẽ redirect vào ô log GUI).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional


def task_subtitle(
    video_path: str,
    model_name: str,
    language: Optional[str],
    translate_vi: bool,
) -> str:
    from ..whisper_sub import write_srt, _default_srt_path

    output_path = _default_srt_path(video_path)
    write_srt(
        video_path,
        output_path,
        model_name=model_name,
        language=language,
        translate_to_vi=translate_vi,
    )
    return output_path


def task_dub(
    video_path: str,
    srt_path: Optional[str],
    voice: str,
    whisper_model: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    from ..dub_vi import dub_video

    return dub_video(
        video_path,
        srt_path=srt_path or None,
        voice=voice,
        whisper_model=whisper_model,
        progress_cb=progress_cb,
    )


def task_split(video_path: str, max_minutes: float) -> str:
    from ..video_split import get_duration_seconds, compute_cut_point, split_two_parts, _default_outputs

    duration = get_duration_seconds(video_path)
    cut, err = compute_cut_point(duration, max(1.0, max_minutes) * 60.0)
    if err:
        raise RuntimeError(err)
    out1, out2 = _default_outputs(video_path)
    split_two_parts(video_path, out1, out2, cut)
    return f"{out1}\n{out2}"


def task_hardsub(
    video_path: str,
    srt_path: Optional[str],
    mark_path: Optional[str],
    crf: int,
    preset: str,
) -> str:
    from ..hardsub.batch_hardsub import (
        Job,
        _run_ffmpeg,
        build_parser,
        find_srt_for,
        DEFAULT_MARK_PATH,
    )

    video = Path(video_path)
    srt = Path(srt_path) if srt_path else find_srt_for(video)
    if not srt or not srt.is_file():
        raise RuntimeError(
            f"Không tìm thấy phụ đề SRT cho video. Hãy kéo-thả kèm file .srt, "
            f"hoặc tạo phụ đề trước (tab Phụ đề)."
        )

    mark = Path(mark_path) if mark_path else DEFAULT_MARK_PATH
    if not mark.is_file():
        raise RuntimeError(f"Không tìm thấy watermark: {mark}")

    output = video.with_name(f"{video.stem}.hardsub.mp4")
    args = build_parser().parse_args([])
    args.overwrite = True
    args.crf = crf
    args.preset = preset

    job = Job(video_path=video, srt_path=srt, output_path=output)
    _run_ffmpeg(job, mark, args)
    return str(output)


def task_compress(
    image_paths: List[str],
    jpeg_quality: int,
    output_dir: Optional[str],
) -> str:
    from ..compress.batch_compress import compress_image, build_parser, _human_size
    from datetime import datetime

    args = build_parser().parse_args([])
    args.jpeg_quality = jpeg_quality
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    results = []
    for raw_path in image_paths:
        input_path = Path(raw_path)
        out_dir = Path(output_dir) if output_dir else input_path.parent
        output_path = out_dir / f"{input_path.stem}_{timestamp}{input_path.suffix}"
        result = compress_image(input_path, output_path, args)
        print(
            f"[ok] {input_path.name} -> {output_path.name} | "
            f"{_human_size(result.original_size)} -> {_human_size(result.output_size)} "
            f"(-{result.saved_ratio:.1%})"
        )
        results.append(str(output_path))

    return "\n".join(results)


def task_watermark(
    image_paths: List[str],
    mark_path: Optional[str],
    output_dir: Optional[str],
) -> str:
    from ..watermark.batch_watermark import process_image, build_parser, DEFAULT_MARK_PATH
    from datetime import datetime

    args = build_parser().parse_args([])
    mark = Path(mark_path) if mark_path else DEFAULT_MARK_PATH
    if not mark.is_file():
        raise RuntimeError(f"Không tìm thấy watermark: {mark}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    for raw_path in image_paths:
        input_path = Path(raw_path)
        out_dir = Path(output_dir) if output_dir else input_path.parent
        output_path = out_dir / f"{input_path.stem}_{timestamp}{input_path.suffix}"
        process_image(input_path, output_path, mark, args)
        print(f"[ok] {input_path.name} -> {output_path.name}")
        results.append(str(output_path))

    return "\n".join(results)


def task_heic(
    image_paths: List[str],
    output_format: str,
    output_dir: Optional[str],
) -> str:
    from ..heic.batch_heic import convert_heic_image, build_parser, _human_size
    from datetime import datetime

    args = build_parser().parse_args([])
    args.format = output_format
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    results = []
    for raw_path in image_paths:
        input_path = Path(raw_path)
        out_dir = Path(output_dir) if output_dir else input_path.parent
        suffix = ".jpg" if output_format in ("jpeg", "jpg") else f".{output_format}"
        output_path = out_dir / f"{input_path.stem}_{timestamp}{suffix}"
        result = convert_heic_image(input_path, output_path, args)
        print(
            f"[ok] {input_path.name} -> {output_path.name} | "
            f"{_human_size(result.original_size)} -> {_human_size(result.output_size)} "
            f"(-{result.saved_ratio:.1%})"
        )
        results.append(str(output_path))

    return "\n".join(results)
