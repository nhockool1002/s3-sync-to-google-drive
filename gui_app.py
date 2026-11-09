"""Entry point cho GUI (chạy trực tiếp hoặc build .app bằng PyInstaller)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
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
