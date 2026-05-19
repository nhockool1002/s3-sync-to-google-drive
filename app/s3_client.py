import os
from typing import Iterator, Dict, Any

import boto3


def get_s3_client():
    """
    Tạo S3 client từ ENV:
      - AWS_ACCESS_KEY_ID
      - AWS_SECRET_ACCESS_KEY
      - AWS_DEFAULT_REGION
    """
    return boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION"),
    )


def list_objects(bucket: str, prefix: str = "") -> Iterator[Dict[str, Any]]:
    """
    Liệt kê object trong bucket theo prefix (recursive).
    Trả về generator các dict object thông tin từ S3.
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    kwargs: Dict[str, Any] = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix

    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            yield obj


def download_object(bucket: str, key: str, local_path: str) -> None:
    """
    Tải 1 object về local_path, tự tạo thư mục nếu cần.
    """
    s3 = get_s3_client()

    dir_name = os.path.dirname(local_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    s3.download_file(bucket, key, local_path)
