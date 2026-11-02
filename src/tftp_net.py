"""Tiện ích địa chỉ IPv4/IPv6 dùng chung cho client và server.

Module này gom mọi chỗ "hiểu" địa chỉ mạng về một nơi để tránh hai bên xử lý
khác nhau:

- ``getaddrinfo()`` trả về sockaddr đầy đủ (IPv4 2-tuple, IPv6 4-tuple có
  ``flowinfo`` và ``scope_id``). Không bao giờ cắt IPv6 còn ``(host, port)``.
- Địa chỉ link-local cần zone/interface ID: ``fe80::1%12``. Nếu hệ điều hành
  không tự hiểu chuỗi zone, module tách zone và gán ``scope_id`` thủ công.
- IPv4-mapped IPv6 (``::ffff:127.0.0.1``) được chuẩn hóa về IPv4 khi so sánh
  để rate limit/log/session key không tạo hai danh tính giả.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Literal

FamilyMode = Literal["auto", "ipv4", "ipv6", "dual"]

FAMILY_MODES: tuple[str, ...] = ("auto", "ipv4", "ipv6", "dual")


@dataclass(frozen=True)
class ResolvedAddress:
    """Một kết quả ``getaddrinfo()`` đã lọc theo họ địa chỉ."""

    family: int
    sockaddr: tuple
    host: str
    port: int
    scope_id: int = 0

    @property
    def is_ipv6(self) -> bool:
        return self.family == socket.AF_INET6

    @property
    def family_name(self) -> str:
        return "IPv6" if self.is_ipv6 else "IPv4"


def _strip_brackets(text: str) -> str:
    return text[1:-1] if text.startswith("[") and text.endswith("]") else text


def parse_host_port(value: str, default_port: int) -> tuple[str, int]:
    """Parse ``host[:port]``, hỗ trợ IPv6 ``[::1]:6969`` và zone ``[fe80::1%12]``.

    Địa chỉ IPv6 không ngoặc có nhiều dấu ``:`` được coi là host (không có port),
    giữ tương thích với cú pháp cũ.
    """
    text = value.strip()
    if not text:
        return "127.0.0.1", default_port
    if text.startswith("["):
        if "]:" in text:
            host_part, _, port_part = text.rpartition("]:")
            host = host_part.lstrip("[")
            if not port_part:
                return host or "127.0.0.1", default_port
            try:
                port = int(port_part)
            except ValueError:
                raise ValueError(f"port không hợp lệ: {port_part!r}") from None
            if not 1 <= port <= 65535:
                raise ValueError(f"port ngoài khoảng 1..65535: {port}")
            return host or "127.0.0.1", port
        if text.endswith("]"):
            return _strip_brackets(text) or "127.0.0.1", default_port
        raise ValueError(f"địa chỉ IPv6 không hợp lệ: {value!r}")
    if ":" in text:
        if text.count(":") != 1:
            return text, default_port
        host, _, port_part = text.rpartition(":")
        if not port_part:
            return host or "127.0.0.1", default_port
        try:
            port = int(port_part)
        except ValueError:
            raise ValueError(f"port không hợp lệ: {port_part!r}") from None
        if not 1 <= port <= 65535:
            raise ValueError(f"port ngoài khoảng 1..65535: {port}")
        return host or "127.0.0.1", port
    return text, default_port


def _zone_to_scope_id(zone: str) -> int:
    if not zone:
        return 0
    if zone.isdigit():
        return int(zone)
    try:
        return socket.if_nametoindex(zone)
    except (AttributeError, OSError):
        return 0


def resolve_addresses(
    host: str, port: int, family: FamilyMode = "auto"
) -> list[ResolvedAddress]:
    """Phân giải hostname/địa chỉ thành danh sách sockaddr theo thứ tự ưu tiên.

    ``family='auto'`` trả cả A và AAAA. Lỗi phân giải được ném lại dưới dạng
    ``OSError`` để caller báo rõ trong log/CLI thay vì đoán.
    """
    if family not in FAMILY_MODES:
        raise ValueError(f"family không hợp lệ: {family!r}")
    if family == "dual":
        family = "auto"

    zone = ""
    base_host = host
    if "%" in host:
        base_host, zone = host.rsplit("%", 1)

    flags = getattr(socket, "AI_NUMERICSERV", 0)
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_DGRAM, 0, flags)
    except socket.gaierror:
        if not zone:
            raise
        # Một số OS không hiểu chuỗi zone; bỏ zone và gán scope_id thủ công.
        infos = socket.getaddrinfo(
            base_host, port, socket.AF_UNSPEC, socket.SOCK_DGRAM, 0, flags
        )

    wanted = {
        "auto": {socket.AF_INET, socket.AF_INET6},
        "ipv4": {socket.AF_INET},
        "ipv6": {socket.AF_INET6},
    }[family]

    scope_id = _zone_to_scope_id(zone)
    resolved: list[ResolvedAddress] = []
    seen: set[tuple] = set()
    sockaddr: tuple
    for info_family, _socktype, _proto, _canon, raw_sockaddr in infos:
        if info_family not in wanted:
            continue
        sockaddr = tuple(raw_sockaddr)
        if info_family == socket.AF_INET6:
            if scope_id:
                sockaddr = (sockaddr[0], sockaddr[1], sockaddr[2], scope_id)
            address_host = sockaddr[0]
            address_scope = int(sockaddr[3]) if len(sockaddr) > 3 else 0
        else:
            address_host = sockaddr[0]
            address_scope = 0
        key = (info_family, normalize_ip(address_host), int(sockaddr[1]), address_scope)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(
            ResolvedAddress(
                family=info_family,
                sockaddr=tuple(sockaddr),
                host=address_host,
                port=int(sockaddr[1]),
                scope_id=address_scope,
            )
        )
    if not resolved:
        raise OSError(f"không có địa chỉ {family} cho {host!r}")
    return resolved


def normalize_ip(host: str) -> str:
    """Chuẩn hóa địa chỉ IP để so sánh; IPv4-mapped IPv6 trở về IPv4."""
    text = _strip_brackets(host)
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text.lower()
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def address_key(sockaddr: tuple) -> tuple[str, int, int]:
    """Khóa chuẩn hóa cho session/rate limit: ``(ip, port, scope_id)``."""
    host = str(sockaddr[0]) if sockaddr else ""
    port = int(sockaddr[1]) if len(sockaddr) > 1 else 0
    scope = int(sockaddr[3]) if len(sockaddr) > 3 else 0
    if normalize_ip(host).count(":") == 0:
        scope = 0
    return normalize_ip(host), port, scope


def same_ip(left: tuple | str, right: tuple | str) -> bool:
    """So sánh phần địa chỉ IP, bỏ qua port và chuẩn hóa IPv4-mapped."""
    left_host = left[0] if isinstance(left, tuple) else left
    right_host = right[0] if isinstance(right, tuple) else right
    return normalize_ip(left_host) == normalize_ip(right_host)


def format_host(host: str) -> str:
    """Thêm ngoặc vuông cho IPv6 khi hiển thị/log."""
    text = _strip_brackets(host.strip())
    if ":" in text:
        return f"[{text}]"
    return text


def format_endpoint(host: str, port: int) -> str:
    return f"{format_host(host)}:{int(port)}"


def sockaddr_for(host: str, port: int, family: int) -> tuple:
    """Tạo sockaddr đúng kiểu; IPv6 link-local giữ zone dưới dạng ``scope_id``."""
    host = _strip_brackets(host.strip())
    if family != socket.AF_INET6:
        return (host, int(port))
    zone = ""
    if "%" in host:
        host, zone = host.rsplit("%", 1)
    scope_id = _zone_to_scope_id(zone)
    if scope_id:
        return (host, int(port), 0, scope_id)
    return (host, int(port), 0, 0)


def format_sockaddr(sockaddr: tuple) -> str:
    if not sockaddr:
        return "?"
    host = str(sockaddr[0])
    port = int(sockaddr[1]) if len(sockaddr) > 1 else 0
    return format_endpoint(host, port)


def is_wildcard(host: str) -> bool:
    return _strip_brackets(host.strip()) in ("", "0.0.0.0", "::")


def family_for_host(host: str, requested: FamilyMode = "auto") -> FamilyMode:
    """Suy ra chế độ family từ host khi người dùng không chỉ định."""
    if requested != "auto":
        return requested
    text = _strip_brackets(host.strip())
    if ":" in text:
        return "ipv6"
    return "ipv4"


def split_family_hosts(
    host: str, host6: str | None, family: FamilyMode
) -> list[tuple[int, str]]:
    """Trả danh sách ``(family, bind_host)`` cho listener của server.

    Dual-stack dùng hai socket riêng (IPv4 + IPv6) thay vì dựa vào
    ``IPV6_V6ONLY`` mặc định khác nhau giữa Windows/Linux.
    """
    mode = family_for_host(host, family)
    host = _strip_brackets(host.strip())
    host6 = _strip_brackets(host6.strip()) if host6 else None
    if mode == "ipv4":
        v4_host = host if ":" not in host else "0.0.0.0"
        return [(socket.AF_INET, v4_host or "0.0.0.0")]
    if mode == "ipv6":
        v6_host = host6 or (host if ":" in host else "::")
        return [(socket.AF_INET6, v6_host or "::")]
    # dual
    v4_host = host if ":" not in host else "0.0.0.0"
    v6_host = host6 or (host if ":" in host else "::")
    return [(socket.AF_INET, v4_host or "0.0.0.0"), (socket.AF_INET6, v6_host or "::")]
