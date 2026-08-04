"""
Tải file (video) từ Google Drive qua API với OAuth.

Phạm vi drive.readonly: chỉ file mà tài khoản Google bạn đăng nhập có quyền xem/tải
(chủ sở hữu hoặc được chia sẻ). Không vượt qua hạn chế quyền của chủ file.

Lần đầu chạy sẽ mở trình duyệt xin quyền và lưu token_download.json (tách khỏi token.json của sync).
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from typing import Optional, Tuple

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

# Đọc mọi file Drive mà user có quyền (khác drive.file chỉ thấy file do app tạo)
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."


def _base_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _paths() -> dict[str, str]:
    base = _base_dir()
    creds = os.getenv(
        "GOOGLE_OAUTH_CLIENT_SECRETS",
        os.path.join(base, "credentials.json"),
    )
    token = os.getenv(
        "GOOGLE_OAUTH_TOKEN_DOWNLOAD",
        os.path.join(base, "token_download.json"),
    )
    return {"creds": creds, "token": token}


def get_credentials() -> Credentials:
    paths = _paths()
    token_path = paths["token"]
    creds: Credentials | None = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

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
                paths["creds"], SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def parse_file_id_from_url(text: str) -> Optional[str]:
    """
    Trích file ID từ URL Drive hoặc URL Google Docs/Sheets/Slides.
    """
    text = text.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", text):
        return text

    for pattern in (
        r"/file/d/([A-Za-z0-9_-]+)",
        r"/presentation/d/([A-Za-z0-9_-]+)",
        r"/document/d/([A-Za-z0-9_-]+)",
        r"/spreadsheets/d/([A-Za-z0-9_-]+)",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    return None


def _list_children_page(
    service: object, parent_id: str, page_token: Optional[str]
) -> dict:
    return (
        service.files()
        .list(
            q=f"'{parent_id}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, size)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )


def list_files_recursive(
    service: object,
    parent_id: str,
    prefix: str = "",
) -> list[dict]:
    """
    Liệt kê đệ quy mọi file (không phải folder/shortcut) trong 1 folder Drive.

    Bỏ qua file Google Workspace gốc (Docs/Sheets/Slides/...) vì không tải
    trực tiếp bằng get_media được (cần export riêng, xem download_file()).

    Trả về list dict: id, name, path (đường dẫn tương đối kèm thư mục con), mimeType, size.
    """
    out: list[dict] = []
    page_token: Optional[str] = None

    while True:
        res = _list_children_page(service, parent_id, page_token)
        for f in res.get("files", []):
            fid = f.get("id") or ""
            name = f.get("name") or ""
            mime = f.get("mimeType") or ""
            rel = f"{prefix}{name}" if prefix else name

            if mime == FOLDER_MIME:
                out.extend(list_files_recursive(service, fid, f"{rel}/"))
            elif mime == SHORTCUT_MIME:
                continue
            elif mime.startswith(GOOGLE_NATIVE_PREFIX):
                continue
            else:
                out.append(
                    {
                        "id": fid,
                        "name": name,
                        "path": rel,
                        "mimeType": mime,
                        "size": f.get("size"),
                    }
                )

        page_token = res.get("nextPageToken")
        if not page_token:
            break

    return out


def download_file_by_id(service: object, file_id: str, output_path: str) -> None:
    """
    Tải nhanh 1 file nhị phân theo id, không gọi thêm API lấy metadata
    (dùng khi đã có id/mimeType sẵn từ list_files_recursive()).
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(output_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def _export_mime_for_google_file(
    google_mime: str, output_path: str
) -> Tuple[str, str]:
    """
    Chọn MIME export phù hợp với file Google Workspace và đuôi file đích.
    Trả về (export_mime, mô tả ngắn).
    """
    ext = os.path.splitext(output_path)[1].lower()
    gm = google_mime

    if gm == "application/vnd.google-apps.presentation":
        if ext in (".pptx", ".ppt"):
            return (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "PowerPoint (.pptx)",
            )
        if ext == ".pdf":
            return "application/pdf", "PDF"
        if ext == ".odp":
            return "application/vnd.oasis.opendocument.presentation", "ODP"
        if ext == ".txt":
            return "text/plain", "Plain text"
        # Mặc định: PPTX
        return (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "PowerPoint (.pptx)",
        )

    if gm == "application/vnd.google-apps.document":
        if ext in (".docx", ".doc"):
            return (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "Word (.docx)",
            )
        if ext == ".pdf":
            return "application/pdf", "PDF"
        if ext in (".odt",):
            return "application/vnd.oasis.opendocument.text", "ODT"
        if ext in (".html", ".htm"):
            return "text/html", "HTML"
        if ext == ".txt":
            return "text/plain", "Plain text"
        if ext == ".rtf":
            return "application/rtf", "RTF"
        return (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "Word (.docx)",
        )

    if gm == "application/vnd.google-apps.spreadsheet":
        if ext in (".xlsx", ".xls"):
            return (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "Excel (.xlsx)",
            )
        if ext == ".pdf":
            return "application/pdf", "PDF"
        if ext == ".csv":
            return "text/csv", "CSV"
        if ext == ".ods":
            return "application/x-vnd.oasis.opendocument.spreadsheet", "ODS"
        return (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Excel (.xlsx)",
        )

    if gm == "application/vnd.google-apps.drawing":
        if ext == ".pdf":
            return "application/pdf", "PDF"
        if ext == ".svg":
            return "image/svg+xml", "SVG"
        if ext == ".png":
            return "image/png", "PNG"
        if ext in (".jpg", ".jpeg"):
            return "image/jpeg", "JPEG"
        return "application/pdf", "PDF"

    raise RuntimeError(
        f"Loại Google Apps chưa được hỗ trợ export: {gm}. "
        "Thử tải từ trình duyệt hoặc mở issue."
    )


def _drive_http_error_user_message(exc: HttpError) -> Optional[str]:
    """
    Chuyển HttpError Drive thành thông báo dễ hiểu (hoặc None nếu không xử lý đặc biệt).
    """
    try:
        status = int(exc.resp.status) if exc.resp else 0
    except Exception:
        status = 0
    try:
        raw = exc.content.decode("utf-8", errors="replace")
    except Exception:
        raw = str(exc)

    if status == 403 and "cannotExportFile" in raw:
        return (
            "Google từ chối export file này (403, cannotExportFile). "
            "Đây là giới hạn phía Google/Workspace, không phải lỗi tool.\n\n"
            "Thường gặp khi:\n"
            "  • Chủ file bật “Ngăn người xem tải xuống, in và sao chép” (link chỉ xem).\n"
            "  • Quản trị domain/Apps chặn export qua API.\n\n"
            "Việc nên thử: mở Slides/Docs trên trình duyệt → File → Tải xuống (PDF/PPTX); "
            "hoặc nhờ chủ file tắt chặn tải, cấp quyền chỉnh sửa, hoặc gửi bản sao có thể export."
        )

    if status == 403 and "insufficientFilePermissions" in raw:
        return (
            "Không đủ quyền với file này (403). Kiểm tra tài khoản đang đăng nhập OAuth "
            "có được chia sẻ đúng email và quyền tải."
        )

    if status == 404:
        return "Không tìm thấy file (404). Kiểm tra FILE_ID và quyền truy cập."

    return None


def _download_media_request(request: object, output_path: str) -> None:
    """Ghi nội dung từ MediaIoBaseDownload xuống output_path."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    try:
        with io.FileIO(output_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    print(f"\rTải: {pct}%", end="", file=sys.stderr)
            print(file=sys.stderr)
    except HttpError as e:
        try:
            if os.path.isfile(output_path):
                os.remove(output_path)
        except OSError:
            pass
        msg = _drive_http_error_user_message(e)
        if msg:
            raise RuntimeError(msg) from e
        raise


def download_file(
    file_id: str,
    output_path: str,
    *,
    acknowledge_abuse: bool = False,
) -> None:
    """
    Tải file nhị phân (video, …) hoặc export Google Slides/Docs/Sheets theo đuôi file đích.
    """
    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    meta = (
        service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,size",
            supportsAllDrives=True,
        )
        .execute()
    )

    name = meta.get("name", file_id)
    mime = meta.get("mimeType", "")
    print(f"File: {name} ({mime})")

    if mime.startswith("application/vnd.google-apps."):
        export_mime, kind = _export_mime_for_google_file(mime, output_path)
        print(f"Export Google Workspace → {kind} ({export_mime})")
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        _download_media_request(request, output_path)
        print(f"Đã lưu: {output_path}")
        return

    request = service.files().get_media(
        fileId=file_id,
        supportsAllDrives=True,
        acknowledgeAbuse=acknowledge_abuse,
    )
    _download_media_request(request, output_path)
    print(f"Đã lưu: {output_path}")


def _prompt_file_id() -> str:
    raw = input("Nhập FILE_ID hoặc URL Google Drive: ").strip()
    fid = parse_file_id_from_url(raw)
    if not fid:
        print("Không nhận diện được FILE_ID hoặc URL hợp lệ.", file=sys.stderr)
        sys.exit(1)
    return fid


def _prompt_output_path() -> str:
    raw = input("Đường dẫn file lưu (vd: ./video.mp4): ").strip()
    if not raw:
        print("Cần có đường dẫn file đích.", file=sys.stderr)
        sys.exit(1)
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tải file từ Google Drive (OAuth, drive.readonly)."
    )
    g = parser.add_mutually_exclusive_group(required=False)
    g.add_argument("--file-id", help="ID file trên Drive")
    g.add_argument("--url", help="Link chia sẻ Drive (có /file/d/ID)")
    parser.add_argument(
        "-o",
        "--output",
        help="Đường dẫn file local để lưu (vd: ./video.mp4); bỏ trống để nhập khi chạy",
    )
    parser.add_argument(
        "--acknowledge-abuse",
        action="store_true",
        help="Bắt buộc nếu Drive cảnh báo file có thể bị đánh dấu abuse",
    )
    args = parser.parse_args()

    if args.file_id:
        fid = args.file_id.strip()
    elif args.url:
        fid = parse_file_id_from_url(args.url)
        if not fid:
            print("Không trích được file ID từ --url.", file=sys.stderr)
            sys.exit(1)
    else:
        fid = _prompt_file_id()

    out = args.output.strip() if args.output else _prompt_output_path()

    download_file(fid, out, acknowledge_abuse=args.acknowledge_abuse)


if __name__ == "__main__":
    main()
