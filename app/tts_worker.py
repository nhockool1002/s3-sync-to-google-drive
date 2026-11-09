"""Worker 1-lần-1-process để gọi edge-tts.

Chạy riêng mỗi câu trong 1 process mới (thay vì gọi lặp lại edge_tts.Communicate
trong cùng 1 process) vì thư viện edge-tts có bug: sau lần gọi đầu tiên thành
công trong 1 process, các lần gọi sau trong CÙNG process đó hay bị lỗi
"No audio was received" (cache DRM/clock-skew bị stale). Tách mỗi câu ra 1
process riêng (qua `python -m app.tts_worker` khi chạy từ source, hoặc
`<app đã build> --tts-worker` khi đã đóng gói) tránh được bug này hoàn toàn.
"""
from __future__ import annotations

import argparse
import asyncio


def main(argv: list[str] | None = None) -> None:
    import edge_tts

    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", required=True)
    parser.add_argument("--rate", default="+0%")
    parser.add_argument("--text", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    async def _run() -> None:
        communicate = edge_tts.Communicate(args.text, args.voice, rate=args.rate)
        await communicate.save(args.out)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
