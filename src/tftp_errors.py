"""Bảng mã lỗi chuẩn RFC 1350 (mục 4) và các ngoại lệ của TFTP.

Mọi lỗi trên đường truyền đều mang theo một `code` tương ứng với bảng trong
RFC 1350 để chuyển thành gói ERROR khi cần trả về cho phía đối diện.
"""

from __future__ import annotations

ERR_NOT_DEFINED = 0
ERR_FILE_NOT_FOUND = 1
ERR_ACCESS_VIOLATION = 2
ERR_DISK_FULL = 3
ERR_ILLEGAL_OPERATION = 4
ERR_UNKNOWN_TID = 5
ERR_FILE_EXISTS = 6
ERR_NO_SUCH_USER = 7

ERROR_MESSAGES = {
    ERR_NOT_DEFINED: "Not defined",
    ERR_FILE_NOT_FOUND: "File not found",
    ERR_ACCESS_VIOLATION: "Access violation",
    ERR_DISK_FULL: "Disk full or allocation exceeded",
    ERR_ILLEGAL_OPERATION: "Illegal TFTP operation",
    ERR_UNKNOWN_TID: "Unknown transfer ID",
    ERR_FILE_EXISTS: "File already exists",
    ERR_NO_SUCH_USER: "No such user",
}


def error_message(code: int) -> str:
    """Trả về thông báo mặc định của một mã lỗi TFTP."""
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES[ERR_NOT_DEFINED])


class TftpError(Exception):
    """Lỗi TFTP mang mã lỗi RFC 1350."""

    code = ERR_NOT_DEFINED

    def __init__(self, message: str = "", code: int | None = None) -> None:
        if code is not None:
            self.code = code
        self.message = message or error_message(self.code)
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class TftpFileNotFound(TftpError):
    code = ERR_FILE_NOT_FOUND


class TftpAccessViolation(TftpError):
    code = ERR_ACCESS_VIOLATION


class TftpDiskFull(TftpError):
    code = ERR_DISK_FULL


class TftpIllegalOperation(TftpError):
    code = ERR_ILLEGAL_OPERATION


class TftpUnknownTid(TftpError):
    code = ERR_UNKNOWN_TID


class TftpFileExists(TftpError):
    code = ERR_FILE_EXISTS


class TftpNoSuchUser(TftpError):
    code = ERR_NO_SUCH_USER


class TftpFormatError(TftpError):
    """Gói tin nhận được không đúng định dạng RFC 1350."""

    code = ERR_ILLEGAL_OPERATION


class TftpTimeout(TftpError):
    """Hết số lần thử lại mà vẫn không nhận được gói tin hợp lệ."""

    def __init__(self, message: str = "Timeout: không nhận được phản hồi hợp lệ") -> None:
        super().__init__(message, code=ERR_NOT_DEFINED)


class TftpRemoteError(TftpError):
    """Phía đối diện (server/client) gửi về gói ERROR."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message or error_message(code), code=code)
