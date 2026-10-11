"""Design tokens và cơ chế đổi theme cho GUI (PySide6 / Qt).

Hệ thống thị giác: app dashboard hiện đại, điều hướng bằng top-bar, nội dung
chia card, tông tím, panel mềm, bo góc tròn, tương phản cao.

**Vì sao không dùng stylesheet (QSS)?**
Trên Qt 6.12 + Windows, `QApplication.setStyleSheet` chỉ áp được cho một phần
cây widget (thường là vài widget đầu tiên); phần còn lại rơi về màu mặc định
của hệ điều hành — quan sát được và không ổn định giữa các lần chạy. Vì vậy:

  - **Widget do hệ điều hành vẽ** (QLineEdit, QSpinBox, QComboBox, QTableWidget,
    QCheckBox, QScrollBar…) lấy màu từ :class:`QPalette` — đây là kênh chuẩn,
    luôn áp dụng.
  - **Bề mặt thương hiệu** (card, ô số liệu, nút, pill, thanh tiến trình) tự vẽ
    bằng `QPainter` trong `components.py`.

Thang giá trị:
  - Typography : 20 số liệu / 15 tiêu đề hero / 11 tiêu đề card / 9.5 nút /
    9 body / 8.5 field / 8 caption (số liệu luôn mono)
  - Spacing    : 4 / 8 / 12 / 16 / 24 / 32
  - Bo góc     : 8 (input, nút, ô) / 10 (card) / 999 (pill)
Mọi màu khai báo ở đây; đổi theme chỉ cần thay dict.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

# --------------------------------------------------------------------- font ---
# Ưu tiên Inter / JetBrains Mono như chuẩn; tự fallback về font có sẵn trên OS.
SANS = ["Inter", "Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Roboto", "Arial"]
MONO = ["JetBrains Mono", "Cascadia Mono", "Cascadia Code", "Consolas", "Courier New"]

_sans_family = SANS[-1]
_mono_family = MONO[-1]


def _first_available(families: list[str]) -> str:
    available = set(QFontDatabase.families())
    for family in families:
        if family in available:
            return family
    return families[-1]


def apply_fonts(app) -> tuple[str, str]:
    """Đặt font mặc định cho ứng dụng, trả về (sans, mono) thực sự dùng."""
    global _sans_family, _mono_family
    _sans_family = _first_available(SANS)
    _mono_family = _first_available(MONO)
    font = QFont(_sans_family)
    font.setPointSize(10)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(font)
    return _sans_family, _mono_family


def font(
    size: float = 10.0,
    *,
    weight: QFont.Weight = QFont.Weight.Normal,
    mono: bool = False,
    letter_spacing: float = 0.0,
) -> QFont:
    """Tạo QFont theo thang typography (không dùng QSS nên phải set trực tiếp)."""
    f = QFont(_mono_family if mono else _sans_family)
    f.setPointSizeF(size)
    f.setWeight(weight)
    if letter_spacing:
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100 + letter_spacing)
    return f


def medium(size: float = 10.0, *, mono: bool = False) -> QFont:
    return font(size, weight=QFont.Weight.Medium, mono=mono)


def bold(size: float = 10.0, *, mono: bool = False) -> QFont:
    return font(size, weight=QFont.Weight.DemiBold, mono=mono)


def label_font(size: float = 8.5, mono: bool = False) -> QFont:
    """Font cho nhãn phụ (chữ nhỏ, hơi đậm, dễ đọc)."""
    return font(size, weight=QFont.Weight.DemiBold, mono=mono)


def caps_font(size: float = 8.5) -> QFont:
    """Font cho nhãn viết hoa như `FILE TRONG ROOT`."""
    return font(size, weight=QFont.Weight.Bold, letter_spacing=8.0)


# -------------------------------------------------------------------- tokens ---
LIGHT: dict[str, str] = {
    "bg": "#F5F4F2",
    "surface": "#FFFFFF",
    "surface_alt": "#FAF9F7",
    "surface_sunken": "#ECEAE6",
    "border": "#E5E2DC",
    "border_strong": "#D6D1C8",
    "text": "#1C1917",
    "text_muted": "#57534E",
    "text_subtle": "#76716C",
    "primary": "#6D28D9",
    "primary_hover": "#5B21B6",
    "primary_pressed": "#4C1D95",
    "primary_soft": "#F1EAFE",
    "primary_border": "#D9C8FA",
    "on_primary": "#FFFFFF",
    "success": "#10B981",
    "success_soft": "#E7F9F1",
    "success_border": "#B6EBD5",
    "warning": "#F59E0B",
    "warning_soft": "#FEF6E7",
    "warning_border": "#F7DFB0",
    "danger": "#EF4444",
    "danger_soft": "#FDECEC",
    "danger_border": "#F7C9C9",
    "shadow": "#1C1917",
    "terminal": "#FFFFFF",
    "terminal_border": "#E5E2DC",
    "log_info": "#3F3B36",
    "log_debug": "#A8A29E",
    "log_warning": "#B45309",
    "log_error": "#DC2626",
}

DARK: dict[str, str] = {
    "bg": "#0B0B0E",
    "surface": "#131315",
    "surface_alt": "#1B1A18",
    "surface_sunken": "#0F0E0D",
    "border": "#2A2725",
    "border_strong": "#3B3532",
    "text": "#FAFAF9",
    "text_muted": "#B0B0B8",
    "text_subtle": "#93939D",
    "primary": "#8B5CF6",
    "primary_hover": "#7C3AED",
    "primary_pressed": "#6D28D9",
    "primary_soft": "#221A38",
    "primary_border": "#4A3670",
    "on_primary": "#FFFFFF",
    "success": "#10B981",
    "success_soft": "#0C2419",
    "success_border": "#12503A",
    "warning": "#F59E0B",
    "warning_soft": "#2A1F08",
    "warning_border": "#54390D",
    "danger": "#F87171",
    "danger_soft": "#2B1214",
    "danger_border": "#5B2226",
    "shadow": "#000000",
    "terminal": "#0B0B10",
    "terminal_border": "#26262E",
    "log_info": "#D4D4DC",
    "log_debug": "#71717A",
    "log_warning": "#FBBF24",
    "log_error": "#FB7185",
}


def theme_tokens(name: str) -> dict[str, str]:
    return LIGHT if name == "light" else DARK


def build_palette(t: dict[str, str]) -> QPalette:
    """Dựng QPalette cho widget do hệ điều hành vẽ.

    Palette là kênh chuẩn của Qt và không bị giới hạn như stylesheet, nên mọi
    input / bảng / checkbox / scrollbar đều nhận đúng màu ở cả hai theme.
    """
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(t["bg"]))
    p.setColor(QPalette.ColorRole.WindowText, QColor(t["text"]))
    p.setColor(QPalette.ColorRole.Base, QColor(t["surface"]))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(t["surface_alt"]))
    p.setColor(QPalette.ColorRole.Text, QColor(t["text"]))
    p.setColor(QPalette.ColorRole.Button, QColor(t["surface"]))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(t["text"]))
    p.setColor(QPalette.ColorRole.BrightText, QColor(t["danger"]))
    p.setColor(QPalette.ColorRole.Highlight, QColor(t["primary"]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(t["on_primary"]))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(t["surface"]))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(t["text"]))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(t["text_subtle"]))
    p.setColor(QPalette.ColorRole.Link, QColor(t["primary"]))
    p.setColor(QPalette.ColorRole.Mid, QColor(t["border_strong"]))
    p.setColor(QPalette.ColorRole.Dark, QColor(t["border"]))
    p.setColor(QPalette.ColorRole.Light, QColor(t["surface"]))
    p.setColor(QPalette.ColorRole.Midlight, QColor(t["border"]))
    p.setColor(QPalette.ColorRole.Shadow, QColor(t["shadow"]))

    disabled = QPalette.ColorGroup.Disabled
    p.setColor(disabled, QPalette.ColorRole.Text, QColor(t["text_subtle"]))
    p.setColor(disabled, QPalette.ColorRole.WindowText, QColor(t["text_subtle"]))
    p.setColor(disabled, QPalette.ColorRole.ButtonText, QColor(t["text_subtle"]))
    p.setColor(disabled, QPalette.ColorRole.HighlightedText, QColor(t["surface"]))
    return p


# ------------------------------------------------------------ trạng thái theme ---
_THEME_NAME = "light"
_tokens: dict[str, str] = dict(LIGHT)


def current_tokens() -> dict[str, str]:
    return _tokens


def is_light() -> bool:
    return _THEME_NAME == "light"


def apply_theme(app, name: str) -> None:
    """Đổi theme toàn app: ghi token + nạp palette mới."""
    global _THEME_NAME, _tokens
    _THEME_NAME = "light" if name.startswith("light") else "dark"
    _tokens = dict(theme_tokens(_THEME_NAME))
    app.setPalette(build_palette(_tokens))


def apply_input_palette(widget) -> None:
    """Palette tường minh cho control do hệ điều hành vẽ.

    QLineEdit/QSpinBox/QComboBox/QTableWidget kế thừa palette của cha nên dễ
    ăn nhầm màu nền `bg` khiến form xám lệch khỏi card trắng. Hàm này ghim nền
    về `surface`, khung về `border`, gọi lại sau mỗi lần đổi theme.
    """
    t = current_tokens()
    pal = widget.palette()
    pal.setColor(QPalette.ColorRole.Base, QColor(t["surface"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(t["text"]))
    pal.setColor(QPalette.ColorRole.Window, QColor(t["surface"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(t["surface"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(t["surface_alt"]))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(t["primary"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(t["on_primary"]))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(t["text_subtle"]))
    pal.setColor(QPalette.ColorRole.Mid, QColor(t["border_strong"]))
    pal.setColor(QPalette.ColorRole.Dark, QColor(t["border"]))
    pal.setColor(QPalette.ColorRole.Light, QColor(t["surface"]))
    widget.setPalette(pal)
