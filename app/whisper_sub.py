"""
Tạo phụ đề SRT từ file video/audio bằng OpenAI Whisper.

- Mặc định **tự phát hiện ngôn ngữ** (`--language auto`). Chỉ dùng `--language vi`
  khi toàn bộ lời thoại là tiếng Việt; nếu ép `vi` trong khi audio là tiếng Anh
  sẽ ra chữ lẫn loạn (Whisper cố gắng viết âm thanh ngoại ngữ thành "tiếng Việt").
- `--translate-vi`: sau khi nhận dạng, dịch từng dòng sang tiếng Việt (cần mạng,
  dùng Google Translate qua thư viện deep-translator).

Cần ffmpeg trên PATH để đọc video.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import warnings
from math import modf
from typing import Any, List, Optional

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None  # type: ignore[misc, assignment]

# Lazy import whisper trong transcribe_* để lỗi thiếu gói rõ ràng hơn


def format_srt_timestamp(t: float) -> str:
    """Chuyển giây (float) sang định dạng thời gian SRT HH:MM:SS,mmm."""
    t = max(0.0, t)
    frac, whole = modf(t)
    ms = int(round(frac * 1000))
    if ms >= 1000:
        ms = 0
        whole += 1.0
    w = int(whole)
    h = w // 3600
    m = (w % 3600) // 60
    s = w % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_MYMEMORY_LANG_MAP = {
    "en": "en-US",
    "vi": "vi-VN",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "zh": "zh-CN",
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-ES",
    "pt": "pt-PT",
    "ru": "ru-RU",
    "th": "th-TH",
}


def _translate_one(
    translators: List[Any],
    text: str,
    *,
    max_retries: int = 3,
    base_delay: float = 0.3,
) -> tuple[str, bool]:
    """
    Dịch một chuỗi bằng danh sách translator (thử lần lượt), có retry khi lỗi
    mạng / rate limit. Trả về (text_đã_dịch_hoặc_gốc, đã_dịch_thành_công?).
    """
    for translator in translators:
        for attempt in range(max_retries):
            try:
                return translator.translate(text), True
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(base_delay * (2**attempt))
    return text, False


def translate_segments_to_vi(
    segments: List[dict[str, Any]],
    source_lang: str,
) -> List[dict[str, Any]]:
    """
    Dịch nội dung từng segment sang tiếng Việt (giữ nguyên timestamp).
    Video dài có thể có hàng nghìn dòng — bước này lâu và cần mạng ổn định.
    """
    try:
        from deep_translator import GoogleTranslator, MyMemoryTranslator
    except ImportError as exc:
        raise RuntimeError(
            "Cần gói deep-translator: pip install deep-translator"
        ) from exc

    sl = (source_lang or "en").lower().strip()
    if sl in ("vi", "vie"):
        return segments

    translators: List[Any] = []
    try:
        translators.append(GoogleTranslator(source=sl, target="vi"))
    except Exception:
        try:
            translators.append(GoogleTranslator(source="auto", target="vi"))
        except Exception:
            pass
    try:
        mymemory_source = _MYMEMORY_LANG_MAP.get(sl, f"{sl}-{sl.upper()}")
        translators.append(MyMemoryTranslator(source=mymemory_source, target="vi-VN"))
    except Exception:
        pass

    if not translators:
        raise RuntimeError("Không khởi tạo được bất kỳ dịch vụ dịch nào.")

    n = len(segments)
    nonempty = sum(1 for s in segments if (s.get("text") or "").strip())
    print(
        f"\nĐang dịch {nonempty}/{n} dòng phụ đề sang tiếng Việt (sau 100% Whisper). "
        "Có thể mất từ vài phút đến hơn một giờ tùy độ dài; không tắt terminal.\n",
        file=sys.stderr,
    )

    out: List[dict[str, Any]] = []
    iterator: Any = segments
    if tqdm is not None:
        iterator = tqdm(
            segments,
            desc="Dịch VI",
            unit="dòng",
            file=sys.stderr,
            mininterval=0.5,
        )

    failed = 0
    for seg in iterator:
        text = (seg.get("text") or "").strip()
        if not text:
            out.append(seg)
            continue
        vi_text, ok = _translate_one(translators, text)
        if not ok:
            failed += 1
        new_seg = dict(seg)
        new_seg["text"] = vi_text
        out.append(new_seg)

    if failed:
        print(
            f"\nCảnh báo: {failed}/{nonempty} dòng dịch thất bại (giữ nguyên bản gốc) "
            "do lỗi mạng hoặc bị giới hạn truy vấn (rate limit).\n",
            file=sys.stderr,
        )

    return out


def segments_to_srt(segments: List[dict[str, Any]]) -> str:
    """Ghép các segment Whisper thành nội dung file SRT."""
    lines: List[str] = []
    for i, seg in enumerate(segments, start=1):
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        text = (seg.get("text") or "").strip()
        lines.append(str(i))
        lines.append(
            f"{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}"
        )
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def transcribe_to_srt(
    media_path: str,
    *,
    model_name: str = "base",
    language: Optional[str] = None,
    device: Optional[str] = None,
    translate_to_vi: bool = False,
) -> str:
    """
    Chạy Whisper và trả về nội dung SRT (UTF-8).

    language=None: tự phát hiện (khuyến nghị cho video đa ngôn ngữ / chủ yếu tiếng Anh).
    """
    try:
        import whisper
    except ImportError as exc:
        raise RuntimeError(
            "Chưa cài openai-whisper. Chạy: pip install openai-whisper"
        ) from exc

    warnings.filterwarnings(
        "ignore",
        message="FP16 is not supported on CPU; using FP32 instead",
        category=UserWarning,
    )

    if not os.path.isfile(media_path):
        raise FileNotFoundError(f"Không tìm thấy file: {media_path}")

    load_kw: dict[str, str] = {}
    if device:
        load_kw["device"] = device

    model = whisper.load_model(model_name, **load_kw)
    result = model.transcribe(
        media_path,
        language=language,
        task="transcribe",
        verbose=False,
    )
    segs = result.get("segments") or []
    detected = (result.get("language") or "en").strip()

    if translate_to_vi:
        segs = translate_segments_to_vi(segs, detected)
        print(
            f"\nĐã dịch xong (nguồn: {detected} → vi).",
            file=sys.stderr,
        )

    return segments_to_srt(segs)


def write_srt(media_path: str, output_path: str, **kwargs: Any) -> None:
    """Tạo SRT và ghi ra output_path."""
    srt = transcribe_to_srt(media_path, **kwargs)
    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(srt)
    print(f"Đã ghi phụ đề: {output_path}")


def _default_srt_path(media_path: str) -> str:
    base, _ = os.path.splitext(os.path.abspath(media_path))
    return base + ".srt"


def _prompt_media_path() -> str:
    raw = input("Đường dẫn file video/audio: ").strip().strip('"')
    if not raw:
        print("Cần có đường dẫn file.", file=sys.stderr)
        sys.exit(1)
    return raw


def _prompt_output_path(media_path: str) -> str:
    default = _default_srt_path(media_path)
    raw = input(f"File SRT đích [Enter = {default}]: ").strip().strip('"')
    return raw if raw else default


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tạo phụ đề SRT từ video/audio bằng Whisper (tự phát hiện ngôn ngữ mặc định)."
    )
    parser.add_argument(
        "media",
        nargs="?",
        help="Đường dẫn file video hoặc audio",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="File .srt đầu ra; mặc định cùng tên với media",
    )
    parser.add_argument(
        "--model",
        default="base",
        help="Tên model Whisper: tiny, base, small, medium, large, ... (default: base)",
    )
    parser.add_argument(
        "--language",
        default="auto",
        metavar="CODE",
        help=(
            "auto = tự phát hiện (mặc định); hoặc mã ISO: vi, en, ja, ... "
            "Chỉ dùng vi khi lời thoại thực sự là tiếng Việt."
        ),
    )
    parser.add_argument(
        "--translate-vi",
        action="store_true",
        help="Sau khi nhận dạng, dịch toàn bộ sang tiếng Việt (cần mạng, pip install deep-translator).",
    )
    parser.add_argument(
        "--device",
        choices=["cpu", "cuda"],
        help="Thiết bị (mặc định để Whisper tự chọn)",
    )
    args = parser.parse_args()

    media_from_cli = args.media
    media = args.media.strip() if args.media else None
    if not media:
        media = _prompt_media_path()

    out = args.output.strip() if args.output else None
    if not out:
        if not media_from_cli:
            out = _prompt_output_path(media)
        else:
            out = _default_srt_path(media)

    lang_raw = (args.language or "").strip().lower()
    if lang_raw in ("auto", "", "none"):
        lang: Optional[str] = None
    else:
        lang = lang_raw

    write_srt(
        media,
        out,
        model_name=args.model,
        language=lang,
        device=args.device,
        translate_to_vi=args.translate_vi,
    )


if __name__ == "__main__":
    main()
