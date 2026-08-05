"""
Sync một folder Google Drive (đệ quy) về thư mục trên NAS qua mount SMB trên macOS.

Chạy trên Mac; NAS phải mount sẵn (vd: Finder → smb://DS223 → /Volumes/homes/...).
Trước khi sync: đếm file trên Drive và kiểm tra ghi được vào đích NAS.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from googleapiclient.errors import HttpError

from .drive_download import (
    build_drive_service,
    download_file_meta_with_service,
    get_credentials,
)
from .transfer import _safe_cleanup_empty_dirs, _safe_remove_file
from .utils import ensure_dir

PROJECT_ROOT = Path(__file__).resolve().parents[1]
from .view_video_drive import FOLDER_MIME, SHORTCUT_MIME, parse_folder_id_from_url

FOLDER_LIST_FIELDS = (
    "nextPageToken, files(id, name, mimeType, size, md5Checksum, modifiedTime)"
)

UNSUPPORTED_GOOGLE_APPS = {
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
}

GOOGLE_APP_DEFAULT_EXT = {
    "application/vnd.google-apps.document": ".docx",
    "application/vnd.google-apps.spreadsheet": ".xlsx",
    "application/vnd.google-apps.presentation": ".pptx",
    "application/vnd.google-apps.drawing": ".png",
}

DEFAULT_NAS_SHARE = "homes"
DEFAULT_NAS_REL_PICTURE = "admins/Photos/PICTURE"
DEFAULT_NAS_BASE = f"/Volumes/{DEFAULT_NAS_SHARE}"
DEFAULT_NAS_SYNC_DEST = os.path.join(
    DEFAULT_NAS_BASE, *DEFAULT_NAS_REL_PICTURE.split("/")
)
DEFAULT_TEMP_DIR = os.getenv("TEMP_DIR", "/tmp/drive_nas_sync")

FAIL_LOG_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\|\s+\[FAIL\]\s+(.+?):\s+"
)
FAILED_ARTIFACT_VERSION = "drive_nas_retry v1"

RETRYABLE_SYNC_MARKERS = (
    "can't assign requested address",
    "connection aborted",
    "connection reset",
    "connection was closed",
    "connect timeout",
    "read operation timed out",
    "read timeout",
    "broken pipe",
    "disk not available",
    "host is down",
    "network is down",
    "no route to host",
    "not connected",
    "operation not permitted",
    "operation timed out",
    "resource temporarily unavailable",
    "socket is not connected",
    "stale nfs file handle",
    "temporarily unavailable",
    "timed out",
    "timeout on endpoint",
)

RETRYABLE_ERRNOS = frozenset({1, 49, 51, 57, 60, 61, 64, 65, 110})


def _nas_file_retries() -> int:
    return int(os.getenv("NAS_FILE_RETRIES", "3"))


def _nas_retry_delay() -> int:
    return int(os.getenv("NAS_RETRY_DELAY", "15"))


def _nas_reconnect_wait() -> int:
    return int(os.getenv("NAS_RECONNECT_WAIT", "300"))


def _nas_keepalive_enabled() -> bool:
    return os.getenv("NAS_KEEPALIVE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _auto_mount_enabled() -> bool:
    return os.getenv("NAS_AUTO_MOUNT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _parse_smb_mounts() -> List[Dict[str, str]]:
    """Đọc `mount` để lấy host/share/mount_point của các SMB đang gắn."""
    mounts: List[Dict[str, str]] = []
    try:
        result = subprocess.run(
            ["mount"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in result.stdout.splitlines():
            lower = line.lower()
            if "smbfs" not in lower and "//" not in line:
                continue
            if " on " not in line:
                continue
            source, rest = line.split(" on ", 1)
            mount_point = rest.split(" (", 1)[0].strip()
            source = source.strip()
            if not source.startswith("//"):
                continue
            body = source[2:]
            user = ""
            host = ""
            share = ""
            if "@" in body:
                user_part, _, tail = body.partition("@")
                user = user_part
                if "/" in tail:
                    host, share = tail.split("/", 1)
                else:
                    host = tail
            elif "/" in body:
                host, share = body.split("/", 1)
            else:
                host = body
            if not host:
                continue
            mounts.append(
                {
                    "host": host,
                    "share": share,
                    "user": user,
                    "mount_point": mount_point,
                }
            )
    except (OSError, subprocess.SubprocessError):
        pass
    return mounts


def _detect_smb_host_from_mount(mount_point: str) -> str:
    """Lấy IP/hostname từ mount Finder hiện có (vd 192.168.1.175)."""
    target = os.path.abspath(mount_point)
    for item in _parse_smb_mounts():
        if os.path.abspath(item["mount_point"]) == target:
            return item["host"]
    return ""


def _smb_host() -> str:
    configured = os.getenv("NAS_SMB_HOST_LAN", "").strip()
    if configured:
        return configured
    detected = _detect_smb_host_from_mount(_smb_mount_point())
    if detected:
        return detected
    return "DS223"


def _smb_share() -> str:
    return os.getenv("NAS_SMB_SHARE", DEFAULT_NAS_SHARE).strip()


def _smb_mount_point() -> str:
    return os.getenv("NAS_SMB_MOUNT_POINT", DEFAULT_NAS_BASE).strip()


def _smb_mount_url() -> str:
    """URL cho mount_smbfs: //user:pass@host/share"""
    host = _smb_host()
    share = _smb_share()
    user = os.getenv("NAS_SMB_USER", "").strip()
    password = os.getenv("NAS_SMB_PASSWORD", "").strip()
    if user and password:
        cred = f"{quote(user, safe='')}:{quote(password, safe='')}"
        return f"//{cred}@{host}/{share}"
    if user:
        return f"//{quote(user, safe='')}@{host}/{share}"
    return f"//{host}/{share}"


def _smb_mount_url_display() -> str:
    host = _smb_host()
    share = _smb_share()
    user = os.getenv("NAS_SMB_USER", "").strip()
    if user:
        return f"//{user}@{host}/{share}"
    return f"//{host}/{share}"


def _mount_smbfs_bin() -> str:
    path = shutil.which("mount_smbfs")
    if path:
        return path
    for candidate in ("/sbin/mount_smbfs", "/usr/sbin/mount_smbfs"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("Không tìm thấy mount_smbfs trên macOS.")


def try_unmount_smb(mount_point: Optional[str] = None) -> bool:
    target = mount_point or _smb_mount_point()
    if not os.path.exists(target):
        return True
    for cmd in (
        ["diskutil", "unmount", "force", target],
        ["umount", "-f", target],
    ):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    try:
        return not os.path.ismount(target)
    except OSError:
        return False


def _prepare_mount_point(mount_point: str) -> None:
    """Chuẩn bị mount point; không crash nếu /Volumes/homes đã do Finder tạo."""
    if os.path.ismount(mount_point):
        return
    if os.path.isdir(mount_point):
        probe = os.path.join(mount_point, f".probe_{os.getpid()}")
        try:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
            return
        except OSError:
            try_unmount_smb(mount_point)

    if os.path.lexists(mount_point) and not os.path.isdir(mount_point):
        raise RuntimeError(
            f"Mount point {mount_point} tồn tại nhưng không phải thư mục."
        )

    if os.path.isdir(mount_point):
        return

    try:
        os.makedirs(mount_point, mode=0o755, exist_ok=True)
    except PermissionError as exc:
        if os.path.isdir(mount_point):
            # Mount point do Finder/macOS tạo sẵn — mount_smbfs sẽ dùng sau khi gỡ mount cũ.
            return
        raise RuntimeError(
            f"Không tạo được {mount_point}: {exc}\n"
            "  Mount smb://"
            f"{_smb_host()}/{_smb_share()} qua Finder (Cmd+K) một lần, "
            "hoặc chạy: python3 -m app.drive_to_nas --mount-only"
        ) from exc


def try_mount_smb(
    *,
    emit: Optional[Callable[[str], None]] = None,
    force_remount: bool = False,
) -> bool:
    """
    Mount SMB qua mount_smbfs (macOS).
    Nếu chỉ có NAS_SMB_USER (không password), macOS dùng mật khẩu đã lưu Keychain.
    """
    mount_point = _smb_mount_point()
    share_root = mount_point

    if not force_remount:
        ok, _ = check_nas_destination(share_root)
        if ok:
            return True

    if force_remount or os.path.ismount(mount_point) or os.path.lexists(mount_point):
        if emit:
            emit(f"{_ts()} | [NAS MOUNT] Gỡ mount cũ: {mount_point}")
        try_unmount_smb(mount_point)

    try:
        _prepare_mount_point(mount_point)
    except RuntimeError as exc:
        if emit:
            emit(f"{_ts()} | [NAS MOUNT] {exc}")
        return False

    smb_url = _smb_mount_url()
    display_url = _smb_mount_url_display()
    host = _smb_host()

    if emit:
        emit(
            f"{_ts()} | [NAS MOUNT] mount_smbfs {display_url} → {mount_point}"
        )
        if host != os.getenv("NAS_SMB_HOST_LAN", "").strip() and host != "DS223":
            emit(
                f"{_ts()} | [NAS MOUNT] Dùng host từ mount hiện có: {host} "
                f"(đặt NAS_SMB_HOST_LAN={host} trong .env để cố định)"
            )

    try:
        result = subprocess.run(
            [_mount_smbfs_bin(), smb_url, mount_point],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if emit:
            emit(f"{_ts()} | [NAS MOUNT] lỗi: {exc}")
        return False

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "mount_smbfs thất bại").strip()
        if emit:
            emit(f"{_ts()} | [NAS MOUNT] thất bại: {err}")
        return False

    ok, message = check_nas_destination(share_root)
    if not ok and emit:
        emit(f"{_ts()} | [NAS MOUNT] đã mount nhưng chưa ghi được: {message}")
    return ok


def ensure_smb_mounted(
    *,
    emit: Optional[Callable[[str], None]] = None,
) -> bool:
    """Thử mount SMB nếu bật NAS_AUTO_MOUNT (chỉ kiểm tra share gốc /Volumes/homes)."""
    if not _auto_mount_enabled():
        return False
    mount_point = _smb_mount_point()
    ok, _ = check_nas_destination(mount_point)
    if ok:
        return True
    try:
        if os.path.lexists(mount_point):
            return try_mount_smb(emit=emit, force_remount=True)
        return try_mount_smb(emit=emit)
    except (OSError, RuntimeError) as exc:
        if emit:
            emit(f"{_ts()} | [NAS MOUNT] {exc}")
        return False


def ensure_nas_mount_root(
    *,
    emit: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """Đảm bảo share SMB gốc mount được; không nhầm với thư mục con chưa tạo."""
    mount_point = _smb_mount_point()
    ok, message = check_nas_destination(mount_point)
    if ok:
        return True, message
    if _auto_mount_enabled():
        ensure_smb_mounted(emit=emit)
        ok, message = check_nas_destination(mount_point)
    return ok, message


def _is_retryable_sync_error(exc: Exception) -> bool:
    if isinstance(exc, OSError) and exc.errno in RETRYABLE_ERRNOS:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in RETRYABLE_SYNC_MARKERS)


@dataclass(frozen=True)
class RetryContext:
    folder_id: str
    folder_name: str
    sync_dest: str
    base_dest: str
    source_path: str
    failed_paths: List[str]


@dataclass(frozen=True)
class DriveEntry:
    id: str
    name: str
    path: str
    mime_type: str
    size: Optional[int]
    md5: Optional[str]
    modified_time: Optional[str]


@dataclass(frozen=True)
class PreflightResult:
    folder_name: str
    folder_id: str
    entries: List[DriveEntry]
    base_dest: str
    dest_path: str
    to_download: int
    to_skip: int
    total_bytes: int


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _tail_follow_cmd(path: str) -> str:
    """Lệnh tail -f an toàn khi path có khoảng trắng."""
    return f"tail -f {shlex.quote(path)}"


def _human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _should_ignore_name(name: str) -> bool:
    return name.startswith(".") or name.lower().endswith(".ds_store")


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
            fields=FOLDER_LIST_FIELDS,
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )


def collect_files_recursive(
    service: Any,
    parent_id: str,
    prefix: str = "",
) -> List[DriveEntry]:
    """Liệt kê đệ quy mọi file có thể tải (bỏ folder, shortcut, file ẩn)."""
    out: List[DriveEntry] = []
    page_token: Optional[str] = None

    while True:
        res = _list_page(service, parent_id, page_token)
        for f in res.get("files", []):
            mid = f.get("id") or ""
            name = f.get("name") or ""
            mime = f.get("mimeType") or ""
            if _should_ignore_name(name):
                continue

            rel = f"{prefix}{name}" if prefix else name

            if mime == FOLDER_MIME:
                sub_prefix = f"{rel}/"
                out.extend(collect_files_recursive(service, mid, sub_prefix))
                continue
            if mime == SHORTCUT_MIME:
                continue
            if mime in UNSUPPORTED_GOOGLE_APPS:
                continue

            size_raw = f.get("size")
            size = int(size_raw) if size_raw is not None else None
            out.append(
                DriveEntry(
                    id=mid,
                    name=name,
                    path=rel,
                    mime_type=mime,
                    size=size,
                    md5=f.get("md5Checksum"),
                    modified_time=f.get("modifiedTime"),
                )
            )

        page_token = res.get("nextPageToken")
        if not page_token:
            break

    return out


def _google_export_path(rel_path: str, mime_type: str) -> str:
    ext = GOOGLE_APP_DEFAULT_EXT.get(mime_type)
    if not ext:
        return rel_path
    if rel_path.lower().endswith(ext):
        return rel_path
    return f"{rel_path}{ext}"


def local_path_for_entry(dest_root: str, entry: DriveEntry) -> str:
    rel = entry.path
    if entry.mime_type.startswith("application/vnd.google-apps."):
        rel = _google_export_path(rel, entry.mime_type)
    return os.path.join(dest_root, rel)


def _temp_path_for(temp_dir: str, rel_display: str) -> str:
    return os.path.join(temp_dir, rel_display)


def _download_entry_to_nas(
    service: Any,
    entry: DriveEntry,
    final_path: str,
    rel_display: str,
    temp_dir: str,
    *,
    file_index: int = 0,
    file_total: int = 0,
    acknowledge_abuse: bool = False,
    emit: Optional[Any] = None,
    file_retries: Optional[int] = None,
) -> None:
    """Tải về temp trên Mac, chuyển lên NAS (retry riêng từng pha)."""
    retries = _nas_file_retries() if file_retries is None else file_retries
    retry_delay = _nas_retry_delay()
    temp_path = _temp_path_for(temp_dir, rel_display)
    temp_parent = os.path.dirname(os.path.abspath(temp_path))
    if temp_parent:
        os.makedirs(temp_parent, exist_ok=True)

    meta = {
        "id": entry.id,
        "name": entry.name,
        "mimeType": entry.mime_type,
        "size": entry.size,
    }

    prefix = f"({file_index}/{file_total}) " if file_index and file_total else ""
    last_logged_pct = -1

    def _emit_download_progress(pct: int) -> None:
        nonlocal last_logged_pct
        if not emit:
            return
        if pct < 100 and pct - last_logged_pct < 5 and last_logged_pct >= 0:
            return
        last_logged_pct = pct
        size_hint = ""
        if entry.size is not None and entry.size > 0:
            done_bytes = min(entry.size, int(entry.size * pct / 100))
            size_hint = f" {_human_size(done_bytes)}/{_human_size(entry.size)}"
        emit(
            f"{_ts()} | {prefix}[DL {pct:3d}%]{size_hint} {rel_display}"
        )

    for attempt in range(1, retries + 1):
        last_logged_pct = -1
        try:
            download_file_meta_with_service(
                service,
                meta,
                temp_path,
                acknowledge_abuse=acknowledge_abuse,
                on_progress=_emit_download_progress,
            )
            break
        except Exception as exc:  # noqa: BLE001
            _safe_remove_file(temp_path, temp_dir)
            if attempt >= retries or not _is_retryable_sync_error(exc):
                raise
            wait = min(retry_delay * (2 ** (attempt - 1)), 120)
            if emit:
                emit(
                    f"{_ts()} | [RETRY DL] {rel_display} "
                    f"({attempt}/{retries}) sau {wait}s: {exc}"
                )
            time.sleep(wait)
    else:
        raise RuntimeError(f"Không tải được từ Drive: {rel_display}")

    final_parent = os.path.dirname(os.path.abspath(final_path))
    nas_probe = _existing_ancestor_for_probe(final_parent or final_path)

    try:
        for attempt in range(1, retries + 1):
            try:
                wait_for_nas_writable(nas_probe, emit=emit)
                if final_parent:
                    os.makedirs(final_parent, exist_ok=True)
                shutil.move(temp_path, final_path)
                return
            except Exception as exc:  # noqa: BLE001
                if attempt >= retries or not _is_retryable_sync_error(exc):
                    raise
                wait = min(retry_delay * (2 ** (attempt - 1)), 120)
                if emit:
                    emit(
                        f"{_ts()} | [RETRY NAS] {rel_display} "
                        f"({attempt}/{retries}) sau {wait}s: {exc}"
                    )
                time.sleep(wait)
    finally:
        _safe_remove_file(temp_path, temp_dir)
        if temp_parent:
            _safe_cleanup_empty_dirs(temp_parent, temp_dir)


def should_skip_local(entry: DriveEntry, local_path: str) -> bool:
    if not os.path.isfile(local_path):
        return False
    if entry.size is not None:
        try:
            return os.path.getsize(local_path) == entry.size
        except OSError:
            return False
    if entry.mime_type.startswith("application/vnd.google-apps."):
        try:
            return os.path.getsize(local_path) > 0
        except OSError:
            return False
    return False


def find_macos_smb_mounts(host_hint: str = "") -> List[str]:
    mounts: List[str] = []
    vol_dir = Path("/Volumes")
    if vol_dir.is_dir():
        for path in sorted(vol_dir.iterdir()):
            if path.is_dir() and not path.name.startswith("."):
                mounts.append(str(path))

    hint = host_hint.lower()
    for item in _parse_smb_mounts():
        mount_point = item["mount_point"]
        if mount_point and mount_point not in mounts:
            if not hint or hint in item["host"].lower() or hint in mount_point.lower():
                mounts.append(mount_point)

    if hint:
        try:
            result = subprocess.run(
                ["mount"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for line in result.stdout.splitlines():
                lower = line.lower()
                if "smbfs" not in lower and "//" not in lower:
                    continue
                if hint not in lower:
                    continue
                if " on " not in line:
                    continue
                mount_point = line.split(" on ", 1)[1].split(" (", 1)[0].strip()
                if mount_point and mount_point not in mounts:
                    mounts.append(mount_point)
        except (OSError, subprocess.SubprocessError):
            pass

    return mounts


def _sanitize_folder_name(name: str) -> str:
    """Tên folder an toàn cho NAS/macOS."""
    bad = '/\\:*?"<>|'
    cleaned = "".join("_" if c in bad else c for c in name).strip().strip(".")
    return cleaned or "drive_folder"


def build_sync_dest(
    base_dest: str,
    drive_folder_name: str,
    *,
    use_drive_subfolder: bool,
) -> str:
    """Đích sync: homes/admins/Photos/PICTURE/<tên folder Drive>."""
    if not use_drive_subfolder:
        return base_dest
    return os.path.join(base_dest, _sanitize_folder_name(drive_folder_name))


def _display_nas_rel_path(base_dest: str, sync_dest: str) -> str:
    """Hiển thị dạng homes/admins/Photos/PICTURE/... cho dễ đối chiếu File Station."""
    vol_prefix = "/Volumes/"
    if sync_dest.startswith(vol_prefix):
        return sync_dest[len(vol_prefix) :]
    base_name = os.path.basename(base_dest.rstrip(os.sep)) or base_dest
    if os.path.normpath(sync_dest) == os.path.normpath(base_dest):
        return base_name
    rel = os.path.relpath(sync_dest, base_dest)
    return f"{base_name}/{rel}"


def _configured_sync_dest() -> str:
    """Thư mục PICTURE mặc định: /Volumes/homes/admins/Photos/PICTURE."""
    raw = os.getenv("NAS_SYNC_DEST", DEFAULT_NAS_SYNC_DEST).strip()
    return resolve_dest_path(raw or DEFAULT_NAS_SYNC_DEST)


def _picture_dest_under_share(share_root: str) -> str:
    return os.path.join(share_root, *DEFAULT_NAS_REL_PICTURE.split("/"))


def _remote_mount_hint() -> str:
    host = os.getenv("NAS_SMB_HOST", "nhutnm.synology.me").strip()
    share = os.getenv("NAS_SMB_SHARE", DEFAULT_NAS_SHARE).strip()
    local_host = os.getenv("NAS_SMB_HOST_LAN", "DS223").strip()
    return (
        "\n  Đồng bộ cần mount SMB (Finder → Cmd+K), không dùng link DSM :5001.\n"
        f"  • Trong nhà (LAN): smb://{local_host}/{share}\n"
        f"  • Ngoài nhà: bật VPN vào NAS rồi mount smb://{local_host}/{share}\n"
        f"    (hoặc Tailscale; QuickConnect SMB nếu đã bật trong DSM).\n"
        f"  • Link https://{host}:5001/ chỉ mở giao diện web DSM, không ghi file qua tool.\n"
        f"  • Sau khi mount, đích thường là: {_configured_sync_dest()}\n"
        f"  • NAS_AUTO_MOUNT=1: tool tự mount qua mount_smbfs "
        f"(cần NAS_SMB_USER hoặc đã lưu mật khẩu Keychain)."
    )


def guess_nas_base_dest() -> str:
    """Share NAS gốc homes (mount SMB)."""
    configured = _configured_sync_dest()
    if configured.startswith(DEFAULT_NAS_BASE + os.sep) or configured == DEFAULT_NAS_BASE:
        return DEFAULT_NAS_BASE

    if os.path.isdir(DEFAULT_NAS_BASE):
        return DEFAULT_NAS_BASE

    share = os.getenv("NAS_SMB_SHARE", DEFAULT_NAS_SHARE).strip()
    for mount in find_macos_smb_mounts():
        if os.path.basename(mount) == share:
            return mount
        nested = os.path.join(mount, share)
        if os.path.isdir(nested):
            return nested

    return DEFAULT_NAS_BASE


def default_sync_dest(folder_name: str) -> str:
    """Đích mặc định: homes/admins/Photos/PICTURE/<tên folder Drive>."""
    return build_sync_dest(
        _configured_sync_dest(),
        folder_name,
        use_drive_subfolder=True,
    )


def resolve_sync_paths(
    folder_name: str,
    user_dest: Optional[str],
    *,
    use_drive_subfolder: bool,
) -> Tuple[str, str]:
    """
    Trả về (base_dest, sync_dest).
    Mặc định: homes/admins/Photos/PICTURE/<tên folder Drive>.
    """
    nas_base = guess_nas_base_dest()
    picture_dest = _configured_sync_dest()
    safe_name = _sanitize_folder_name(folder_name)
    share = os.getenv("NAS_SMB_SHARE", DEFAULT_NAS_SHARE).strip()

    if not use_drive_subfolder:
        sync_dest = resolve_dest_path(user_dest) if user_dest else picture_dest
        base = os.path.dirname(sync_dest.rstrip(os.sep)) or nas_base
        return base, sync_dest

    if not user_dest:
        sync_dest = build_sync_dest(picture_dest, folder_name, use_drive_subfolder=True)
        return picture_dest, sync_dest

    resolved = resolve_dest_path(user_dest)
    resolved_base = os.path.basename(resolved.rstrip(os.sep))

    if resolved_base == share:
        picture_path = _picture_dest_under_share(resolved)
        sync_dest = build_sync_dest(
            picture_path, folder_name, use_drive_subfolder=True
        )
        return picture_path, sync_dest

    if resolved_base == os.path.basename(picture_dest.rstrip(os.sep)):
        sync_dest = build_sync_dest(
            resolved, folder_name, use_drive_subfolder=True
        )
        return resolved, sync_dest

    if resolved_base == safe_name:
        parent = os.path.dirname(resolved.rstrip(os.sep)) or picture_dest
        return parent, resolved

    sync_dest = build_sync_dest(resolved, folder_name, use_drive_subfolder=True)
    return resolved, sync_dest


def resolve_dest_path(raw: str) -> str:
    text = raw.strip()
    if text.lower().startswith("smb://"):
        raise ValueError(
            "Đường dẫn SMB URL không dùng trực tiếp được. "
            "Hãy mount NAS trong Finder (Cmd+K → smb://DS223), "
            "rồi nhập path mount, vd: /Volumes/homes"
        )
    return os.path.abspath(os.path.expanduser(text))


def nas_keepalive_touch(dest: str) -> None:
    """Ghi probe nhẹ lên NAS giữa các file để SMB không idle quá lâu."""
    if not _nas_keepalive_enabled():
        return
    probe = os.path.join(dest, ".drive_nas_keepalive")
    try:
        with open(probe, "a", encoding="utf-8") as fh:
            fh.write(".")
        if os.path.getsize(probe) > 4096:
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write(".")
    except OSError as exc:
        raise RuntimeError(f"NAS keepalive thất bại ({dest}): {exc}") from exc


def ensure_nas_alive(
    dest: str,
    *,
    emit: Optional[Any] = None,
) -> None:
    try:
        nas_keepalive_touch(dest)
    except RuntimeError:
        if emit:
            ensure_smb_mounted(emit=emit)
        wait_for_nas_writable(dest, emit=emit)
        nas_keepalive_touch(dest)


def wait_for_nas_writable(
    dest: str,
    *,
    emit: Optional[Any] = None,
    max_wait: Optional[int] = None,
) -> None:
    """Chờ mount SMB ghi được lại (tối đa NAS_RECONNECT_WAIT giây)."""
    limit = _nas_reconnect_wait() if max_wait is None else max_wait
    interval = 10
    deadline = time.time() + limit
    last_message = ""
    probe_dir = _existing_ancestor_for_probe(dest)

    while True:
        ok, message = check_nas_destination(probe_dir)
        if ok:
            if last_message and emit:
                emit(f"{_ts()} | [NAS OK] Mount SMB đã sẵn sàng lại.")
            return
        last_message = message
        if time.time() >= deadline:
            raise RuntimeError(
                f"NAS chưa sẵn sàng sau {limit}s.\n"
                f"  Lỗi cuối: {message}\n"
                f"  Gợi ý: mount thủ công smb://{_smb_host()}/{_smb_share()} "
                f"hoặc cấu hình NAS_SMB_USER trong .env (NAS_AUTO_MOUNT=1)."
            )
        if _auto_mount_enabled():
            ensure_smb_mounted(emit=emit)
            ok, message = check_nas_destination(probe_dir)
            if ok:
                if emit:
                    emit(f"{_ts()} | [NAS OK] Đã mount SMB lại thành công.")
                return
        if emit:
            emit(
                f"{_ts()} | [NAS WAIT] Chờ mount SMB ({int(deadline - time.time())}s còn lại)..."
            )
        time.sleep(interval)


def _existing_ancestor_for_probe(dest: str) -> str:
    """Tìm thư mục tổ tiên gần nhất đã tồn tại để probe ghi NAS."""
    mount_floor = os.path.abspath(_smb_mount_point())
    path = os.path.abspath(dest)
    candidate = ""

    while True:
        if os.path.isdir(path):
            if not candidate or len(path) >= len(candidate):
                candidate = path
        if path == mount_floor:
            break
        parent = os.path.dirname(path)
        if parent == path or len(parent) < len(mount_floor):
            break
        path = parent

    if candidate and len(candidate) >= len(mount_floor):
        return candidate
    if os.path.isdir(mount_floor):
        return mount_floor
    return os.path.abspath(dest)


def check_nas_destination(dest: str) -> Tuple[bool, str]:
    remote_hint = _remote_mount_hint()
    if not os.path.isdir(dest):
        mounts = find_macos_smb_mounts()
        hint = ""
        if mounts:
            hint = "\n  Mount SMB đang thấy trên Mac:\n"
            hint += "\n".join(f"    - {m}" for m in mounts)
        return False, f"Thư mục đích không tồn tại: {dest}{hint}{remote_hint}"

    probe = os.path.join(dest, f".drive_nas_probe_{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        mounts = find_macos_smb_mounts()
        hint = ""
        if mounts:
            hint = "\n  Mount SMB đang thấy:\n"
            hint += "\n".join(f"    - {m}" for m in mounts)
        return False, f"Không ghi được vào NAS ({dest}): {exc}{hint}{remote_hint}"

    return True, dest


def verify_drive_folder(service: Any, folder_id: str) -> str:
    try:
        meta = (
            service.files()
            .get(
                fileId=folder_id,
                fields="id,name,mimeType",
                supportsAllDrives=True,
            )
            .execute()
        )
    except HttpError as exc:
        if exc.resp.status == 404:
            raise RuntimeError("Không tìm thấy folder Drive hoặc không có quyền.") from exc
        raise

    if meta.get("mimeType") != FOLDER_MIME:
        raise RuntimeError("ID không phải folder Google Drive.")
    return meta.get("name") or folder_id


def run_preflight(
    service: Any,
    folder_id: str,
    dest: str,
    base_dest: str,
) -> PreflightResult:
    folder_name = verify_drive_folder(service, folder_id)
    print(f"{_ts()} | Đang quét folder Drive (đệ quy)...", flush=True)
    entries = collect_files_recursive(service, folder_id)

    to_download = 0
    to_skip = 0
    total_bytes = 0
    for entry in entries:
        local_path = local_path_for_entry(dest, entry)
        if should_skip_local(entry, local_path):
            to_skip += 1
        else:
            to_download += 1
            if entry.size is not None:
                total_bytes += entry.size

    return PreflightResult(
        folder_name=folder_name,
        folder_id=folder_id,
        entries=entries,
        base_dest=base_dest,
        dest_path=dest,
        to_download=to_download,
        to_skip=to_skip,
        total_bytes=total_bytes,
    )


def _entry_rel_path(sync_dest: str, entry: DriveEntry) -> str:
    local_path = local_path_for_entry(sync_dest, entry)
    return os.path.relpath(local_path, sync_dest)


def _failed_artifact_path(
    log_dir: str,
    folder_name: str,
    *,
    progress_log: Optional[str] = None,
) -> str:
    if progress_log:
        if progress_log.endswith(".progress.log"):
            return progress_log.replace(".progress.log", ".failed.txt")
        base, _ext = os.path.splitext(progress_log)
        return f"{base}.failed.txt"
    ensure_dir(log_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _sanitize_folder_name(folder_name)
    return os.path.join(log_dir, f"{stamp}_drive_nas_{safe}.failed.txt")


def write_failed_artifact(
    path: str,
    *,
    folder_id: str,
    folder_name: str,
    sync_dest: str,
    base_dest: str,
    failures: List[Tuple[str, str]],
) -> None:
    ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
    lines = [
        f"# {FAILED_ARTIFACT_VERSION}",
        f"folder_id={folder_id}",
        f"folder_name={folder_name}",
        f"sync_dest={sync_dest}",
        f"base_dest={base_dest}",
        "---",
    ]
    for rel_path, _err in failures:
        lines.append(rel_path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _parse_failed_artifact(path: str) -> RetryContext:
    folder_id = ""
    folder_name = ""
    sync_dest = ""
    base_dest = ""
    failed_paths: List[str] = []
    in_body = False

    with open(path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                if line.startswith(f"# {FAILED_ARTIFACT_VERSION}"):
                    continue
                if not line.startswith("#"):
                    pass
                continue
            if line == "---":
                in_body = True
                continue
            if not in_body:
                if line.startswith("folder_id="):
                    folder_id = line.split("=", 1)[1].strip()
                elif line.startswith("folder_name="):
                    folder_name = line.split("=", 1)[1].strip()
                elif line.startswith("sync_dest="):
                    sync_dest = line.split("=", 1)[1].strip()
                elif line.startswith("base_dest="):
                    base_dest = line.split("=", 1)[1].strip()
            else:
                failed_paths.append(line)

    if not failed_paths:
        raise ValueError(f"Không có file lỗi trong: {path}")
    if not folder_id or not sync_dest:
        raise ValueError(
            f"File {path} thiếu metadata (folder_id, sync_dest). "
            "Dùng file .failed.txt do tool tạo, hoặc .progress.log đầy đủ."
        )
    if not base_dest:
        base_dest = os.path.dirname(sync_dest.rstrip(os.sep))
    return RetryContext(
        folder_id=folder_id,
        folder_name=folder_name or folder_id,
        sync_dest=sync_dest,
        base_dest=base_dest,
        source_path=path,
        failed_paths=failed_paths,
    )


def _parse_log_field(line: str, label: str) -> Optional[str]:
    match = re.search(rf"{re.escape(label)}\s*:\s*(.+?)\s*$", line)
    if not match:
        return None
    return match.group(1).strip()


def _parse_progress_log_for_retry(path: str) -> RetryContext:
    folder_id = ""
    folder_name = ""
    sync_dest = ""
    base_dest = ""
    failed_paths: List[str] = []
    seen: Set[str] = set()

    with open(path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n")

            value = _parse_log_field(line, "Drive folder")
            if value:
                folder_name = value
            value = _parse_log_field(line, "Folder ID")
            if value:
                folder_id = value
            value = _parse_log_field(line, "NAS đích")
            if value:
                sync_dest = value
            value = _parse_log_field(line, "NAS dest")
            if value:
                sync_dest = value
            value = _parse_log_field(line, "NAS gốc")
            if value:
                base_dest = value

            match = FAIL_LOG_RE.match(line)
            if match:
                rel = match.group(1).strip()
                if rel not in seen:
                    seen.add(rel)
                    failed_paths.append(rel)

    if not failed_paths:
        failed_artifact = _failed_artifact_path("", "", progress_log=path)
        if os.path.isfile(failed_artifact):
            return _parse_failed_artifact(failed_artifact)
        raise ValueError(f"Không tìm thấy dòng [FAIL] trong log: {path}")
    if not folder_id or not sync_dest:
        raise ValueError(
            f"Log {path} thiếu Folder ID hoặc NAS đích. "
            "Dùng file .failed.txt kèm theo nếu có."
        )
    if not base_dest:
        base_dest = os.path.dirname(sync_dest.rstrip(os.sep))
    return RetryContext(
        folder_id=folder_id,
        folder_name=folder_name or folder_id,
        sync_dest=sync_dest,
        base_dest=base_dest,
        source_path=path,
        failed_paths=failed_paths,
    )


def load_retry_context(path: str) -> RetryContext:
    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"Không tìm thấy file: {resolved}")
    if resolved.endswith(".failed.txt"):
        return _parse_failed_artifact(resolved)
    return _parse_progress_log_for_retry(resolved)


def find_latest_retry_artifact(log_dir: str) -> Optional[str]:
    root = Path(log_dir)
    if not root.is_dir():
        return None

    candidates: List[Path] = []
    candidates.extend(root.glob("*_drive_nas_*.failed.txt"))
    candidates.extend(root.glob("*_drive_nas_*.progress.log"))
    if not candidates:
        return None

    def sort_key(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    for path in sorted(candidates, key=sort_key, reverse=True):
        try:
            ctx = load_retry_context(str(path))
        except (OSError, ValueError):
            continue
        if ctx.failed_paths:
            return str(path)
    return None


def filter_entries_for_retry(
    entries: List[DriveEntry],
    sync_dest: str,
    retry_paths: List[str],
) -> Tuple[List[DriveEntry], List[str]]:
    want = set(retry_paths)
    matched: List[DriveEntry] = []
    found: Set[str] = set()
    for entry in entries:
        rel = _entry_rel_path(sync_dest, entry)
        if rel in want:
            matched.append(entry)
            found.add(rel)
    missing = sorted(want - found)
    return matched, missing


def _progress_log_path(log_dir: str, folder_name: str) -> str:
    ensure_dir(log_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _sanitize_folder_name(folder_name)
    return os.path.join(log_dir, f"{stamp}_drive_nas_{safe}.progress.log")


def _pid_path_for(progress_log_path: str) -> str:
    base, _ext = os.path.splitext(progress_log_path)
    return f"{base}.pid"


def _worker_command(
    args: argparse.Namespace,
    folder_id: str,
    sync_dest: str,
    base_dest: str,
    *,
    progress_log: Optional[str] = None,
) -> List[str]:
    command = [
        sys.executable,
        "-m",
        "app.drive_to_nas",
        "--foreground",
        "--yes",
        "--folder-id",
        folder_id,
        "--sync-dest",
        sync_dest,
        "--base-dest",
        base_dest,
        "--log-dir",
        args.log_dir,
        "--temp-dir",
        args.temp_dir,
    ]
    if progress_log:
        command.extend(["--progress-log", progress_log])
    if args.overwrite:
        command.append("--overwrite")
    if args.acknowledge_abuse:
        command.append("--acknowledge-abuse")
    if args.no_drive_subfolder:
        command.append("--no-drive-subfolder")
    if getattr(args, "retry_failed", None):
        command.extend(["--retry-failed", args.retry_failed])
    return command


def start_background(
    args: argparse.Namespace,
    folder_id: str,
    sync_dest: str,
    base_dest: str,
    folder_name: str,
    preflight: PreflightResult,
) -> Tuple[int, str, str]:
    progress_log_path = _progress_log_path(args.log_dir, folder_name)
    pid_path = _pid_path_for(progress_log_path)

    with open(progress_log_path, "a", encoding="utf-8", buffering=1) as log_file:
        log_file.write(f"{_ts()} | === Sync Google Drive -> NAS (nền) ===\n")
        log_file.write(f"{_ts()} | Drive folder : {folder_name}\n")
        log_file.write(f"{_ts()} | Folder ID    : {folder_id}\n")
        log_file.write(f"{_ts()} | NAS đích     : {sync_dest}\n")
        log_file.write(f"{_ts()} | Temp Mac     : {args.temp_dir}\n")
        log_file.write(f"{_ts()} | Tổng file    : {len(preflight.entries)}\n")
        log_file.write(f"{_ts()} | Cần tải      : {preflight.to_download}\n")
        log_file.write(f"{_ts()} | Skip (đã có) : {preflight.to_skip}\n\n")
        log_file.flush()

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            _worker_command(
                args, folder_id, sync_dest, base_dest, progress_log=progress_log_path
            ),
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.write(f"{_ts()} | PID: {process.pid}\n")
        log_file.write(f"{_ts()} | Worker sync đã chạy nền.\n\n")

    with open(pid_path, "w", encoding="utf-8") as pid_file:
        pid_file.write(f"{process.pid}\n")

    return process.pid, progress_log_path, pid_path


def print_preflight_report(preflight: PreflightResult, nas_ok: bool, nas_message: str) -> None:
    print("\n=== Kiểm tra trước khi sync Drive → NAS ===")
    print(f"Drive folder : {preflight.folder_name}")
    print(f"Folder ID    : {preflight.folder_id}")
    print(f"Tổng file    : {len(preflight.entries)}")
    print(f"Dung lượng   : ~{_human_size(preflight.total_bytes)} (file nhị phân có size)")
    print(f"Cần tải      : {preflight.to_download}")
    print(f"Skip (đã có) : {preflight.to_skip}")
    nas_rel = _display_nas_rel_path(preflight.base_dest, preflight.dest_path)
    print(f"Đường NAS    : {nas_rel}")
    print(f"  (macOS path: {preflight.dest_path})")
    if nas_ok:
        print("NAS          : OK (thư mục tồn tại, ghi thử thành công)")
    else:
        print(f"NAS          : LỖI — {nas_message}")


def sync_drive_to_nas(
    folder_id: str,
    sync_dest: str,
    base_dest: str,
    *,
    temp_dir: str = DEFAULT_TEMP_DIR,
    log_dir: str = "./logs",
    progress_log: Optional[str] = None,
    overwrite: bool = False,
    acknowledge_abuse: bool = False,
    yes: bool = False,
    worker_mode: bool = False,
    skip_preflight: bool = False,
    retry_paths: Optional[List[str]] = None,
) -> str:
    creds = get_credentials()
    service = build_drive_service(creds)

    def emit(line: str, *, err: bool = False) -> None:
        print(line, file=sys.stderr if err else sys.stdout, flush=True)

    if not skip_preflight:
        mount_ok, mount_message = ensure_nas_mount_root(emit=emit)
        if not mount_ok:
            raise RuntimeError(mount_message)

        ensure_dir(sync_dest)

        nas_ok_dest, nas_message_dest = check_nas_destination(sync_dest)
        if not nas_ok_dest:
            ensure_dir(base_dest)
            nas_ok_dest, nas_message_dest = check_nas_destination(sync_dest)
        preflight = run_preflight(service, folder_id, sync_dest, base_dest)
        if not worker_mode:
            print_preflight_report(preflight, nas_ok_dest, nas_message_dest)

        if not nas_ok_dest:
            raise RuntimeError(nas_message_dest)

        if not preflight.entries:
            emit("\nFolder Drive không có file để sync.")
            return ""
    else:
        mount_ok, mount_message = ensure_nas_mount_root(emit=emit)
        if not mount_ok:
            raise RuntimeError(mount_message)
        ensure_dir(sync_dest)
        nas_ok, nas_message = check_nas_destination(sync_dest)
        if not nas_ok:
            raise RuntimeError(nas_message)
        preflight = run_preflight(service, folder_id, sync_dest, base_dest)
        if not preflight.entries:
            emit(f"{_ts()} | Folder Drive không có file để sync.")
            return ""

    entries = preflight.entries
    missing_retry: List[str] = []
    if retry_paths:
        entries, missing_retry = filter_entries_for_retry(
            preflight.entries, sync_dest, retry_paths
        )
        emit(
            f"\n{_ts()} | Retry {len(entries)} file lỗi "
            f"(từ {len(retry_paths)} dòng trong danh sách)"
        )
        if missing_retry:
            emit(
                f"{_ts()} | Không tìm thấy trên Drive ({len(missing_retry)}):",
                err=True,
            )
            for rel in missing_retry:
                emit(f"{_ts()} | - {rel}", err=True)
        if not entries:
            emit(f"{_ts()} | Không có file nào để retry.")
            return ""

    if not yes and not worker_mode:
        raw = input("\nTiếp tục sync? (y/N): ").strip().lower()
        if raw not in ("y", "yes"):
            emit("Đã huỷ.")
            return ""

    emit(f"\n{_ts()} | Bắt đầu sync {len(entries)} file → {sync_dest}")
    ensure_dir(temp_dir)
    emit(f"{_ts()} | Temp Mac (xoá sau mỗi file): {temp_dir}")

    success = 0
    skipped = 0
    failed = 0
    failures: List[Tuple[str, str]] = []
    total = len(entries)

    emit(f"{_ts()} | === Sync Google Drive -> NAS ===")
    emit(f"{_ts()} | Folder ID : {folder_id}")
    emit(f"{_ts()} | Folder    : {preflight.folder_name}")
    emit(f"{_ts()} | NAS gốc  : {base_dest}")
    emit(f"{_ts()} | NAS dest  : {sync_dest}")
    if retry_paths:
        emit(f"{_ts()} | Retry     : {total} file")
    emit(f"{_ts()} | Total     : {total}\n")

    for index, entry in enumerate(entries, start=1):
        local_path = local_path_for_entry(sync_dest, entry)
        rel_display = os.path.relpath(local_path, sync_dest)

        if not overwrite and should_skip_local(entry, local_path):
            skipped += 1
            line = f"{_ts()} | ({index}/{total}) [SKIP] {rel_display}"
            emit(line)
            continue

        emit(f"{_ts()} | ({index}/{total}) [DL   0%] {rel_display}")
        try:
            ensure_nas_alive(sync_dest, emit=emit)
            _download_entry_to_nas(
                service,
                entry,
                local_path,
                rel_display,
                temp_dir,
                file_index=index,
                file_total=total,
                acknowledge_abuse=acknowledge_abuse,
                emit=emit,
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            err = str(exc)
            failures.append((rel_display, err))
            emit(f"{_ts()} | [FAIL] {rel_display}: {err}", err=True)
            continue

        success += 1
        try:
            out_size = os.path.getsize(local_path)
        except OSError:
            out_size = 0
        emit(f"{_ts()} | [OK] {rel_display} ({_human_size(out_size)})")

    emit(f"\n{_ts()} | === SUMMARY ===")
    emit(f"{_ts()} | Success : {success}")
    emit(f"{_ts()} | Skipped : {skipped}")
    emit(f"{_ts()} | Failed  : {failed}")
    if failures:
        emit(f"\n{_ts()} | Failed files:")
        for path, err in failures:
            emit(f"{_ts()} | - {path}: {err}")
        failed_path = _failed_artifact_path(
            log_dir,
            preflight.folder_name,
            progress_log=progress_log,
        )
        write_failed_artifact(
            failed_path,
            folder_id=folder_id,
            folder_name=preflight.folder_name,
            sync_dest=sync_dest,
            base_dest=base_dest,
            failures=failures,
        )
        emit(f"\n{_ts()} | Danh sách file lỗi: {failed_path}")
        emit(
            f"{_ts()} | Chạy lại: python3 -m app.drive_to_nas "
            f"--retry-failed {shlex.quote(failed_path)} --foreground --yes"
        )

    emit(
        f"\n{_ts()} | Hoàn tất sync: "
        f"{success} OK, {skipped} skip, {failed} lỗi."
    )
    return sync_dest


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


def _prompt_dest(folder_name: str) -> str:
    mounts = find_macos_smb_mounts()
    if mounts:
        print("\nMount SMB đang thấy trên Mac:")
        for mount in mounts:
            print(f"  - {mount}")

    default_dest = default_sync_dest(folder_name)
    print(f"\nMặc định sync vào: {default_dest}")
    prompt = f"Thư mục NAS đích [Enter = {default_dest}]: "
    raw = input(prompt).strip() or default_dest
    if not raw:
        print("Cần có đường dẫn thư mục NAS đích.", file=sys.stderr)
        sys.exit(1)
    try:
        return resolve_dest_path(raw)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync folder Google Drive về NAS (macOS, mount SMB trước)."
    )
    parser.add_argument(
        "--folder-id",
        help="ID folder Drive hoặc URL chứa /folders/<ID>",
    )
    parser.add_argument(
        "--dest",
        help=(
            "Thư mục NAS đích. Mặc định: homes/admins/Photos/PICTURE/<tên folder Drive>. "
            "Nếu chỉ truyền /Volumes/homes sẽ tự thêm admins/Photos/PICTURE."
        ),
    )
    parser.add_argument(
        "--log-dir",
        default=os.getenv("LOG_DIR", "./logs"),
        help="Thư mục log tiến trình.",
    )
    parser.add_argument(
        "--temp-dir",
        default=DEFAULT_TEMP_DIR,
        help=f"Thư mục tạm trên Mac khi tải từ Drive, default: {DEFAULT_TEMP_DIR}",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Tải lại cả file đã tồn tại trên NAS.",
    )
    parser.add_argument(
        "--acknowledge-abuse",
        action="store_true",
        help="Xác nhận tải file Drive cảnh báo abuse.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Bỏ qua prompt xác nhận sau bước kiểm tra.",
    )
    parser.add_argument(
        "--no-drive-subfolder",
        action="store_true",
        help="Không tạo subfolder theo tên folder Drive; ghi thẳng vào --dest.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Chỉ kiểm tra số lượng Drive và kết nối NAS, không tải.",
    )
    parser.add_argument(
        "--mount-only",
        action="store_true",
        help="Chỉ thử mount SMB (mount_smbfs), không sync.",
    )
    parser.add_argument(
        "--retry-failed",
        nargs="?",
        const="__LATEST__",
        metavar="LOG_OR_FAILED_FILE",
        help=(
            "Chỉ tải lại file [FAIL] từ lần chạy trước (.progress.log hoặc .failed.txt). "
            "Không truyền PATH = dùng file failed/log mới nhất trong --log-dir."
        ),
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Chạy sync trực tiếp trong terminal (worker nội bộ, không dùng khi gọi từ menu).",
    )
    parser.add_argument(
        "--sync-dest",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--base-dest",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--progress-log",
        help=argparse.SUPPRESS,
    )
    return parser


def _resolve_inputs(args: argparse.Namespace) -> Tuple[str, str, str, str]:
    """Trả về folder_id, folder_name, base_dest, sync_dest."""
    raw_folder = (args.folder_id or "").strip()
    if raw_folder:
        folder_id = parse_folder_id_from_url(raw_folder)
        if not folder_id:
            print("Không nhận diện được folder ID từ --folder-id.", file=sys.stderr)
            sys.exit(1)
    else:
        folder_id = _prompt_folder_id()

    creds = get_credentials()
    service = build_drive_service(creds)
    folder_name = verify_drive_folder(service, folder_id)

    if args.sync_dest and args.base_dest:
        return folder_id, folder_name, args.base_dest, args.sync_dest

    use_drive_subfolder = not args.no_drive_subfolder
    user_dest: Optional[str] = None
    if args.dest:
        try:
            user_dest = resolve_dest_path(args.dest)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    elif sys.stdin.isatty() and not args.foreground:
        user_dest = _prompt_dest(folder_name)

    base_dest, sync_dest = resolve_sync_paths(
        folder_name,
        user_dest,
        use_drive_subfolder=use_drive_subfolder,
    )
    return folder_id, folder_name, base_dest, sync_dest


def _run_preflight_checks(
    folder_id: str,
    sync_dest: str,
    base_dest: str,
    folder_name: str,
) -> PreflightResult:
    creds = get_credentials()
    service = build_drive_service(creds)

    nas_ok, nas_message = ensure_nas_mount_root()
    if not nas_ok:
        print_preflight_report(
            PreflightResult(
                folder_name=folder_name,
                folder_id=folder_id,
                entries=[],
                base_dest=base_dest,
                dest_path=sync_dest,
                to_download=0,
                to_skip=0,
                total_bytes=0,
            ),
            False,
            nas_message,
        )
        raise RuntimeError(nas_message)

    ensure_dir(sync_dest)
    nas_ok_dest, nas_message_dest = check_nas_destination(sync_dest)
    preflight = run_preflight(service, folder_id, sync_dest, base_dest)
    print_preflight_report(preflight, nas_ok_dest, nas_message_dest)

    if not nas_ok_dest:
        raise RuntimeError(nas_message_dest)
    return preflight


def _resolve_retry_inputs(
    args: argparse.Namespace,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[List[str]]]:
    if args.retry_failed is None:
        return None, None, None, None, None

    retry_source = args.retry_failed
    if retry_source == "__LATEST__":
        retry_source = find_latest_retry_artifact(args.log_dir)
        if not retry_source:
            print(
                "Không tìm thấy file .failed.txt hoặc .progress.log có [FAIL] "
                f"trong {args.log_dir}.",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        ctx = load_retry_context(retry_source)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    args.retry_failed = retry_source

    print(f"\n=== Retry file lỗi từ: {ctx.source_path} ===")
    print(f"Drive folder : {ctx.folder_name}")
    print(f"Folder ID    : {ctx.folder_id}")
    print(f"NAS đích     : {ctx.sync_dest}")
    print(f"Số file lỗi  : {len(ctx.failed_paths)}")
    return (
        ctx.folder_id,
        ctx.folder_name,
        ctx.base_dest,
        ctx.sync_dest,
        ctx.failed_paths,
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.mount_only:
        def _mount_emit(line: str) -> None:
            print(line, flush=True)

        if ensure_smb_mounted(emit=_mount_emit):
            mount_point = _smb_mount_point()
            ok, message = check_nas_destination(mount_point)
            print(f"\nSMB mount OK: {mount_point}")
            print(f"URL: smb://{_smb_host()}/{_smb_share()}")
            if not ok:
                print(f"Cảnh báo: {message}", file=sys.stderr)
                sys.exit(1)
            return
        print("Mount SMB thất bại.", file=sys.stderr)
        sys.exit(1)

    retry_folder_id, retry_folder_name, retry_base, retry_dest, retry_paths = (
        _resolve_retry_inputs(args)
    )
    if retry_paths is not None:
        folder_id = retry_folder_id or ""
        folder_name = retry_folder_name or ""
        base_dest = retry_base or ""
        sync_dest = retry_dest or ""
    else:
        folder_id, folder_name, base_dest, sync_dest = _resolve_inputs(args)

    sync_kwargs = dict(
        temp_dir=args.temp_dir,
        log_dir=args.log_dir,
        progress_log=args.progress_log,
        overwrite=args.overwrite,
        acknowledge_abuse=args.acknowledge_abuse,
        retry_paths=retry_paths,
    )

    if args.check_only:
        try:
            _run_preflight_checks(folder_id, sync_dest, base_dest, folder_name)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        return

    if args.foreground and args.sync_dest and args.base_dest:
        worker_retry_paths = retry_paths
        if worker_retry_paths is None and args.retry_failed:
            try:
                worker_retry_paths = load_retry_context(args.retry_failed).failed_paths
            except (OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(1)
        try:
            sync_drive_to_nas(
                folder_id,
                sync_dest,
                base_dest,
                yes=True,
                worker_mode=True,
                skip_preflight=True,
                retry_paths=worker_retry_paths,
                **{k: v for k, v in sync_kwargs.items() if k != "retry_paths"},
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        return

    if retry_paths:
        if not args.yes:
            raw = input("\nTiếp tục retry file lỗi? (y/N): ").strip().lower()
            if raw not in ("y", "yes"):
                print("Đã huỷ.")
                return
        if args.foreground:
            try:
                sync_drive_to_nas(
                    folder_id,
                    sync_dest,
                    base_dest,
                    yes=True,
                    worker_mode=False,
                    skip_preflight=True,
                    **sync_kwargs,
                )
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(1)
            return

        try:
            preflight = _run_preflight_checks(
                folder_id, sync_dest, base_dest, folder_name
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

        pid, progress_log_path, pid_path = start_background(
            args,
            folder_id,
            sync_dest,
            base_dest,
            folder_name,
            preflight,
        )
        print("\nĐã chuyển retry Drive → NAS sang chạy nền.")
        print(f"PID: {pid}")
        print(f"File log tiến trình: {progress_log_path}")
        print(f"File PID: {pid_path}")
        print(f"Theo dõi log: {_tail_follow_cmd(progress_log_path)}")
        return

    try:
        preflight = _run_preflight_checks(
            folder_id, sync_dest, base_dest, folder_name
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    if not preflight.entries:
        print("\nFolder Drive không có file để sync.")
        return

    if not args.yes:
        raw = input("\nTiếp tục sync? (y/N): ").strip().lower()
        if raw not in ("y", "yes"):
            print("Đã huỷ.")
            return

    if args.foreground:
        try:
            sync_drive_to_nas(
                folder_id,
                sync_dest,
                base_dest,
                yes=True,
                worker_mode=False,
                skip_preflight=True,
                **sync_kwargs,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        return

    pid, progress_log_path, pid_path = start_background(
        args,
        folder_id,
        sync_dest,
        base_dest,
        folder_name,
        preflight,
    )
    print("\nĐã chuyển sync Drive → NAS sang chạy nền.")
    print(f"PID: {pid}")
    print(f"File log tiến trình: {progress_log_path}")
    print(f"File PID: {pid_path}")
    print(f"Theo dõi log: {_tail_follow_cmd(progress_log_path)}")


if __name__ == "__main__":
    main()
