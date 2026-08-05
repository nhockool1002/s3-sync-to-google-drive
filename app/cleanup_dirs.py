from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

DIRECTORY_GROUPS = {
    "compress": {
        "source": PROJECT_ROOT / "compress" / "source",
        "output": PROJECT_ROOT / "compress" / "output",
    },
    "watermark": {
        "source": PROJECT_ROOT / "app" / "watermark" / "source",
        "output": PROJECT_ROOT / "app" / "watermark" / "output",
    },
    "hardsub": {
        "source": PROJECT_ROOT / "hardsub" / "source",
        "output": PROJECT_ROOT / "hardsub" / "output",
    },
    "video": {
        "source": PROJECT_ROOT / "video" / "source",
        "output": PROJECT_ROOT / "video" / "output",
    },
    "heic": {
        "source": PROJECT_ROOT / "heic" / "source",
        "output": PROJECT_ROOT / "heic" / "output",
    },
}

KEEP_NAMES = {".gitkeep"}


def _is_within_project(path: Path) -> bool:
    try:
        path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return False
    return True


def _clear_directory(path: Path) -> tuple[int, int]:
    if not _is_within_project(path):
        raise RuntimeError(f"Từ chối xoá ngoài project: {path}")

    path.mkdir(parents=True, exist_ok=True)

    deleted_files = 0
    deleted_dirs = 0
    for child in path.iterdir():
        if child.name in KEEP_NAMES:
            continue

        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
            deleted_dirs += 1
        else:
            child.unlink(missing_ok=True)
            deleted_files += 1

    gitkeep = path / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")

    return deleted_files, deleted_dirs


def _select_from_menu(title: str, options: list[str]) -> str:
    print(f"\n{title}")
    for index, option in enumerate(options, start=1):
        print(f"{index}. {option}")

    while True:
        raw = input("Chọn: ").strip()
        try:
            choice = int(raw)
        except ValueError:
            print("Vui lòng nhập số.")
            continue

        if 1 <= choice <= len(options):
            return options[choice - 1]
        print("Lựa chọn không hợp lệ.")


def _resolve_targets(tool: str, target: str) -> list[tuple[str, str, Path]]:
    groups: list[str]
    if tool == "all":
        groups = list(DIRECTORY_GROUPS)
    else:
        groups = [tool]

    target_names = ["source", "output"] if target == "both" else [target]
    resolved: list[tuple[str, str, Path]] = []
    for group in groups:
        for target_name in target_names:
            resolved.append((group, target_name, DIRECTORY_GROUPS[group][target_name]))
    return resolved


def _confirm(targets: list[tuple[str, str, Path]]) -> bool:
    print("\nSắp xoá nội dung trong các thư mục sau:")
    for group, target_name, path in targets:
        print(f"- {group}/{target_name}: {path}")
    print("\nLưu ý: chỉ giữ lại `.gitkeep`; các file/folder khác sẽ bị xoá.")

    raw = input("Nhập YES để xác nhận xoá: ").strip()
    return raw == "YES"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clear source/output cho compress, watermark, hardsub, video, heic."
    )
    parser.add_argument(
        "--tool",
        choices=[*DIRECTORY_GROUPS.keys(), "all"],
        help="Nhóm cần clear.",
    )
    parser.add_argument(
        "--target",
        choices=["source", "output", "both"],
        help="Thư mục cần clear.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Bỏ qua prompt xác nhận. Chỉ dùng khi chắc chắn.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    tool = args.tool
    if not tool:
        tool = _select_from_menu(
            "Chọn nhóm cần clear",
            [*DIRECTORY_GROUPS.keys(), "all"],
        )

    target = args.target
    if not target:
        target = _select_from_menu(
            "Chọn thư mục cần clear",
            ["source", "output", "both"],
        )

    targets = _resolve_targets(tool, target)
    if not args.yes and not _confirm(targets):
        print("Đã huỷ.")
        return

    total_files = 0
    total_dirs = 0
    for group, target_name, path in targets:
        try:
            deleted_files, deleted_dirs = _clear_directory(path)
        except Exception as exc:  # noqa: BLE001
            print(f"[fail] {group}/{target_name}: {exc}", file=sys.stderr)
            continue

        total_files += deleted_files
        total_dirs += deleted_dirs
        print(
            f"[ok] {group}/{target_name}: "
            f"xoá {deleted_files} file, {deleted_dirs} folder"
        )

    print(f"Hoàn tất clear: {total_files} file, {total_dirs} folder.")


if __name__ == "__main__":
    main()
