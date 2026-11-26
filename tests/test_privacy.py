"""Chốt an toàn cho repo public.

Repo này công khai trên GitHub, nên không được phép có trong lịch sử git:
báo cáo / slide / tài liệu môn học, khoá riêng, cấu hình bí mật hay dữ liệu
người dùng.

Test chạy cùng `python -m unittest discover` nên CI sẵn có sẽ chặn mọi
commit vi phạm, không cần thêm job riêng. Test tự bỏ qua khi không có git
hoặc đang chạy từ bản đóng gói (sdist/wheel không có thư mục .git).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Đường dẫn (tính từ gốc repo) không bao giờ được track.
FORBIDDEN_PATHS = [
    r"^report_build/",
    r"^make_(?:report|contributions)_pptx\.py$",
    r"^deploy/wireguard/generated/",
    r"^web-data/",
    r"\.(?:docx?|pptx?|xlsx?|pdf|odt|odp|ods|rtf|msi)$",
    r"(?:^|/)\.env$",
    r"\.(?:key|pem|p12|pfx|kdbx)$",
    r"(?:^|/)id_(?:rsa|ed25519|ecdsa)(?:\.pub)?$",
    # files/ chỉ giữ đúng ba file mẫu; mọi thứ khác là dữ liệu người dùng.
    r"^files/(?!empty\.dat$|hello\.txt$|sample\.bin$)",
]

# Nội dung cho thấy một khoá riêng bị commit nhầm.
FORBIDDEN_CONTENT = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----"),
    re.compile(r"(?:PrivateKey|PresharedKey)\s*=\s*[A-Za-z0-9+/]{40,}={0,2}"),
]

# Các quy tắc .gitignore phải còn, để không ai lỡ xoá mất lớp bảo vệ.
REQUIRED_IGNORE = [
    "report_build/",
    "deck_build/",
    "build_deck.py",
    "make_report_pptx.py",
    "make_contributions_pptx.py",
    "*.docx",
    "*.pptx",
    "*.pdf",
    "~$*",
    "deploy/wireguard/generated/",
    "web-data/",
    "files/*",
]

# Chỉ đọc phần đầu file để tránh nạp file nhị phân lớn vào RAM.
MAX_SCAN_BYTES = 2 * 1024 * 1024

# Chính file này chứa các mẫu regex ở trên nên phải bỏ qua khi quét nội dung.
SELF_PATH = Path(__file__).resolve().relative_to(REPO).as_posix()


def tracked_files() -> list[str] | None:
    """Danh sách file trong index; None nếu không có git để hỏi."""
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO,
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = result.stdout.decode("utf-8", "replace")
    return [name for name in raw.split("\0") if name]


class TestPublicRepoHygiene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tracked_files()
        if cls.files is None:
            raise unittest.SkipTest("không chạy được git ls-files (bản đóng gói?)")

    def test_no_personal_or_report_files_tracked(self):
        """Không có báo cáo, slide, khoá riêng hay dữ liệu người dùng trong index."""
        offenders = []
        for name in self.files:
            for pattern in FORBIDDEN_PATHS:
                if re.search(pattern, name, re.IGNORECASE):
                    offenders.append(f"{name} (khớp {pattern})")
                    break
        self.assertEqual(offenders, [], "repo public không được track các file này")

    def test_no_private_key_material_in_tracked_files(self):
        """Không có nội dung khoá riêng trong file đang track."""
        offenders = []
        for name in self.files:
            if name == SELF_PATH:
                continue
            path = REPO / name
            try:
                raw = path.read_bytes()[:MAX_SCAN_BYTES]
            except OSError:
                continue
            # Bỏ qua file nhị phân: byte 0 trong đoạn đầu là dấu hiệu.
            if b"\0" in raw[:8192]:
                continue
            text = raw.decode("utf-8", "replace")
            for pattern in FORBIDDEN_CONTENT:
                if pattern.search(text):
                    offenders.append(name)
                    break
        self.assertEqual(offenders, [], "file đang track chứa khoá riêng")

    def test_gitignore_keeps_protective_rules(self):
        """`.gitignore` phải giữ các quy tắc chặn dữ liệu cá nhân."""
        ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
        rules = {line.strip() for line in ignore.splitlines() if line.strip()}
        rules = {line for line in rules if not line.startswith("#")}
        missing = [rule for rule in REQUIRED_IGNORE if rule not in rules]
        self.assertEqual(missing, [], "thiếu quy tắc .gitignore")


if __name__ == "__main__":
    unittest.main()
