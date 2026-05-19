"""
Duyệt và xem video trong một folder Google Drive (kèm thư mục con) qua trình duyệt local.

OAuth drive.readonly + token_download.json giống app.drive_download.
Stream byte-range từ Drive API qua localhost để thẻ <video> có thể seek.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import webbrowser
from typing import Any, Dict, List, Optional

from flask import Flask, Response, abort, request, stream_with_context
from markupsafe import escape
from google.auth.transport.requests import AuthorizedSession, Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .drive_download import get_credentials

DRIVE_MEDIA_URL = (
    "https://www.googleapis.com/drive/v3/files/{file_id}"
    "?alt=media&supportsAllDrives=true"
)

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# Chỉ cho phép ID an toàn trong URL
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def parse_folder_id_from_url(text: str) -> Optional[str]:
    """Trích folder ID từ URL Drive hoặc trả về ID thuần nếu hợp lệ."""
    text = text.strip()
    if _FILE_ID_RE.match(text):
        return text
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    return None


def _list_page(
    service: Any,
    parent_id: str,
    page_token: Optional[str],
) -> Dict[str, Any]:
    return (
        service.files()
        .list(
            q=f"'{parent_id}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )


def collect_videos_recursive(
    service: Any,
    parent_id: str,
    prefix: str = "",
) -> List[Dict[str, str]]:
    """
    Liệt kê đệ quy mọi file có mimeType bắt đầu bằng video/.
    Trả về danh sách dict: id, name, path, mimeType.
    """
    out: List[Dict[str, str]] = []
    page_token: Optional[str] = None

    while True:
        res = _list_page(service, parent_id, page_token)
        for f in res.get("files", []):
            mid = f.get("id") or ""
            name = f.get("name") or ""
            mime = f.get("mimeType") or ""
            rel = f"{prefix}{name}" if prefix else name

            if mime == FOLDER_MIME:
                sub_prefix = f"{rel}/"
                out.extend(collect_videos_recursive(service, mid, sub_prefix))
            elif mime == SHORTCUT_MIME:
                # Bỏ qua shortcut ở bản đầu (có thể mở rộng resolve target sau)
                continue
            elif mime.startswith("video/"):
                out.append(
                    {
                        "id": mid,
                        "name": name,
                        "path": rel,
                        "mimeType": mime,
                    }
                )

        page_token = res.get("nextPageToken")
        if not page_token:
            break

    return out


def _allowed_ids(videos: List[Dict[str, str]]) -> set[str]:
    return {v["id"] for v in videos}


def _forward_headers_from_drive(resp: Any) -> List[tuple[str, str]]:
    """Chọn header từ phản hồi Drive để chuyển cho trình duyệt."""
    names = (
        "Content-Type",
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
        "Cache-Control",
    )
    hlist: List[tuple[str, str]] = []
    for n in names:
        v = resp.headers.get(n)
        if v:
            hlist.append((n, v))
    return hlist


def create_app(
    videos: List[Dict[str, str]],
    creds: Any,
) -> Flask:
    app = Flask(__name__)
    allowed = _allowed_ids(videos)
    # Tránh đổi thứ tự; sort theo path để dễ tìm
    videos_sorted = sorted(videos, key=lambda x: x["path"].lower())

    @app.get("/")
    def index():
        rows = []
        for v in videos_sorted:
            pid = escape(v["id"])
            path = escape(v["path"])
            rows.append(
                f"<tr><td>{path}</td>"
                f'<td><a href="/play/{pid}">Xem</a></td></tr>'
            )
        body = "\n".join(rows) if rows else "<tr><td colspan=2>(Không có video)</td></tr>"
        html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Video trong folder Drive</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 1rem; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 56rem; }}
    th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.5rem; text-align: left; }}
    th {{ background: #f5f5f5; }}
    a {{ color: #1a73e8; }}
  </style>
</head>
<body>
  <h1>Chọn video</h1>
  <p>Stream qua máy local (OAuth). Chỉ các file trong danh sách dưới đây.</p>
  <table>
    <thead><tr><th>Đường dẫn</th><th></th></tr></thead>
    <tbody>
    {body}
    </tbody>
  </table>
</body>
</html>"""
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.get("/play/<file_id>")
    def play(file_id: str):
        if file_id not in allowed:
            abort(404)
        meta = next((v for v in videos_sorted if v["id"] == file_id), None)
        if not meta:
            abort(404)
        title = escape(meta["path"])
        fid = escape(file_id)
        html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 1rem; }}
    video {{ max-width: 100%; height: auto; background: #000; }}
    a {{ color: #1a73e8; }}
  </style>
</head>
<body>
  <p><a href="/">← Danh sách</a></p>
  <h1>{title}</h1>
  <video src="/stream/{fid}" controls preload="metadata"></video>
</body>
</html>"""
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.route("/stream/<file_id>", methods=["GET", "HEAD"])
    def stream(file_id: str):
        if file_id not in allowed:
            abort(404)

        if not creds.valid:
            creds.refresh(Request())
        session = AuthorizedSession(creds)
        url = DRIVE_MEDIA_URL.format(file_id=file_id)
        req_headers: Dict[str, str] = {}
        rng = request.headers.get("Range")
        if rng:
            req_headers["Range"] = rng

        try:
            # stream=True để không đọc hết body vào RAM
            upstream = session.request(
                request.method,
                url,
                headers=req_headers,
                stream=True,
            )
        except Exception as exc:
            return Response(
                f"Lỗi kết nối Drive: {exc}",
                status=502,
                mimetype="text/plain; charset=utf-8",
            )

        status = upstream.status_code
        if status not in (200, 206):
            try:
                err_body = upstream.text[:2000]
            except Exception:
                err_body = ""
            upstream.close()
            msg = _stream_error_message(status, err_body)
            return Response(msg, status=status, mimetype="text/plain; charset=utf-8")

        out_headers = _forward_headers_from_drive(upstream)

        if request.method == "HEAD":
            upstream.close()
            return Response(status=status, headers=out_headers)

        @stream_with_context
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return Response(
            generate(),
            status=status,
            headers=out_headers,
            direct_passthrough=True,
        )

    return app


def _stream_error_message(status: int, body: str) -> str:
    if status == 403:
        return (
            "403: Drive từ chối đọc file (quyền, virus scan chưa xong, abuse, hoặc chính sách tải).\n"
            + (body[:500] if body else "")
        )
    if status == 404:
        return "404: Không tìm thấy file."
    return f"Lỗi stream HTTP {status}\n" + (body[:500] if body else "")


def _prompt_folder_id() -> str:
    raw = input(
        "Nhập Google Drive folder ID hoặc URL folder "
        "(vd: https://drive.google.com/drive/folders/...): "
    ).strip()
    fid = parse_folder_id_from_url(raw)
    if not fid:
        print("Không nhận diện được folder ID hoặc URL hợp lệ.", file=sys.stderr)
        sys.exit(1)
    return fid


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Liệt kê và xem video trong folder Google Drive (OAuth, drive.readonly)."
    )
    parser.add_argument(
        "--folder-id",
        help="ID folder Drive hoặc URL chứa /folders/<ID>",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Chỉ nên dùng 127.0.0.1 (mặc định).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Cổng HTTP (0 = hệ thống chọn).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Không mở trình duyệt tự động.",
    )
    args = parser.parse_args()

    if args.host != "127.0.0.1":
        print(
            "Cảnh báo: chỉ nên bind 127.0.0.1 để tránh lộ token/luồng video.",
            file=sys.stderr,
        )

    raw_folder = (args.folder_id or "").strip()
    if raw_folder:
        folder_id = parse_folder_id_from_url(raw_folder)
        if not folder_id:
            print("Không nhận diện được folder ID từ --folder-id.", file=sys.stderr)
            sys.exit(1)
    else:
        folder_id = _prompt_folder_id()

    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    try:
        service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        if e.resp.status == 404:
            print("Không tìm thấy folder hoặc không có quyền.", file=sys.stderr)
            sys.exit(1)
        raise

    print("Đang quét thư mục (có thể mất vài phút nếu cây lớn)...", file=sys.stderr)
    videos = collect_videos_recursive(service, folder_id)
    print(f"Tìm thấy {len(videos)} video.", file=sys.stderr)

    app = create_app(videos, creds)

    def open_browser(url: str) -> None:
        webbrowser.open(url)

    # Cho Flask gán port nếu port=0
    from werkzeug.serving import make_server

    srv = make_server(args.host, args.port, app, threaded=True)
    actual_port = srv.server_port
    base_url = f"http://{args.host}:{actual_port}/"
    print(f"Mở: {base_url}", file=sys.stderr)

    if not args.no_browser:
        threading.Timer(0.4, open_browser, args=[base_url]).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.", file=sys.stderr)


if __name__ == "__main__":
    main()
