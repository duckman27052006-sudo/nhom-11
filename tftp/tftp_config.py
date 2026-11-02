"""Cấu hình server TFTP tách khỏi code (JSON, chỉ dùng stdlib).

Thiết kế cố ý *strict*: khóa lạ hoặc giá trị sai kiểu bị từ chối khi khởi động
thay vì âm thầm bỏ qua. Điều này tránh trường hợp gõ nhầm ``max_file_sie`` và
server chạy với giới hạn mặc định ngoài ý muốn.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .tftp_net import FAMILY_MODES

DEFAULT_MAX_FILE_SIZE = 256 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 500 * 1024 * 1024
DEFAULT_MIN_FREE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TRANSFER_SECONDS = 3600.0
DEFAULT_CLEANUP_TEMP_AGE = 24 * 3600


class ConfigError(ValueError):
    """Cấu hình không hợp lệ; thông báo dành cho người vận hành."""


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    host6: str | None = None
    port: int = 6969
    root: str = "files"
    family: str = "auto"
    timeout: float = 5.0
    retries: int = 5
    blksize: int = 1468
    allow_overwrite: bool = False
    enable_options: bool = True
    max_clients: int = 32
    max_file_size: int | None = DEFAULT_MAX_FILE_SIZE
    allow_subdirs: bool = False
    max_requests_per_second: int = 100
    tid_port_range: tuple[int, int] | None = None
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    max_total_bytes: int | None = DEFAULT_MAX_TOTAL_BYTES
    evict_oldest: bool = True
    max_transfer_seconds: float | None = DEFAULT_MAX_TRANSFER_SECONDS
    cleanup_temp_age: float = DEFAULT_CLEANUP_TEMP_AGE
    stats_interval: float = 0.0
    log_file: str | None = None
    log_max_bytes: int = 10 * 1024 * 1024
    log_backups: int = 5
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_server_kwargs(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "timeout": self.timeout,
            "retries": self.retries,
            "blksize": self.blksize,
            "allow_overwrite": self.allow_overwrite,
            "enable_options": self.enable_options,
            "max_clients": self.max_clients,
            "max_file_size": self.max_file_size,
            "allow_subdirs": self.allow_subdirs,
            "max_requests_per_second": self.max_requests_per_second,
            "family": self.family,
            "host6": self.host6,
            "tid_port_range": self.tid_port_range,
            "min_free_bytes": self.min_free_bytes,
            "max_total_bytes": self.max_total_bytes,
            "evict_oldest": self.evict_oldest,
            "max_transfer_seconds": self.max_transfer_seconds,
            "cleanup_temp_age": self.cleanup_temp_age,
            "stats_interval": self.stats_interval,
        }

    def describe(self) -> str:
        """Mô tả cấu hình hiệu lực, không chứa bí mật."""
        lines = [
            f"host={self.host}",
            f"host6={self.host6 or '(mặc định)'}",
            f"family={self.family}",
            f"port={self.port}",
            f"root={os.path.abspath(self.root)}",
            f"allow_overwrite={self.allow_overwrite}",
            f"max_file_size={self.max_file_size}",
            f"min_free_bytes={self.min_free_bytes}",
            f"max_total_bytes={self.max_total_bytes}",
            f"evict_oldest={self.evict_oldest}",
            f"max_transfer_seconds={self.max_transfer_seconds}",
            f"tid_port_range={self.tid_port_range}",
            f"max_clients={self.max_clients}",
            f"max_requests_per_second={self.max_requests_per_second}",
        ]
        return "\n".join(lines)


_ALLOWED_KEYS = {
    "host",
    "host6",
    "port",
    "root",
    "family",
    "timeout",
    "retries",
    "blksize",
    "allow_overwrite",
    "enable_options",
    "max_clients",
    "max_file_size",
    "allow_subdirs",
    "max_requests_per_second",
    "tid_port_range",
    "min_free_bytes",
    "max_total_bytes",
    "evict_oldest",
    "max_transfer_seconds",
    "cleanup_temp_age",
    "stats_interval",
    "log_file",
    "log_max_bytes",
    "log_backups",
}


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} phải là số nguyên")
    return value


def _as_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} phải là số")
    return float(value)


def _as_optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    result = _as_int(value, name)
    if result <= 0:
        raise ConfigError(f"{name} phải > 0 hoặc null")
    return result


def _as_optional_positive_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    result = _as_float(value, name)
    if result <= 0:
        raise ConfigError(f"{name} phải > 0 hoặc null")
    return result


def from_mapping(data: dict[str, Any]) -> ServerConfig:
    if not isinstance(data, dict):
        raise ConfigError("cấu hình phải là một object JSON")
    unknown = sorted(set(data) - _ALLOWED_KEYS)
    if unknown:
        raise ConfigError(f"khóa cấu hình không hỗ trợ: {', '.join(unknown)}")

    config = ServerConfig()
    for name in ("host", "root"):
        if name in data:
            if not isinstance(data[name], str) or not data[name].strip():
                raise ConfigError(f"{name} phải là chuỗi không rỗng")
            setattr(config, name, data[name].strip())
    if "host6" in data:
        value = data["host6"]
        if value is not None and not isinstance(value, str):
            raise ConfigError("host6 phải là chuỗi hoặc null")
        config.host6 = value.strip() if isinstance(value, str) and value.strip() else None
    if "family" in data:
        if data["family"] not in FAMILY_MODES:
            raise ConfigError(f"family phải là một trong {', '.join(FAMILY_MODES)}")
        config.family = data["family"]
    if "port" in data:
        config.port = _as_int(data["port"], "port")
    if "timeout" in data:
        config.timeout = _as_float(data["timeout"], "timeout")
    if "retries" in data:
        config.retries = _as_int(data["retries"], "retries")
    if "blksize" in data:
        config.blksize = _as_int(data["blksize"], "blksize")
    if "allow_overwrite" in data:
        if not isinstance(data["allow_overwrite"], bool):
            raise ConfigError("allow_overwrite phải là true/false")
        config.allow_overwrite = data["allow_overwrite"]
    if "enable_options" in data:
        if not isinstance(data["enable_options"], bool):
            raise ConfigError("enable_options phải là true/false")
        config.enable_options = data["enable_options"]
    if "max_clients" in data:
        config.max_clients = _as_int(data["max_clients"], "max_clients")
    if "max_file_size" in data:
        config.max_file_size = _as_optional_positive_int(data["max_file_size"], "max_file_size")
    if "allow_subdirs" in data:
        if not isinstance(data["allow_subdirs"], bool):
            raise ConfigError("allow_subdirs phải là true/false")
        config.allow_subdirs = data["allow_subdirs"]
    if "max_requests_per_second" in data:
        config.max_requests_per_second = _as_int(
            data["max_requests_per_second"], "max_requests_per_second"
        )
    if "tid_port_range" in data:
        value = data["tid_port_range"]
        if value is None:
            config.tid_port_range = None
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            low = _as_int(value[0], "tid_port_range[0]")
            high = _as_int(value[1], "tid_port_range[1]")
            config.tid_port_range = (low, high)
        else:
            raise ConfigError("tid_port_range phải là [low, high] hoặc null")
    if "min_free_bytes" in data:
        config.min_free_bytes = _as_int(data["min_free_bytes"], "min_free_bytes")
    if "max_total_bytes" in data:
        config.max_total_bytes = _as_optional_positive_int(
            data["max_total_bytes"], "max_total_bytes"
        )
    if "evict_oldest" in data:
        if not isinstance(data["evict_oldest"], bool):
            raise ConfigError("evict_oldest phải là true/false")
        config.evict_oldest = data["evict_oldest"]
    if "max_transfer_seconds" in data:
        config.max_transfer_seconds = _as_optional_positive_float(
            data["max_transfer_seconds"], "max_transfer_seconds"
        )
    if "cleanup_temp_age" in data:
        config.cleanup_temp_age = _as_float(data["cleanup_temp_age"], "cleanup_temp_age")
    if "stats_interval" in data:
        config.stats_interval = _as_float(data["stats_interval"], "stats_interval")
    if "log_file" in data:
        value = data["log_file"]
        if value is not None and not isinstance(value, str):
            raise ConfigError("log_file phải là chuỗi hoặc null")
        config.log_file = value
    if "log_max_bytes" in data:
        config.log_max_bytes = _as_int(data["log_max_bytes"], "log_max_bytes")
    if "log_backups" in data:
        config.log_backups = _as_int(data["log_backups"], "log_backups")
    validate(config)
    return config


def validate(config: ServerConfig) -> None:
    if not 0 <= config.port <= 65535:
        raise ConfigError("port phải trong khoảng 0..65535")
    if config.timeout <= 0:
        raise ConfigError("timeout phải > 0")
    if config.retries < 1:
        raise ConfigError("retries phải >= 1")
    if not 8 <= config.blksize <= 65464:
        raise ConfigError("blksize phải trong khoảng 8..65464")
    if config.max_clients < 1:
        raise ConfigError("max_clients phải >= 1")
    if config.max_requests_per_second < 1:
        raise ConfigError("max_requests_per_second phải >= 1")
    if config.min_free_bytes < 0:
        raise ConfigError("min_free_bytes phải >= 0")
    if config.max_total_bytes is not None and config.max_total_bytes <= 0:
        raise ConfigError("max_total_bytes phải > 0 hoặc null")
    if config.cleanup_temp_age < 0:
        raise ConfigError("cleanup_temp_age phải >= 0")
    if config.log_max_bytes < 0:
        raise ConfigError("log_max_bytes phải >= 0")
    if config.log_backups < 0:
        raise ConfigError("log_backups phải >= 0")
    if config.tid_port_range is not None:
        low, high = config.tid_port_range
        if not (1 <= low <= high <= 65535):
            raise ConfigError("tid_port_range phải là (low, high) trong 1..65535")


def load_config(path: str) -> ServerConfig:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ConfigError(f"không đọc được cấu hình {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON không hợp lệ trong {path}: {exc}") from exc
    return from_mapping(data)
