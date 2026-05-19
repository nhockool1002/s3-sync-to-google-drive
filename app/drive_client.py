import os
from typing import Dict, Any, Set

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _get_paths() -> Dict[str, str]:
    """
    Lấy đường dẫn credentials/token cho OAuth user.
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    creds_path = os.getenv(
        "GOOGLE_OAUTH_CLIENT_SECRETS",
        os.path.join(base_dir, "credentials.json"),
    )
    token_path = os.getenv(
        "GOOGLE_OAUTH_TOKEN_FILE",
        os.path.join(base_dir, "token.json"),
    )
    return {"creds": creds_path, "token": token_path}


def _get_user_credentials() -> Credentials:
    """
    Lấy Credentials dùng OAuth user (Drive của chính bạn).

    Lần đầu nếu chưa có token, sẽ mở trình duyệt để bạn login
    rồi lưu token vào token.json.
    """
    paths = _get_paths()
    creds: Credentials | None = None

    token_path = paths["token"]

    # Nếu đã có token.json thì load
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # Nếu chưa có hoặc token hết hạn
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Refresh token
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                # Khi token bị thu hồi/expired theo kiểu invalid_grant,
                # cần xoá token cũ và chạy lại OAuth flow để người dùng login.
                if "invalid_grant" in str(exc):
                    try:
                        os.remove(token_path)
                    except OSError:
                        pass
                    creds = None
                else:
                    raise

        # Nếu refresh thất bại (hoặc không có refresh_token) thì chạy lại OAuth flow
        if not creds or not creds.valid:
            if not os.path.exists(paths["creds"]):
                raise RuntimeError(
                    f"Không tìm thấy file OAuth client secrets (credentials.json): {paths['creds']}\n"
                    "Hãy tải file credentials.json từ Google Cloud Console và đặt tại đó,\n"
                    "hoặc set biến GOOGLE_OAUTH_CLIENT_SECRETS trỏ tới đường dẫn phù hợp."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                paths["creds"], SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Lưu token cho lần sau
        with open(token_path, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return creds


def get_drive_service():
    """
    Tạo Google Drive service bằng OAuth user (Drive của bạn).
    """
    creds = _get_user_credentials()
    return build("drive", "v3", credentials=creds)


def list_existing_names(
    service,
    drive_folder_id: str,
) -> Set[str]:
    """
    Lấy toàn bộ tên file hiện có trong 1 folder Drive (hoặc My Drive root).
    Dùng để tăng tốc skip file, tránh phải gọi API cho từng file.
    """
    names: Set[str] = set()

    if drive_folder_id:
        q = f"'{drive_folder_id}' in parents and trashed = false"
    else:
        q = "'root' in parents and trashed = false"

    page_token = None
    while True:
        res = (
            service.files()
            .list(
                q=q,
                spaces="drive",
                fields="nextPageToken, files(name)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        for f in res.get("files", []):
            name = f.get("name")
            if name:
                names.add(name)
        page_token = res.get("nextPageToken")
        if not page_token:
            break

    return names


def file_exists(
    service,
    drive_folder_id: str,
    file_name: str,
) -> bool:
    """
    Kiểm tra xem trong folder Drive đã có file trùng tên hay chưa.
    """
    # Escape dấu nháy đơn trong tên file cho câu query
    safe_name = file_name.replace("'", r"\'")

    if drive_folder_id:
        q = (
            f"name = '{safe_name}' and "
            f"'{drive_folder_id}' in parents and "
            "trashed = false"
        )
    else:
        # Không có folder ID: tìm trong My Drive root
        q = (
            f"name = '{safe_name}' and "
            "'root' in parents and "
            "trashed = false"
        )

    res = (
        service.files()
        .list(q=q, fields="files(id)", pageSize=1)
        .execute()
    )
    return len(res.get("files", [])) > 0


def upload_file(
    service,
    local_path: str,
    drive_folder_id: str,
    file_name: str,
) -> Dict[str, Any]:
    """
    Upload 1 file local lên Google Drive trong folder chỉ định.
    """
    file_metadata = {
        "name": file_name,
    }
    if drive_folder_id:
        file_metadata["parents"] = [drive_folder_id]

    media = MediaFileUpload(local_path, resumable=True)
    return (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id, name, parents")
        .execute()
    )

