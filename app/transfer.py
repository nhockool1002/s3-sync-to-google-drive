import os
import shutil
from datetime import datetime
from typing import List, Tuple, Dict, Any

from .s3_client import list_objects, download_object
from .drive_client import get_drive_service, upload_file, list_existing_names
from .utils import get_log_file_path, ensure_dir


def _ts() -> str:
    """Timestamp chuẩn cho log/console."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _is_within_dir(path: str, base_dir: str) -> bool:
    """
    Kiểm tra path có nằm trong base_dir hay không (theo realpath).
    Dùng để đảm bảo chỉ xoá file trong thư mục tạm.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(path)
    try:
        common = os.path.commonpath([base, target])
    except ValueError:
        return False
    return common == base


def _safe_remove_file(path: str, temp_dir: str) -> None:
    """
    Xoá file nếu và chỉ nếu nằm trong temp_dir.
    """
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
    """
    Dọn thư mục rỗng từ start_dir ngược lên cho tới stop_dir (không vượt quá).
    """
    cur = os.path.realpath(start_dir)
    stop = os.path.realpath(stop_dir)
    while cur and _is_within_dir(cur, stop) and cur != stop:
        try:
            os.rmdir(cur)  # chỉ xoá nếu rỗng
        except OSError:
            break
        cur = os.path.dirname(cur)


def transfer_s3_to_drive(
    bucket: str,
    prefix: str,
    drive_folder_id: str,
    batch_size: int = 50,
    temp_dir: str = "/tmp/s3_sync",
    log_dir: str = "./logs",
) -> str:
    """
    Chuyển dữ liệu từ S3 sang Google Drive theo batch, ghi log chi tiết.

    Trả về đường dẫn file log.
    """
    ensure_dir(temp_dir)
    log_path = get_log_file_path(log_dir, prefix)
    print(f"{_ts()} | File log chi tiết: {log_path}", flush=True)

    total_processed = 0
    success = 0
    failed = 0
    skipped = 0
    failures: List[Tuple[str, str]] = []

    print(f"{_ts()} | Khởi tạo kết nối Google Drive...", flush=True)
    service = get_drive_service()

    # Liệt kê trước để biết tổng số object (bỏ qua file ẩn/.DS_Store)
    print(f"{_ts()} | Đang liệt kê objects trong S3...", flush=True)
    raw_objects: List[Dict[str, Any]] = list(list_objects(bucket, prefix))

    # Lọc bỏ các object tương ứng file ẩn (.DS_Store, tên bắt đầu bằng ".")
    objects: List[Dict[str, Any]] = []
    for obj in raw_objects:
        key = obj["Key"]
        name = os.path.basename(key)
        if name.startswith(".") or name.lower().endswith(".ds_store"):
            # Không đếm và không xử lý các file ẩn/hệ thống
            continue
        objects.append(obj)

    total_objects = len(objects)
    print(
        f"{_ts()} | Tìm thấy tổng cộng {total_objects} object trong S3 (prefix='{prefix or ''}').",
        flush=True,
    )
    # Lấy danh sách tên file hiện có trong folder Drive để skip nhanh
    print(f"{_ts()} | Đang lấy danh sách file hiện có trên Drive...", flush=True)
    existing_names = list_existing_names(service, drive_folder_id)
    print(
        f"{_ts()} | Đã tải danh sách {len(existing_names)} file hiện có trên Drive (để SKIP nhanh).",
        flush=True,
    )

    print(f"{_ts()} | Kết nối Google Drive OK. Bắt đầu xử lý S3...", flush=True)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"{_ts()} | === Transfer S3 -> Google Drive ===\n")
        f.write(f"{_ts()} | Bucket     : {bucket}\n")
        f.write(f"{_ts()} | Prefix     : {prefix or '(root)'}\n")
        f.write(f"{_ts()} | Drive folder ID: {drive_folder_id}\n")
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
                    service=service,
                    drive_folder_id=drive_folder_id,
                    temp_dir=temp_dir,
                    log_file=f,
                    existing_names=existing_names,
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
                service=service,
                drive_folder_id=drive_folder_id,
                temp_dir=temp_dir,
                log_file=f,
                existing_names=existing_names,
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


def _process_batch(
    batch: List[Dict[str, Any]],
    bucket: str,
    prefix: str,
    service,
    drive_folder_id: str,
    temp_dir: str,
    log_file,
    existing_names,
):
    """
    Xử lý 1 batch object:
      - tải từng object về local
      - upload lên Google Drive (nếu chưa tồn tại)
      - ghi log kết quả

    Trả về:
      - success: số object upload thành công
      - failures: danh sách (key, error)
      - skipped: số object đã bỏ qua vì đã tồn tại
    """
    success = 0
    skipped = 0
    failures: List[Tuple[str, str]] = []

    batch_len = len(batch)

    for idx, obj in enumerate(batch, start=1):
        key = obj["Key"]

        # Tính đường dẫn tương đối so với prefix để giữ được cấu trúc.
        # Cẩn thận với dấu "/" để không tạo thành đường dẫn tuyệt đối.
        if prefix and key.startswith(prefix):
            rel_path = key[len(prefix) :]
        else:
            rel_path = key

        # Loại bỏ "/" ở đầu nếu có (tránh đường dẫn tuyệt đối kiểu "/xxx")
        rel_path = rel_path.lstrip("/")

        # Tên file để upload (chỉ lấy tên cuối, không tạo thư mục con trong Drive)
        file_name = os.path.basename(rel_path) or rel_path.replace("/", "_")

        # Bỏ qua các file ẩn/hệ thống như .DS_Store
        if file_name.startswith(".") or file_name.lower().endswith(".ds_store"):
            log_file.write(
                f"{_ts()} | [SKIP] [{idx}/{batch_len}] {key} (file hệ thống/ẩn, không upload)\n"
            )
            skipped += 1
            continue

        # Log chi tiết từng file đang xử lý
        print(
            f"{_ts()} | Đang xử lý object [{idx}/{batch_len}] trong batch: {key}",
            flush=True,
        )

        # Kiểm tra nếu file đã tồn tại trên Drive thì bỏ qua
        if file_name in existing_names:
            log_file.write(
                f"{_ts()} | [SKIP] [{idx}/{batch_len}] {key} (đã tồn tại trên Drive)\n"
            )
            skipped += 1
            continue

        local_path = os.path.join(temp_dir, rel_path)

        try:
            download_object(bucket, key, local_path)
            upload_file(service, local_path, drive_folder_id, file_name)
            # Sau khi upload thành công, bổ sung vào tập existing_names để batch sau skip được
            existing_names.add(file_name)
            log_file.write(f"{_ts()} | [OK]   [{idx}/{batch_len}] {key}\n")
            success += 1
            # Xoá file tạm ngay sau khi xử lý xong
            _safe_remove_file(local_path, temp_dir)
            _safe_cleanup_empty_dirs(os.path.dirname(local_path), temp_dir)
        except Exception as e:  # noqa: BLE001
            log_file.write(f"{_ts()} | [FAIL] [{idx}/{batch_len}] {key} | {e}\n")
            failures.append((key, str(e)))
            # Nếu đã download được file tạm rồi thì xoá để không đầy ổ
            _safe_remove_file(local_path, temp_dir)
            _safe_cleanup_empty_dirs(os.path.dirname(local_path), temp_dir)

    log_file.flush()
    return success, failures, skipped
