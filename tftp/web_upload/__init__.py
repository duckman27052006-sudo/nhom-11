"""Authenticated LAN portal; browsers use HTTP, file transfers use the existing TFTP client."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from .. import enable_utf8_console, tftp_errors
from ..tftp_client import TftpClient
from ..tftp_errors import TftpError
from ..tftp_net import format_endpoint, parse_host_port
from ..web_store import (
    ADMIN,
    MAX_REASON,
    MEMBER,
    SESSION_SECONDS,
    Store,
    StoreError,
    StoreNotFound,
)

LOG = logging.getLogger("tftp.web")
COOKIE = "tftp_session"
USERNAME = re.compile(r"[A-Za-z0-9_]{3,32}\Z")
FORWARDED_HOST = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?\Z"
)


class UploadConfigError(ValueError):
    """Invalid LAN gateway configuration."""


def private_network(value: str):
    network = ipaddress.ip_network(value, strict=False)
    ranges = [ipaddress.ip_network(cidr) for cidr in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
        "100.64.0.0/10", "fc00::/7", "::1/128"
    )]
    for allowed in ranges:
        if (isinstance(network, ipaddress.IPv4Network)
                and isinstance(allowed, ipaddress.IPv4Network)):
            if network.subnet_of(allowed):
                return network
        elif (isinstance(network, ipaddress.IPv6Network)
                and isinstance(allowed, ipaddress.IPv6Network)):
            if network.subnet_of(allowed):
                return network
    raise UploadConfigError(
        "Chỉ chấp nhận subnet private, CGNAT/Tailscale (100.64.0.0/10) hoặc loopback cụ thể."
    )


def _normalized(address: ipaddress.IPv4Address | ipaddress.IPv6Address):
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def forwarded_client(peer, header: str, trusted: list):
    """Rightmost address in X-Forwarded-For that is not one of our own proxies."""
    if not header:
        return peer
    candidates = []
    for part in header.split(","):
        try:
            candidates.append(_normalized(ipaddress.ip_address(part.strip())))
        except ValueError:
            continue
    for candidate in reversed(candidates):
        if not any(candidate in network for network in trusted):
            return candidate
    return peer


def forwarded_origin(headers, fallback: str) -> str:
    """Rebuild the browser origin when a trusted proxy terminates TLS."""
    scheme = (headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    host = (headers.get("X-Forwarded-Host") or "").split(",")[0].strip().lower()
    if scheme not in {"http", "https"} or not host or len(host) > 255:
        return fallback
    if not FORWARDED_HOST.fullmatch(host):
        return fallback
    port = host.rsplit(":", 1)[1] if ":" in host and not host.endswith("]") else ""
    if port:
        if not 1 <= int(port) <= 65535:
            return fallback
        if (scheme, int(port)) in {("https", 443), ("http", 80)}:
            host = host.rsplit(":", 1)[0]
    return f"{scheme}://{host}"


def validate_bind_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value.strip("[]"))
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        private_network(f"{address}/{address.max_prefixlen}")
        return str(address)
    except ValueError as exc:
        raise UploadConfigError(
            "Host phải là IP private, loopback hoặc CGNAT cụ thể."
        ) from exc


def trusted_host_key(value: str) -> str | None:
    """Chuẩn hoá Host header thành khoá so sánh, xử lý đúng IPv6.

    Flask 3.1 dùng ``werkzeug.sansio.utils.host_is_trusted``, hàm này cắt host
    tại dấu ``:`` đầu tiên nên ``[::1]:8080`` bị hiểu thành ``[``. Vì portal
    hỗ trợ bind IPv6, tự chuẩn hoá bằng ``urlsplit``/``ipaddress`` an toàn hơn.
    Trả ``None`` nếu giá trị không phải hostname/IP hợp lệ.
    """
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    value = value.rstrip(".").lower()
    if not value or any(char in value for char in "/\\@ \t\r\n"):
        return None
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address)
    except ValueError:
        pass
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", value) is None:
        return None
    return value


def create_app(config: dict | None = None, *, allowed_networks: list[str] | None = None,
               trusted_proxies: list[str] | None = None,
               tftp_host: str | None = None, tftp_port: int | None = None,
               max_file_size: int | None = None,
               max_concurrent_uploads: int | None = None,
               allowed_hosts: list[str] | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static",
                static_url_path="/web_static")
    app.config.from_mapping(
        FILE_ROOT="files", DATA_DIR="web-data", TFTP_HOST="127.0.0.1", TFTP_PORT=6969,
        TFTP_TIMEOUT=2, TFTP_RETRIES=3, MAX_FILE_SIZE=100 * 1024 * 1024,
        ALLOWED_NETWORKS=["127.0.0.0/8", "::1/128"], TRUSTED_PROXIES=[],
        COOKIE_SECURE=False, MAX_CONCURRENT_UPLOADS=4,
        ALLOWED_HOSTS=["localhost", "127.0.0.1", "::1"],
        HEALTH_CACHE_SECONDS=5.0, HEALTH_TIMEOUT=1.0,
    )
    app.config.update(config or {})
    overrides = dict(ALLOWED_NETWORKS=allowed_networks, TRUSTED_PROXIES=trusted_proxies,
                     TFTP_HOST=tftp_host, TFTP_PORT=tftp_port, MAX_FILE_SIZE=max_file_size,
                     MAX_CONCURRENT_UPLOADS=max_concurrent_uploads,
                     ALLOWED_HOSTS=allowed_hosts)
    app.config.update({key: value for key, value in overrides.items() if value is not None})
    if not app.config["TFTP_HOST"].strip() or not 1 <= app.config["TFTP_PORT"] <= 65535:
        raise UploadConfigError("Địa chỉ hoặc cổng TFTP không hợp lệ.")
    if app.config["MAX_CONCURRENT_UPLOADS"] < 1:
        raise UploadConfigError("Số lượt truyền đồng thời phải lớn hơn 0.")
    if app.config["ALLOWED_HOSTS"] is not None:
        if isinstance(app.config["ALLOWED_HOSTS"], str):
            raise UploadConfigError("ALLOWED_HOSTS phải là danh sách host.")
        configured_hosts = []
        for value in app.config["ALLOWED_HOSTS"]:
            key = trusted_host_key(str(value))
            if key is None:
                raise UploadConfigError(f"Host được phép không hợp lệ: {value!r}")
            configured_hosts.append(key)
        if not configured_hosts:
            raise UploadConfigError("Cần ít nhất một host được phép.")
        app.config["ALLOWED_HOSTS"] = set(configured_hosts)
    if app.config["HEALTH_CACHE_SECONDS"] < 0 or app.config["HEALTH_TIMEOUT"] <= 0:
        raise UploadConfigError("Cấu hình health check không hợp lệ.")
    root = Path(app.config["FILE_ROOT"]).resolve()
    data = Path(app.config["DATA_DIR"]).resolve()
    if data == root or root in data.parents:
        raise UploadConfigError("DATA_DIR phải nằm ngoài thư mục file TFTP.")
    if app.config["MAX_FILE_SIZE"] <= 0:
        raise UploadConfigError("Giới hạn file phải lớn hơn 0.")
    root.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    temporary_root = data / "temporary"
    temporary_root.mkdir(exist_ok=True)
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_FILE_SIZE"] + 64 * 1024
    app.config["MAX_FORM_PARTS"] = 8
    # Werkzeug reads multipart bodies in 64 KiB chunks and may retain CR/LF
    # bytes between chunks. A 64 KiB decoder limit then rejects valid binary
    # files before TFTP runs. Leave bounded parser headroom; file sizes remain
    # independently enforced by MAX_CONTENT_LENGTH and check_size().
    app.config["MAX_FORM_MEMORY_SIZE"] = 512 * 1024
    networks = [private_network(value) for value in app.config["ALLOWED_NETWORKS"]]
    if not networks:
        raise UploadConfigError("Cần ít nhất một subnet LAN được phép.")
    trusted = [private_network(value) for value in app.config["TRUSTED_PROXIES"]]
    store = Store(data / "portal.sqlite3")
    app.extensions["web_store"] = store
    if store.bootstrap_admin:
        LOG.info("Tài khoản đầu tiên nhận quyền quản trị: %s", store.bootstrap_admin)
    slots = threading.BoundedSemaphore(app.config["MAX_CONCURRENT_UPLOADS"])
    app.config["UPLOAD_SEMAPHORE"] = slots

    def fail(message: str, status: int):
        return jsonify(error=message), status

    def state_of(account):
        return store.state(account)

    def session_payload(account, csrf):
        state = state_of(account)
        return dict(user=account["username"] if account and account.get("user_id") else None,
                    id=account["user_id"] if account else None,
                    role=state["role"] if account and account.get("user_id") else None,
                    csrf=csrf, max_file_size=app.config["MAX_FILE_SIZE"],
                    chat=dict(muted=state["muted"], muted_until=state["muted_until"],
                              mute_reason=state["mute_reason"]),
                    can_moderate=state["role"] == ADMIN and bool(account))

    @app.before_request
    def protect():
        allowed_hosts = app.config["ALLOWED_HOSTS"]
        if allowed_hosts is not None:
            raw_host = request.host
            hostname = None
            if raw_host and not any(char in raw_host for char in "/\\@ \t\r\n"):
                try:
                    hostname = urlsplit(f"//{raw_host}").hostname
                except ValueError:
                    pass
            if trusted_host_key(hostname or "") not in allowed_hosts:
                LOG.warning("từ chối Host %r từ %s", raw_host, request.remote_addr)
                return fail("Địa chỉ máy chủ không được phép.", 400)
        if request.path not in {"/api/files", "/api/upload"}:
            request.max_content_length = 16 * 1024
        try:
            address = _normalized(ipaddress.ip_address(request.remote_addr or ""))
        except ValueError:
            return fail("Không xác định được địa chỉ thiết bị.", 403)
        g.client_ip = str(address)
        g.public_origin = request.host_url.rstrip("/")
        if trusted and any(address in network for network in trusted):
            address = forwarded_client(address, request.headers.get("X-Forwarded-For", ""),
                                       trusted)
            g.client_ip = str(address)
            g.public_origin = forwarded_origin(request.headers, g.public_origin)
        if not any(address in network for network in networks):
            LOG.warning("LAN ACL rejected client=%s path=%s", g.client_ip, request.path)
            return fail("Thiết bị không nằm trong mạng LAN được phép.", 403)
        g.token = request.cookies.get(COOKIE, "")
        g.account = store.session(g.token)
        if g.account:
            g.state = store.state(g.account)
            if g.state["banned"]:
                store.drop_session(g.token)
                g.account = g.state = None
                if request.path.startswith("/api/") and request.path != "/api/session":
                    return fail("Tài khoản đã bị khóa. Hãy liên hệ quản trị viên.", 403)
        if request.path.startswith("/api/"):
            if request.path not in {"/api/session", "/api/register", "/api/login"}:
                if not g.account or not g.account["user_id"]:
                    return fail("Vui lòng đăng nhập để tiếp tục.", 401)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("Origin")
                upload_request = request.path in {"/api/files", "/api/upload"}
                if (upload_request and not origin) or (
                        origin and origin != g.public_origin):
                    return fail("Nguồn yêu cầu không hợp lệ.", 403)
                csrf = request.headers.get("X-CSRF-Token", "")
                if (not g.account or not csrf
                        or not secrets.compare_digest(csrf.encode(), g.account["csrf"].encode())):
                    return fail("Phiên biểu mẫu đã hết hạn. Hãy tải lại trang.", 403)

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if not request.path.startswith(app.static_url_path + "/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        messages = {413: "File hoặc yêu cầu vượt quá dung lượng cho phép.",
                    404: "Không tìm thấy nội dung.", 400: "Yêu cầu không hợp lệ.",
                    405: "Thao tác không được hỗ trợ."}
        return fail(messages.get(exc.code, "Không thể xử lý yêu cầu."), exc.code)

    @app.errorhandler(sqlite3.OperationalError)
    def database_error(exc):
        LOG.exception("Database unavailable")
        return fail("Dữ liệu đang bận. Vui lòng thử lại sau.", 503)

    @app.get("/")
    def index():
        return render_template("portal.html")

    health_lock = threading.Lock()
    health_cache: dict[str, tuple[float, dict[str, object], int] | None] = {"entry": None}

    def health_report() -> tuple[dict[str, object], int]:
        checks: dict[str, str] = {}
        healthy = True
        try:
            store.ping()
        except sqlite3.Error as exc:
            LOG.warning("Health check: database lỗi: %s", exc)
            checks["database"] = "error"
            healthy = False
        else:
            checks["database"] = "ok"

        # RRQ một tên chắc chắn không tồn tại: server trả ERROR 1/2 cũng chứng
        # minh TFTP còn sống, mà không đọc file thật. Tên bắt đầu bằng "." bị
        # server coi là file ẩn nên không bao giờ trùng file người dùng.
        probe_name = f".health-{secrets.token_hex(6)}"
        try:
            with tempfile.TemporaryDirectory(dir=temporary_root) as temp:
                probe = TftpClient(
                    app.config["TFTP_HOST"], app.config["TFTP_PORT"],
                    timeout=app.config["HEALTH_TIMEOUT"], retries=1,
                    total_timeout=app.config["HEALTH_TIMEOUT"] * 2,
                )
                probe.download(probe_name, str(Path(temp) / "probe"))
        except tftp_errors.TftpRemoteError:
            checks["tftp"] = "ok"
        except (tftp_errors.TftpError, OSError) as exc:
            LOG.warning("Health check: TFTP không phản hồi: %s", exc)
            checks["tftp"] = "unreachable"
            healthy = False
        else:
            checks["tftp"] = "ok"

        return {"status": "ok" if healthy else "degraded", "checks": checks}, (
            200 if healthy else 503
        )

    @app.get("/healthz")
    def health():
        now = time.monotonic()
        with health_lock:
            entry = health_cache["entry"]
            if entry is not None and now - entry[0] < app.config["HEALTH_CACHE_SECONDS"]:
                return jsonify(entry[1]), entry[2]
            body, status = health_report()
            health_cache["entry"] = (now, body, status)
            return jsonify(body), status

    def session_response(user_id=None):
        token, csrf = store.new_session(g.token, user_id)
        account = store.session(token)
        response = jsonify(session_payload(account, csrf))
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            samesite="Lax", secure=app.config["COOKIE_SECURE"])
        return response

    @app.get("/api/session")
    def session():
        if not g.account or not g.account.get("user_id"):
            if not store.permit("session:" + g.client_ip, 60):
                return fail("Quá nhiều yêu cầu. Hãy đợi một phút.", 429)
            return session_response()
        return jsonify(session_payload(g.account, g.account["csrf"]))

    def credentials():
        body = request.get_json()
        if not isinstance(body, dict):
            return None
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        username = username.strip()
        if not USERNAME.fullmatch(username) or not 8 <= len(password) <= 128:
            return None
        return username, password

    @app.post("/api/register")
    def register():
        if not store.permit("register:" + g.client_ip, 5):
            return fail("Đăng ký quá nhiều lần. Hãy đợi một phút.", 429)
        values = credentials()
        if values is None:
            return fail("Tên tài khoản: 3–32 chữ, số hoặc _. Mật khẩu: 8–128 ký tự.", 400)
        try:
            user_id, _ = store.register(*values)
        except sqlite3.IntegrityError:
            return fail("Tên tài khoản đã được sử dụng.", 409)
        return session_response(user_id)

    @app.post("/api/login")
    def login():
        if not store.permit("login:" + g.client_ip, 10):
            return fail("Đăng nhập quá nhiều lần. Hãy đợi một phút.", 429)
        values = credentials()
        if values is None:
            return fail("Tên tài khoản hoặc mật khẩu không đúng.", 401)
        user_id = store.authenticate(*values)
        if user_id is None:
            return fail("Tên tài khoản hoặc mật khẩu không đúng.", 401)
        state = store.state(store.user(user_id))
        if state["banned"]:
            reason = f" Lý do: {state['ban_reason']}." if state["ban_reason"] else ""
            return fail(f"Tài khoản đã bị khóa.{reason}", 403)
        return session_response(user_id)

    @app.post("/api/logout")
    def logout():
        return session_response()

    def file_path(name: str, *, allow_unicode: bool = False):
        if (not name or (not allow_unicode and not name.isascii())
                or name in {".", ".."} or len(name) > 240
                or name.endswith((".", " ", ".part"))
                or any(ord(char) < 32 or ord(char) == 127 or char in '/\\:<>"|?*' for char in name)
                or name.split(".")[0].upper() in {
                    "CON", "PRN", "AUX", "NUL", "CLOCK$",
                    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
                }):
            return None
        target = root / name
        if target.is_symlink() or target.resolve().parent != root:
            return None
        return target

    @app.get("/api/files")
    def files():
        items = []
        for target in root.iterdir():
            try:
                if file_path(target.name) is not None and target.is_file():
                    stat = target.stat()
                    items.append(dict(name=target.name, size=stat.st_size, modified=stat.st_mtime))
            except OSError:
                continue
        return jsonify(files=sorted(items, key=lambda item: item["name"].casefold()))

    def client():
        return TftpClient(app.config["TFTP_HOST"], app.config["TFTP_PORT"],
                          timeout=app.config["TFTP_TIMEOUT"], retries=app.config["TFTP_RETRIES"])

    def check_size(current, total):
        if current > app.config["MAX_FILE_SIZE"] or (total or 0) > app.config["MAX_FILE_SIZE"]:
            raise ValueError("File vượt quá dung lượng cho phép.")

    def unavailable_message():
        endpoint = format_endpoint(app.config["TFTP_HOST"], app.config["TFTP_PORT"])
        return (
            f"Không kết nối được TFTP tại {endpoint}. "
            "Trên máy chủ, mở TFTP Console và bấm Khởi động server, "
            "hoặc chạy TFTP server riêng. Kiểm tra địa chỉ/cổng --tftp-server nếu server đã chạy."
        )

    @app.get("/api/files/<name>")
    def download(name):
        target = file_path(name)
        if target is None:
            return fail("Tên file không hợp lệ. TFTP yêu cầu tên không dấu (ASCII).", 400)
        if not target.is_file():
            return fail("File không còn tồn tại.", 404)
        if target.stat().st_size > app.config["MAX_FILE_SIZE"]:
            return fail("File vượt quá dung lượng cho phép.", 413)
        if not slots.acquire(blocking=False):
            return fail("Máy chủ đang truyền nhiều file. Hãy thử lại sau.", 503)
        temporary = None
        try:
            temporary = tempfile.TemporaryDirectory(dir=temporary_root)
            local = Path(temporary.name) / "download"
            client().download(name, str(local), progress=check_size)
            response = send_file(local, as_attachment=True, download_name=name,
                                 mimetype="application/octet-stream", conditional=False)
            # Ensure the response closes its file before cleanup, including client disconnects.
            response.direct_passthrough = False
            response.call_on_close(temporary.cleanup)
            response.call_on_close(slots.release)
            return response
        except ValueError:
            if temporary:
                temporary.cleanup()
            slots.release()
            return fail("File vượt quá dung lượng cho phép.", 413)
        except (OSError, TftpError):
            if temporary:
                temporary.cleanup()
            slots.release()
            LOG.exception("TFTP download failed")
            return fail("Không thể tải file từ TFTP. Kiểm tra máy chủ và thử lại.", 502)
        except BaseException:
            if temporary:
                temporary.cleanup()
            slots.release()
            raise

    @app.post("/api/files")
    @app.post("/api/upload")
    def upload():
        incoming = request.files.get("file")
        if incoming is None or file_path(incoming.filename or "", allow_unicode=True) is None:
            return fail("Hãy chọn file có tên hợp lệ.", 400)
        remote = incoming.filename
        if not remote.isascii():
            remote = secure_filename(remote.replace("đ", "d").replace("Đ", "D"))
        if file_path(remote) is None:
            return fail("Không thể chuyển tên file sang tên ASCII hợp lệ.", 400)
        if not slots.acquire(blocking=False):
            response, status = fail("Máy chủ đang truyền nhiều file. Hãy thử lại sau.", 503)
            response.headers["Retry-After"] = "5"
            return response, status
        try:
            with tempfile.TemporaryDirectory(dir=temporary_root) as temp:
                local = Path(temp) / "upload"
                count = 0
                with local.open("wb") as handle:
                    while chunk := incoming.stream.read(64 * 1024):
                        count += len(chunk)
                        check_size(count, None)
                        handle.write(chunk)
                client().upload(str(local), remote)
            LOG.info("Upload from %s by %s", g.client_ip, g.account["username"])
            return jsonify(message="Đã gửi file thành công.", filename=remote, size=count), 201
        except ValueError:
            return fail("File vượt quá dung lượng cho phép.", 413)
        except TftpError as exc:
            status = 504 if isinstance(exc, tftp_errors.TftpTimeout) else {
                tftp_errors.ERR_ACCESS_VIOLATION: 403,
                tftp_errors.ERR_DISK_FULL: 507,
                tftp_errors.ERR_FILE_EXISTS: 409,
            }.get(exc.code, 502)
            messages = {504: unavailable_message(),
                        403: "TFTP server từ chối quyền ghi file.",
                        507: "TFTP server không còn đủ dung lượng.",
                        409: "TFTP server đã có file trùng tên. Hãy đổi tên file."}
            LOG.exception("TFTP upload failed")
            return fail(messages.get(status, "TFTP server không thể tiếp nhận file."), status)
        except (ConnectionError, TimeoutError):
            LOG.exception("TFTP connection failed")
            return fail(unavailable_message(), 504)
        except OSError:
            LOG.exception("TFTP upload failed")
            return fail("Không thể gửi file. Kiểm tra TFTP và chính sách ghi đè.", 502)
        finally:
            slots.release()

    def moderation_args():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise StoreError("Yêu cầu không hợp lệ.")
        active = body.get("active")
        if not isinstance(active, bool):
            raise StoreError("Thiếu trạng thái cần áp dụng.")
        minutes = body.get("minutes")
        if minutes is not None and (not isinstance(minutes, int) or isinstance(minutes, bool)
                                    or not 1 <= minutes <= 10080):
            raise StoreError("Thời hạn phải từ 1 phút đến 7 ngày.")
        reason = body.get("reason", "")
        if not isinstance(reason, str) or len(reason) > MAX_REASON:
            raise StoreError(f"Lý do tối đa {MAX_REASON} ký tự.")
        return active, None if minutes is None else int(minutes), reason.strip()

    def duration_label(minutes):
        if not minutes:
            return "vĩnh viễn"
        if minutes % 1440 == 0:
            return f"{minutes // 1440} ngày"
        if minutes % 60 == 0:
            return f"{minutes // 60} giờ"
        return f"{minutes} phút"

    def record(action, target_id, target, detail):
        store.log(g.account["user_id"], g.account["username"], action, target_id, target, detail)

    def admin_only(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if state_of(g.account)["role"] != ADMIN:
                return fail("Chỉ quản trị viên mới được thực hiện thao tác này.", 403)
            return view(*args, **kwargs)
        return wrapper

    @app.get("/api/messages")
    def messages():
        after = request.args.get("after")
        if after is not None and (not re.fullmatch(r"[0-9]{1,18}", after)):
            return fail("Mốc tin nhắn không hợp lệ.", 400)
        known = request.args.get("epoch")
        if known is not None and not re.fullmatch(r"[0-9]{1,18}", known):
            return fail("Mốc trò chuyện không hợp lệ.", 400)
        epoch = store.epoch()
        # A moderation action deleted messages, so the cursor is no longer trustworthy.
        reset = after is not None and known is not None and int(known) != epoch
        rows = store.messages(None if reset or after is None else int(after))
        state = state_of(g.account)
        return jsonify(messages=rows, epoch=epoch, reset=reset, chat=state,
                       me=g.account["user_id"], can_moderate=state["role"] == ADMIN)

    @app.post("/api/messages")
    def send_message():
        state = state_of(g.account)
        if state["muted"]:
            reason = f" Lý do: {state['mute_reason']}." if state["mute_reason"] else ""
            return fail(f"Bạn đang bị hạn chế gửi tin nhắn.{reason}", 403)
        body = request.get_json()
        text = body.get("body") if isinstance(body, dict) else None
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            return fail("Tin nhắn phải có từ 1 đến 2.000 ký tự.", 400)
        if not store.permit(f"chat:{g.account['user_id']}", 30):
            return fail("Bạn gửi quá nhanh. Hãy đợi một phút.", 429)
        message_id = store.send(g.account["user_id"], text.strip())
        return jsonify(id=message_id), 201

    @app.delete("/api/messages/<int:message_id>")
    def delete_message(message_id):
        admin = state_of(g.account)["role"] == ADMIN
        try:
            result = store.delete_message(message_id, g.account["user_id"], admin=admin)
        except StoreNotFound as exc:
            return fail(str(exc), 404)
        except StoreError as exc:
            return fail(str(exc), 403)
        if admin:
            record("delete", result["user_id"], result["username"], f"Xóa tin nhắn #{message_id}")
        return jsonify(deleted=message_id, message="Đã xóa tin nhắn.")

    @app.delete("/api/messages")
    @admin_only
    def clear_messages():
        scope = request.args.get("scope", "all")
        raw_user = request.args.get("user_id")
        if scope == "user":
            if not raw_user or not re.fullmatch(r"[0-9]{1,18}", raw_user):
                return fail("Tài khoản cần xóa không hợp lệ.", 400)
        elif scope != "all":
            return fail("Phạm vi xóa không hợp lệ.", 400)
        try:
            result = store.purge_messages(scope, int(raw_user) if raw_user else None)
        except StoreNotFound as exc:
            return fail(str(exc), 404)
        except StoreError as exc:
            return fail(str(exc), 400)
        detail = ("Xóa toàn bộ lịch sử chat" if scope == "all"
                  else f"Xóa lịch sử chat của {result['username']}")
        detail += f" · {result['deleted']} tin nhắn"
        record("purge", int(raw_user) if scope == "user" and raw_user else None,
               result["username"] if scope == "user" else "phòng chat chung", detail)
        return jsonify(**result, message=detail)

    @app.get("/api/admin/overview")
    @admin_only
    def admin_overview():
        data = store.overview()
        data["me"] = g.account["user_id"]
        return jsonify(data)

    @app.post("/api/admin/users/<int:user_id>/ban")
    @admin_only
    def admin_ban(user_id):
        try:
            if user_id == g.account["user_id"]:
                raise StoreError("Không thể tự khóa tài khoản của mình.")
            active, minutes, reason = moderation_args()
            result = store.set_ban(user_id, active, minutes, reason)
        except StoreNotFound as exc:
            return fail(str(exc), 404)
        except StoreError as exc:
            return fail(str(exc), 400)
        if active:
            detail = f"Khóa {duration_label(minutes)}"
            detail += f" · {reason}" if reason else ""
        else:
            detail = "Mở khóa tài khoản"
        record("ban" if active else "unban", result["id"], result["username"], detail)
        return jsonify(**result, message=detail)

    @app.post("/api/admin/users/<int:user_id>/mute")
    @admin_only
    def admin_mute(user_id):
        try:
            if user_id == g.account["user_id"]:
                raise StoreError("Không thể tự hạn chế chat của mình.")
            active, minutes, reason = moderation_args()
            result = store.set_mute(user_id, active, minutes, reason)
        except StoreNotFound as exc:
            return fail(str(exc), 404)
        except StoreError as exc:
            return fail(str(exc), 400)
        if active:
            detail = f"Hạn chế chat {duration_label(minutes)}"
            detail += f" · {reason}" if reason else ""
        else:
            detail = "Bỏ hạn chế chat"
        record("mute" if active else "unmute", result["id"], result["username"], detail)
        return jsonify(**result, message=detail)

    @app.post("/api/admin/users/<int:user_id>/role")
    @admin_only
    def admin_role(user_id):
        try:
            if user_id == g.account["user_id"]:
                raise StoreError("Không thể tự đổi vai trò của mình.")
            body = request.get_json(silent=True)
            role = body.get("role") if isinstance(body, dict) else None
            if role not in (ADMIN, MEMBER):
                raise StoreError("Vai trò không hợp lệ.")
            result = store.set_role(user_id, role)
        except StoreNotFound as exc:
            return fail(str(exc), 404)
        except StoreError as exc:
            return fail(str(exc), 400)
        detail = "Cấp quyền quản trị" if role == ADMIN else "Hạ quyền quản trị viên"
        if result["changed"]:
            record("role", result["id"], result["username"], detail)
        return jsonify(**result, message=detail)

    return app


def main(argv=None) -> int:
    enable_utf8_console()
    parser = argparse.ArgumentParser(
        prog="python -m tftp.web_upload",
        description="Cổng file và chat TFTP trong LAN",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--root", default="files", help="Cùng root với TFTP server")
    parser.add_argument("--data-dir", default="web-data")
    parser.add_argument("--allowed-network", action="append")
    parser.add_argument("--trusted-proxy", action="append", metavar="CIDR",
                        help="Proxy tin cậy được đọc X-Forwarded-For/Proto/Host; lặp lại được")
    parser.add_argument("--tftp-server", default="127.0.0.1:6969")
    parser.add_argument("--max-file-size", type=int, default=100, help="MiB mỗi file")
    parser.add_argument("--secure-cookie", action="store_true", help="Chỉ bật sau HTTPS proxy")
    parser.add_argument("--trusted-host", action="append", default=[], metavar="HOST",
                        help="Host header được phép thêm (tên miền nội bộ); lặp lại được")
    parser.add_argument("--max-concurrent-uploads", type=int, default=4)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--admin", metavar="USERNAME",
                        help="Cấp quyền quản trị cho tài khoản đã có trong data-dir")
    args = parser.parse_args(argv)
    try:
        host = ipaddress.ip_address(validate_bind_host(args.host))
        if not 1 <= args.port <= 65535:
            raise ValueError("Port phải nằm trong khoảng 1–65535.")
        tftp_host, tftp_port = parse_host_port(args.tftp_server, 6969)
        if args.threads < 1:
            raise ValueError("Số worker phải lớn hơn 0.")
        networks = args.allowed_network or ([f"{host}/{host.max_prefixlen}"] if host.is_loopback
                                            else [])
        allowed_hosts = [str(host), "localhost", "127.0.0.1", "::1", *args.trusted_host]
        app = create_app(dict(FILE_ROOT=args.root, DATA_DIR=args.data_dir,
                              ALLOWED_NETWORKS=networks, TFTP_HOST=tftp_host,
                              TFTP_PORT=int(tftp_port), MAX_FILE_SIZE=args.max_file_size * 1024**2,
                              MAX_CONCURRENT_UPLOADS=args.max_concurrent_uploads,
                              ALLOWED_HOSTS=allowed_hosts,
                              TRUSTED_PROXIES=args.trusted_proxy or [],
                              COOKIE_SECURE=args.secure_cookie))
        if args.admin:
            store = app.extensions["web_store"]
            target = store.user_by_name(args.admin.strip())
            if target is None:
                raise ValueError(f"Không tìm thấy tài khoản {args.admin.strip()!r} trong data-dir.")
            store.set_role(target["id"], ADMIN)
    except (ValueError, StoreError) as exc:
        parser.error(str(exc))
    from waitress import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("LAN portal: http://%s:%s; TFTP=%s; root=%s", host, args.port,
             args.tftp_server, Path(args.root).resolve())
    if args.trusted_proxy:
        LOG.info("Tin cậy X-Forwarded-* từ: %s", ", ".join(args.trusted_proxy))
    serve(app, host=str(host), port=args.port, threads=args.threads,
          max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
          ipv4=host.version == 4, ipv6=host.version == 6,
          # Trusted-proxy handling lives in create_app and validates the socket peer,
          # so Waitress must not strip forwarded headers before Flask sees them.
          clear_untrusted_proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
