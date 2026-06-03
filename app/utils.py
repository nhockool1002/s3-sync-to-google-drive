import os
from datetime import datetime


def get_log_file_path(log_dir: str, s3_prefix: str) -> str:
    """
    Tạo đường dẫn file log dạng:
      {log_dir}/{YYYYMMDD_HHMMSS}_{prefix}.log
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prefix = s3_prefix.strip("/").replace("/", "_") or "root"

    os.makedirs(log_dir, exist_ok=True)

    return os.path.join(log_dir, f"{ts}_{safe_prefix}.log")


def get_progress_log_file_path(log_dir: str, s3_prefix: str) -> str:
    """
    Tạo đường dẫn file log tiến trình dạng:
      {log_dir}/{YYYYMMDD_HHMMSS}_{prefix}.progress.log
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prefix = s3_prefix.strip("/").replace("/", "_") or "root"

    os.makedirs(log_dir, exist_ok=True)

    return os.path.join(log_dir, f"{ts}_{safe_prefix}.progress.log")


def ensure_dir(path: str) -> None:
    """
    Tạo thư mục nếu chưa tồn tại.
    """
    os.makedirs(path, exist_ok=True)
