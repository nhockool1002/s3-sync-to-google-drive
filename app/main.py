import argparse
import contextlib
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from typing import Callable

from dotenv import load_dotenv

from .transfer import transfer_s3_to_drive
from .utils import ensure_dir, get_progress_log_file_path


@dataclass(frozen=True)
class SyncConfig:
    bucket: str
    prefix: str
    drive_folder_id: str
    batch_size: int
    log_dir: str
    temp_dir: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Menu công cụ sync S3, Google Drive và video."
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Chạy sync S3 -> Google Drive ngay, không hiện menu.",
    )
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET"))
    parser.add_argument("--prefix", default=os.getenv("S3_PREFIX"))
    parser.add_argument(
        "--drive-folder-id",
        default=os.getenv("GOOGLE_DRIVE_FOLDER_ID"),
        help="Google Drive folder ID đích. Để trống nếu muốn upload vào My Drive root.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_get_int_env("BATCH_SIZE"),
        help="Số object xử lý mỗi batch. Mặc định 50.",
    )
    parser.add_argument(
        "--log-dir",
        default=os.getenv("LOG_DIR", "./logs"),
        help="Thư mục lưu progress log và log chi tiết.",
    )
    parser.add_argument(
        "--temp-dir",
        default=os.getenv("TEMP_DIR", "/tmp/s3_sync"),
        help="Thư mục tạm để tải file S3 trước khi upload.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Chạy trực tiếp trong terminal hiện tại thay vì tạo process nền.",
    )
    parser.add_argument(
        "--progress-log",
        action="store_true",
        help="Khi chạy foreground, đồng thời ghi stdout/stderr vào file progress log.",
    )
    return parser.parse_args()


def _sync_args_were_provided(argv: list[str]) -> bool:
    sync_flags = {
        "--sync",
        "--bucket",
        "--prefix",
        "--drive-folder-id",
        "--batch-size",
        "--log-dir",
        "--temp-dir",
        "--foreground",
        "--progress-log",
    }
    for arg in argv:
        if arg in sync_flags or any(arg.startswith(f"{flag}=") for flag in sync_flags):
            return True
    return False


def _get_int_env(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Biến môi trường {name} phải là số nguyên.") from exc


def _prompt_config(args: argparse.Namespace) -> SyncConfig:
    print("=== Sync AWS S3 -> Google Drive ===")

    bucket = args.bucket or input("Nhập tên S3 bucket: ").strip()
    prefix = args.prefix if args.prefix is not None else input(
        "Nhập folder name trên S3 (có thể để trống): "
    ).strip()
    drive_folder_id = (
        args.drive_folder_id
        if args.drive_folder_id is not None
        else input("Nhập Google Drive folder ID: ").strip()
    )
    batch_size = args.batch_size
    if batch_size is None:
        batch_size_str = input("Nhập batch size (default 50): ").strip()
        batch_size = int(batch_size_str) if batch_size_str else 50

    if not bucket:
        raise ValueError("S3 bucket không được để trống.")
    if batch_size <= 0:
        raise ValueError("Batch size phải lớn hơn 0.")

    return SyncConfig(
        bucket=bucket,
        prefix=prefix or "",
        drive_folder_id=drive_folder_id or "",
        batch_size=batch_size,
        log_dir=args.log_dir,
        temp_dir=args.temp_dir,
    )


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_worker_command(config: SyncConfig) -> list[str]:
    return [
        sys.executable,
        "-m",
        "app.main",
        "--sync",
        "--foreground",
        "--bucket",
        config.bucket,
        "--prefix",
        config.prefix,
        "--drive-folder-id",
        config.drive_folder_id,
        "--batch-size",
        str(config.batch_size),
        "--log-dir",
        config.log_dir,
        "--temp-dir",
        config.temp_dir,
    ]


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def _start_background(config: SyncConfig) -> tuple[int, str, str]:
    ensure_dir(config.log_dir)
    progress_log_path = get_progress_log_file_path(config.log_dir, config.prefix)
    pid_path = os.path.splitext(progress_log_path)[0] + ".pid"
    command = _build_worker_command(config)

    with open(progress_log_path, "a", encoding="utf-8", buffering=1) as log_file:
        log_file.write("=== Background sync S3 -> Google Drive ===\n")
        log_file.write(f"Bucket          : {config.bucket}\n")
        log_file.write(f"Prefix          : {config.prefix or '(root)'}\n")
        log_file.write(f"Drive folder ID : {config.drive_folder_id or '(My Drive root)'}\n")
        log_file.write(f"Batch size      : {config.batch_size}\n\n")
        log_file.flush()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            command,
            cwd=_project_root(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        log_file.write(f"PID             : {process.pid}\n")
        log_file.write("Trạng thái      : Đã khởi chạy worker nền.\n\n")

    with open(pid_path, "w", encoding="utf-8") as pid_file:
        pid_file.write(f"{process.pid}\n")

    return process.pid, progress_log_path, pid_path


def _run_foreground(config: SyncConfig) -> str:
    log_path = transfer_s3_to_drive(
        bucket=config.bucket,
        prefix=config.prefix,
        drive_folder_id=config.drive_folder_id,
        batch_size=config.batch_size,
        temp_dir=config.temp_dir,
        log_dir=config.log_dir,
    )

    print("\nHoàn tất chuyển dữ liệu.")
    print(f"File log chi tiết: {log_path}")
    return log_path


def _run_foreground_with_progress_log(config: SyncConfig) -> str:
    ensure_dir(config.log_dir)
    progress_log_path = get_progress_log_file_path(config.log_dir, config.prefix)
    print(f"File log tiến trình: {progress_log_path}")

    with open(progress_log_path, "a", encoding="utf-8", buffering=1) as log_file:
        log_file.write("=== Foreground sync S3 -> Google Drive ===\n")
        log_file.write(f"Bucket          : {config.bucket}\n")
        log_file.write(f"Prefix          : {config.prefix or '(root)'}\n")
        log_file.write(f"Drive folder ID : {config.drive_folder_id or '(My Drive root)'}\n")
        log_file.write(f"Batch size      : {config.batch_size}\n\n")
        log_file.flush()

        tee = _Tee(sys.stdout, log_file)
        with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
            try:
                return _run_foreground(config)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                raise SystemExit(1)


def _run_sync_from_args(args: argparse.Namespace) -> None:
    config = _prompt_config(args)

    if args.foreground:
        if args.progress_log:
            _run_foreground_with_progress_log(config)
        else:
            _run_foreground(config)
        return

    pid, progress_log_path, pid_path = _start_background(config)
    print("\nĐã chuyển tiến trình sync sang chạy nền.")
    print(f"PID: {pid}")
    print(f"File log tiến trình: {progress_log_path}")
    print(f"File PID: {pid_path}")
    print(f"Theo dõi log: tail -f {progress_log_path}")


def _run_module_main(module_name: str, main_func: Callable[[], None]) -> None:
    old_argv = sys.argv[:]
    sys.argv = [f"python -m {module_name}"]
    try:
        main_func()
    finally:
        sys.argv = old_argv


def _print_menu() -> None:
    print("\n=== Menu công cụ ===")
    print("1. Sync AWS S3 -> Google Drive (chạy nền)")
    print("2. Sync AWS S3 -> Google Drive (chạy trực tiếp + progress log)")
    print("3. Đếm object trong S3")
    print("4. Đếm file trong Google Drive folder")
    print("5. Tải file từ Google Drive")
    print("6. Xem video trong Google Drive folder")
    print("7. Tạo phụ đề SRT từ video/audio")
    print("8. Chia video thành 2 phần")
    print("0. Thoát")


def _wait_for_enter() -> None:
    input("\nNhấn Enter để quay lại menu...")


def _run_menu(base_args: argparse.Namespace) -> None:
    while True:
        _print_menu()
        choice = input("Chọn chức năng: ").strip()

        if choice == "0":
            print("Đã thoát.")
            return

        try:
            if choice == "1":
                sync_args = argparse.Namespace(**vars(base_args))
                sync_args.foreground = False
                sync_args.progress_log = False
                _run_sync_from_args(sync_args)
            elif choice == "2":
                sync_args = argparse.Namespace(**vars(base_args))
                sync_args.foreground = True
                sync_args.progress_log = True
                _run_sync_from_args(sync_args)
            elif choice == "3":
                from .count_s3 import main as count_s3_main

                _run_module_main("app.count_s3", count_s3_main)
            elif choice == "4":
                from .count_drive import main as count_drive_main

                _run_module_main("app.count_drive", count_drive_main)
            elif choice == "5":
                from .drive_download import main as drive_download_main

                _run_module_main("app.drive_download", drive_download_main)
            elif choice == "6":
                from .view_video_drive import main as view_video_drive_main

                _run_module_main("app.view_video_drive", view_video_drive_main)
            elif choice == "7":
                from .whisper_sub import main as whisper_sub_main

                _run_module_main("app.whisper_sub", whisper_sub_main)
            elif choice == "8":
                from .video_split import main as video_split_main

                _run_module_main("app.video_split", video_split_main)
            else:
                print("Lựa chọn không hợp lệ. Vui lòng nhập số trong menu.")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                print(f"Chức năng kết thúc với mã lỗi: {exc.code}")
        except Exception:  # noqa: BLE001
            traceback.print_exc()

        if choice != "1":
            _wait_for_enter()


def main() -> None:
    # Load biến môi trường từ file .env nếu có
    load_dotenv()

    args = _parse_args()
    if _sync_args_were_provided(sys.argv[1:]):
        _run_sync_from_args(args)
        return

    _run_menu(args)


if __name__ == "__main__":
    main()
