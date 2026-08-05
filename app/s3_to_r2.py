"""
Sync AWS S3 -> Cloudflare R2 theo batch, ghi log chi tiết OK/FAIL/SKIP.

Chạy:
  python -m app.s3_to_r2
  python -m app.s3_to_r2 \
    --bucket mybucket --prefix folder \
    --r2-bucket dest-bucket --r2-prefix backups
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .r2_upload import (
    _get_positive_int_env,
    _load_dotenv_if_available,
    _positive_int,
    get_r2_client,
    get_transfer_config,
    upload_file,
    UploadProgress,
)
from .s3_client import download_object, list_objects
from .transfer import _safe_cleanup_empty_dirs, _safe_remove_file
from .utils import ensure_dir

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ts() -> str:
    """Timestamp chuẩn cho log/console."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_prefix(prefix: str) -> str:
    return prefix.strip("/").replace("/", "_") or "root"


def _log_path(log_dir: str, prefix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"{ts}_s3_r2_{_safe_prefix(prefix)}.log")


def _progress_log_path(log_dir: str, prefix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"{ts}_s3_r2_{_safe_prefix(prefix)}.progress.log")


def _pid_path_for(progress_log_path: str) -> str:
    return os.path.splitext(progress_log_path)[0] + ".pid"


def _target_key(rel_path: str, r2_prefix: str) -> str:
    rel_path = rel_path.lstrip("/")
    r2_prefix = (r2_prefix or "").strip("/")
    return f"{r2_prefix}/{rel_path}" if r2_prefix else rel_path


def list_existing_r2_keys(client: Any, bucket: str, prefix: str) -> set[str]:
    """Lấy toàn bộ key hiện có trên R2 (theo prefix đích) để skip nhanh."""
    keys: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")

    kwargs: Dict[str, Any] = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix.strip("/")

    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])

    return keys


def _process_batch(
    batch: List[Dict[str, Any]],
    *,
    bucket: str,
    prefix: str,
    client: Any,
    r2_bucket: str,
    r2_prefix: str,
    temp_dir: str,
    transfer_config: Any,
    file_retries: int,
    retry_delay: int,
    log_file: Any,
    existing_keys: set[str],
) -> Tuple[int, List[Tuple[str, str]], int]:
    """
    Xử lý 1 batch object:
      - tải từng object S3 về local
      - upload lên Cloudflare R2 (nếu chưa tồn tại)
      - ghi log kết quả

    Trả về: (success, failures, skipped)
    """
    success = 0
    skipped = 0
    failures: List[Tuple[str, str]] = []
    batch_len = len(batch)

    for idx, obj in enumerate(batch, start=1):
        key = obj["Key"]

        rel_path = key[len(prefix):] if prefix and key.startswith(prefix) else key
        rel_path = rel_path.lstrip("/")
        file_name = os.path.basename(rel_path) or rel_path

        if not rel_path or file_name.startswith(".") or file_name.lower().endswith(".ds_store"):
            log_file.write(
                f"{_ts()} | [SKIP] [{idx}/{batch_len}] {key} (file hệ thống/ẩn, không upload)\n"
            )
            skipped += 1
            continue

        target_key = _target_key(rel_path, r2_prefix)
        if target_key in existing_keys:
            log_file.write(
                f"{_ts()} | [SKIP] [{idx}/{batch_len}] {key} (đã tồn tại trên R2)\n"
            )
            skipped += 1
            continue

        print(
            f"{_ts()} | Đang xử lý object [{idx}/{batch_len}] trong batch: {key}",
            flush=True,
        )

        local_path = os.path.join(temp_dir, rel_path)

        try:
            download_object(bucket, key, local_path)
            progress = UploadProgress(Path(local_path), log_file)
            upload_file(
                client,
                r2_bucket,
                Path(local_path),
                Path(temp_dir),
                True,
                r2_prefix,
                progress,
                transfer_config,
                file_retries,
                retry_delay,
                log_file,
            )
            existing_keys.add(target_key)
            log_file.write(
                f"{_ts()} | [OK]   [{idx}/{batch_len}] {key} -> r2://{r2_bucket}/{target_key}\n"
            )
            success += 1
        except Exception as e:  # noqa: BLE001
            log_file.write(f"{_ts()} | [FAIL] [{idx}/{batch_len}] {key} | {e}\n")
            failures.append((key, str(e)))
        finally:
            _safe_remove_file(local_path, temp_dir)
            _safe_cleanup_empty_dirs(os.path.dirname(local_path), temp_dir)

    log_file.flush()
    return success, failures, skipped


def transfer_s3_to_r2(
    bucket: str,
    prefix: str,
    r2_bucket: str,
    r2_prefix: str = "",
    batch_size: int = 50,
    temp_dir: str = "/tmp/s3_to_r2",
    log_dir: str = "./logs",
    *,
    connect_timeout: int = 30,
    read_timeout: int = 300,
    max_attempts: int = 10,
    multipart_threshold_mb: int = 64,
    multipart_chunk_mb: int = 64,
    concurrency: int = 4,
    file_retries: int = 3,
    retry_delay: int = 15,
) -> str:
    """
    Chuyển dữ liệu từ S3 sang Cloudflare R2 theo batch, ghi log chi tiết.

    Trả về đường dẫn file log.
    """
    ensure_dir(temp_dir)
    log_path = _log_path(log_dir, prefix)
    print(f"{_ts()} | File log chi tiết: {log_path}", flush=True)

    total_processed = 0
    success = 0
    failed = 0
    skipped = 0
    failures: List[Tuple[str, str]] = []

    print(f"{_ts()} | Khởi tạo kết nối Cloudflare R2...", flush=True)
    client = get_r2_client(connect_timeout, read_timeout, max_attempts)
    transfer_config = get_transfer_config(
        multipart_threshold_mb, multipart_chunk_mb, concurrency
    )

    print(f"{_ts()} | Đang liệt kê objects trong S3...", flush=True)
    raw_objects: List[Dict[str, Any]] = list(list_objects(bucket, prefix))

    objects: List[Dict[str, Any]] = []
    for obj in raw_objects:
        name = os.path.basename(obj["Key"])
        if name.startswith(".") or name.lower().endswith(".ds_store"):
            continue
        objects.append(obj)

    total_objects = len(objects)
    print(
        f"{_ts()} | Tìm thấy tổng cộng {total_objects} object trong S3 (prefix='{prefix or ''}').",
        flush=True,
    )

    print(f"{_ts()} | Đang lấy danh sách object hiện có trên R2...", flush=True)
    existing_keys = list_existing_r2_keys(client, r2_bucket, r2_prefix)
    print(
        f"{_ts()} | Đã tải danh sách {len(existing_keys)} object hiện có trên R2 (để SKIP nhanh).",
        flush=True,
    )

    print(f"{_ts()} | Kết nối R2 OK. Bắt đầu xử lý S3...", flush=True)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"{_ts()} | === Transfer S3 -> Cloudflare R2 ===\n")
        f.write(f"{_ts()} | S3 bucket  : {bucket}\n")
        f.write(f"{_ts()} | S3 prefix  : {prefix or '(root)'}\n")
        f.write(f"{_ts()} | R2 bucket  : {r2_bucket}\n")
        f.write(f"{_ts()} | R2 prefix  : {r2_prefix or '(root)'}\n")
        f.write(f"{_ts()} | Total S3 objects: {total_objects}\n\n")

        batch: List[Dict[str, Any]] = []
        batch_index = 0
        for obj in objects:
            batch.append(obj)
            if len(batch) >= batch_size:
                batch_index += 1
                print(
                    f"{_ts()} | Đang xử lý batch #{batch_index} ({len(batch)} objects)... "
                    f"[Processed={total_processed}, Total={total_objects}]",
                    flush=True,
                )
                s, fl, sk = _process_batch(
                    batch,
                    bucket=bucket,
                    prefix=prefix,
                    client=client,
                    r2_bucket=r2_bucket,
                    r2_prefix=r2_prefix,
                    temp_dir=temp_dir,
                    transfer_config=transfer_config,
                    file_retries=file_retries,
                    retry_delay=retry_delay,
                    log_file=f,
                    existing_keys=existing_keys,
                )
                total_processed += len(batch)
                success += s
                failures.extend(fl)
                failed += len(fl)
                skipped += sk

                remain = total_objects - total_processed
                print(
                    f"{_ts()} | Kết thúc batch #{batch_index}: "
                    f"[OK={s}, FAIL={len(fl)}, SKIP={sk}, REMAIN={remain}]",
                    flush=True,
                )
                batch = []

        if batch:
            batch_index += 1
            print(
                f"{_ts()} | Đang xử lý batch #{batch_index} ({len(batch)} objects - batch cuối)... "
                f"[Processed={total_processed}, Total={total_objects}]",
                flush=True,
            )
            s, fl, sk = _process_batch(
                batch,
                bucket=bucket,
                prefix=prefix,
                client=client,
                r2_bucket=r2_bucket,
                r2_prefix=r2_prefix,
                temp_dir=temp_dir,
                transfer_config=transfer_config,
                file_retries=file_retries,
                retry_delay=retry_delay,
                log_file=f,
                existing_keys=existing_keys,
            )
            total_processed += len(batch)
            success += s
            failures.extend(fl)
            failed += len(fl)
            skipped += sk

            remain = total_objects - total_processed
            print(
                f"{_ts()} | Kết thúc batch #{batch_index} (batch cuối): "
                f"[OK={s}, FAIL={len(fl)}, SKIP={sk}, REMAIN={remain}]",
                flush=True,
            )

        f.write(f"\n{_ts()} | === SUMMARY ===\n")
        f.write(f"{_ts()} | Total objects : {total_objects}\n")
        f.write(f"{_ts()} | Processed     : {total_processed}\n")
        f.write(f"{_ts()} | Success       : {success}\n")
        f.write(f"{_ts()} | Failed        : {failed}\n")
        f.write(f"{_ts()} | Skipped       : {skipped}\n")

        if failures:
            f.write(f"\n{_ts()} | Failed objects:\n")
            for key, err in failures:
                f.write(f"{_ts()} | - {key}: {err}\n")

    print(
        f"{_ts()} | Hoàn tất sync: "
        f"Total={total_objects}, Processed={total_processed}, "
        f"OK={success}, FAIL={failed}, SKIP={skipped}",
        flush=True,
    )

    # Dọn dẹp thư mục tạm sau khi hoàn tất (không ảnh hưởng logs)
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
    except OSError:
        pass

    return log_path


def _prompt_config(args: argparse.Namespace) -> argparse.Namespace:
    print("=== Sync AWS S3 -> Cloudflare R2 ===")

    if not args.bucket:
        args.bucket = input("Nhập tên S3 bucket: ").strip()
    if args.prefix is None:
        args.prefix = input("Nhập folder/prefix trên S3 (có thể để trống): ").strip()
    if not args.r2_bucket:
        args.r2_bucket = input("Nhập tên bucket R2 đích: ").strip()
    if args.r2_prefix is None:
        args.r2_prefix = input(
            "Nhập prefix/path trên R2 đích (Enter để giữ nguyên cấu trúc thư mục S3): "
        ).strip()

    if not args.bucket:
        raise ValueError("S3 bucket không được để trống.")
    if not args.r2_bucket:
        raise ValueError("R2 bucket đích không được để trống.")

    args.prefix = args.prefix or ""
    args.r2_prefix = args.r2_prefix or ""
    return args


def _worker_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "-m",
        "app.s3_to_r2",
        "--foreground",
        "--bucket",
        args.bucket,
        "--prefix",
        args.prefix,
        "--r2-bucket",
        args.r2_bucket,
        "--r2-prefix",
        args.r2_prefix,
        "--batch-size",
        str(args.batch_size),
        "--log-dir",
        args.log_dir,
        "--temp-dir",
        args.temp_dir,
        "--connect-timeout",
        str(args.connect_timeout),
        "--read-timeout",
        str(args.read_timeout),
        "--max-attempts",
        str(args.max_attempts),
        "--multipart-threshold-mb",
        str(args.multipart_threshold_mb),
        "--multipart-chunk-mb",
        str(args.multipart_chunk_mb),
        "--concurrency",
        str(args.concurrency),
        "--file-retries",
        str(args.file_retries),
        "--retry-delay",
        str(args.retry_delay),
    ]


def start_background(args: argparse.Namespace) -> tuple[int, str, str]:
    ensure_dir(args.log_dir)
    progress_log_path = _progress_log_path(args.log_dir, args.prefix)
    pid_path = _pid_path_for(progress_log_path)
    command = _worker_command(args)

    with open(progress_log_path, "a", encoding="utf-8", buffering=1) as log_file:
        log_file.write("=== Background sync S3 -> Cloudflare R2 ===\n")
        log_file.write(f"S3 bucket  : {args.bucket}\n")
        log_file.write(f"S3 prefix  : {args.prefix or '(root)'}\n")
        log_file.write(f"R2 bucket  : {args.r2_bucket}\n")
        log_file.write(f"R2 prefix  : {args.r2_prefix or '(root)'}\n")
        log_file.write(f"Batch size : {args.batch_size}\n\n")
        log_file.flush()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        log_file.write(f"PID        : {process.pid}\n")
        log_file.write("Trạng thái : Đã khởi chạy worker nền.\n\n")

    with open(pid_path, "w", encoding="utf-8") as pid_file:
        pid_file.write(f"{process.pid}\n")

    return process.pid, progress_log_path, pid_path


def run_foreground(args: argparse.Namespace) -> str:
    log_path = transfer_s3_to_r2(
        bucket=args.bucket,
        prefix=args.prefix,
        r2_bucket=args.r2_bucket,
        r2_prefix=args.r2_prefix,
        batch_size=args.batch_size,
        temp_dir=args.temp_dir,
        log_dir=args.log_dir,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        max_attempts=args.max_attempts,
        multipart_threshold_mb=args.multipart_threshold_mb,
        multipart_chunk_mb=args.multipart_chunk_mb,
        concurrency=args.concurrency,
        file_retries=args.file_retries,
        retry_delay=args.retry_delay,
    )
    print("\nHoàn tất chuyển dữ liệu.")
    print(f"File log chi tiết: {log_path}")
    return log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync AWS S3 -> Cloudflare R2 theo batch, ghi log chi tiết OK/FAIL/SKIP."
    )
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET"), help="Tên S3 bucket nguồn.")
    parser.add_argument(
        "--prefix",
        default=os.getenv("S3_PREFIX"),
        help="Folder/prefix trên S3 (có thể để trống).",
    )
    parser.add_argument(
        "--r2-bucket",
        dest="r2_bucket",
        default=os.getenv("R2_BUCKET_NAME"),
        help="Tên bucket R2 đích. Mặc định đọc R2_BUCKET_NAME từ .env.",
    )
    parser.add_argument(
        "--r2-prefix",
        dest="r2_prefix",
        default=os.getenv("R2_SYNC_PREFIX"),
        help="Prefix/path trên bucket R2 đích, mặc định giữ nguyên cấu trúc thư mục S3.",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=_get_positive_int_env("BATCH_SIZE", 50),
        help="Số object xử lý mỗi batch. Mặc định 50.",
    )
    parser.add_argument(
        "--log-dir",
        default=os.getenv("LOG_DIR", "./logs"),
        help="Thư mục lưu progress log và log chi tiết.",
    )
    parser.add_argument(
        "--temp-dir",
        default=os.getenv("TEMP_DIR_S3_R2", "/tmp/s3_to_r2"),
        help="Thư mục tạm để tải file S3 trước khi upload lên R2.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Chạy trực tiếp trong terminal hiện tại thay vì tạo process nền.",
    )
    parser.add_argument(
        "--connect-timeout",
        type=_positive_int,
        default=_get_positive_int_env("R2_CONNECT_TIMEOUT", 30),
        help="Timeout kết nối R2, giây. Mặc định 30 hoặc R2_CONNECT_TIMEOUT.",
    )
    parser.add_argument(
        "--read-timeout",
        type=_positive_int,
        default=_get_positive_int_env("R2_READ_TIMEOUT", 300),
        help="Timeout đọc phản hồi R2, giây. Mặc định 300 hoặc R2_READ_TIMEOUT.",
    )
    parser.add_argument(
        "--max-attempts",
        type=_positive_int,
        default=_get_positive_int_env("R2_MAX_ATTEMPTS", 10),
        help="Số lần botocore retry request. Mặc định 10 hoặc R2_MAX_ATTEMPTS.",
    )
    parser.add_argument(
        "--multipart-threshold-mb",
        type=_positive_int,
        default=_get_positive_int_env("R2_MULTIPART_THRESHOLD_MB", 64),
        help="Ngưỡng bật multipart upload, MB. Mặc định 64.",
    )
    parser.add_argument(
        "--multipart-chunk-mb",
        type=_positive_int,
        default=_get_positive_int_env("R2_MULTIPART_CHUNK_MB", 64),
        help="Dung lượng mỗi part multipart, MB. Mặc định 64.",
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=_get_positive_int_env("R2_UPLOAD_CONCURRENCY", 4),
        help="Số part upload song song. Mặc định 4 hoặc R2_UPLOAD_CONCURRENCY.",
    )
    parser.add_argument(
        "--file-retries",
        type=_positive_int,
        default=_get_positive_int_env("R2_FILE_RETRIES", 3),
        help="Số lần retry lại file khi gặp lỗi tạm thời. Mặc định 3.",
    )
    parser.add_argument(
        "--retry-delay",
        type=_positive_int,
        default=_get_positive_int_env("R2_RETRY_DELAY_SECONDS", 15),
        help="Thời gian chờ retry file ban đầu, giây. Mặc định 15.",
    )
    return parser


def main() -> None:
    _load_dotenv_if_available()
    parser = build_parser()
    args = parser.parse_args()
    args = _prompt_config(args)

    if args.foreground:
        run_foreground(args)
        return

    pid, progress_log_path, pid_path = start_background(args)
    print("\nĐã chuyển tiến trình sync S3 -> R2 sang chạy nền.")
    print(f"PID: {pid}")
    print(f"File log tiến trình: {progress_log_path}")
    print(f"File PID: {pid_path}")
    print(f"Theo dõi log: tail -f {progress_log_path}")


if __name__ == "__main__":
    main()
