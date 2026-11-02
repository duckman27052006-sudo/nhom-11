"""LAN browser upload gateway that forwards files to a TFTP server."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
import tempfile
import threading
from urllib.parse import urlsplit

from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.utils import secure_filename

from .. import enable_utf8_console, tftp_errors
from ..tftp_client import TftpClient
from ..tftp_net import parse_host_port

LOG = logging.getLogger("tftp.web_upload")
CHUNK_SIZE = 64 * 1024
MULTIPART_OVERHEAD = 64 * 1024


class UploadConfigError(ValueError):
    """Invalid or unsafe upload portal configuration."""


def _normalize_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value.split("%", 1)[0].strip("[]"))
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _parse_private_networks(
    values: list[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not values:
        raise UploadConfigError("cần ít nhất một --allowed-network để giới hạn truy cập LAN")
    networks = []
    for value in values:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise UploadConfigError(f"mạng không hợp lệ: {value!r}") from exc
        if not (network.is_private or network.is_loopback):
            raise UploadConfigError(f"chỉ cho phép subnet private/loopback, nhận được {value!r}")
        networks.append(network)
    return tuple(networks)


def validate_bind_host(value: str) -> str:
    """Require a literal private or loopback IP, never a wildcard/public bind."""
    try:
        address = _normalize_ip(value)
    except ValueError as exc:
        raise UploadConfigError("host phải là địa chỉ IP private/loopback cụ thể") from exc
    if address.is_unspecified or not (address.is_private or address.is_loopback):
        raise UploadConfigError("web upload chỉ bind vào IP private/loopback, không bind wildcard")
    return str(address)


def _is_allowed(
    remote: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    try:
        address = _normalize_ip(remote)
    except ValueError:
        return False
    return any(address.version == network.version and address in network for network in networks)


def create_app(
    *,
    allowed_networks: list[str] | None = None,
    tftp_host: str = "127.0.0.1",
    tftp_port: int = 6969,
    max_file_size: int = 100 * 1024 * 1024,
    max_concurrent_uploads: int = 5,
) -> Flask:
    if max_file_size < 1:
        raise UploadConfigError("max_file_size phải lớn hơn 0")
    if not tftp_host.strip():
        raise UploadConfigError("địa chỉ TFTP server không được để trống")
    if not 1 <= tftp_port <= 65535:
        raise UploadConfigError("TFTP port phải trong khoảng 1..65535")
    if max_concurrent_uploads < 1:
        raise UploadConfigError("max_concurrent_uploads phải >= 1")

    networks = _parse_private_networks(allowed_networks or [])

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        MAX_CONTENT_LENGTH=max_file_size + MULTIPART_OVERHEAD,
        TFTP_HOST=tftp_host,
        TFTP_PORT=tftp_port,
        MAX_FILE_SIZE=max_file_size,
        ALLOWED_NETWORKS=networks,
        CSRF_TOKEN=secrets.token_urlsafe(32),
        MAX_CONCURRENT_UPLOADS=max_concurrent_uploads,
        UPLOAD_SEMAPHORE=threading.BoundedSemaphore(max_concurrent_uploads),
    )

    @app.before_request
    def restrict_to_allowed_lan() -> None:
        remote = request.remote_addr or ""
        if not _is_allowed(remote, app.config["ALLOWED_NETWORKS"]):
            abort(403)

    @app.after_request
    def harden_response(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.teardown_request
    def release_upload_slot(_error: BaseException | None) -> None:
        if request.environ.pop("_tftp_upload_slot_acquired", False):
            app.config["UPLOAD_SEMAPHORE"].release()

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            csrf_token=app.config["CSRF_TOKEN"],
            max_file_size=app.config["MAX_FILE_SIZE"],
        )

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.post("/api/upload")
    def upload():
        token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token, app.config["CSRF_TOKEN"]):
            abort(403)

        origin = request.headers.get("Origin")
        if not origin:
            abort(403)
        origin_parts = urlsplit(origin)
        if (
            origin_parts.netloc.lower() != request.host.lower()
            or origin_parts.scheme != request.scheme
        ):
            abort(403)

        semaphore: threading.BoundedSemaphore = app.config["UPLOAD_SEMAPHORE"]
        if not semaphore.acquire(blocking=False):
            return (
                jsonify(
                    error=(
                        f"Đã đủ {app.config['MAX_CONCURRENT_UPLOADS']} lượt gửi đồng thời. "
                        "Vui lòng thử lại sau."
                    )
                ),
                503,
                {"Retry-After": "5"},
            )
        request.environ["_tftp_upload_slot_acquired"] = True

        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify(error="Chọn một file trước khi gửi."), 400
        filename = secure_filename(uploaded.filename)
        if (
            not filename
            or len(filename.encode("utf-8")) > 255
            or filename in {".", ".."}
            or filename.startswith(".")
        ):
            return jsonify(error="Tên file không hợp lệ."), 400

        temporary_path: str | None = None
        size = 0
        try:
            try:
                fd, temporary_path = tempfile.mkstemp(prefix="tftp-web-upload-", suffix=".part")
                with os.fdopen(fd, "wb") as target:
                    while True:
                        chunk = uploaded.stream.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > app.config["MAX_FILE_SIZE"]:
                            return jsonify(error="File vượt quá giới hạn dung lượng."), 413
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            except OSError:
                LOG.exception("could not stage upload from %s", request.remote_addr)
                return jsonify(error="Không thể tiếp nhận file tạm trên máy chủ web."), 507

            if size == 0:
                return jsonify(error="File rỗng không được chấp nhận."), 400

            try:
                TftpClient(app.config["TFTP_HOST"], app.config["TFTP_PORT"]).upload(
                    temporary_path, filename
                )
            except tftp_errors.TftpError as exc:
                if isinstance(exc, tftp_errors.TftpTimeout):
                    status = 504
                else:
                    status = {
                        tftp_errors.ERR_ACCESS_VIOLATION: 403,
                        tftp_errors.ERR_DISK_FULL: 507,
                        tftp_errors.ERR_FILE_EXISTS: 409,
                    }.get(exc.code, 502)
                LOG.warning(
                    "TFTP upload failed for browser client %s, file %s: %s",
                    request.remote_addr,
                    filename,
                    exc,
                )
                return jsonify(error=f"TFTP server từ chối file: {exc.message}"), status
            except OSError:
                LOG.exception(
                    "could not forward upload from %s to TFTP server",
                    request.remote_addr,
                )
                return jsonify(error="Không thể kết nối hoặc gửi file tới TFTP server."), 502

            LOG.info(
                "TFTP upload completed from browser client %s: %s (%d bytes)",
                request.remote_addr,
                filename,
                size,
            )
            return jsonify(filename=filename, size=size), 201
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    LOG.exception("could not remove temporary upload file")

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify(error="Yêu cầu vượt quá giới hạn dung lượng."), 413

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    enable_utf8_console()
    parser = argparse.ArgumentParser(
        prog="tftp-web-upload",
        description="Gateway nhận file qua trình duyệt và chuyển tiếp bằng TFTP trong mạng LAN.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="IP private cụ thể để bind; mặc định 127.0.0.1 (chỉ máy này)",
    )
    parser.add_argument("--port", type=int, default=8080, help="cổng HTTP (mặc định 8080)")
    parser.add_argument(
        "--allowed-network",
        action="append",
        required=True,
        help="subnet private được phép, ví dụ 192.168.1.0/24 (có thể lặp)",
    )
    parser.add_argument(
        "--tftp-server",
        default="127.0.0.1:6969",
        help="TFTP server đích host[:port], IPv6 dùng [::1]:6969",
    )
    parser.add_argument("--max-file-size", type=int, default=100, help="dung lượng tối đa (MiB)")
    parser.add_argument(
        "--max-concurrent-uploads",
        type=int,
        default=5,
        help="số lượt upload đồng thời tối đa (mặc định 5)",
    )
    parser.add_argument("--threads", type=int, default=8, help="số worker Waitress")
    args = parser.parse_args(argv)

    try:
        host = validate_bind_host(args.host)
        _parse_private_networks(args.allowed_network)
        if not args.tftp_server.strip():
            raise UploadConfigError("địa chỉ TFTP server không được để trống")
        tftp_host, tftp_port = parse_host_port(args.tftp_server, 6969)
        if not 1 <= args.port <= 65535:
            raise UploadConfigError("port phải trong khoảng 1..65535")
        if args.threads < 1:
            raise UploadConfigError("threads phải >= 1")
        if args.max_concurrent_uploads < 1:
            raise UploadConfigError("max-concurrent-uploads phải >= 1")
        app = create_app(
            allowed_networks=args.allowed_network,
            tftp_host=tftp_host,
            tftp_port=tftp_port,
            max_file_size=args.max_file_size * 1024 * 1024,
            max_concurrent_uploads=args.max_concurrent_uploads,
        )
    except (UploadConfigError, OSError, ValueError) as exc:
        parser.error(str(exc))

    from waitress import serve

    if ":" in host:
        listen = f"[{host}]:{args.port}"
        serve(app, listen=listen, threads=args.threads, ipv4=False, ipv6=True)
    else:
        serve(app, host=host, port=args.port, threads=args.threads)
    return 0


__all__ = ["UploadConfigError", "create_app", "main", "validate_bind_host"]
