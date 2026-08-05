from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "r2" / "source"
DEFAULT_LOG_DIR = PROJECT_ROOT / "r2" / "logs"
MB = 1024 * 1024

RETRYABLE_ERROR_CODES = {
    "InternalError",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "SlowDown",
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
}
RETRYABLE_ERROR_MARKERS = (
    "connection aborted",
    "connection reset",
    "connection was closed",
    "connect timeout",
    "read timeout",
    "service unavailable",
    "slowdown",
    "temporarily unavailable",
    "timed out",
    "timeout on endpoint",
    "too many requests",
)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iter_files(source_dir: Path, recursive: bool) -> Iterable[Path]:
    pattern = "**/*" if recursive else "*"
    for path in sorted(source_dir.glob(pattern)):
        if path.is_file() and not path.name.startswith("."):
            yield path


def _prompt_choice(files: list[Path]) -> Optional[Path]:
    print("\nFile trong r2/source:")
    print("0. Upload tất cả")
    for index, path in enumerate(files, start=1):
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"{index}. {path.name} ({size_mb:.2f} MB)")

    while True:
        raw = input("Chọn file upload: ").strip()
        try:
            choice = int(raw)
        except ValueError:
            print("Vui lòng nhập số thứ tự.")
            continue

        if choice == 0:
            return None
        if 1 <= choice <= len(files):
            return files[choice - 1]
        print("Lựa chọn không hợp lệ.")


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Thiếu biến môi trường {name} trong .env")
    return value


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("Giá trị phải lớn hơn 0.")
    return value


def _get_positive_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return _positive_int(value)
    except (TypeError, ValueError, argparse.ArgumentTypeError) as exc:
        raise ValueError(f"Biến môi trường {name} phải là số nguyên lớn hơn 0.") from exc


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv()


def get_r2_client(connect_timeout: int, read_timeout: int, max_attempts: int):
    try:
        import boto3
        from botocore.config import Config
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Thiếu boto3/botocore. Cài bằng: pip install -r requirements.txt"
        ) from exc

    endpoint_url = _require_env("R2_ENDPOINT_URL")
    access_key_id = _require_env("R2_ACCESS_KEY_ID")
    secret_access_key = _require_env("R2_SECRET_ACCESS_KEY")
    retry_mode = os.getenv("R2_RETRY_MODE", "standard").strip() or "standard"
    addressing_style = os.getenv("R2_ADDRESSING_STYLE", "path").strip() or "path"

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=os.getenv("R2_REGION", "auto"),
        config=Config(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"max_attempts": max_attempts, "mode": retry_mode},
            s3={"addressing_style": addressing_style},
            tcp_keepalive=True,
        ),
    )


def get_transfer_config(
    multipart_threshold_mb: int,
    multipart_chunk_mb: int,
    concurrency: int,
):
    try:
        from boto3.s3.transfer import TransferConfig
    except ModuleNotFoundError as exc:
        raise RuntimeError("Thiếu boto3. Cài bằng: pip install -r requirements.txt") from exc

    return TransferConfig(
        multipart_threshold=multipart_threshold_mb * MB,
        multipart_chunksize=multipart_chunk_mb * MB,
        max_concurrency=concurrency,
        use_threads=concurrency > 1,
    )


def _log_path(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return log_dir / f"{stamp}_r2_upload.log"


def _pid_path_for(log_path: Path) -> Path:
    return log_path.with_suffix(".pid")


def _object_key(path: Path, source_dir: Path, recursive: bool, prefix: str) -> str:
    relative = path.relative_to(source_dir) if recursive else Path(path.name)
    key = relative.as_posix()
    prefix = prefix.strip("/")
    return f"{prefix}/{key}" if prefix else key


class UploadProgress:
    def __init__(self, path: Path, log_file, *, min_interval: float = 1.0) -> None:
        self.path = path
        self.log_file = log_file
        self.total = max(1, path.stat().st_size)
        self.seen = 0
        self.min_interval = min_interval
        self.last_emit = 0.0
        self.complete_emitted = False
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.seen = 0
            self.last_emit = 0.0
            self.complete_emitted = False

    def __call__(self, bytes_amount: int) -> None:
        with self.lock:
            if self.complete_emitted:
                return

            self.seen = min(self.total, self.seen + bytes_amount)
            now = time.monotonic()
            is_complete = self.seen >= self.total
            if not is_complete and now - self.last_emit < self.min_interval:
                return
            self.last_emit = now

            percent = self.seen * 100 / self.total
            uploaded_mb = self.seen / (1024 * 1024)
            total_mb = self.total / (1024 * 1024)
            self.log_file.write(
                f"{_ts()} | [PROGRESS] {self.path.name}: "
                f"{percent:.1f}% ({uploaded_mb:.2f}/{total_mb:.2f} MB)\n"
            )
            self.log_file.flush()
            if is_complete:
                self.complete_emitted = True


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return str(code) if code else ""


def _is_retryable_upload_error(exc: Exception) -> bool:
    code = _error_code(exc)
    if code in RETRYABLE_ERROR_CODES:
        return True

    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in RETRYABLE_ERROR_MARKERS)


def upload_file(
    client,
    bucket: str,
    source_path: Path,
    source_dir: Path,
    recursive: bool,
    prefix: str,
    progress: UploadProgress,
    transfer_config,
    file_retries: int,
    retry_delay: int,
    log_file,
) -> str:
    key = _object_key(source_path, source_dir, recursive, prefix)
    for attempt in range(1, file_retries + 1):
        if attempt > 1:
            progress.reset()

        try:
            client.upload_file(
                str(source_path),
                bucket,
                key,
                Callback=progress,
                Config=transfer_config,
            )
            return key
        except Exception as exc:  # noqa: BLE001
            if attempt >= file_retries or not _is_retryable_upload_error(exc):
                raise

            wait_seconds = min(retry_delay * (2 ** (attempt - 1)), 300)
            log_file.write(
                f"{_ts()} | [RETRY] {source_path.name}: attempt "
                f"{attempt}/{file_retries} lỗi tạm thời: {exc}. "
                f"Thử lại sau {wait_seconds}s.\n"
            )
            log_file.flush()
            time.sleep(wait_seconds)

    raise RuntimeError("Upload không hoàn tất sau các lần retry.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload file từ r2/source lên Cloudflare R2 và ghi log vào r2/logs."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"Folder chứa file upload, default: {DEFAULT_SOURCE_DIR}",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help=f"Folder log, default: {DEFAULT_LOG_DIR}",
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("R2_BUCKET_NAME"),
        help="Tên bucket R2. Mặc định đọc R2_BUCKET_NAME từ .env.",
    )
    parser.add_argument(
        "--prefix",
        default=os.getenv("R2_UPLOAD_PREFIX", ""),
        help="Prefix/path trên bucket R2, mặc định rỗng.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Quét cả thư mục con trong r2/source.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Upload tất cả file, không hỏi chọn.",
    )
    parser.add_argument(
        "--file",
        action="append",
        type=Path,
        dest="files",
        help="File cụ thể cần upload. Có thể truyền nhiều lần.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Chạy upload trực tiếp thay vì spawn process nền.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="File log cụ thể cho worker nền.",
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


def _resolve_files_from_args(
    args: argparse.Namespace,
    source_dir: Path,
) -> list[Path]:
    files = list(_iter_files(source_dir, args.recursive))
    if not files and not args.files:
        print(f"Không tìm thấy file trong: {source_dir}")
        return []

    if args.files:
        resolved_files: list[Path] = []
        for file_path in args.files:
            path = file_path.expanduser().resolve()
            if not path.is_file():
                print(f"Không tìm thấy file: {path}", file=sys.stderr)
                continue
            resolved_files.append(path)
        return resolved_files

    if args.all:
        return files

    selected = _prompt_choice(files)
    return files if selected is None else [selected]


def _worker_command(args: argparse.Namespace, upload_files: list[Path], log_path: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "app.r2_upload",
        "--foreground",
        "--source",
        str(args.source),
        "--log-dir",
        str(args.log_dir),
        "--bucket",
        args.bucket,
        "--prefix",
        args.prefix,
        "--log-file",
        str(log_path),
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
    if args.recursive:
        command.append("--recursive")
    for path in upload_files:
        command.extend(["--file", str(path)])
    return command


def start_background(args: argparse.Namespace, upload_files: list[Path]) -> tuple[int, Path, Path]:
    log_dir = args.log_dir.expanduser().resolve()
    log_path = _log_path(log_dir)
    pid_path = _pid_path_for(log_path)

    with open(log_path, "a", encoding="utf-8", buffering=1) as log_file:
        log_file.write(f"{_ts()} | === Upload R2 nền ===\n")
        log_file.write(f"{_ts()} | Source: {args.source}\n")
        log_file.write(f"{_ts()} | Bucket: {args.bucket}\n")
        log_file.write(f"{_ts()} | Prefix: {args.prefix or '(root)'}\n")
        log_file.write(f"{_ts()} | Total files: {len(upload_files)}\n")
        for path in upload_files:
            log_file.write(f"{_ts()} | - {path}\n")
        log_file.write("\n")
        log_file.flush()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            _worker_command(args, upload_files, log_path),
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.write(f"{_ts()} | PID: {process.pid}\n")
        log_file.write(f"{_ts()} | Worker upload đã chạy nền.\n\n")

    with open(pid_path, "w", encoding="utf-8") as pid_file:
        pid_file.write(f"{process.pid}\n")

    return process.pid, log_path, pid_path


def run_upload(
    args: argparse.Namespace,
    upload_files: list[Path],
    log_path: Path,
) -> None:
    source_dir = args.source.expanduser().resolve()
    bucket = (args.bucket or "").strip()

    success_count = 0
    failed_count = 0

    with open(log_path, "a", encoding="utf-8", buffering=1) as log:
        log.write(f"{_ts()} | === Upload R2 ===\n")
        log.write(f"{_ts()} | Source: {source_dir}\n")
        log.write(f"{_ts()} | Bucket: {bucket}\n")
        log.write(f"{_ts()} | Prefix: {args.prefix or '(root)'}\n")
        log.write(f"{_ts()} | Total files: {len(upload_files)}\n\n")
        log.write(
            f"{_ts()} | Config: connect_timeout={args.connect_timeout}s, "
            f"read_timeout={args.read_timeout}s, max_attempts={args.max_attempts}, "
            f"multipart_chunk={args.multipart_chunk_mb}MB, "
            f"concurrency={args.concurrency}, file_retries={args.file_retries}\n\n"
        )

        client = get_r2_client(
            args.connect_timeout,
            args.read_timeout,
            args.max_attempts,
        )
        transfer_config = get_transfer_config(
            args.multipart_threshold_mb,
            args.multipart_chunk_mb,
            args.concurrency,
        )
        for index, path in enumerate(upload_files, start=1):
            size_mb = path.stat().st_size / (1024 * 1024)
            print(f"{_ts()} | [{index}/{len(upload_files)}] Upload: {path.name}")
            log.write(
                f"{_ts()} | [START] [{index}/{len(upload_files)}] "
                f"{path} ({size_mb:.2f} MB)\n"
            )
            log.flush()

            try:
                progress = UploadProgress(path, log)
                key = upload_file(
                    client,
                    bucket,
                    path,
                    source_dir,
                    args.recursive,
                    args.prefix,
                    progress,
                    transfer_config,
                    args.file_retries,
                    args.retry_delay,
                    log,
                )
            except Exception as exc:  # noqa: BLE001
                failed_count += 1
                print(f"{_ts()} | [fail] {path.name}: {exc}", file=sys.stderr)
                log.write(f"{_ts()} | [FAIL] {path} | {exc}\n")
                log.flush()
                continue

            success_count += 1
            print(f"{_ts()} | [ok] {path.name} -> r2://{bucket}/{key}")
            log.write(f"{_ts()} | [OK] {path} -> r2://{bucket}/{key}\n")
            log.flush()

        log.write(
            f"\n{_ts()} | Hoàn tất: {success_count} thành công, {failed_count} lỗi.\n"
        )

    print(f"\nFile log: {log_path}")


def main() -> None:
    _load_dotenv_if_available()
    parser = build_parser()
    args = parser.parse_args()

    args.source = args.source.expanduser().resolve()
    args.log_dir = args.log_dir.expanduser().resolve()
    args.bucket = (args.bucket or "").strip()
    args.prefix = (args.prefix or "").strip()

    if not args.bucket:
        print("Thiếu bucket. Hãy set R2_BUCKET_NAME trong .env.", file=sys.stderr)
        sys.exit(1)
    if not args.source.is_dir():
        print(f"Không tìm thấy folder source: {args.source}", file=sys.stderr)
        sys.exit(1)

    upload_files = _resolve_files_from_args(args, args.source)
    if not upload_files:
        return

    if args.foreground:
        log_path = args.log_file.expanduser().resolve() if args.log_file else _log_path(args.log_dir)
        run_upload(args, upload_files, log_path)
        return

    pid, log_path, pid_path = start_background(args, upload_files)
    print("Đã chuyển upload R2 sang chạy nền.")
    print(f"PID: {pid}")
    print(f"File log tiến trình: {log_path}")
    print(f"File PID: {pid_path}")
    print(f"Theo dõi log: tail -f {log_path}")


if __name__ == "__main__":
    main()
