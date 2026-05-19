import os
from typing import Optional

from dotenv import load_dotenv

from .drive_client import get_drive_service


def count_files_in_folder(
    drive_folder_id: Optional[str] = None,
) -> int:
    """
    Đếm số lượng file (không tính file trong thùng rác) trong 1 folder Google Drive.

    - Nếu truyền `drive_folder_id` -> đếm trong folder đó.
    - Nếu để None hoặc chuỗi rỗng -> đếm trong My Drive root.
    """
    service = get_drive_service()

    if drive_folder_id:
        q = f"'{drive_folder_id}' in parents and trashed = false"
    else:
        q = "'root' in parents and trashed = false"

    total = 0
    page_token = None

    while True:
        response = (
            service.files()
            .list(
                q=q,
                spaces="drive",
                fields="nextPageToken, files(id)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )

        total += len(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return total


def main() -> None:
    load_dotenv()

    folder_id = input(
        "Nhập Google Drive folder ID (để trống nếu muốn đếm My Drive root): "
    ).strip()
    if not folder_id:
        folder_id = None

    total = count_files_in_folder(folder_id)

    if folder_id:
        print(f"Folder ID {folder_id} có {total} file (không tính file trong thùng rác).")
    else:
        print(f"My Drive root có {total} file (không tính file trong thùng rác).")


if __name__ == "__main__":
    main()

