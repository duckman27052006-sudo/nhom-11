"""Cửa sổ chính của GUI TFTP.

Bố cục chia theo **4 trang** thay vì dồn tất cả vào một màn hình dài:

    0. Tổng quan      — trạng thái server, số liệu, lối tắt
    1. Truyền file   — Tải về | Gửi lên (dùng chung một ô "đích đến")
    2. Máy chủ       — cấu hình đầy đủ + bảng file trong thư mục gốc
    3. Nhật ký       — log thời gian thực, lọc và lưu file

Server chạy ở QThread, truyền file chạy ở worker thread nên giao diện không
bao giờ bị treo. Chuyển trang dùng `motion`; màu sắc lấy từ
`theme` — toàn bộ bề mặt tự vẽ, không dùng stylesheet (xem `theme` để biết lý do).
"""

from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import QObject, QPropertyAnimation, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..tftp_client import TftpClient, _parse_server
from ..tftp_errors import TftpError
from ..tftp_server import TftpServer
from . import motion, theme
from .components import (
    ActionTile,
    ActivityFeed,
    BrandMark,
    Card,
    CardHeader,
    ChevronComboBox,
    DataTable,
    DropZone,
    ErrorBanner,
    Field,
    FilePicker,
    NumberInput,
    ShimmerProgressBar,
    StatTile,
    StatusPill,
    Surface,
    ThemedButton,
    Toast,
    make_button,
    restyle_tree,
)
from .logging_bridge import LogBridge

APP_TITLE = "TFTP Console"
MODE_ITEMS = ("octet", "netascii")
MAX_LOG_LINES = 1000

PAGE_OVERVIEW = 0
PAGE_TRANSFER = 1
PAGE_SERVER = 2
PAGE_LOG = 3
NAV_ITEMS = ("Tổng quan", "Truyền file", "Máy chủ", "Nhật ký")


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def muted(widget: QWidget, key: str = "text_muted") -> QWidget:
    """Tô màu chữ theo token (thay cho QSS)."""
    widget.setStyleSheet(f"color: {theme.current_tokens()[key]};")
    return widget


# ------------------------------------------------------------------- workers ---
class ServerThread(QThread):
    """Chạy `TftpServer.serve_forever` ngoài thread giao diện."""

    failed = Signal(str)
    ready = Signal(int)

    def __init__(self, server: TftpServer, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.server = server

    def run(self) -> None:  # noqa: D102
        try:
            self.ready.emit(self.server.bound_port)
            self.server.serve_forever()
        except OSError as exc:
            self.failed.emit(str(exc))


class TransferWorker(QObject):
    """Chạy download/upload ở thread riêng, báo tiến trình về UI."""

    progress = Signal(int, object)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, client: TftpClient, direction: str, source: str, target: str) -> None:
        super().__init__()
        self.client = client
        self.direction = direction
        self.source = source
        self.target = target

    @Slot()
    def run(self) -> None:
        try:
            if self.direction == "download":
                result = self.client.download(self.source, self.target, progress=self._emit)
            else:
                result = self.client.upload(self.source, self.target, progress=self._emit)
        except TftpError as exc:
            self.failed.emit(str(exc))
        except OSError as exc:
            self.failed.emit(f"Lỗi hệ thống: {exc}")
        else:
            self.finished_ok.emit(result)

    def _emit(self, done: int, total: int | None) -> None:
        self.progress.emit(done, total)


# ------------------------------------------------------------------ top bar ---
class TopBar(Surface):
    """Thanh trên: thương hiệu + 4 tab điều hướng + trạng thái + đổi theme."""

    nav_changed = Signal(int)
    theme_toggled = Signal()

    def __init__(self) -> None:
        super().__init__(border_sides=(False, False, True, False))
        self.setFixedHeight(68)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(12)

        layout.addWidget(BrandMark("T", size=34, radius=10))

        titles = QVBoxLayout()
        titles.setSpacing(1)
        titles.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        name = QLabel("TFTP Console")
        name.setFont(theme.bold(11))
        titles.addWidget(name)
        tag = QLabel(f"RFC 1350 · v{__version__}")
        tag.setFont(theme.font(8))
        tag.setStyleSheet(f"color: {theme.current_tokens()['text_subtle']};")
        titles.addWidget(tag)
        layout.addLayout(titles)
        layout.addSpacing(12)

        self.nav_buttons: list[ThemedButton] = []
        nav_box = Surface(self, bg_key="surface_sunken", border_key="border", radius=10)
        nav_layout = QHBoxLayout(nav_box)
        nav_layout.setContentsMargins(4, 4, 4, 4)
        nav_layout.setSpacing(2)
        for index, label in enumerate(NAV_ITEMS):
            button = ThemedButton(label, minimum_height=32)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, i=index: self.nav_changed.emit(i))
            self.nav_buttons.append(button)
            nav_layout.addWidget(button)
        layout.addWidget(nav_box)
        layout.addStretch(1)

        self.status = StatusPill("Server đang tắt", "idle", width=186)
        layout.addWidget(self.status)

        self.theme_button = ThemedButton("Dark", "ghost", "Đổi nền sáng / tối", 30)
        self.theme_button.clicked.connect(self.theme_toggled)
        layout.addWidget(self.theme_button)

        self.select_nav(0)

    def select_nav(self, index: int) -> None:
        for position, button in enumerate(self.nav_buttons):
            button.setVariant("seg_active" if position == index else "ghost")
            button.setChecked(position == index)
            button.update()

    def set_status(self, state: str, text: str) -> None:
        self.status.set_state(state, text)


# -------------------------------------------------------------- status bar ---
class StatusBar(Surface):
    """Dải dưới cùng: thông báo ngắn + gợi ý phím tắt."""

    def __init__(self) -> None:
        super().__init__(border_sides=(True, False, False, False))
        self.setFixedHeight(30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(12)
        self.message = QLabel("Sẵn sàng — chọn file để bắt đầu")
        self.message.setFont(theme.font(8, mono=True))
        self.message.setStyleSheet(f"color: {theme.current_tokens()['text_muted']};")
        layout.addWidget(self.message, 1)
        self.hint = QLabel("Ctrl+L: xóa nhật ký · Ctrl+K: dừng server")
        self.hint.setFont(theme.font(8, mono=True))
        self.hint.setStyleSheet(f"color: {theme.current_tokens()['text_subtle']};")
        layout.addWidget(self.hint)


# -------------------------------------------------------------- main window ---
class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1320, 860)
        self.setMinimumSize(1000, 660)
        self.setAutoFillBackground(True)
        self.palette().setColor(self.backgroundRole(), theme.current_tokens()["bg"])
        self.setPalette(self.palette())

        self.server: TftpServer | None = None
        self.server_thread: ServerThread | None = None
        self.transfer_thread: QThread | None = None
        self.transfer_worker: TransferWorker | None = None
        self.theme_name = "light"
        self._intro_fade: QPropertyAnimation | None = None
        self._log_lines: list[tuple[str, str]] = []

        self.log_bridge = LogBridge(self)
        self.uptime = motion.Ticker(1000, self)
        self.uptime.tick.connect(self._on_tick)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.top_bar = TopBar()
        self.top_bar.nav_changed.connect(self.go_to)
        self.top_bar.theme_toggled.connect(self._toggle_theme)
        root.addWidget(self.top_bar)

        self.stack = motion.PageStack()
        self.stack.changed.connect(self._on_page_changed)
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_overview())
        self.stack.addWidget(self._build_transfer())
        self.stack.addWidget(self._build_server_page())
        self.stack.addWidget(self._build_log_page())

        self.status_bar = StatusBar()
        root.addWidget(self.status_bar)
        self.toast = Toast(self)
        self.toast.hide()

        self.log_bridge.attach(callback=self._on_log)
        self._install_shortcuts()
        self._set_running(False)
        self._set_backgrounds()
        self._refresh_files(quiet=True)
        # Feed "Hoạt động gần đây" không bao giờ trống: có sẵn 1 dòng chào.
        logging.getLogger("tftp.gui").info(
            "giao diện sẵn sàng — thư mục gốc: %s", self._root_path()
        )

    def _install_shortcuts(self) -> None:
        for sequence, slot in (("Ctrl+L", self._clear_log), ("Ctrl+K", self._stop_server)):
            action = QAction(self)
            action.setShortcut(sequence)
            action.triggered.connect(slot)
            self.addAction(action)

    @staticmethod
    def _page(content: QWidget) -> QScrollArea:
        """Bọc nội dung trong vùng cuộn để cửa sổ nhỏ không cắt form.

        Nội dung dàn full chiều rộng trang (không bó giữa max-width).
        """
        content.setAutoFillBackground(True)
        palette = content.palette()
        palette.setColor(content.backgroundRole(), theme.current_tokens()["bg"])
        content.setPalette(palette)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    # ================================================== trang 0: Tổng quan ===
    def _build_overview(self) -> QWidget:
        content = QWidget()
        grid = QVBoxLayout(content)
        grid.setContentsMargins(24, 24, 24, 28)
        grid.setSpacing(20)

        hero = Card(elevated=True)
        hero_layout = QHBoxLayout()
        hero_layout.setSpacing(20)

        hero_text = QVBoxLayout()
        hero_text.setSpacing(4)
        self.hero_title = QLabel("Server đang tắt")
        self.hero_title.setFont(theme.bold(15))
        hero_text.addWidget(self.hero_title)
        self.hero_detail = QLabel(
            "Bấm “Khởi động server” để phục vụ file trong thư mục gốc."
        )
        self.hero_detail.setFont(theme.font(9))
        muted(self.hero_detail)
        self.hero_detail.setWordWrap(True)
        hero_text.addWidget(self.hero_detail)

        self.hero_clock = QLabel("00:00")
        self.hero_clock.setFont(theme.bold(9.5, mono=True))
        self.hero_clock.setStyleSheet(f"color: {theme.current_tokens()['primary']};")
        hero_text.addWidget(self.hero_clock)
        hero_layout.addLayout(hero_text, 1)

        self.hero_start = make_button("Khởi động server", "primary")
        self.hero_start.setMinimumHeight(44)
        self.hero_start.setMinimumWidth(168)
        self.hero_start.clicked.connect(self._start_server)
        self.hero_stop = make_button("Dừng server", "danger")
        self.hero_stop.setMinimumHeight(44)
        self.hero_stop.setMinimumWidth(148)
        self.hero_stop.clicked.connect(self._stop_server)
        self.hero_files = make_button("Mở thư mục", "tertiary")
        self.hero_files.setMinimumHeight(36)
        self.hero_files.clicked.connect(self._open_root_folder)
        actions = QVBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.hero_start)
        actions.addWidget(self.hero_stop)
        actions.addWidget(self.hero_files)
        hero_layout.addLayout(actions, 0)
        hero.add_layout(hero_layout)
        grid.addWidget(hero)
        self.hero_card = hero

        mid = QHBoxLayout()
        mid.setSpacing(16)
        stats_box = QWidget()
        stats_grid = QGridLayout(stats_box)
        stats_grid.setContentsMargins(0, 0, 0, 0)
        stats_grid.setSpacing(16)
        self.stat_files = StatTile("File trong root")
        self.stat_size = StatTile("Tổng dung lượng")
        self.stat_sessions = StatTile("Phiên đang mở")
        self.stat_blksize = StatTile("Blksize")
        stats_grid.addWidget(self.stat_files, 0, 0)
        stats_grid.addWidget(self.stat_size, 0, 1)
        stats_grid.addWidget(self.stat_sessions, 1, 0)
        stats_grid.addWidget(self.stat_blksize, 1, 1)
        mid.addWidget(stats_box, 1)

        activity = Card()
        activity.add(CardHeader("Hoạt động gần đây", "6 dòng log mới nhất."))
        self.activity = ActivityFeed(6)
        activity.add(self.activity)
        self.activity_card = activity
        mid.addWidget(activity, 1)
        grid.addLayout(mid)

        grid.addWidget(self._section_label("Lối tắt nhanh"))
        shortcuts = QHBoxLayout()
        shortcuts.setSpacing(16)
        self.tile_download = ActionTile(
            "Tải file về", "Chọn file trên server và tải xuống máy.", "down"
        )
        self.tile_download.clicked.connect(lambda: self.go_to(PAGE_TRANSFER))
        self.tile_upload = ActionTile(
            "Gửi file lên", "Kéo thả file hoặc chọn từ đĩa để ghi lên server.", "upload"
        )
        self.tile_upload.clicked.connect(lambda: self.go_to(PAGE_TRANSFER))
        for tile in (self.tile_download, self.tile_upload):
            shortcuts.addWidget(tile, 1)
        links = QVBoxLayout()
        links.setSpacing(4)
        self.link_server = make_button("Cấu hình máy chủ", "tertiary")
        self.link_server.clicked.connect(lambda: self.go_to(PAGE_SERVER))
        self.link_log = make_button("Nhật ký", "tertiary")
        self.link_log.clicked.connect(lambda: self.go_to(PAGE_LOG))
        links.addWidget(self.link_server)
        links.addWidget(self.link_log)
        links.addStretch(1)
        shortcuts.addLayout(links, 0)
        grid.addLayout(shortcuts)

        grid.addStretch(1)
        return self._page(content)

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setFont(theme.medium(8.5))
        label.setStyleSheet(f"color: {theme.current_tokens()['text_subtle']};")
        return label

    # ================================================= trang 1: Truyền ===
    def _build_transfer(self) -> QWidget:
        content = QWidget()
        grid = QVBoxLayout(content)
        grid.setContentsMargins(24, 24, 24, 28)
        grid.setSpacing(20)

        # Một hàng địa chỉ dùng chung cho cả hai chiều: trước đây phải gõ 2 lần,
        # nay gọn thành một hàng (không còn card riêng).
        target_row = QHBoxLayout()
        target_row.setSpacing(12)
        target_label = QLabel("Đích đến")
        target_label.setFont(theme.medium(9))
        muted(target_label)
        target_row.addWidget(target_label)
        self.target_edit = QLineEdit("127.0.0.1:6969")
        self.target_edit.setMinimumHeight(40)
        self.target_edit.setPlaceholderText("127.0.0.1:6969")
        self.target_edit.editingFinished.connect(self._refresh_server_files)
        target_row.addWidget(self.target_edit, 1)
        sync_button = make_button("Dùng server đang chạy", "subtle")
        sync_button.clicked.connect(self._use_running_server)
        target_row.addWidget(sync_button, 0)
        grid.addLayout(target_row)

        columns = QHBoxLayout()
        columns.setSpacing(20)
        self.download_card = self._build_download_card()
        self.upload_card = self._build_upload_card()
        columns.addWidget(self.download_card, 1)
        columns.addWidget(self.upload_card, 1)
        grid.addLayout(columns)

        server_card = Card()
        head_row = QHBoxLayout()
        head_row.setSpacing(12)
        head_row.addWidget(
            CardHeader("File trên server", "Bấm một hàng để điền vào ô tải về."), 1
        )
        refresh_files = make_button("Làm mới", "subtle", minimum_height=34)
        refresh_files.clicked.connect(self._refresh_server_files)
        head_row.addWidget(refresh_files, 0, Qt.AlignmentFlag.AlignVCenter)
        server_card.add_layout(head_row)
        # Nguồn hiện tại: thư mục gốc trên máy này (dây tới list_files từ xa
        # khi backend có tính năng liệt kê của server).
        self.server_files = DataTable(
            ["Tên file", "Kích thước"], minimum_height=200, numeric_columns=(1,)
        )
        self.server_files.table.cellClicked.connect(self._on_server_file_clicked)
        server_card.add(self.server_files)
        self.server_files_card = server_card
        grid.addWidget(server_card)

        grid.addStretch(1)
        return self._page(content)

    def _build_download_card(self) -> Card:
        card = Card()
        header = CardHeader("Tải về", "RRQ — server đẩy file về máy này.")
        self.download_header = header
        card.add(header)

        self.download_name = QLineEdit()
        self.download_name.setPlaceholderText("sample.bin")
        self.download_name.setMinimumHeight(34)
        self.download_save = FilePicker("Lưu thành", save_mode=True)
        self.download_mode = ChevronComboBox()
        self.download_mode.addItems(MODE_ITEMS)
        self.download_mode.setFixedWidth(200)
        self.download_blksize = NumberInput(8, 65464, 1468, width=120)

        form = QVBoxLayout()
        form.setSpacing(12)
        form.addWidget(Field("Tên file trên server", self.download_name))
        form.addWidget(self.download_save)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(Field("Chế độ", self.download_mode))
        row.addWidget(Field("blksize", self.download_blksize))
        row.addStretch(1)
        form.addLayout(row)

        self.download_button = make_button("Tải về", "primary")
        self.download_button.setMinimumHeight(40)
        self.download_button.clicked.connect(self._start_download)

        self.download_banner = ErrorBanner()
        self.download_progress = ShimmerProgressBar()
        self.download_status = QLabel("Chưa tải file nào trong phiên này")
        self.download_status.setFont(theme.font(8.5))
        muted(self.download_status, "text_subtle")
        self.download_status.setWordWrap(True)
        card.add_layout(form)
        # Khoảng trống dồn lên đây (không nở header) để nút bám đáy card,
        # thẳng hàng với nút Gửi lên ở card cạnh.
        card.add_stretch(1)
        card.add(self.download_button)
        card.add(self.download_banner)
        card.add(self.download_progress)
        card.add(self.download_status)
        return card

    def _build_upload_card(self) -> Card:
        card = Card()
        header = CardHeader("Gửi lên", "WRQ — máy này đẩy file lên server.")
        self.upload_header = header
        card.add(header)

        self.upload_pick = FilePicker("Chọn file nguồn")
        self.upload_remote = QLineEdit()
        self.upload_remote.setPlaceholderText("uploaded.bin")
        self.upload_remote.setMinimumHeight(34)
        self.upload_mode = ChevronComboBox()
        self.upload_mode.addItems(MODE_ITEMS)
        self.upload_mode.setFixedWidth(200)
        self.upload_blksize = NumberInput(8, 65464, 1468, width=120)
        self.upload_drop = DropZone("Kéo file vào đây\nhoặc bấm để chọn")
        self.upload_drop.fileDropped.connect(self._on_upload_dropped)

        form = QVBoxLayout()
        form.setSpacing(12)
        form.addWidget(self.upload_pick)
        form.addWidget(Field("Tên trên server", self.upload_remote))
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(Field("Chế độ", self.upload_mode))
        row.addWidget(Field("blksize", self.upload_blksize))
        row.addStretch(1)
        form.addLayout(row)
        form.addWidget(self.upload_drop)

        self.upload_button = make_button("Gửi lên", "primary")
        self.upload_button.setMinimumHeight(40)
        self.upload_button.clicked.connect(self._start_upload)

        self.upload_banner = ErrorBanner()
        self.upload_progress = ShimmerProgressBar()
        self.upload_status = QLabel("Chưa gửi file nào trong phiên này")
        self.upload_status.setFont(theme.font(8.5))
        muted(self.upload_status, "text_subtle")
        self.upload_status.setWordWrap(True)
        card.add_layout(form)
        # Nút bám đáy card, thẳng hàng với nút Tải về (khoảng trống dồn ở đây).
        card.add_stretch(1)
        card.add(self.upload_button)
        card.add(self.upload_banner)
        card.add(self.upload_progress)
        card.add(self.upload_status)
        return card

    # ================================================ trang 2: Máy chủ ===
    def _build_server_page(self) -> QWidget:
        content = QWidget()
        grid = QVBoxLayout(content)
        grid.setContentsMargins(24, 24, 24, 28)
        grid.setSpacing(20)
        grid.addWidget(self._build_server_card())

        files = Card()
        self.files_header = CardHeader(
            "Thư mục gốc", "Nội dung đang được phục vụ. Bấm đúp để điền vào ô tải về."
        )
        files.add(self.files_header)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.files_hint = QLabel("")
        self.files_hint.setFont(theme.font(8.5))
        muted(self.files_hint, "text_subtle")
        actions.addWidget(self.files_hint, 1)
        refresh = make_button("Làm mới", "subtle", "Đọc lại danh sách file trong thư mục gốc")
        refresh.clicked.connect(lambda: self._refresh_files(quiet=False))
        open_folder = make_button("Mở thư mục", "ghost")
        open_folder.clicked.connect(self._open_root_folder)
        actions.addWidget(refresh)
        actions.addWidget(open_folder)
        files.add_layout(actions)

        self.files_table = DataTable(
            ["Tên file", "Kích thước", "Sửa lần cuối"],
            minimum_height=260,
            numeric_columns=(1,),
        )
        self.files_table.table.itemDoubleClicked.connect(self._on_file_activated)
        self.files_table.empty.actioned.connect(self._pick_root)
        files.add(self.files_table)
        grid.addWidget(files, 1)
        return self._page(content)

    def _build_server_card(self) -> Card:
        card = Card()
        header = CardHeader(
            "Cấu hình máy chủ",
            "Áp dụng khi bấm Khởi động. TID riêng cho mỗi phiên theo RFC 1350 §4.2.",
        )
        self.server_header = header
        card.add(header)

        self.host_edit = QLineEdit("0.0.0.0")
        self.host_edit.setMinimumHeight(34)
        self.port_spin = NumberInput(1, 65535, 6969, width=110)
        self.root_edit = QLineEdit("files")
        self.root_edit.setMinimumHeight(34)
        self.root_edit.editingFinished.connect(lambda: self._refresh_files(quiet=True))
        self.root_edit.textChanged.connect(self._sync_root_tooltip)
        self.root_edit.setToolTip("files")
        self.root_edit.setCursorPosition(len("files"))
        browse = make_button("Chọn...", "subtle", minimum_height=34)
        browse.clicked.connect(self._pick_root)

        root_row = QWidget()
        root_layout = QHBoxLayout(root_row)
        root_layout.setSpacing(8)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(self.root_edit, 1)
        root_layout.addWidget(browse, 0)

        # Hàng lắng nghe: host chiếm 3/4, cổng 1/4 (đỡ chênh độ dài).
        listen_row = QHBoxLayout()
        listen_row.setSpacing(12)
        listen_row.addWidget(Field("Địa chỉ lắng nghe", self.host_edit), 3)
        listen_row.addWidget(Field("Cổng UDP", self.port_spin, "69 cần quyền admin"), 1)
        card.add_layout(listen_row)

        card.add(Field("Thư mục gốc", root_row))

        # Ba giới hạn chia 3 cột bằng nhau tuyệt đối.
        self.blksize_spin = NumberInput(8, 65464, 1468)
        self.max_clients_spin = NumberInput(1, 4096, 32)
        self.max_size_spin = NumberInput(
            0, 2_147_483_647, 0, suffix=" byte", special_text="không giới hạn"
        )
        limits = QGridLayout()
        limits.setHorizontalSpacing(12)
        limits.setColumnStretch(0, 1)
        limits.setColumnStretch(1, 1)
        limits.setColumnStretch(2, 1)
        limits.addWidget(Field("Blksize mặc định", self.blksize_spin, "8..65464"), 0, 0)
        limits.addWidget(Field("Client tối đa", self.max_clients_spin), 0, 1)
        limits.addWidget(
            Field("Giới hạn upload", self.max_size_spin, "0 = không giới hạn"), 0, 2
        )
        card.add_layout(limits)

        options_label = QLabel("Tùy chọn")
        options_label.setFont(theme.medium(8.5))
        muted(options_label, "text_subtle")
        card.add(options_label)

        toggles = QGridLayout()
        toggles.setHorizontalSpacing(20)
        toggles.setVerticalSpacing(10)
        toggles.setColumnStretch(0, 1)
        toggles.setColumnStretch(1, 1)
        self.no_overwrite = QCheckBox("Không ghi đè")
        self.no_options = QCheckBox("Tắt option RFC 2347")
        self.allow_subdirs = QCheckBox("Cho phép thư mục con")
        self.low_rate = QCheckBox("Giới hạn 100 request/s")
        self.low_rate.setChecked(True)
        for box in (self.no_overwrite, self.no_options, self.allow_subdirs, self.low_rate):
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            box.setMinimumHeight(26)
        toggles.addWidget(self.no_overwrite, 0, 0)
        toggles.addWidget(self.no_options, 0, 1)
        toggles.addWidget(self.allow_subdirs, 1, 0)
        toggles.addWidget(self.low_rate, 1, 1)

        card.add_layout(toggles)
        return card

    # ================================================== trang 3: Nhật ký ===
    def _build_log_page(self) -> QWidget:
        page = QWidget()
        page.setAutoFillBackground(True)
        palette = page.palette()
        palette.setColor(page.backgroundRole(), theme.current_tokens()["bg"])
        page.setPalette(palette)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        title = QLabel("Nhật ký thời gian thực")
        title.setFont(theme.bold(11))
        toolbar.addWidget(title)
        self.log_count = QLabel("")
        self.log_count.setFont(theme.font(8.5))
        muted(self.log_count, "text_subtle")
        toolbar.addWidget(self.log_count)
        toolbar.addStretch(1)

        self.log_filter = ChevronComboBox()
        self.log_filter.addItems(["Tất cả", "Thông tin", "Cảnh báo + lỗi"])
        self.log_filter.setFixedWidth(170)
        self.log_filter.currentIndexChanged.connect(self._rebuild_log)
        toolbar.addWidget(self.log_filter)

        self.log_autoscroll = QCheckBox("Tự cuộn")
        self.log_autoscroll.setChecked(True)
        self.log_autoscroll.setCursor(Qt.CursorShape.PointingHandCursor)
        toolbar.addWidget(self.log_autoscroll)
        bottom_button = make_button("Về cuối", "ghost")
        bottom_button.clicked.connect(self._scroll_log_to_end)
        toolbar.addWidget(bottom_button)

        save_button = make_button("Lưu ra file", "ghost")
        save_button.clicked.connect(self._save_log)
        clear_button = make_button("Xóa", "ghost")
        clear_button.clicked.connect(self._clear_log)
        toolbar.addWidget(save_button)
        toolbar.addWidget(clear_button)
        layout.addLayout(toolbar)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_view.setFont(theme.font(9, mono=True))
        self._style_terminal()
        layout.addWidget(self.log_view, 1)

        note = QLabel(
            "Server và client đều chạy ở thread riêng; nhật ký ở đây nhận trực tiếp "
            "từ logging của Python nên không bỏ sót gói tin nào."
        )
        note.setFont(theme.font(8.5))
        muted(note, "text_subtle")
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _style_terminal(self) -> None:
        tokens = theme.current_tokens()
        self.log_view.setStyleSheet(
            f"QTextEdit {{ background: {tokens['terminal']};"
            f" color: {tokens['log_info']};"
            f" border: 1px solid {tokens['terminal_border']};"
            f" border-radius: 10px; padding: 10px; }}"
        )

    # --------------------------------------------------------------- logging ---
    @Slot(str, str)
    def _on_log(self, level: str, text: str) -> None:
        self._log_lines.append((level, text))
        if len(self._log_lines) > MAX_LOG_LINES:
            del self._log_lines[: len(self._log_lines) - MAX_LOG_LINES]
            self._refresh_log_view()
        else:
            self._append_log_line(level, text)
            self.log_count.setText(f"{len(self._log_lines)} dòng")
        self.activity.push(level, text)
        if self.log_autoscroll.isChecked():
            self._scroll_log_to_end()
        if self.log_filter.currentIndex() != 0:
            self._rebuild_log()
        if level in ("ERROR", "CRITICAL"):
            self.status_bar.message.setText(text[-90:])

    @staticmethod
    def _log_color(level: str, tokens: dict[str, str]) -> str:
        return {
            "DEBUG": tokens["log_debug"],
            "INFO": tokens["log_info"],
            "WARNING": tokens["log_warning"],
            "ERROR": tokens["log_error"],
            "CRITICAL": tokens["log_error"],
        }.get(level, tokens["log_info"])

    def _append_log_line(self, level: str, text: str) -> None:
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        color = self._log_color(level, theme.current_tokens())
        self.log_view.append(f'<span style="color:{color}">{safe}</span>')

    def _refresh_log_view(self) -> None:
        """Vẽ lại toàn bộ log từ `_log_lines`: đổi màu theo theme + cắt tràn."""
        self.log_view.clear()
        for level, text in self._log_lines:
            self._append_log_line(level, text)
        self.log_count.setText(f"{len(self._log_lines)} dòng")
        self._rebuild_log()

    @Slot()
    def _rebuild_log(self) -> None:
        """Lọc nhật ký theo mức độ chọn ở thanh công cụ."""
        mode = self.log_filter.currentIndex()
        block = self.log_view.document().begin()
        while block.isValid():
            text = block.text()
            if mode == 0:
                keep = True
            elif mode == 1:
                keep = " INFO " in text or "DEBUG" in text
            else:
                keep = any(level in text for level in ("WARNING", "ERROR", "CRITICAL"))
            block.setVisible(keep)
            block = block.next()

    @Slot()
    def _scroll_log_to_end(self) -> None:
        bar = self.log_view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.maximum())

    @Slot()
    def _clear_log(self) -> None:
        self._log_lines.clear()
        self.log_view.clear()
        self.log_count.setText("")

    @Slot()
    def _save_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Lưu nhật ký", "tftp-gui.log", "Log file (*.log);;Text (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.log_view.toPlainText())
        except OSError as exc:
            self.status_bar.message.setText(f"Không lưu được nhật ký: {exc}")
            return
        self.status_bar.message.setText(f"Đã lưu nhật ký: {path}")

    # ------------------------------------------------------------ navigation ---
    @Slot(int)
    def go_to(self, index: int) -> None:
        if index != self.stack.currentIndex():
            self.stack.drift_to(index)

    @Slot(int)
    def _on_page_changed(self, index: int) -> None:
        self.top_bar.select_nav(index)

    # ---------------------------------------------------------------- theme ---
    @Slot()
    def _toggle_theme(self) -> None:
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, self.theme_name)
        self.top_bar.theme_button.setText("Light" if self.theme_name == "dark" else "Dark")
        self._style_terminal()
        self._refresh_log_view()
        restyle_tree(self)
        self._set_backgrounds()
        self._refresh_server_files()

    def _set_backgrounds(self) -> None:
        """Đặt nền cho các container gốc + ghim palette cho input hệ điều hành."""
        bg = theme.current_tokens()["bg"]
        for widget in (self, *self.stack.findChildren(QScrollArea)):
            widget.setAutoFillBackground(True)
            palette = widget.palette()
            palette.setColor(widget.backgroundRole(), bg)
            widget.setPalette(palette)
        for index in range(self.stack.count()):
            page = self.stack.widget(index)
            content = page if isinstance(page, QWidget) else None
            if content is None:
                continue
            target = content.widget() if isinstance(content, QScrollArea) else content
            if target is None:
                continue
            target.setAutoFillBackground(True)
            palette = target.palette()
            palette.setColor(target.backgroundRole(), bg)
            target.setPalette(palette)
        # Mọi ô nhập/bảng/header: nền surface, không ăn màu bg kế thừa.
        for kind in (QLineEdit, QSpinBox, QComboBox):
            for control in self.findChildren(kind):
                theme.apply_input_palette(control)
        for table in self.findChildren(QTableWidget):
            theme.apply_input_palette(table)
            theme.apply_input_palette(table.horizontalHeader())
            theme.apply_input_palette(table.verticalHeader())

    # ---------------------------------------------------------------- server ---
    @Slot()
    def _pick_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Chọn thư mục gốc", self.root_edit.text())
        if path:
            self.root_edit.setText(path)
            self.root_edit.setCursorPosition(len(path))
            self._refresh_files(quiet=False)

    @Slot(str)
    def _sync_root_tooltip(self, text: str) -> None:
        self.root_edit.setToolTip(text)

    @Slot()
    def _open_root_folder(self) -> None:
        root = os.path.abspath(self.root_edit.text() or "files")
        if os.path.isdir(root):
            os.startfile(root)  # noqa: S606 - mở thư mục cho người dùng
        else:
            self.status_bar.message.setText(f"Không tồn tại thư mục: {root}")

    @Slot()
    def _start_server(self) -> None:
        if self.server is not None:
            return
        root = os.path.abspath(self.root_edit.text().strip() or "files")
        if not os.path.isdir(root):
            self.status_bar.message.setText(f"Thư mục gốc không tồn tại: {root}")
            return

        try:
            server = TftpServer(
                self.host_edit.text().strip() or "0.0.0.0",
                self.port_spin.value(),
                root=root,
                blksize=self.blksize_spin.value(),
                allow_overwrite=not self.no_overwrite.isChecked(),
                enable_options=not self.no_options.isChecked(),
                max_clients=self.max_clients_spin.value(),
                max_file_size=self.max_size_spin.value() or None,
                allow_subdirs=self.allow_subdirs.isChecked(),
                max_requests_per_second=100 if self.low_rate.isChecked() else 1_000_000,
            )
        except OSError as exc:
            self.status_bar.message.setText(f"Không tạo được server: {exc}")
            self.top_bar.set_status("err", "Lỗi cấu hình")
            return

        self.server = server
        self.server_thread = ServerThread(server, self)
        self.server_thread.ready.connect(self._on_server_ready)
        self.server_thread.failed.connect(self._on_server_failed)
        self.server_thread.start()
        self._set_running(True)
        self.uptime.start()
        self.top_bar.set_status("ok", "Server đang chạy")
        port = self.port_spin.value()
        self.status_bar.message.setText(f"Đang lắng nghe trên {server.host}:{port}")
        self._refresh_files(quiet=True)
        self._use_running_server()

    @Slot(int)
    def _on_server_ready(self, port: int) -> None:
        self.top_bar.set_status("ok", f"Server đang chạy · :{port}")
        self.status_bar.message.setText(
            f"Sẵn sàng phục vụ tại udp://{self.host_edit.text()}:{port}"
        )

    @Slot(str)
    def _on_server_failed(self, message: str) -> None:
        self.top_bar.set_status("err", "Server lỗi")
        self.status_bar.message.setText(f"Server không chạy được: {message}")
        self._set_running(False)

    @Slot()
    def _stop_server(self) -> None:
        if self.server is None:
            self.status_bar.message.setText("Server đang tắt")
            return
        self.status_bar.message.setText("Đang dừng server...")
        self.top_bar.set_status("warn", "Đang dừng")
        server, thread = self.server, self.server_thread
        self.server = None
        self.server_thread = None
        server.shutdown()
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        self.uptime.stop()
        self._set_running(False)
        self.hero_clock.setText("00:00")
        self.top_bar.set_status("idle", "Server đang tắt")
        self.status_bar.message.setText("Server đã dừng")

    def _refresh_hero(self) -> None:
        """Vẽ lại hero theo trạng thái server: một nút chính + một nút phụ."""
        running = self.server is not None
        self.hero_start.setVisible(not running)
        self.hero_stop.setVisible(running)
        if running:
            host = self.host_edit.text().strip() or "0.0.0.0"
            bound = self.server_thread.server.bound_port if self.server_thread else 0
            port = bound or self.port_spin.value()
            self.hero_title.setText("Server đang chạy")
            self.hero_detail.setText(
                f"Phục vụ {self._root_path()} trên udp://{host}:{port}"
            )
        else:
            self.hero_title.setText("Server đang tắt")
            self.hero_detail.setText(
                "Bấm “Khởi động server” để phục vụ file trong thư mục gốc."
            )

    def _set_running(self, running: bool) -> None:
        self._refresh_hero()
        for widget in (
            self.host_edit,
            self.port_spin,
            self.root_edit,
            self.blksize_spin,
            self.max_clients_spin,
            self.max_size_spin,
            self.no_overwrite,
            self.no_options,
            self.allow_subdirs,
            self.low_rate,
        ):
            widget.setEnabled(not running)
        self.server_header.set_badge("ĐANG CHẠY" if running else "ĐÃ TẮT")
        self.stat_sessions.set_number(0, animate=False)

    # ----------------------------------------------------------------- files ---
    def _root_path(self) -> str:
        return os.path.abspath(self.root_edit.text().strip() or "files")

    def _scan_files(self) -> list[tuple[str, int, float]]:
        root = self._root_path()
        if not os.path.isdir(root):
            return []
        entries = []
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if not os.path.isfile(full) or name.endswith(".part"):
                continue
            try:
                stat = os.stat(full)
            except OSError:
                continue
            entries.append((name, stat.st_size, stat.st_mtime))
        entries.sort(key=lambda item: item[0].lower())
        return entries

    @Slot()
    def _refresh_files(self, quiet: bool = False) -> None:
        entries = self._scan_files()
        table = self.files_table
        if not os.path.isdir(self._root_path()):
            self.files_hint.setText(f"Không tìm thấy thư mục: {self._root_path()}")
            table.show_empty(
                "Không tìm thấy thư mục", self._root_path(), "Chọn thư mục…"
            )
            self._refresh_server_files()
            return

        table.clear_rows()
        for name, size, mtime in entries:
            table.add_row(
                [name, human_size(size), time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))],
                right_align=(1,),
            )

        total = sum(item[1] for item in entries)
        self.files_hint.setText(f"{len(entries)} file · {human_size(total)}")
        self.files_header.set_badge(f"{len(entries)} mục")
        self.stat_files.set_number(len(entries))
        self.stat_size.set_text(human_size(total))
        self.stat_blksize.set_number(self.blksize_spin.value())
        if not quiet:
            self.status_bar.message.setText(f"Đã đọc {len(entries)} file trong thư mục gốc")
        self._refresh_server_files()

    @Slot()
    def _refresh_server_files(self) -> None:
        """Dựng lại bảng file trên server ở trang Truyền file."""
        table = self.server_files
        table.clear_rows()
        entries = self._scan_files()
        if not entries:
            if not os.path.isdir(self._root_path()):
                table.show_empty("Không tìm thấy thư mục", self._root_path())
            else:
                table.show_empty("Chưa có file", "Thư mục gốc đang trống.")
            return
        for name, size, _ in entries:
            table.add_row([name, human_size(size)], right_align=(1,))

    @Slot(int, int)
    def _on_server_file_clicked(self, row: int, _col: int) -> None:
        item = self.server_files.table.item(row, 0)
        if item is not None:
            self._quick_pick(item.text())

    @Slot(str)
    def _quick_pick(self, name: str) -> None:
        self.download_name.setText(name)
        if not self.download_save.text().strip():
            self.download_save.setText(os.path.basename(name))
        self.status_bar.message.setText(f"Đã chọn '{name}' — bấm Tải về để bắt đầu")

    @Slot()
    def _use_running_server(self) -> None:
        host = self.host_edit.text().strip() or "0.0.0.0"
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        port = self.server_thread.server.bound_port if self.server else self.port_spin.value()
        self.target_edit.setText(f"{host}:{port}")

    @Slot(QTableWidgetItem)
    def _on_file_activated(self, item: QTableWidgetItem) -> None:
        name = self.files_table.table.item(item.row(), 0).text()
        self._quick_pick(name)
        self.go_to(PAGE_TRANSFER)

    @Slot(str)
    def _on_upload_dropped(self, path: str) -> None:
        self.upload_pick.setText(path)
        self.upload_remote.setText(os.path.basename(path))

    # ------------------------------------------------------------- transfers ---
    def _start_transfer(
        self, direction: str, client: TftpClient, source: str, target: str
    ) -> None:
        self.transfer_thread = QThread(self)
        self.transfer_worker = TransferWorker(client, direction, source, target)
        self.transfer_worker.moveToThread(self.transfer_thread)
        self.transfer_thread.started.connect(self.transfer_worker.run)

        download = direction == "download"
        (self.download_progress if download else self.upload_progress).reset()

        worker = self.transfer_worker
        worker.progress.connect(
            self._on_download_progress if download else self._on_upload_progress
        )
        worker.finished_ok.connect(self._on_download_ok if download else self._on_upload_ok)
        worker.failed.connect(self._on_download_err if download else self._on_upload_err)
        worker.finished_ok.connect(self._transfer_done)
        worker.failed.connect(self._transfer_done)

        self.transfer_thread.start()
        self.download_button.setEnabled(not download)
        self.upload_button.setEnabled(download)

    @Slot(str)
    def _transfer_done(self, _result: str) -> None:
        thread, self.transfer_thread = self.transfer_thread, None
        self.transfer_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(2000)
        self.download_button.setEnabled(True)
        self.upload_button.setEnabled(True)

    def _target_host_port(self) -> tuple[str, int] | None:
        text = self.target_edit.text().strip() or "127.0.0.1:6969"
        try:
            return _parse_server(text)
        except ValueError as exc:
            self.status_bar.message.setText(f"Địa chỉ server không hợp lệ: {exc}")
            return None

    @Slot()
    def _start_download(self) -> None:
        name = self.download_name.text().strip()
        if not name:
            self.status_bar.message.setText("Cần nhập tên file cần tải")
            return
        target = self._target_host_port()
        if target is None:
            return
        save = self.download_save.text().strip() or os.path.basename(name) or "download.out"

        client = TftpClient(
            target[0],
            target[1],
            timeout=5.0,
            retries=5,
            blksize=self.download_blksize.value(),
            mode=self.download_mode.currentText(),
        )
        self.download_header.set_badge("ĐANG TẢI")
        self.download_banner.clear()
        self.download_status.setText(f"Đang tải '{name}' từ {target[0]}:{target[1]}...")
        self._start_transfer("download", client, name, save)

    @Slot(int, object)
    def _on_download_progress(self, done: int, total: int | None) -> None:
        bar = self.download_progress
        bar.set_progress(done, total)
        if total:
            percent = min(100, int(done * 100 / total))
            self.download_status.setText(
                f"{percent}% · {human_size(done)} / {human_size(total)}"
            )
        else:
            self.download_status.setText(f"Đã tải {human_size(done)}")

    @Slot(str)
    def _on_download_ok(self, path: str) -> None:
        bar = self.download_progress
        bar.set_busy(False)
        bar.set_progress(1, 1)
        bar.set_state("ok")
        self.download_header.set_badge("XONG")
        self.download_status.setText(f"Đã lưu: {path}")
        self.status_bar.message.setText(f"Tải về thành công: {path}")
        self.toast.show_message(f"Tải về thành công: {path}", "ok")
        self._refresh_files(quiet=True)

    @Slot(str)
    def _on_download_err(self, message: str) -> None:
        self.download_progress.set_state("err")
        self.download_header.set_badge("LỖI")
        self.download_banner.set_message(message)
        self.download_status.setText(message)
        self.status_bar.message.setText(f"Tải về thất bại: {message}")
        self.toast.show_message(f"Tải về thất bại: {message}", "err")

    @Slot()
    def _start_upload(self) -> None:
        source = self.upload_pick.text().strip()
        if not source or not os.path.isfile(source):
            self.upload_pick.set_error("Chọn một file có thật trên đĩa")
            return
        self.upload_pick.set_error("")
        target = self._target_host_port()
        if target is None:
            return
        remote = self.upload_remote.text().strip() or os.path.basename(source)

        client = TftpClient(
            target[0],
            target[1],
            timeout=5.0,
            retries=5,
            blksize=self.upload_blksize.value(),
            mode=self.upload_mode.currentText(),
        )
        self.upload_header.set_badge("ĐANG GỬI")
        self.upload_banner.clear()
        self.upload_status.setText(
            f"Đang gửi '{os.path.basename(source)}' lên {target[0]}:{target[1]}..."
        )
        self._start_transfer("upload", client, source, remote)

    @Slot(int, object)
    def _on_upload_progress(self, done: int, total: int | None) -> None:
        bar = self.upload_progress
        bar.set_progress(done, total)
        if total:
            percent = min(100, int(done * 100 / total))
            self.upload_status.setText(
                f"{percent}% · {human_size(done)} / {human_size(total)}"
            )
        else:
            self.upload_status.setText(f"Đã gửi {human_size(done)}")

    @Slot(str)
    def _on_upload_ok(self, name: str) -> None:
        bar = self.upload_progress
        bar.set_busy(False)
        bar.set_progress(1, 1)
        bar.set_state("ok")
        self.upload_header.set_badge("XONG")
        self.upload_status.setText(f"Server đã lưu '{name}'")
        self.status_bar.message.setText(f"Gửi lên thành công: {name}")
        self.toast.show_message(f"Gửi lên thành công: {name}", "ok")
        self._refresh_files(quiet=True)

    @Slot(str)
    def _on_upload_err(self, message: str) -> None:
        self.upload_progress.set_state("err")
        self.upload_header.set_badge("LỖI")
        self.upload_banner.set_message(message)
        self.upload_status.setText(message)
        self.status_bar.message.setText(f"Gửi lên thất bại: {message}")
        self.toast.show_message(f"Gửi lên thất bại: {message}", "err")

    # ------------------------------------------------------------------ misc ---
    @Slot()
    def _on_tick(self) -> None:
        if self.server is None:
            return
        self.hero_clock.setText(motion.human_clock(self.uptime.seconds))
        self.stat_sessions.set_number(self.server.active_sessions, animate=False)

    # ----------------------------------------------------------------- intro ---
    def play_intro(self) -> None:
        """Chạy hiệu ứng mở app: cửa sổ mờ dần."""
        self.setWindowOpacity(0.0)
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(260)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(motion.out_cubic())
        fade.start()
        self._intro_fade = fade

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - tên do Qt đặt
        if self.toast.isVisible():
            self.toast.reposition()
        super().resizeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - tên do Qt đặt
        if self.server is not None:
            self._stop_server()
        if self.transfer_thread is not None:
            self.transfer_thread.quit()
            self.transfer_thread.wait(2000)
        self.log_bridge.detach()
        super().closeEvent(event)
