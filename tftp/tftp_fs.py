"""Tiện ích filesystem dùng chung cho server và client.

Mục tiêu: ghi file đích theo kiểu "atomic và không ghi đè" nhưng **không phụ
thuộc hard link**.

``os.link`` là cách gọn nhất để làm việc đó trên NTFS/ext4, nhưng FAT32, exFAT
(USB, thẻ nhớ — rất phổ biến trên Windows) và một số filesystem mạng không hỗ
trợ: khi đó ``link()`` ném ``OSError`` với errno ``EPERM``/``ENOTSUP``/``EXDEV``
thay vì ``FileExistsError``. Không có fallback thì mọi lượt upload/tải về trên ổ
USB đều thất bại kèm thông báo khó hiểu (thậm chí bị báo sai là "đĩa đầy").
"""

from __future__ import annotations

import errno
import os

LINK_UNSUPPORTED: frozenset[int] = frozenset(
    {
        errno.EPERM,
        errno.EMLINK,
        errno.EXDEV,
        errno.ENOSYS,
        getattr(errno, "EOPNOTSUPP", errno.ENOSYS),
        getattr(errno, "ENOTSUP", errno.ENOSYS),
    }
)
"""Các errno mà ``os.link`` trả về khi filesystem không hỗ trợ hard link."""

COPY_CHUNK = 1024 * 1024


def _link(source: str, target: str) -> None:
    """Tạo hard link; tách riêng để test giả lập được filesystem không hỗ trợ."""
    os.link(source, target)


def commit_without_overwrite(temp_path: str, path: str) -> bool:
    """Đưa ``temp_path`` thành ``path`` nếu ``path`` chưa tồn tại.

    Trả ``True`` khi đã ghi xong, ``False`` khi ``path`` đã có sẵn (không ghi
    đè). Ưu tiên hard link vì atomic và rẻ; nếu filesystem không hỗ trợ thì
    chuyển sang tạo file độc quyền (``O_EXCL``) rồi copy nội dung.
    """
    try:
        _link(temp_path, path)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno not in LINK_UNSUPPORTED:
            raise
        return _copy_exclusive(temp_path, path)
    _remove_quietly(temp_path)
    return True


def _copy_exclusive(temp_path: str, path: str) -> bool:
    """Fallback: tạo ``path`` với ``O_EXCL`` rồi copy từ ``temp_path``.

    ``O_EXCL`` giữ đúng hợp đồng "không ghi đè" mà không cần hard link. Nếu
    copy lỗi giữa chừng thì xoá file đích dở dang để không để lại rác mang tên
    của file hoàn chỉnh.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        return False
    try:
        target = os.fdopen(fd, "wb")
    except OSError:
        os.close(fd)
        raise
    try:
        with target, open(temp_path, "rb") as source:
            while True:
                chunk = source.read(COPY_CHUNK)
                if not chunk:
                    break
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    _remove_quietly(temp_path)
    return True


def _remove_quietly(path: str) -> None:
    """Xoá file tạm; lỗi ở bước dọn dẹp không được làm hỏng lượt ghi đã xong."""
    try:
        os.remove(path)
    except OSError:
        pass
