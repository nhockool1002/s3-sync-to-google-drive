import os
from typing import Optional

from dotenv import load_dotenv

from .s3_client import list_objects


def count_s3_objects(
    bucket: str,
    prefix: Optional[str] = "",
    skip_hidden: bool = True,
) -> int:
    """
    Đếm số lượng object trong 1 S3 bucket (theo prefix).

    - bucket: tên S3 bucket.
    - prefix: "folder" trên S3 (có thể rỗng để đếm cả bucket).
    - skip_hidden:
        - True: bỏ qua key có tên file bắt đầu bằng "." hoặc ".DS_Store".
        - False: đếm tất cả.
    """
    total = 0
    for obj in list_objects(bucket, prefix or ""):
        key = obj["Key"]
        if skip_hidden:
            name = os.path.basename(key)
            if name.startswith(".") or name.lower().endswith(".ds_store"):
                continue
        total += 1
    return total


def main() -> None:
    load_dotenv()

    bucket = input("Nhập tên S3 bucket: ").strip()
    prefix = input(
        "Nhập folder name trên S3 (có thể để trống để đếm cả bucket): "
    ).strip()

    skip_hidden_input = input(
        "Bỏ qua file ẩn (.DS_Store, bắt đầu bằng .)? [Y/n] (default: Y): "
    ).strip().lower()
    skip_hidden = skip_hidden_input in ("", "y", "yes")

    total = count_s3_objects(bucket, prefix, skip_hidden=skip_hidden)

    if prefix:
        print(
            f"S3 bucket '{bucket}' với prefix '{prefix}' có {total} object "
            f"({'bỏ qua file ẩn' if skip_hidden else 'bao gồm cả file ẩn'})."
        )
    else:
        print(
            f"S3 bucket '{bucket}' (toàn bộ) có {total} object "
            f"({'bỏ qua file ẩn' if skip_hidden else 'bao gồm cả file ẩn'})."
        )


if __name__ == "__main__":
    main()

