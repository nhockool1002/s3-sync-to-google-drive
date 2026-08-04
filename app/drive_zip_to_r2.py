"""
Tải toàn bộ 1 folder Google Drive (đệ quy), nén thành 1 file .zip,
rồi upload file zip đó lên Cloudflare R2.

OAuth drive.readonly + token_download.json giống app.drive_download / app.drive_to_r2.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv
from googleapiclient.discovery import build

from .drive_download import download_file_by_id, get_credentials, list_files_recursive
from .r2_client import get_r2_client, upload_file
from .utils import ensure_dir, get_log_file_path


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_zip_key(key: str) -> str:
    key = key.strip("/")
    if not key.lower().endswith(".zip"):
        key = f"{key}.zip"
    return key


def zip_drive_folder_to_r2(
    drive_folder_id: str,
    r2_bucket: str,
    r2_key: str,
    temp_dir: str = "/tmp/drive_r2_zip",
    log_dir: str = "./logs",
) -> str:
    """
    Tải toàn bộ folder Drive (đệ quy) về temp, nén thành .zip, upload lên R2.

    Trả về đường dẫn file log.
    """
    r2_key = _normalize_zip_key(r2_key)

    ensure_dir(temp_dir)
    log_label = os.path.splitext(os.path.basename(r2_key))[0] or "drive_zip"
    log_path = get_log_file_path(log_dir, f"zip_{log_label}")
    print(f"{_ts()} | File log chi tiết: {log_path}", flush=True)

    download_dir = os.path.join(temp_dir, "download")
    ensure_dir(download_dir)

    print(f"{_ts()} | Khởi tạo kết nối Google Drive...", flush=True)
    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    print(f"{_ts()} | Đang liệt kê file trong folder Drive (đệ quy)...", flush=True)
    files: List[Dict[str, Any]] = list_files_recursive(service, drive_folder_id)
    total_files = len(files)
    print(f"{_ts()} | Tìm thấy tổng cộng {total_files} file trên Drive.", flush=True)

    downloaded = 0
    failures: List[Tuple[str, str]] = []

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"{_ts()} | === Zip Google Drive folder -> Cloudflare R2 ===\n")
        f.write(f"{_ts()} | Drive folder ID: {drive_folder_id}\n")
        f.write(f"{_ts()} | R2 bucket      : {r2_bucket}\n")
        f.write(f"{_ts()} | R2 key         : {r2_key}\n")
        f.write(f"{_ts()} | Total files    : {total_files}\n\n")

        for idx, item in enumerate(files, start=1):
            rel_path = item["path"]
            local_path = os.path.join(download_dir, rel_path)
            print(
                f"{_ts()} | Đang tải file [{idx}/{total_files}]: {rel_path}",
                flush=True,
            )
            try:
                download_file_by_id(service, item["id"], local_path)
                f.write(f"{_ts()} | [OK]   [{idx}/{total_files}] {rel_path}\n")
                downloaded += 1
            except Exception as e:  # noqa: BLE001
                f.write(f"{_ts()} | [FAIL] [{idx}/{total_files}] {rel_path} | {e}\n")
                failures.append((rel_path, str(e)))
            f.flush()

        zip_path = os.path.join(temp_dir, os.path.basename(r2_key))
        print(f"{_ts()} | Đang nén {downloaded} file thành zip...", flush=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, filenames in os.walk(download_dir):
                for filename in filenames:
                    full_path = os.path.join(root, filename)
                    arcname = os.path.relpath(full_path, download_dir)
                    zf.write(full_path, arcname)

        zip_size = os.path.getsize(zip_path) if os.path.exists(zip_path) else 0
        print(
            f"{_ts()} | Đã tạo file zip: {zip_path} ({zip_size} bytes). "
            f"Đang upload lên R2 ({r2_key})...",
            flush=True,
        )

        r2 = get_r2_client()
        upload_file(r2, r2_bucket, zip_path, r2_key)

        f.write(f"\n{_ts()} | === SUMMARY ===\n")
        f.write(f"{_ts()} | Total files : {total_files}\n")
        f.write(f"{_ts()} | Downloaded  : {downloaded}\n")
        f.write(f"{_ts()} | Failed      : {len(failures)}\n")
        f.write(f"{_ts()} | Zip size    : {zip_size} bytes\n")
        f.write(f"{_ts()} | R2 key      : {r2_key}\n")

        if failures:
            f.write(f"\n{_ts()} | Failed files:\n")
            for path, err in failures:
                f.write(f"{_ts()} | - {path}: {err}\n")

    print(
        f"{_ts()} | Hoàn tất: Total={total_files}, Downloaded={downloaded}, "
        f"Failed={len(failures)}, ZipSize={zip_size} bytes, R2 key={r2_key}",
        flush=True,
    )

    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except OSError:
        pass

    return log_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nén 1 folder Google Drive thành zip rồi upload lên Cloudflare R2."
    )
    parser.add_argument(
        "--drive-folder-id",
        default=os.getenv("GOOGLE_DRIVE_FOLDER_ID"),
        help="ID folder Drive nguồn.",
    )
    parser.add_argument("--bucket", default=os.getenv("R2_BUCKET"), help="Tên bucket R2 đích.")
    parser.add_argument(
        "--key",
        default=os.getenv("R2_ZIP_KEY"),
        help="Đường dẫn/tên file zip đích trên R2, vd: backups/drive_export.zip",
    )
    parser.add_argument("--log-dir", default=os.getenv("LOG_DIR", "./logs"))
    parser.add_argument(
        "--temp-dir", default=os.getenv("R2_ZIP_TEMP_DIR", "/tmp/drive_r2_zip")
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = _parse_args()

    print("=== Zip Google Drive folder -> Cloudflare R2 ===")
    drive_folder_id = args.drive_folder_id or input("Nhập Google Drive folder ID nguồn: ").strip()
    bucket = args.bucket or input("Nhập tên R2 bucket đích: ").strip()
    key = args.key or input(
        "Nhập tên/đường dẫn file zip trên R2 (vd: backups/drive_export.zip): "
    ).strip()

    if not drive_folder_id:
        print("Cần có Google Drive folder ID.", file=sys.stderr)
        sys.exit(1)
    if not bucket:
        print("Cần có tên R2 bucket.", file=sys.stderr)
        sys.exit(1)
    if not key:
        print("Cần có tên/đường dẫn file zip đích trên R2.", file=sys.stderr)
        sys.exit(1)

    log_path = zip_drive_folder_to_r2(
        drive_folder_id=drive_folder_id,
        r2_bucket=bucket,
        r2_key=key,
        temp_dir=args.temp_dir,
        log_dir=args.log_dir,
    )

    print("\nHoàn tất nén và upload.")
    print(f"File log chi tiết: {log_path}")


if __name__ == "__main__":
    main()
