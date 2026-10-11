# GUI Redesign — TFTP Console (PySide6)

> Tài liệu đặc tả làm lại giao diện. **Chỉ đổi lớp trình bày** (`src/gui/`,
> `src/tftp_gui.py`); logic TFTP (`tftp_client/server/packet/errors`), worker,
> thread, signal/slot giữ nguyên API. Không thêm dependency mới.

Cơ sở đánh giá: skill `redesign-existing-projects` (audit typography, màu,
layout, trạng thái, component, icon), đã đối chiếu toàn bộ `src/gui/`
(`theme.py`, `components.py`, `main_window.py` ~1216 dòng, `motion.py`,
`logging_bridge.py`, `tftp_gui.py`) + 2 ảnh chụp màn hình do người dùng cung cấp.

---

## 1. Đánh giá hiện trạng (vì sao trông "sơ xài / AI")

### 1.1. Dấu vân tay AI phổ biến có mặt trong app

| # | Vấn đề (theo audit) | Biểu hiện trong code |
|---|---|---|
| 1 | Hàng 3–4 card bằng nhau | 4 `StatTile` dàn đều (`main_window.py:333`), 4 `ActionTile` dàn đều (`:344`) — đúng pattern "feature row AI" |
| 2 | Card generic: border + shadow + nền trắng | Mọi khối đều là `Card`/`Surface` giống hệt nhau, radius 12, bóng vẽ tay (`components.py:117`) |
| 3 | Pill badge + nút primary/ghost cặp đôi | `StatusPill`, badge `CardHeader`, cặp nút "Khởi động/Dừng" |
| 4 | Màu accent tím bão hòa cao (`#9333EA`) trên nền xám lạnh | Đúng fingerprint "purple AI aesthetic"; xám `#F6F6F8` lạnh trong khi tím rực |
| 5 | Chữ hoa toàn bộ cho subheader | `caps_font` + `"FILE TRONG ROOT"`, `"LỐI TẮT NHANH"`, `"ĐÃ TẮT"` |
| 6 | Glyph emoji/ký tự thay icon | `"↓"`, `"↑"`, `"⚙"`, `"≡"`, `"›"` vẽ bằng text (`BrandMark`, `ActionTile`) — nét không đồng đều, ⚙/≡ là font hệ thống |
| 7 | Shadow đen generic | `_paint_shadow` dùng đen `#0F172A` alpha thấp, không tint theo nền |
| 8 | Thiếu empty/error/loading state có thiết kế | Trống → `QLabel` chữ xám ("Thư mục gốc đang trống…"); lỗi → text đỏ trong status; không có skeleton |
| 9 | Copy khô, thụ động | "Chưa có phiên tải nào", "Sẵn sàng" — ổn nhưng vô hồn; hint lặp lại ("Dùng chung cho cả tải về và gửi lên") |

### 1.2. Lỗi bố cục thật (không chỉ gu thẩm mỹ)

- **Card "Cấu hình máy chủ" lệch cột** (ảnh người dùng gửi): `_build_server_card`
  dùng 2 `QGridLayout` riêng — `line1` stretch `2:0:3`, `line2` không set stretch —
  nên hàng 1 và hàng 2 không chung lưới dọc. `port_spin` cứng 120px trong khi
  `max_clients_spin` nở tự do.
- **SpinBox mũi tên ↑↓ cao 34px** thô, 4 spinbox mỗi cái một rộng.
- **Trang Truyền file**: 2 card Tải/Gửi cạnh nhau nhưng nút, thanh progress, status
  không thẳng baseline khi nội dung dài ngắn khác nhau.
- **Không có max-width**: cửa sổ 1320px dàn full-bleed, card kéo giãn vô hạn.
- **Mật độ chữ**: caption 8.5pt DemiBold + hint 8.5pt ở khắp nơi, nhiều dòng hint
  lặp ý nhau gây nhiễu.

### 1.3. Chuyển động

- Bug thật: `PageStack.slide_to` kết thúc ở `QPoint(0, self.y())` — tọa độ của
  stack trong cửa sổ (≈68px) chứ không phải `(0,0)` trong stack → trang nhảy dọc
  cuối animation.
- `entrance.play` chạy **đè lên** slide (card trượt trong khi trang đang trượt).
- `Entrance.play` đo `widget.pos()` khi layout chưa ổn định → đích sai khi resize.

### 1.4. Điểm đang tốt — giữ lại

- Kiến trúc **QPainter + QPalette, không QSS** (`theme.py:6-15`): đúng quyết định
  kỹ thuật cho Qt 6.12/Windows, redesign không được phá.
- Token tập trung một dict, đổi theme một dòng (`apply_theme`).
- `TransferWorker`/`ServerThread` chạy nền, GUI không treo.
- Focus ring tự vẽ trên nút (`ThemedButton:621`), font mono cho số/log, riêng biệt
  log `tftp.*` qua `LogBridge`.

---

## 2. Định hướng thiết kế mới: "Network Ops Console"

Thay vì dashboard tím generic, chuyển sang ngôn ngữ **bảng điều khiển vận hành
mạng**: nền xám ấm, một accent duy nhất, số liệu tabular, đường phân tách thay vì
bóng đổ, trạng thái máy (server on/off/transfer) là trung tâm thị giác.

- **Một accent**: giữ họ tím để không xa lạ, nhưng hạ bão hòa
  (`#7C3AED` → đề xuất `#6D28D9` light / `#8B5CF6` dark chỉ cho điểm nhấn nhỏ).
  Mọi semantic (ok/warn/err) giữ nguyên nhưng chỉ xuất hiện ở chấm trạng thái,
  badge, thanh progress — không tô nền card.
- **Bề mặt phẳng, viền mảnh**: bỏ bóng đổ nhiều lớp; card = nền `surface` + viền
  1px `border`; chỉ hero/status dùng nền tint nhẹ. Chiều sâu thể hiện bằng
  **nhóm nền** (`surface_sunken` cho vùng log/feed) thay vì shadow.
- **Xám ấm thống nhất**: `bg` light `#F5F4F2` (ấm) thay vì `#F6F6F8` (lạnh);
  dark giữ `#0B0B0E` (đã tốt, chỉ chỉnh border ấm lên một chút).
- **Chữ thường (sentence case)** cho toàn bộ tiêu đề/hint; bỏ caps lock trừ badge
  trạng thái 2 từ ("ĐANG CHẠY").
- **Icon vẽ tay bằng QPainter** (đường thẳng 1.6px, round cap — cùng ngôn ngữ với
  mũi tên `ChevronComboBox` hiện có): thay toàn bộ `"↓↑⚙≡›"` bằng
  `IconGlyph` vẽ vector (arrow-down/up, gear, log, chevron-right, refresh, folder,
  file, x). Nét đồng nhất, không phụ thuộc font hệ thống.
- **Số liệu tabular**: mọi con số (kích thước, cổng, blksize, clock) dùng font
  mono; nhãn dùng sans.
- **Terminal/log mỗi theme một bộ**: light nền trắng chữ tối, dark nền đen chữ
  sáng (không để mảng đen lạc lõng giữa trang sáng).

---

## 3. Design tokens v2 (sửa trong `theme.py`)

```python
# Phông chữ: giữ stack hiện tại (Inter → Segoe UI Variable → Segoe UI),
# bổ sung weight Medium/SemiBold cho hierarchy mịn hơn.
SANS = ["Inter", "Segoe UI Variable Text", "Segoe UI", ...]  # giữ nguyên
MONO = ["JetBrains Mono", "Cascadia Mono", "Consolas", ...]  # giữ nguyên

# Type scale (pt): 11 display / 10 title / 9 body / 8.5 field / 8 caption / 12 numeric
# Weight: Normal 400 body · Medium 500 label phụ · DemiBold 600 tiêu đề · Bold 700 số lớn
# Tracking: display -2%, caption +6% (bỏ caps +8% hiện tại)

LIGHT = {
    "bg": "#F5F4F2",            # ấm, thay #F6F6F8 lạnh
    "surface": "#FFFFFF",
    "surface_alt": "#FAF9F7",
    "surface_sunken": "#ECEAE6", # vùng log/feed
    "border": "#E5E2DC",
    "border_strong": "#D6D1C8",
    "text": "#1C1917",           # stone-900, ấm
    "text_muted": "#57534E",
    "text_subtle": "#76716C",     # đủ tương phản ~4.6:1 trên nền trắng
    "primary": "#6D28D9",        # hạ bão hòa từ #9333EA
    "primary_hover": "#5B21B6",
    "primary_pressed": "#4C1D95",
    "primary_soft": "#F1EAFE",
    "primary_border": "#D9C8FA",
    "on_primary": "#FFFFFF",
    # success/warning/danger: giữ hue, chỉ dùng cho dot/badge/progress
    "shadow": "#1C1917",         # shadow tint theo nền (bỏ #0F172A)
    ...
}
# DARK: cấu trúc giống LIGHT; text_muted #B0B0B8, text_subtle #93939D
# (xám cũ #78716C quá mờ trên nền đen); border ấm; primary #8B5CF6.
```

- Radius: card 12 → **10**, tile/input/nút **8**, pill/badge 999 (giữ), bảng/log **8**.
- Spacing giữ thang 4/8/12/16/24/32; padding card `20/18/20/20` → **`20/20/20/24`**
  (đáy nhỉnh hơn cho cân bằng quang học).
- Nội dung dàn full chiều rộng trang (đã thử max-width 1160 căn giữa nhưng
  phản hồi người dùng muốn dàn đều → bỏ).

---

## 4. Hệ component v2 (sửa trong `components.py`)

| Component | Thay đổi chính |
|---|---|
| `Surface` | Thêm `elevated: bool=False`; mặc định **không bóng** (chỉ viền). Bóng tint `shadow` token, 1 lớp duy nhất, chỉ dùng cho hero + toast. |
| `Card` | Padding `20/20/20/24`, spacing 16 → **14**; bỏ hover-lift (card nội dung không phải nút — bỏ affordance giả). |
| `CardHeader` | Title 11pt SemiBold sentence case; badge vuông radius 6 → **pill nhỏ**, chữ 8pt Medium. |
| `SectionLabel` | Bỏ caps lock: 8.5pt Medium, `text_subtle`, sentence case ("Lối tắt nhanh"). |
| `ThemedButton` | Giữ 4 variant; thêm `tertiary` (chữ tím, không viền) cho hành động phụ; nút primary cao 40, chữ 9.5 SemiBold; focus ring giữ. |
| `IconGlyph` (mới) | QWidget vẽ vector 1 nét (down/up/gear/log/chevron/refresh/folder/file/dot/x), size 14–18, màu `current`. Thay mọi glyph text. |
| `ActionTile` | Bỏ mũi tên `›` text → `IconGlyph(chevron)`; mark vuông radius 8 nền `primary_soft`, icon tím (thay ô tím đặc + chữ trắng — bớt "AI"). Bỏ hover-lift nền, chỉ đổi viền. |
| `StatTile` | Nhãn sentence case 8pt Medium (bỏ caps); số 20pt Bold **mono**; thêm delta/hint dòng 2. Không equal-height ép buộc — cho `sizePolicy` tự nhiên. |
| `Field` | Thêm `error: str` → viền input đỏ + chữ đỏ (thay `setProperty("state")` rời rạc của `FilePicker`). Caption + control + hint/error thẳng hàng. |
| `NumberInput` (mới) | Bọc `QSpinBox`: **ẩn nút ↑↓** (`NoButtons`), chữ mono căn phải, hậu tố đơn vị ("byte", "/s"). Fix triệt để spinbox thô trong ảnh. |
| `TextInput`/`Select` | (Tùy chọn) bọc QLineEdit/QComboBox để viền/focus đồng nhất qua `QPalette` + `ChevronComboBox` hiện có. |
| `DataTable` (mới, bọc QTableWidget) | Header 8.5pt Medium `text_muted`, không in đậm; hàng cao 34; zebra nhẹ `surface_alt`; cột số căn phải mono; empty state vẽ sẵn (icon + 2 dòng chữ + nút hành động). |
| `EmptyState` / `ErrorBanner` (mới) | Khung dashed `border_strong` + icon + tiêu đề + mô tả + nút chính. Dùng cho: root trống, list server rỗng, lỗi kết nối (thay QLabel xám). |
| `StatusPill` | Giữ pulse; chữ 8.5 Medium; rộng co theo text (đã có `setMinimumWidth` động — giữ). |
| `ShimmerProgressBar` | Cao 8 → **6**; track `surface_sunken`; bỏ dải sáng trắng khi đã có % (chỉ shimmer khi busy không xác định). |
| `DropZone` | Giữ dashed; thêm icon upload + chữ 2 dòng ("Kéo file vào đây" / "hoặc bấm để chọn") — bấm mở dialog (hiện tại chỉ kéo được). |
| `Toast` (mới) | Hàng thông báo trượt từ dưới status bar cho thành công/thất bại (thay việc chỉ đổi text status dễ bỏ lỡ). |

---

## 5. Bố cục từng trang (sửa trong `main_window.py`)

### 5.1. Khung chung

- Khung trang full-bleed (nội dung dàn đều ra trang theo phản hồi người dùng,
  không bó giữa).
- TopBar: giữ 68px; nav 4 nút chuyển sang **segmented control** (một dải nền
  `surface_sunken`, nút active nền `surface` + viền) thay 4 nút rời — gọn và ít "AI".
- StatusBar: giữ; thêm vùng toast chồng góc phải dưới.

### 5.2. Trang Tổng quan — bỏ hàng 4 card đều

```
┌ Hero (status-driven) ─────────────────────────────┐
│ ● Server đang chạy      [Dừng server] [Mở thư mục] │
│ udp://0.0.0.0:6969 · uptime 04:12 · 3 file        │
├──────────────────────────────┬────────────────────┤
│ Số liệu (2×2 grid nhỏ)       │ Hoạt động gần đây  │
│  File 3 · Dung lượng 1.9 MB  │  (feed, sunken)    │
│  Phiên 0  · Blksize 1468     │                    │
├──────────────────────────────┴────────────────────┤
│ Lối tắt nhanh: [Tải file về] [Gửi file lên]       │
└───────────────────────────────────────────────────┘
```

- Hero đổi nội dung theo trạng thái (tắt/chạy/lỗi) thay vì tĩnh; nút chính duy
  nhất theo trạng thái + 1 tertiary phụ.
- 4 StatTile → grid 2×2 gọn trong nửa trái; nửa phải là ActivityFeed (cân bằng,
  phá đối xứng 1 hàng dài).
- 4 ActionTile → 2 lối tắt thật sự dùng (Tải/Gửi) + link text cho Cấu hình/Nhật ký.

### 5.3. Trang Truyền file — thẳng baseline

- 2 card Tải/Gửi: dùng chung `FormGrid` (label/control/hint cùng hàng), nút
  **pin đáy card** (`addStretch` trước nút) để 2 nút ngang hàng dù nội dung lệch.
- Card "File trên server" (tính năng đã duyệt): header + nút Làm mới + `DataTable`
  gọn (Tên · Kích thước) thay lưới chip; bấm hàng → điền ô tải về. Trống/lỗi →
  `EmptyState`/`ErrorBanner` + nút thử lại.
- Ô đích đến gộp vào header card dạng segmented: `[127.0.0.1:6969] [Dùng server
  đang chạy]` trên cùng một hàng, bỏ card "Đích đến" riêng (giảm 1 card thừa).

### 5.4. Trang Máy chủ — thiết kế lại card Cấu hình

Card chia 4 khối dọc theo nhịp card (thay lưới 3×2 chung khiến cột hẹp/rộng
lệch nhau):

1. Hàng lắng nghe: `[Địa chỉ lắng nghe (3 phần)] [Cổng UDP (1 phần)]`.
2. `Thư mục gốc + [Chọn…]` full hàng.
3. Ba giới hạn 3 cột **bằng nhau tuyệt đối** (stretch 1:1:1).
4. Nhãn "Tùy chọn" đặt sát trên grid checkbox 2×2 đều nhau (cột 1:1,
   checkbox cao tối thiểu 26px cho thẳng baseline).

- `Field`/`CardHeader`/`ActionTile` luôn có stretch cuối VBox: khoảng thừa rơi
  xuống đáy, nhãn + control dồn lên trên — nếu không QLabel nhãn (Preferred) sẽ
  tự giãn và đẩy control xuống, gây lệch 17px giữa các cột (đã đo thực tế).

- Mọi ô số dùng `NumberInput` (ẩn ↑↓, mono căn phải, rộng tối thiểu + nở đều).
- Bảng file → `DataTable` (cột Tên stretch, Kích thước/Ngày mono căn phải),
  nút Làm mới/Mở thư mục gom vào header bảng.

### 5.5. Trang Nhật ký

- Thanh công cụ gom: filter + nút Lưu/Xóa vào 1 hàng gọn; `QTextEdit` nền
  `terminal` giữ; thêm autoscroll toggle + nút "Về cuối".
- Màu mức log giữ token hiện tại (đã tốt).

---

## 6. Chuyển động v2 (sửa trong `motion.py`)

1. **Drift lên thay vì trượt ngang**: trang cũ ẩn ngay, trang mới trôi từ
   `y=+28` về `0` trong 500ms OutCubic (`PageStack.drift_to`) — một trang vẽ
   một lúc nên hết rung lắc.
2. **Không entrance card**: animate `pos` của card nằm trong layout bị layout
   giằng lại gây rung, nên đã gỡ bỏ lớp `Entrance` — chuyển trang chỉ còn đúng
   một lớp chuyển động duy nhất.
3. `_settle`: đưa trang hiện tại về `(0,0)` khi bị ngắt (resize giữa animation,
   bấm tab liên tục); `setUpdatesEnabled(False)` quanh bước định vị ban đầu
   để không chớp 1 frame.
4. Thời gian: drift 500ms OutCubic; fade mở app giữ nguyên (`play_intro`).
5. Nguyên tắc: hover chỉ đổi màu/viền, không lift.

---

## 7. Copywriting (viết lại toàn bộ chuỗi UI)

- Sentence case, chủ động, cụ thể: "Chưa có phiên tải nào" → "Chưa tải file nào
  trong phiên này"; "Sẵn sàng" → "Sẵn sàng — chọn file để bắt đầu".
- Bỏ dấu `!`, bỏ "Oops"; lỗi nêu nguyên nhân + hành động: "Không nối được
  127.0.0.1:6969 — kiểm tra server rồi bấm Thử lại."
- Hint mỗi field ≤ 1 dòng, không lặp ý của label.

---

## 8. Lộ trình thực hiện (không phá chức năng)

| Phase | Việc | File | Verify |
|---|---|---|---|
| P0 | Fix lưới card Cấu hình (1 grid 3×2), `NumberInput`, bỏ caps thừa | `main_window.py`, `components.py` | Chụp 2 theme so cột thẳng |
| P1 | Tokens v2 (xám ấm, tím hạ bão hòa, type scale) + `IconGlyph` thay glyph text | `theme.py`, `components.py` | `restyle_tree` 2 theme |
| P2 | Bố cục lại 4 trang theo §5 (hero, segmented nav, form pin đáy, DataTable, Empty/Error/Toast) | `main_window.py`, `components.py` | Click-through 4 trang |
| P3 | Motion v2 (§6) + copywriting (§7) | `motion.py`, `main_window.py` | Resize giữa slide không kẹt |
| P4 | README/docs cập nhật ảnh mới | `README.md`, `docs/` | — |

Sau mỗi phase: `ruff check src` + `python -m unittest discover` (giữ 72 test xanh)
+ smoke GUI offscreen (`QT_QPA_PLATFORM=offscreen`).

**Ràng buộc bất biến**: QPainter+QPalette (không QSS toàn cục); Fusion style;
worker/thread/signal API giữ nguyên; hỗ trợ light+dark; cửa sổ tối thiểu
1000×660 vẫn cuộn được (`_page` giữ).
