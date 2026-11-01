"""Tiện ích địa chỉ cho TFTP: hỗ trợ cả IPv4 và IPv6.

TFTP chạy trên UDP nên mọi địa chỉ đều là ``(host, port)`` (IPv4) hoặc
``(host, port, flowinfo, scope_id)`` (IPv6). Gom việc phân giải địa chỉ về một
chỗ để client và server dùng chung, tránh mỗi nơi hard-code ``AF_INET``.
"""

from __future__ import annotations

import socket

#: Địa chỉ socket: 2-tuple cho IPv4, 4-tuple cho IPv6.
Addr = tuple[str, int] | tuple[str, int, int, int]


def is_ipv6_literal(host: str) -> bool:
    """True nếu `host` là địa chỉ IPv6 literal (chấp nhận cả dạng có zone id)."""
    candidate = host.split("%", 1)[0]
    if not candidate or ":" not in candidate:
        return False
    try:
        socket.inet_pton(socket.AF_INET6, candidate)
    except OSError:
        return False
    return True


def _pick(infos: list[tuple]) -> tuple[int, Addr]:
    """Chọn kết quả phân giải: ưu tiên IPv4 để không đổi hành vi cũ."""
    if not infos:
        raise OSError("không phân giải được địa chỉ")
    infos.sort(key=lambda info: info[0] != socket.AF_INET)
    family, _, _, _, sockaddr = infos[0]
    return family, sockaddr


def resolve_remote(host: str, port: int) -> tuple[int, Addr]:
    """Phân giải ``host:port`` phía client. Trả về ``(family, sockaddr)``.

    Ưu tiên IPv4 khi hostname có cả hai bản ghi, nhưng vẫn dùng IPv6 khi
    host chỉ có IPv6 hoặc là IPv6 literal (``::1``).
    """
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    return _pick(list(infos))


def resolve_bind(host: str, port: int) -> tuple[int, Addr]:
    """Phân giải địa chỉ bind phía server.

    Hỗ trợ wildcard ``""``/``"0.0.0.0"`` (IPv4) và ``"::"`` (IPv6). Hostname
    được phân giải như bình thường, ưu tiên IPv4.
    """
    if not host:
        return socket.AF_INET, ("", port)
    if host == "::":
        return socket.AF_INET6, ("::", port)
    if is_ipv6_literal(host):
        return socket.AF_INET6, (host, port)
    infos = socket.getaddrinfo(
        host, port, type=socket.SOCK_DGRAM, flags=socket.AI_PASSIVE
    )
    return _pick(list(infos))


def session_bind_host(host: str, family: int) -> str:
    """Host để bind socket phiên (TID) cùng họ địa chỉ với socket lắng nghe."""
    if family == socket.AF_INET6:
        return "::" if host in ("", "::") else host
    return "" if host in ("", "0.0.0.0") else host
