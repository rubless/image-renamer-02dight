import sys
import os
import struct
import shutil
import logging
import datetime
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QListWidget, QFileDialog, QMessageBox,
    QComboBox
)
from PyQt5.QtCore import Qt

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# 지원하는 이미지 확장자 (set으로 빠른 검색)
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff"}


# ── EXIF 촬영 날짜 읽기 ──
def get_exif_date(path):
    """EXIF에서 DateTimeOriginal을 읽어 datetime으로 반환. 없으면 None."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None

            while True:
                marker_data = f.read(4)
                if not marker_data:
                    return None
                marker, size = struct.unpack(">HH", marker_data)

                if marker == 0xFFE1:
                    data = f.read(size - 2)
                    if data[:6] != b"Exif\x00\x00":
                        continue

                    tiff_header = data[6:]
                    endian = ">" if tiff_header[:2] == b"MM" else "<"

                    if struct.unpack(endian + "H", tiff_header[2:4])[0] != 42:
                        return None

                    ifd_offset = struct.unpack(endian + "I", tiff_header[4:8])[0]
                    return _search_exif_date(tiff_header, endian, ifd_offset)
                else:
                    f.seek(size - 2, 1)
    except Exception as e:
        logging.error(f"Error reading EXIF data from {path}: {e}")
        return None


def _search_exif_date(tiff_data, endian, ifd_offset):
    """IFD를 순회하며 DateTimeOriginal(0x9003)을 찾는다."""
    visited_offsets = set()
    offsets_to_process = [ifd_offset]

    while offsets_to_process:
        current_offset = offsets_to_process.pop()

        if current_offset in visited_offsets or current_offset >= len(tiff_data):
            continue
        visited_offsets.add(current_offset)

        num_entries_data = tiff_data[current_offset : current_offset + 2]
        if not num_entries_data or len(num_entries_data) < 2:
            continue

        n_entries = struct.unpack(endian + "H", num_entries_data)[0]

        for i in range(n_entries):
            entry_start = current_offset + 2 + i * 12
            if entry_start + 12 > len(tiff_data):
                break

            tag, typ, count = struct.unpack(endian + "HHI", tiff_data[entry_start : entry_start + 8])
            value_or_offset = struct.unpack(endian + "I", tiff_data[entry_start + 8 : entry_start + 12])[0]

            if tag == 0x8769:
                offsets_to_process.append(value_or_offset)

            if tag == 0x9003:
                if value_or_offset + 19 <= len(tiff_data):
                    try:
                        date_str = tiff_data[value_or_offset : value_or_offset + 19].decode("ascii", "ignore")
                        return datetime.datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        logging.warning(f"Could not parse EXIF date string: {date_str}")
                        return None
                else:
                    logging.warning("EXIF DateTimeOriginal offset out of bounds.")
                    return None

        next_ifd_offset_data = tiff_data[entry_start + 12 : entry_start + 16]
        if len(next_ifd_offset_data) == 4:
            next_ifd_offset = struct.unpack(endian + "I", next_ifd_offset_data)[0]
            if next_ifd_offset != 0:
                offsets_to_process.append(next_ifd_offset)

    return None


def get_sort_date(path):
    """촬영 날짜 우선 → 없으면 수정 날짜 반환."""
    exif_date = get_exif_date(path)
    if exif_date:
        return exif_date
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        logging.error(f"Could not get modification time for {path}")
        return datetime.datetime.min


class ImageRenamer(QWidget):
    def __init__(self):
        super().__init__()
        self.counter = 1
        self.output_folder = ""
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("이미지 순번 정리기 (2자리)")
        self.setFixedSize(600, 550)
        self.setAcceptDrops(True)

        layout = QVBoxLayout()

        # ── 저장 폴더 선택 영역 ──
        folder_layout = QHBoxLayout()
        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("저장할 폴더 경로를 선택하세요...")
        self.folder_input.setReadOnly(True)

        folder_btn = QPushButton("폴더 선택")
        folder_btn.clicked.connect(self.select_folder)

        folder_layout.addWidget(self.folder_input)
        folder_layout.addWidget(folder_btn)
        layout.addLayout(folder_layout)

        # ── 정렬 기준 선택 ──
        sort_layout = QHBoxLayout()
        sort_layout.addWidget(QLabel("정렬 기준:"))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "촬영 날짜 우선 (없으면 수정 날짜)",
            "파일 이름순",
            "수정 날짜순"
        ])
        sort_layout.addWidget(self.sort_combo)
        layout.addLayout(sort_layout)

        # ── 드래그 앤 드롭 안내 라벨 ──
        self.drop_label = QLabel("여기에 이미지를 드래그 앤 드롭하세요")
        self.drop_label.setAlignment(Qt.AlignCenter)
        self.drop_label.setFixedHeight(100)
        self.drop_label.setStyleSheet(
            "border: 2px dashed #aaa; font-size: 14px; color: #555;"
        )
        layout.addWidget(self.drop_label)

        # ── 처리 카운터 표시 ── ★ 변경: 01
        self.counter_label = QLabel("다음 번호: 01")
        self.counter_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #2a7ad5;")
        layout.addWidget(self.counter_label)

        # ── 처리 로그 리스트 ──
        self.log_list = QListWidget()
        layout.addWidget(self.log_list)

        # ── 카운터 초기화 버튼 ──
        reset_btn = QPushButton("카운터 초기화")
        reset_btn.clicked.connect(self.reset_counter)
        layout.addWidget(reset_btn)

        self.setLayout(layout)

    # ── 폴더 선택 ──
    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "저장 폴더 선택")
        if folder:
            self.output_folder = folder
            self.folder_input.setText(folder)
            logging.info(f"저장 폴더 설정: {folder}")

    # ── 카운터 초기화 ── ★ 변경: 01
    def reset_counter(self):
        self.counter = 1
        self.update_counter_label()
        self.log_list.addItem("── 카운터가 01로 초기화되었습니다 ──")
        logging.info("카운터 초기화됨")

    # ── 카운터 라벨 업데이트 ── ★ 변경: 02d
    def update_counter_label(self):
        self.counter_label.setText(f"다음 번호: {self.counter:02d}")

    # ── 드래그 진입 ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_label.setStyleSheet(
                "border: 2px dashed #2a7ad5; font-size: 14px; color: #2a7ad5; background-color: #e8f0fe;"
            )

    # ── 드래그 이탈 ──
    def dragLeaveEvent(self, event):
        self.drop_label.setStyleSheet(
            "border: 2px dashed #aaa; font-size: 14px; color: #555;"
        )

    # ── 파일 정렬 ──
    def sort_files(self, file_paths):
        """선택된 정렬 기준에 따라 파일 경로 리스트를 정렬하여 반환."""
        sort_idx = self.sort_combo.currentIndex()
        if sort_idx == 0:
            return sorted(file_paths, key=lambda p: get_sort_date(p))
        elif sort_idx == 1:
            return sorted(file_paths, key=lambda p: os.path.basename(p).lower())
        elif sort_idx == 2:
            return sorted(file_paths, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else float('inf'))
        return sorted(file_paths)

    # ── 드롭 처리 ──
    def dropEvent(self, event):
        self.drop_label.setStyleSheet(
            "border: 2px dashed #aaa; font-size: 14px; color: #555;"
        )

        if not self.output_folder:
            QMessageBox.warning(self, "경고", "먼저 저장할 폴더를 선택하세요.")
            return

        urls = event.mimeData().urls()
        file_paths = []
        for url in urls:
            local_path = url.toLocalFile()
            if local_path:
                file_paths.append(local_path)

        if not file_paths:
            self.log_list.addItem("⚠️ 드롭된 파일이 없습니다.")
            logging.warning("No valid file paths found in drop event.")
            return

        sorted_file_paths = self.sort_files(file_paths)

        sort_names = [
            "촬영 날짜 우선 (없으면 수정 날짜)",
            "파일 이름순",
            "수정 날짜순"
        ]
        sort_idx = self.sort_combo.currentIndex()
        self.log_list.addItem(f"── 정렬 기준: {sort_names[sort_idx]} ──")

        processed_count = 0
        for file_path in sorted_file_paths:
            if not os.path.exists(file_path):
                msg = f"⚠️ 원본 파일이 존재하지 않아 건너뜀: {os.path.basename(file_path)}"
                self.log_list.addItem(msg)
                logging.warning(msg)
                continue

            if os.path.isdir(file_path):
                msg = f"⚠️ 폴더는 지원하지 않습니다: {os.path.basename(file_path)}"
                self.log_list.addItem(msg)
                logging.warning(msg)
                continue

            _, ext = os.path.splitext(file_path)
            ext_lower = ext.lower()

            if ext_lower not in SUPPORTED_EXTENSIONS:
                msg = f"⚠️ 지원하지 않는 형식: {os.path.basename(file_path)}"
                self.log_list.addItem(msg)
                logging.warning(msg)
                continue

            # ★ 변경: 02d (2자리: 01, 02, ...)
            new_name = f"{self.counter:02d}{ext_lower}"
            dest_path = os.path.join(self.output_folder, new_name)

            if os.path.exists(dest_path):
                msg = f"⚠️ 대상 폴더에 이미 존재하여 건너뜀: {new_name}"
                self.log_list.addItem(msg)
                logging.warning(msg)
                continue

            try:
                shutil.copy2(file_path, dest_path)
                msg = f"✅ {os.path.basename(file_path)} → {new_name}"
                self.log_list.addItem(msg)
                logging.info(msg)
                processed_count += 1
                self.counter += 1
            except Exception as e:
                msg = f"❌ 복사 실패: {os.path.basename(file_path)} ({e})"
                self.log_list.addItem(msg)
                logging.error(msg)

        if processed_count > 0:
            self.update_counter_label()

        if processed_count == 0:
            self.log_list.addItem("── 처리할 유효한 이미지가 없었습니다. ──")
            logging.info("No valid images processed in this drop event.")
        else:
            self.log_list.addItem(f"── 총 {processed_count}개 파일 처리 완료 ──")
            logging.info(f"Successfully processed {processed_count} files.")

        self.log_list.scrollToBottom()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ImageRenamer()
    window.show()
    sys.exit(app.exec_())