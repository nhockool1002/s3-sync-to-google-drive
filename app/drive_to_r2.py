"""
Đồng bộ dữ liệu từ 1 folder Google Drive sang Cloudflare R2.

OAuth drive.readonly + token_download.json giống app.drive_download / app.view_video_drive.
Giữ nguyên cấu trúc thư mục con của Drive khi đặt tên key trên R2
(khác app.transfer vốn dồn hết file S3 vào 1 folder Drive phẳng).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from typing import Any, Dict, List, Set, Tuple

from dotenv import load_dotenv
from googleapiclient.discovery import build

from .drive_download import download_file_by_id, get_credentials, list_files_recursive
from .r2_client import get_r2_client, list_existing_keys, upload_file
from .utils import ensure_dir, get_log_file_path


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _is_within_dir(path: str, base_dir: str) -> bool:
    base = os.path.realpath(base_dir)
    target = os.path.realpath(path)
    try:
        common = os.path.commonpath([base, target])
    except ValueError:
        return False
    return common == base


def _safe_remove_file(path: str, temp_dir: str) -> None:
    if not path:
        return
    if not _is_within_dir(path, temp_dir):
        return
    try:
        if os.path.exists(path) and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _safe_cleanup_empty_dirs(start_dir: str, stop_dir: str) -> None:
    cur = os.path.realpath(start_dir)
    stop = os.path.realpath(stop_dir)
    while cur and _is_within_dir(cur, stop) and cur != stop:
        try:
            os.rmdir(cur)
        except OSError:
            break
        cur = os.path.dirname(cur)


def _build_key(r2_prefix: str, rel_path: str) -> str:
    prefix = r2_prefix.strip("/")
    return f"{prefix}/{rel_path}" if prefix else rel_path


def transfer_drive_to_r2(
    drive_folder_id: str,
    r2_bucket: str,
    r2_prefix: str = "",
    batch_size: int = 50,
    temp_dir: str = "/tmp/drive_r2_sync",
    log_dir: str = "./logs",
) -> str:
    """
    Chuyển dữ liệu từ 1 folder Google Drive (đệ quy) sang Cloudflare R2 theo batch.

    Trả về đường dẫn file log.
    """
    ensure_dir(temp_dir)
    log_path = get_log_file_path(log_dir, r2_prefix or "drive_to_r2")
    print(f"{_ts()} | File log chi tiết: {log_path}", flush=True)

    print(f"{_ts()} | Khởi tạo kết nối Google Drive...", flush=True)
    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    print(f"{_ts()} | Đang liệt kê file trong folder Drive (đệ quy)...", flush=True)
    files: List[Dict[str, Any]] = list_files_recursive(service, drive_folder_id)
    total_files = len(files)
    print(f"{_ts()} | Tìm thấy tổng cộng {total_files} file trên Drive.", flush=True)

    print(f"{_ts()} | Khởi tạo kết nối Cloudflare R2...", flush=True)
    r2 = get_r2_client()
    print(f"{_ts()} | Đang lấy danh sách key hiện có trên R2...", flush=True)
    existing_keys: Set[str] = list_existing_keys(r2, r2_bucket, r2_prefix)
    print(
        f"{_ts()} | Đã tải danh sách {len(existing_keys)} key hiện có trên R2 (để SKIP nhanh).",
        flush=True,
    )

    total_processed = 0
    success = 0
    failed = 0
    skipped = 0
    failures: List[Tuple[str, str]] = []

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"{_ts()} | === Transfer Google Drive -> Cloudflare R2 ===\n")
        f.write(f"{_ts()} | Drive folder ID: {drive_folder_id}\n")
        f.write(f"{_ts()} | R2 bucket      : {r2_bucket}\n")
        f.write(f"{_ts()} | R2 prefix      : {r2_prefix or '(root)'}\n")
        f.write(f"{_ts()} | Total files    : {total_files}\n\n")

        batch: List[Dict[str, Any]] = []
        batch_index = 0

        def _flush_batch(is_last: bool) -> None:
            nonlocal batch, batch_index, total_processed, success, failed, skipped
            batch_index += 1
            label = " (batch cuối)" if is_last else ""
            print(
                f"{_ts()} | Đang xử lý batch #{batch_index} ({len(batch)} file){label}... "
                f"[Processed={total_processed}, Total={total_files}]",
                flush=True,
            )
            s, fl, sk = _process_batch(
                batch,
                service=service,
                r2=r2,
                r2_bucket=r2_bucket,
                r2_prefix=r2_prefix,
                temp_dir=temp_dir,
                log_file=f,
                existing_keys=existing_keys,
            )
            total_processed += len(batch)
            success += s
            failures.extend(fl)
            failed += len(fl)
            skipped += sk

            remain = total_files - total_processed
            print(
                f"{_ts()} | Kết thúc batch #{batch_index}{label}: "
                f"[OK={s}, FAIL={len(fl)}, SKIP={sk}, REMAIN={remain}]",
                flush=True,
            )
            batch = []

        for item in files:
            batch.append(item)
            if len(batch) >= batch_size:
                _flush_batch(is_last=False)

        if batch:
            _flush_batch(is_last=True)

        f.write(f"\n{_ts()} | === SUMMARY ===\n")
        f.write(f"{_ts()} | Total files : {total_files}\n")
        f.write(f"{_ts()} | Processed   : {total_processed}\n")
        f.write(f"{_ts()} | Success     : {success}\n")
        f.write(f"{_ts()} | Failed      : {failed}\n")
        f.write(f"{_ts()} | Skipped     : {skipped}\n")

        if failures:
            f.write(f"\n{_ts()} | Failed files:\n")
            for path, err in failures:
                f.write(f"{_ts()} | - {path}: {err}\n")

    print(
        f"{_ts()} | Hoàn tất sync: "
        f"Total={total_files}, Processed={total_processed}, "
        f"OK={success}, FAIL={failed}, SKIP={skipped}",
        flush=True,
    )

    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
    except OSError:
        pass

    return log_path


def _process_batch(
    batch: List[Dict[str, Any]],
    service: Any,
    r2: Any,
    r2_bucket: str,
    r2_prefix: str,
    temp_dir: str,
    log_file: Any,
    existing_keys: Set[str],
):
    success = 0
    skipped = 0
    failures: List[Tuple[str, str]] = []

    batch_len = len(batch)

    for idx, item in enumerate(batch, start=1):
        rel_path = item["path"]
        key = _build_key(r2_prefix, rel_path)

        print(
            f"{_ts()} | Đang xử lý file [{idx}/{batch_len}] trong batch: {rel_path}",
            flush=True,
        )

        if key in existing_keys:
            log_file.write(
                f"{_ts()} | [SKIP] [{idx}/{batch_len}] {rel_path} (đã tồn tại trên R2: {key})\n"
            )
            skipped += 1
            continue

        local_path = os.path.join(temp_dir, rel_path)

        try:
            download_file_by_id(service, item["id"], local_path)
            upload_file(r2, r2_bucket, local_path, key)
            existing_keys.add(key)
            log_file.write(f"{_ts()} | [OK]   [{idx}/{batch_len}] {rel_path} -> {key}\n")
            success += 1
            _safe_remove_file(local_path, temp_dir)
            _safe_cleanup_empty_dirs(os.path.dirname(local_path), temp_dir)
        except Exception as e:  # noqa: BLE001
            log_file.write(f"{_ts()} | [FAIL] [{idx}/{batch_len}] {rel_path} | {e}\n")
            failures.append((rel_path, str(e)))
            _safe_remove_file(local_path, temp_dir)
            _safe_cleanup_empty_dirs(os.path.dirname(local_path), temp_dir)

    log_file.flush()
    return success, failures, skipped


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Đồng bộ dữ liệu từ Google Drive sang Cloudflare R2."
    )
    parser.add_argument(
        "--drive-folder-id",
        default=os.getenv("GOOGLE_DRIVE_FOLDER_ID"),
        help="ID folder Drive nguồn.",
    )
    parser.add_argument("--bucket", default=os.getenv("R2_BUCKET"), help="Tên bucket R2 đích.")
    parser.add_argument(
        "--prefix",
        default=os.getenv("R2_PREFIX"),
        help="Prefix/key gốc trên R2 (có thể để trống).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("R2_BATCH_SIZE", "50") or 50),
        help="Số file xử lý mỗi batch. Mặc định 50.",
    )
    parser.add_argument("--log-dir", default=os.getenv("LOG_DIR", "./logs"))
    parser.add_argument(
        "--temp-dir", default=os.getenv("R2_TEMP_DIR", "/tmp/drive_r2_sync")
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = _parse_args()

    print("=== Sync Google Drive -> Cloudflare R2 ===")
    drive_folder_id = args.drive_folder_id or input("Nhập Google Drive folder ID nguồn: ").strip()
    bucket = args.bucket or input("Nhập tên R2 bucket đích: ").strip()
    prefix = (
        args.prefix
        if args.prefix is not None
        else input("Nhập prefix/key gốc trên R2 (có thể để trống): ").strip()
    )
    batch_size = args.batch_size

    if not drive_folder_id:
        print("Cần có Google Drive folder ID.", file=sys.stderr)
        sys.exit(1)
    if not bucket:
        print("Cần có tên R2 bucket.", file=sys.stderr)
        sys.exit(1)
    if batch_size <= 0:
        print("Batch size phải lớn hơn 0.", file=sys.stderr)
        sys.exit(1)

    log_path = transfer_drive_to_r2(
        drive_folder_id=drive_folder_id,
        r2_bucket=bucket,
        r2_prefix=prefix or "",
        batch_size=batch_size,
        temp_dir=args.temp_dir,
        log_dir=args.log_dir,
    )

    print("\nHoàn tất chuyển dữ liệu.")
    print(f"File log chi tiết: {log_path}")


if __name__ == "__main__":
    main()
