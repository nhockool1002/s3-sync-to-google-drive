"""
Lồng tiếng Việt (dub) cho video.

Pipeline:
 1. Lấy phụ đề tiếng Việt: dùng file SRT có sẵn (nếu truyền vào), hoặc tự
    tạo bằng Whisper (nhận diện ngôn ngữ gốc) rồi dịch sang tiếng Việt.
 2. Với mỗi dòng, đọc bằng edge-tts (giọng Việt), tự tăng tốc đọc nếu câu
    dài hơn khoảng thời gian cho phép (đến khi câu kế tiếp bắt đầu).
 3. Ghép toàn bộ các đoạn giọng đọc vào 1 track audio dài bằng video.
 4. Mux: giữ nguyên hình, thay audio gốc bằng track tiếng Việt.

Cần: ffmpeg/ffprobe trên PATH, package edge-tts + pydub (+ audioop-lts
trên Python >= 3.13), và (khi tự tạo phụ đề) openai-whisper + deep-translator.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, List, Optional

from pydub import AudioSegment

ProgressCB = Callable[[str], None]

VOICE_FEMALE = "vi-VN-HoaiMyNeural"
VOICE_MALE = "vi-VN-NamMinhNeural"
DEFAULT_VOICE = VOICE_MALE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tts_worker_command(voice: str, rate: str, text: str, out_path: str) -> list[str]:
    """Lệnh chạy 1 câu TTS trong 1 process riêng (xem app/tts_worker.py)."""
    if getattr(sys, "frozen", False):
        # Đã đóng gói bằng PyInstaller: tự gọi lại chính app với cờ ẩn.
        return [sys.executable, "--tts-worker", "--voice", voice, "--rate", rate,
                "--text", text, "--out", out_path]
    return [sys.executable, "-m", "app.tts_worker", "--voice", voice, "--rate", rate,
            "--text", text, "--out", out_path]


def _log(progress_cb: Optional[ProgressCB], message: str) -> None:
    if progress_cb:
        progress_cb(message)
    else:
        print(message)


def _parse_srt_timestamp(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(path: str) -> List[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    blocks = re.split(r"\n\s*\n", content.strip())
    segments: List[dict[str, Any]] = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        time_line_idx = 1 if lines[0].strip().isdigit() else 0
        time_line = lines[time_line_idx]
        m = re.match(
            r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", time_line
        )
        if not m:
            continue
        start = _parse_srt_timestamp(m.group(1))
        end = _parse_srt_timestamp(m.group(2))
        text = " ".join(lines[time_line_idx + 1 :]).strip()
        segments.append({"start": start, "end": end, "text": text})
    return segments


def segments_from_video(
    video_path: str,
    model_name: str = "base",
    progress_cb: Optional[ProgressCB] = None,
) -> List[dict[str, Any]]:
    """Tạo phụ đề tiếng Việt từ video bằng Whisper + dịch (nếu cần)."""
    from .whisper_sub import translate_segments_to_vi

    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError(
            "Chưa cài openai-whisper. Chạy: pip install openai-whisper"
        ) from exc

    _log(progress_cb, f"Đang nhận diện giọng nói (Whisper model={model_name})...")
    model = whisper.load_model(model_name)
    result = model.transcribe(video_path, task="transcribe", verbose=False)
    segments = result.get("segments") or []
    detected = (result.get("language") or "en").strip()
    _log(progress_cb, f"Ngôn ngữ phát hiện: {detected}")

    if detected.lower() not in ("vi", "vie"):
        _log(progress_cb, "Đang dịch sang tiếng Việt...")
        segments = translate_segments_to_vi(segments, detected)

    return [
        {"start": float(s.get("start", 0.0)), "end": float(s.get("end", 0.0)), "text": (s.get("text") or "").strip()}
        for s in segments
    ]


def _get_video_duration(video_path: str) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path,
        ],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def _atempo_chain(factor: float) -> str:
    """ffmpeg atempo chỉ hỗ trợ 0.5-2.0 mỗi filter; ghép chuỗi cho hệ số lớn hơn."""
    filters = []
    f = factor
    while f > 2.0:
        filters.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        filters.append("atempo=0.5")
        f /= 0.5
    filters.append(f"atempo={f:.4f}")
    return ",".join(filters)


def _speed_up_with_ffmpeg(in_path: str, out_path: str, factor: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", in_path, "-filter:a", _atempo_chain(factor), out_path],
        check=True, capture_output=True,
    )


def _synth(text: str, voice: str, rate: str, out_path: str, max_retries: int = 20) -> None:
    """Đọc 1 câu bằng edge-tts. Dịch vụ của Microsoft đôi khi nghẽn tạm thời
    (vài giây tới vài phút) và trả về rỗng; retry với backoff tăng dần."""
    last_err = None
    for attempt in range(max_retries):
        if os.path.exists(out_path):
            os.remove(out_path)
        proc = subprocess.run(
            _tts_worker_command(voice, rate, text, out_path),
            cwd=PROJECT_ROOT,
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return
        last_err = proc.stderr.strip() or proc.stdout.strip()
        time.sleep(min(3.0 * (attempt + 1), 45.0))
    raise RuntimeError(f"edge-tts thất bại sau {max_retries} lần thử: {last_err}")


def dub_video(
    video_path: str,
    srt_path: Optional[str] = None,
    output_path: Optional[str] = None,
    voice: str = DEFAULT_VOICE,
    whisper_model: str = "base",
    progress_cb: Optional[ProgressCB] = None,
    work_dir: Optional[str] = None,
) -> str:
    """
    Tạo bản lồng tiếng Việt cho video. Trả về đường dẫn file output.

    - srt_path: nếu có, dùng luôn (phải là phụ đề đã ở tiếng Việt).
      Nếu None, tự tạo bằng Whisper + dịch.
    - output_path: mặc định "<tên video>-vi-dub.<ext>" cùng thư mục video.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Không tìm thấy video: {video_path}")

    base, ext = os.path.splitext(os.path.abspath(video_path))
    ext = ext or ".mp4"
    if not output_path:
        output_path = f"{base}-vi-dub{ext}"

    if srt_path:
        if not os.path.isfile(srt_path):
            raise FileNotFoundError(f"Không tìm thấy SRT: {srt_path}")
        segments = parse_srt(srt_path)
    else:
        segments = segments_from_video(video_path, whisper_model, progress_cb)

    n = len(segments)
    if n == 0:
        raise RuntimeError("Không có dòng phụ đề nào để lồng tiếng.")

    _log(progress_cb, f"Đang đo thời lượng video...")
    video_duration = _get_video_duration(video_path)

    own_temp = work_dir is None
    temp_dir = work_dir or tempfile.mkdtemp(prefix="dub_vi_")
    try:
        base_track = AudioSegment.silent(duration=int(video_duration * 1000) + 2000)

        for i, seg in enumerate(segments):
            text = seg["text"].strip()
            if not text:
                continue
            start = seg["start"]
            window_end = segments[i + 1]["start"] if i + 1 < n else video_duration
            available = max(window_end - start, 0.5)

            raw_path = os.path.join(temp_dir, f"seg_{i:03d}_raw.mp3")
            _synth(text, voice, "+0%", raw_path)
            dur = AudioSegment.from_file(raw_path).duration_seconds

            clip_path = raw_path
            if dur > available * 1.05:
                pct = min(int((dur / available - 1) * 100), 50)
                adj_path = os.path.join(temp_dir, f"seg_{i:03d}_adj.mp3")
                _synth(text, voice, f"+{pct}%", adj_path)
                clip_path = adj_path
                dur = AudioSegment.from_file(adj_path).duration_seconds
                if dur > available * 1.05:
                    factor = dur / available
                    sped_path = os.path.join(temp_dir, f"seg_{i:03d}_sped.mp3")
                    _speed_up_with_ffmpeg(adj_path, sped_path, factor)
                    clip_path = sped_path
                    dur = AudioSegment.from_file(clip_path).duration_seconds

            clip_audio = AudioSegment.from_file(clip_path)
            base_track = base_track.overlay(clip_audio, position=int(start * 1000))
            _log(progress_cb, f"[{i + 1}/{n}] {start:.1f}s (dài {dur:.1f}s / được {available:.1f}s) -> ok")

        audio_track_path = os.path.join(temp_dir, "vi_dub_track.wav")
        base_track.export(audio_track_path, format="wav")
        _log(progress_cb, "Đang ghép audio tiếng Việt vào video...")

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", audio_track_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                output_path,
            ],
            check=True, capture_output=True, text=True,
        )
    finally:
        if own_temp:
            shutil.rmtree(temp_dir, ignore_errors=True)

    _log(progress_cb, f"Đã tạo video lồng tiếng Việt: {output_path}")
    return output_path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Lồng tiếng Việt cho video (whisper + dịch + TTS).")
    parser.add_argument("video", nargs="?", help="Đường dẫn video")
    parser.add_argument("--srt", help="File SRT tiếng Việt có sẵn (nếu không truyền sẽ tự tạo)")
    parser.add_argument("-o", "--output", help="File video output")
    parser.add_argument("--voice", default=DEFAULT_VOICE, choices=[VOICE_MALE, VOICE_FEMALE])
    parser.add_argument("--whisper-model", default="base")
    args = parser.parse_args()

    video = args.video or input("Đường dẫn video: ").strip().strip('"')
    if not video:
        print("Cần có đường dẫn video.", file=sys.stderr)
        sys.exit(1)

    dub_video(
        video,
        srt_path=args.srt,
        output_path=args.output,
        voice=args.voice,
        whisper_model=args.whisper_model,
    )


if __name__ == "__main__":
    main()
