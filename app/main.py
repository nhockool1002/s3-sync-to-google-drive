import os

from dotenv import load_dotenv

from .transfer import transfer_s3_to_drive


def main() -> None:
    # Load biến môi trường từ file .env nếu có
    load_dotenv()

    print("=== Sync AWS S3 -> Google Drive ===")

    bucket = input("Nhập tên S3 bucket: ").strip()
    prefix = input("Nhập folder name trên S3 (có thể để trống): ").strip()
    drive_folder_id = input("Nhập Google Drive folder ID: ").strip()
    batch_size_str = input("Nhập batch size (default 50): ").strip()

    batch_size = int(batch_size_str) if batch_size_str else 50

    log_dir = os.getenv("LOG_DIR", "./logs")

    log_path = transfer_s3_to_drive(
        bucket=bucket,
        prefix=prefix,
        drive_folder_id=drive_folder_id,
        batch_size=batch_size,
        log_dir=log_dir,
    )

    print("\nHoàn tất chuyển dữ liệu.")
    print(f"File log chi tiết: {log_path}")


if __name__ == "__main__":
    main()
