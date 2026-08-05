"""
Quét và (tuỳ chọn) xoá ảnh/video nằm trực tiếp trong My Drive root.

- Quét: OAuth drive.readonly (token_download.json).
- Xoá: OAuth drive (token_drive_manage.json) — cần quyền đầy đủ vì file root
  thường không do app sync tạo (scope drive.file không đủ).
- Mặc định chuyển vào Thùng rác (có thể khôi phục). Tuỳ chọn xoá vĩnh viễn.
- Thư mục con không bị ảnh hưởng.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import Any, Callable, List, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .drive_download import _base_dir, get_credentials

READONLY_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
MANAGE_SCOPES = ["https://www.googleapis.com/auth/drive"]

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

LIST_FIELDS = "nextPageToken, files(id, name, mimeType, size, modifiedTime)"

CONFIRM_TRASH = "XOÁ"
CONFIRM_PERMANENT = "XOÁ VĨNH VIỄN"


@dataclass(frozen=True)
class MediaEntry:
    id: str
    name: str
    mime_type: str
    kind: str
    size: Optional[int]
    modified_time: Optional[str]


@dataclass
class DeleteResult:
    ok: int = 0
    fail: int = 0
    errors: List[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def _manage_paths() -> dict[str, str]:
    base = _base_dir()
    creds = os.getenv(
        "GOOGLE_OAUTH_CLIENT_SECRETS",
        os.path.join(base, "credentials.json"),
    )
    token = os.getenv(
        "GOOGLE_OAUTH_TOKEN_MANAGE",
        os.path.join(base, "token_drive_manage.json"),
    )
    return {"creds": creds, "token": token}


def get_manage_credentials() -> Credentials:
    """OAuth drive — dùng cho thao tác xoá file trên Drive."""
    paths = _manage_paths()
    token_path = paths["token"]
    creds: Credentials | None = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, MANAGE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                if "invalid_grant" in str(exc):
                    try:
                        os.remove(token_path)
                    except OSError:
                        pass
                    creds = None
                else:
                    raise

        if not creds or not creds.valid:
            if not os.path.exists(paths["creds"]):
                raise RuntimeError(
                    f"Không tìm thấy credentials.json: {paths['creds']}\n"
                    "Tải OAuth client (Desktop) từ Google Cloud Console và đặt file đó,\n"
                    "hoặc set GOOGLE_OAUTH_CLIENT_SECRETS."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                paths["creds"], MANAGE_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def _human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _media_kind(mime: str) -> Optional[str]:
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    return None


def _list_page(
    service: Any,
    page_token: Optional[str],
) -> dict[str, Any]:
    q = (
        "'root' in parents and trashed = false "
        "and (mimeType contains 'image/' or mimeType contains 'video/')"
    )
    return (
        service.files()
        .list(
            q=q,
            spaces="drive",
            fields=LIST_FIELDS,
            pageSize=1000,
            pageToken=page_token,
            orderBy="name",
        )
        .execute()
    )


def collect_root_media(service: Any) -> List[MediaEntry]:
    """Liệt kê ảnh/video trực tiếp trong My Drive root."""
    out: List[MediaEntry] = []
    page_token: Optional[str] = None

    while True:
        res = _list_page(service, page_token)
        for f in res.get("files", []):
            mime = f.get("mimeType") or ""
            if mime in (FOLDER_MIME, SHORTCUT_MIME):
                continue
            kind = _media_kind(mime)
            if not kind:
                continue

            size_raw = f.get("size")
            size = int(size_raw) if size_raw is not None else None
            out.append(
                MediaEntry(
                    id=f.get("id") or "",
                    name=f.get("name") or "",
                    mime_type=mime,
                    kind=kind,
                    size=size,
                    modified_time=f.get("modifiedTime"),
                )
            )

        page_token = res.get("nextPageToken")
        if not page_token:
            break

    return out


def _summary(entries: List[MediaEntry]) -> tuple[int, int, int, int]:
    images = sum(1 for e in entries if e.kind == "image")
    videos = len(entries) - images
    total_bytes = sum(e.size for e in entries if e.size is not None)
    return len(entries), images, videos, total_bytes


def _print_report(entries: List[MediaEntry]) -> None:
    count, images, videos, total_bytes = _summary(entries)
    unknown_size = [e for e in entries if e.size is None]

    print("\n=== My Drive root — ảnh & video ===")
    print(f"Tổng số file     : {count}")
    print(f"  · Ảnh          : {images}")
    print(f"  · Video         : {videos}")
    print(f"Dung lượng tổng  : {_human_size(total_bytes)} ({total_bytes:,} bytes)")
    if unknown_size:
        print(
            f"Lưu ý            : {len(unknown_size)} file không có trường size "
            "(không tính vào tổng)."
        )

    if not entries:
        print("\nKhông tìm thấy ảnh/video nào ở thư mục gốc My Drive.")
        return

    print("\n--- Chi tiết từng file ---")
    name_w = max(len(e.name) for e in entries)
    name_w = min(max(name_w, 20), 60)
    print(f"{'STT':>4}  {'Loại':<6}  {'Dung lượng':>12}  {'Tên file'}")
    print("-" * (4 + 2 + 6 + 2 + 12 + 2 + name_w))

    for i, e in enumerate(sorted(entries, key=lambda x: x.name.lower()), start=1):
        size_str = _human_size(e.size) if e.size is not None else "—"
        kind_label = "Ảnh" if e.kind == "image" else "Video"
        name = e.name if len(e.name) <= name_w else e.name[: name_w - 1] + "…"
        print(f"{i:>4}  {kind_label:<6}  {size_str:>12}  {name}")


def _write_json(path: str, entries: List[MediaEntry]) -> None:
    count, images, videos, total_bytes = _summary(entries)
    payload = {
        "scope": "my_drive_root",
        "total_count": count,
        "image_count": images,
        "video_count": videos,
        "total_bytes": total_bytes,
        "files": [asdict(e) for e in entries],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _write_csv(path: str, entries: List[MediaEntry]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "kind", "size_bytes", "mime_type", "modified_time", "id"])
        for e in sorted(entries, key=lambda x: x.name.lower()):
            writer.writerow(
                [
                    e.name,
                    e.kind,
                    e.size if e.size is not None else "",
                    e.mime_type,
                    e.modified_time or "",
                    e.id,
                ]
            )


def delete_root_media_entries(
    service: Any,
    entries: List[MediaEntry],
    *,
    permanent: bool = False,
    on_progress: Optional[Callable[[int, int, MediaEntry], None]] = None,
) -> DeleteResult:
    """Xoá (hoặc chuyển Thùng rác) các file đã quét ở My Drive root."""
    result = DeleteResult()
    total = len(entries)

    for i, entry in enumerate(entries, start=1):
        try:
            if permanent:
                service.files().delete(fileId=entry.id).execute()
            else:
                service.files().update(
                    fileId=entry.id,
                    body={"trashed": True},
                    supportsAllDrives=True,
                ).execute()
            result.ok += 1
        except HttpError as exc:
            result.fail += 1
            result.errors.append(f"{entry.name} ({entry.id}): {exc}")
        except Exception as exc:  # noqa: BLE001
            result.fail += 1
            result.errors.append(f"{entry.name} ({entry.id}): {exc}")

        if on_progress:
            on_progress(i, total, entry)

    return result


def _print_delete_result(result: DeleteResult, *, permanent: bool) -> None:
    action = "xoá vĩnh viễn" if permanent else "chuyển vào Thùng rác"
    print(f"\n=== Kết quả {action} ===")
    print(f"Thành công : {result.ok}")
    print(f"Thất bại   : {result.fail}")
    if not permanent and result.ok:
        print("Có thể khôi phục từ Thùng rác trên drive.google.com.")
    if result.errors:
        print("\nLỗi (tối đa 20 dòng đầu):")
        for line in result.errors[:20]:
            print(f"  - {line}")
        if len(result.errors) > 20:
            print(f"  ... và {len(result.errors) - 20} lỗi khác.")


def _confirm_delete(entries: List[MediaEntry], *, permanent: bool) -> bool:
    count, images, videos, total_bytes = _summary(entries)
    if count == 0:
        print("Không có file nào để xoá.")
        return False

    print("\n*** CẢNH BÁO ***")
    if permanent:
        print("Bạn sắp XOÁ VĨNH VIỄN — không thể khôi phục từ Thùng rác.")
    else:
        print("Các file sẽ được chuyển vào Thùng rác (có thể khôi phục).")

    print(f"Số file        : {count} ({images} ảnh, {videos} video)")
    print(f"Dung lượng     : ~{_human_size(total_bytes)}")
    print("Phạm vi        : CHỈ My Drive root — thư mục con KHÔNG bị ảnh hưởng.")

    expected = CONFIRM_PERMANENT if permanent else CONFIRM_TRASH
    typed = input(f'\nNhập chính xác "{expected}" để tiếp tục: ').strip()
    if typed != expected:
        print("Đã huỷ — không xoá file nào.")
        return False
    return True


def _prompt_action() -> str:
    print("\n--- Tool 16: My Drive root — ảnh & video ---")
    print("1. Chỉ quét (hiển thị số lượng & dung lượng)")
    print("2. Quét rồi xoá (chuyển vào Thùng rác)")
    print("3. Quét rồi xoá vĩnh viễn")
    return input("Chọn (1/2/3, Enter = 1): ").strip() or "1"


def _default_progress(i: int, total: int, entry: MediaEntry) -> None:
    if i == 1 or i == total or i % 100 == 0:
        print(f"  [{i}/{total}] {entry.name[:70]}", file=sys.stderr)


def _run_delete(
    entries: List[MediaEntry],
    *,
    permanent: bool,
    skip_confirm: bool,
) -> None:
    if not skip_confirm and not _confirm_delete(entries, permanent=permanent):
        return

    print("\nĐang xác thực quyền xoá (drive scope)...", file=sys.stderr)
    manage_creds = get_manage_credentials()
    manage_service = build("drive", "v3", credentials=manage_creds)

    action = "xoá vĩnh viễn" if permanent else "chuyển Thùng rác"
    print(f"\nĐang {action} {len(entries)} file...", file=sys.stderr)
    result = delete_root_media_entries(
        manage_service,
        entries,
        permanent=permanent,
        on_progress=_default_progress,
    )
    _print_delete_result(result, permanent=permanent)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Quét và (tuỳ chọn) xoá ảnh/video ở My Drive root."
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="Ghi kết quả quét ra file JSON.",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        help="Ghi danh sách file ra CSV.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Sau khi quét, chuyển file vào Thùng rác.",
    )
    parser.add_argument(
        "--permanent",
        action="store_true",
        help="Xoá vĩnh viễn thay vì Thùng rác (dùng cùng --delete).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Bỏ qua xác nhận khi xoá (nguy hiểm).",
    )
    args = parser.parse_args()

    interactive = len(sys.argv) <= 1
    action = "1"
    if interactive:
        action = _prompt_action()

    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    print("Đang quét My Drive root...", file=sys.stderr)
    try:
        entries = collect_root_media(service)
    except HttpError as exc:
        print(f"Lỗi Google Drive API: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    _print_report(entries)

    if args.json:
        _write_json(args.json, entries)
        print(f"\nĐã ghi JSON: {args.json}")
    if args.csv:
        _write_csv(args.csv, entries)
        print(f"Đã ghi CSV: {args.csv}")

    do_delete = args.delete or action in ("2", "3")
    permanent = args.permanent or action == "3"
    if do_delete:
        _run_delete(entries, permanent=permanent, skip_confirm=args.yes)


if __name__ == "__main__":
    main()
