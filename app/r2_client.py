import os
from typing import Any, Dict, Iterator, Set

import boto3
from botocore.config import Config


def get_r2_client():
    """
    Tạo client S3-compatible cho Cloudflare R2 từ ENV:
      - R2_ACCOUNT_ID (dùng để suy ra endpoint nếu không set R2_ENDPOINT_URL)
      - R2_ENDPOINT_URL (tuỳ chọn, ghi đè endpoint mặc định)
      - R2_ACCESS_KEY_ID
      - R2_SECRET_ACCESS_KEY
    """
    account_id = os.getenv("R2_ACCOUNT_ID", "").strip()
    endpoint_url = os.getenv("R2_ENDPOINT_URL", "").strip()
    if not endpoint_url:
        if not account_id:
            raise RuntimeError(
                "Thiếu R2_ACCOUNT_ID hoặc R2_ENDPOINT_URL trong biến môi trường. "
                "Lấy Account ID từ Cloudflare Dashboard -> R2."
            )
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def list_objects(client, bucket: str, prefix: str = "") -> Iterator[Dict[str, Any]]:
    """
    Liệt kê object trong bucket R2 theo prefix (recursive).
    """
    paginator = client.get_paginator("list_objects_v2")

    kwargs: Dict[str, Any] = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix

    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            yield obj


def list_existing_keys(client, bucket: str, prefix: str = "") -> Set[str]:
    """
    Lấy toàn bộ key hiện có trong bucket R2 (theo prefix), dùng để skip nhanh.
    """
    return {obj["Key"] for obj in list_objects(client, bucket, prefix)}


def upload_file(client, bucket: str, local_path: str, key: str) -> None:
    """
    Upload 1 file local lên R2 tại key chỉ định.
    """
    client.upload_file(local_path, bucket, key)
