from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from . import tasks
from .widgets import DropListWidget, Worker

VIDEO_EXT = [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"]
IMAGE_EXT = [".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp", ".tif", ".tiff"]
HEIC_EXT = [".heic", ".heif"]
SRT_EXT = [".srt"]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sync Toolbox")
        self.resize(900, 700)

        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(1)  # chạy 1 job 1 lúc, tránh log lẫn nhau
        self._active_buttons: list[QPushButton] = []

        splitter = QSplitter(Qt.Orientation.Vertical)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_subtitle_tab(), "Phụ đề")
        self.tabs.addTab(self._build_dub_tab(), "Lồng tiếng VI")
        self.tabs.addTab(self._build_hardsub_tab(), "Hardsub")
        self.tabs.addTab(self._build_split_tab(), "Chia video")
        self.tabs.addTab(self._build_compress_tab(), "Nén ảnh")
        self.tabs.addTab(self._build_watermark_tab(), "Watermark")
        self.tabs.addTab(self._build_heic_tab(), "Convert HEIC")

        splitter.addWidget(self.tabs)

        log_container = QWidget()
        log_layout = QVBoxLayout(log_container)
        log_layout.addWidget(QLabel("Log:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        log_layout.addWidget(self.log_view)
        splitter.addWidget(log_container)
        splitter.setSizes([480, 220])

        self.setCentralWidget(splitter)

    # ---------- helpers ----------

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for btn in self._active_buttons:
            btn.setEnabled(enabled)

    def _run_task(self, run_button: QPushButton, fn, *args, on_done=None, **kwargs) -> None:
        self._set_buttons_enabled(False)
        worker = Worker(fn, *args, **kwargs)
        worker.signals.log.connect(self._append_log)

        def _finished(ok: bool, message: str) -> None:
            self._set_buttons_enabled(True)
            if ok:
                self._append_log(f"✔ {message}")
                if on_done:
                    on_done(message)
            else:
                self._append_log(f"✘ Lỗi: {message}")
                QMessageBox.warning(self, "Lỗi", message)

        worker.signals.finished.connect(_finished)
        self.pool.start(worker)

    @staticmethod
    def _choose_dir(parent, line_edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(parent, "Chọn thư mục output")
        if path:
            line_edit.setText(path)

    @staticmethod
    def _choose_file(parent, line_edit: QLineEdit, filter_: str = "") -> None:
        path, _ = QFileDialog.getOpenFileName(parent, "Chọn file", filter=filter_)
        if path:
            line_edit.setText(path)

    def _output_dir_row(self) -> tuple[QHBoxLayout, QLineEdit]:
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText("(mặc định: cùng thư mục file gốc)")
        browse = QPushButton("Chọn...")
        browse.clicked.connect(lambda: self._choose_dir(self, edit))
        row.addWidget(edit)
        row.addWidget(browse)
        return row, edit

    # ---------- tab: Phụ đề (whisper) ----------

    def _build_subtitle_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Kéo-thả 1 file video/audio vào đây:"))

        drop = DropListWidget(extensions=VIDEO_EXT + [".mp3", ".wav", ".m4a"], multi=False)
        layout.addWidget(drop)

        form = QFormLayout()
        model = QComboBox()
        model.addItems(["tiny", "base", "small", "medium", "large"])
        model.setCurrentText("base")
        form.addRow("Model Whisper:", model)

        language = QLineEdit()
        language.setPlaceholderText("auto (để trống = tự nhận diện)")
        form.addRow("Ngôn ngữ gốc:", language)

        translate = QCheckBox("Dịch sang tiếng Việt sau khi nhận diện")
        form.addRow(translate)
        layout.addLayout(form)

        run_btn = QPushButton("Tạo phụ đề")
        self._active_buttons.append(run_btn)
        layout.addWidget(run_btn)

        def on_run() -> None:
            video = drop.first_path()
            if not video:
                QMessageBox.information(self, "Thiếu file", "Hãy kéo-thả 1 file video/audio.")
                return
            lang = language.text().strip() or None
            self._run_task(
                run_btn,
                tasks.task_subtitle,
                video,
                model.currentText(),
                lang,
                translate.isChecked(),
            )

        run_btn.clicked.connect(on_run)
        layout.addStretch()
        return w

    # ---------- tab: Lồng tiếng Việt (dub) ----------

    def _build_dub_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(
            QLabel(
                "Kéo-thả 1 file video. Có thể kèm 1 file .srt tiếng Việt có sẵn\n"
                "(nếu không có, sẽ tự nhận diện giọng nói rồi dịch sang tiếng Việt)."
            )
        )

        drop = DropListWidget(extensions=VIDEO_EXT + SRT_EXT, multi=True)
        layout.addWidget(drop)

        form = QFormLayout()
        voice = QComboBox()
        voice.addItem("Nam (NamMinh)", "vi-VN-NamMinhNeural")
        voice.addItem("Nữ (HoaiMy)", "vi-VN-HoaiMyNeural")
        form.addRow("Giọng đọc:", voice)

        whisper_model = QComboBox()
        whisper_model.addItems(["tiny", "base", "small", "medium"])
        whisper_model.setCurrentText("base")
        form.addRow("Model Whisper (khi không có SRT):", whisper_model)
        layout.addLayout(form)

        run_btn = QPushButton("Tạo video lồng tiếng Việt")
        self._active_buttons.append(run_btn)
        layout.addWidget(run_btn)

        def on_run() -> None:
            paths = drop.file_paths()
            videos = [p for p in paths if p.lower().endswith(tuple(VIDEO_EXT))]
            srts = [p for p in paths if p.lower().endswith(".srt")]
            if not videos:
                QMessageBox.information(self, "Thiếu file", "Hãy kéo-thả 1 file video.")
                return
            self._run_task(
                run_btn,
                tasks.task_dub,
                videos[0],
                srts[0] if srts else None,
                voice.currentData(),
                whisper_model.currentText(),
            )

        run_btn.clicked.connect(on_run)
        layout.addStretch()
        return w

    # ---------- tab: Hardsub ----------

    def _build_hardsub_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(
            QLabel(
                "Kéo-thả 1 file video + 1 file .srt (nếu không kèm sẽ tự tìm file .srt\n"
                "cùng tên bên cạnh video). Kết quả: hardsub + nén + watermark mờ góc."
            )
        )

        drop = DropListWidget(extensions=VIDEO_EXT + SRT_EXT, multi=True)
        layout.addWidget(drop)

        form = QFormLayout()
        mark_edit = QLineEdit()
        mark_edit.setPlaceholderText("(mặc định: app/watermark/mark.jpeg)")
        mark_row = QHBoxLayout()
        mark_browse = QPushButton("Chọn...")
        mark_browse.clicked.connect(lambda: self._choose_file(self, mark_edit, "Images (*.png *.jpg *.jpeg)"))
        mark_row.addWidget(mark_edit)
        mark_row.addWidget(mark_browse)
        form.addRow("Watermark:", mark_row)

        crf = QSpinBox()
        crf.setRange(0, 51)
        crf.setValue(28)
        form.addRow("CRF (nén, thấp = nét hơn, nặng hơn):", crf)

        preset = QComboBox()
        preset.addItems(["ultrafast", "fast", "medium", "slow", "veryslow"])
        preset.setCurrentText("medium")
        form.addRow("Preset encoder:", preset)
        layout.addLayout(form)

        run_btn = QPushButton("Hardsub + nén + watermark")
        self._active_buttons.append(run_btn)
        layout.addWidget(run_btn)

        def on_run() -> None:
            paths = drop.file_paths()
            videos = [p for p in paths if p.lower().endswith(tuple(VIDEO_EXT))]
            srts = [p for p in paths if p.lower().endswith(".srt")]
            if not videos:
                QMessageBox.information(self, "Thiếu file", "Hãy kéo-thả 1 file video.")
                return
            self._run_task(
                run_btn,
                tasks.task_hardsub,
                videos[0],
                srts[0] if srts else None,
                mark_edit.text().strip() or None,
                crf.value(),
                preset.currentText(),
            )

        run_btn.clicked.connect(on_run)
        layout.addStretch()
        return w

    # ---------- tab: Chia video ----------

    def _build_split_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Kéo-thả 1 file video vào đây:"))

        drop = DropListWidget(extensions=VIDEO_EXT, multi=False)
        layout.addWidget(drop)

        form = QFormLayout()
        max_minutes = QDoubleSpinBox()
        max_minutes.setRange(1.0, 600.0)
        max_minutes.setValue(59.0)
        form.addRow("Độ dài tối đa mỗi phần (phút):", max_minutes)
        layout.addLayout(form)

        run_btn = QPushButton("Chia thành 2 phần")
        self._active_buttons.append(run_btn)
        layout.addWidget(run_btn)

        def on_run() -> None:
            video = drop.first_path()
            if not video:
                QMessageBox.information(self, "Thiếu file", "Hãy kéo-thả 1 file video.")
                return
            self._run_task(run_btn, tasks.task_split, video, max_minutes.value())

        run_btn.clicked.connect(on_run)
        layout.addStretch()
        return w

    # ---------- tab: Nén ảnh ----------

    def _build_compress_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Kéo-thả 1 hoặc nhiều ảnh vào đây:"))

        drop = DropListWidget(extensions=IMAGE_EXT, multi=True)
        layout.addWidget(drop)

        form = QFormLayout()
        quality = QSpinBox()
        quality.setRange(1, 95)
        quality.setValue(72)
        form.addRow("Chất lượng JPEG:", quality)

        out_row, out_edit = self._output_dir_row()
        form.addRow("Thư mục output:", out_row)
        layout.addLayout(form)

        run_btn = QPushButton("Nén ảnh")
        self._active_buttons.append(run_btn)
        layout.addWidget(run_btn)

        def on_run() -> None:
            paths = drop.file_paths()
            if not paths:
                QMessageBox.information(self, "Thiếu file", "Hãy kéo-thả ít nhất 1 ảnh.")
                return
            self._run_task(
                run_btn,
                tasks.task_compress,
                paths,
                quality.value(),
                out_edit.text().strip() or None,
            )

        run_btn.clicked.connect(on_run)
        layout.addStretch()
        return w

    # ---------- tab: Watermark ----------

    def _build_watermark_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Kéo-thả 1 hoặc nhiều ảnh vào đây:"))

        drop = DropListWidget(extensions=IMAGE_EXT, multi=True)
        layout.addWidget(drop)

        form = QFormLayout()
        mark_edit = QLineEdit()
        mark_edit.setPlaceholderText("(mặc định: app/watermark/mark.jpeg)")
        mark_row = QHBoxLayout()
        mark_browse = QPushButton("Chọn...")
        mark_browse.clicked.connect(lambda: self._choose_file(self, mark_edit, "Images (*.png *.jpg *.jpeg)"))
        mark_row.addWidget(mark_edit)
        mark_row.addWidget(mark_browse)
        form.addRow("Watermark mới:", mark_row)

        out_row, out_edit = self._output_dir_row()
        form.addRow("Thư mục output:", out_row)
        layout.addLayout(form)

        run_btn = QPushButton("Xoá watermark cũ + chèn watermark mới")
        self._active_buttons.append(run_btn)
        layout.addWidget(run_btn)

        def on_run() -> None:
            paths = drop.file_paths()
            if not paths:
                QMessageBox.information(self, "Thiếu file", "Hãy kéo-thả ít nhất 1 ảnh.")
                return
            self._run_task(
                run_btn,
                tasks.task_watermark,
                paths,
                mark_edit.text().strip() or None,
                out_edit.text().strip() or None,
            )

        run_btn.clicked.connect(on_run)
        layout.addStretch()
        return w

    # ---------- tab: Convert HEIC ----------

    def _build_heic_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(QLabel("Kéo-thả 1 hoặc nhiều file HEIC/HEIF vào đây:"))

        drop = DropListWidget(extensions=HEIC_EXT, multi=True)
        layout.addWidget(drop)

        form = QFormLayout()
        fmt = QComboBox()
        fmt.addItems(["jpeg", "png", "webp", "avif"])
        form.addRow("Định dạng output:", fmt)

        out_row, out_edit = self._output_dir_row()
        form.addRow("Thư mục output:", out_row)
        layout.addLayout(form)

        run_btn = QPushButton("Convert")
        self._active_buttons.append(run_btn)
        layout.addWidget(run_btn)

        def on_run() -> None:
            paths = drop.file_paths()
            if not paths:
                QMessageBox.information(self, "Thiếu file", "Hãy kéo-thả ít nhất 1 file HEIC/HEIF.")
                return
            self._run_task(
                run_btn,
                tasks.task_heic,
                paths,
                fmt.currentText(),
                out_edit.text().strip() or None,
            )

        run_btn.clicked.connect(on_run)
        layout.addStretch()
        return w
