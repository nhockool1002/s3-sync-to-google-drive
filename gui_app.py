"""Entry point cho GUI (chạy trực tiếp hoặc build .app bằng PyInstaller)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _fix_path_for_gui_launch() -> None:
    """App mở bằng double-click trên macOS không kế thừa PATH từ shell
    (.zshrc/.bash_profile), nên các lệnh cài qua Homebrew như ffmpeg/ffprobe
    sẽ không tìm thấy được. Thêm thủ công các thư mục Homebrew phổ biến."""
    extra_dirs = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin"]
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    for d in extra_dirs:
        if d not in parts and os.path.isdir(d):
            parts.append(d)
    os.environ["PATH"] = os.pathsep.join(parts)


def main() -> None:
    _fix_path_for_gui_launch()

    # Khi đã đóng gói .app, app/dub_vi.py tự gọi lại chính binary này với cờ
    # --tts-worker để chạy 1 câu TTS trong 1 process riêng (xem app/tts_worker.py).
    if len(sys.argv) > 1 and sys.argv[1] == "--tts-worker":
        from app.tts_worker import main as tts_worker_main

        tts_worker_main(sys.argv[2:])
        return

    from dotenv import load_dotenv
    from PySide6.QtWidgets import QApplication

    from app.gui.main_window import MainWindow

    load_dotenv()
    app = QApplication(sys.argv)
    app.setApplicationName("Sync Toolbox")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
