#!/usr/bin/env bash
# Ma trận tương thích: project client <-> tftpd-hpa, atftp <-> project server.
#
# Đây là bằng chứng duy nhất cho các đường OACK / blksize / TID / upload mà bộ
# self-test không thể xác nhận (cả hai đầu đều do project viết). Chạy trên
# Ubuntu (CI dùng ubuntu-latest); cần quyền sudo để chạy in.tftpd.
set -euo pipefail

ROOT_A="$(mktemp -d)"
ROOT_B="$(mktemp -d)"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
SIZE=$((3 * 1024 * 1024))
PORT=6969
SERVER_PID=""
TFTPD_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then kill "$SERVER_PID" 2>/dev/null || true; fi
  if [[ -n "$TFTPD_PID" ]]; then sudo kill "$TFTPD_PID" 2>/dev/null || true; fi
  rm -rf "$ROOT_A" "$ROOT_B"
}
trap cleanup EXIT

echo "== phiên bản =="
"$PYTHON" --version
atftp --version 2>&1 | head -n1 || true
in.tftpd --version 2>&1 | head -n1 || true

echo "== chuẩn bị dữ liệu =="
chmod 755 "$ROOT_A" "$ROOT_B"
"$PYTHON" - "$ROOT_A/pattern.bin" "$SIZE" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
size = int(sys.argv[2])
path.write_bytes(bytes(((i * 31 + 7) % 256) for i in range(size)))
PY
cp "$ROOT_A/pattern.bin" "$ROOT_B/pattern.bin"

start_tftpd() {
  echo "== tftpd-hpa: 127.0.0.1:$PORT (root $ROOT_A) =="
  chmod 666 "$ROOT_A/pattern.bin" 2>/dev/null || true
  # -c cho phép tạo file (WRQ), -s chroot vào root, -u chạy đúng user hiện tại
  # để file tải về/ghi lên không bị đổi chủ sở hữu.
  sudo in.tftpd --listen --address "127.0.0.1:$PORT" --secure -c -u "$(id -un)" "$ROOT_A" &
  TFTPD_PID=$!
  sleep 1
}

stop_tftpd() {
  if [[ -n "$TFTPD_PID" ]]; then
    sudo kill "$TFTPD_PID" 2>/dev/null || true
    wait "$TFTPD_PID" 2>/dev/null || true
    TFTPD_PID=""
  fi
  sleep 0.3
}

start_project_server() {
  echo "== project server: 127.0.0.1:$PORT (root $ROOT_B, windowsize 8) =="
  "$PYTHON" -m tftp.tftp_server --host 127.0.0.1 --port "$PORT" \
    --root "$ROOT_B" --windowsize 8 --log-file /tmp/tftp-interop-server.log &
  SERVER_PID=$!
  for _ in $(seq 1 50); do
    if "$PYTHON" - "$PORT" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(0.2)
try:
    # RRQ tới file không tồn tại: chỉ cần thấy có phản hồi là server đã sống.
    s.sendto(b"\x00\x01no-such-file\x00octet\x00", ("127.0.0.1", int(sys.argv[1])))
    s.recvfrom(1024)
except OSError:
    raise SystemExit(1)
PY
    then return 0; fi
    sleep 0.2
  done
  echo "server không khởi động được" >&2
  cat /tmp/tftp-interop-server.log >&2 || true
  return 1
}

stop_project_server() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
  fi
}

assert_same() {
  local left=$1 right=$2 label=$3
  if ! cmp -s "$left" "$right"; then
    echo "FAIL: $label: nội dung khác nhau" >&2
    exit 1
  fi
  echo "OK: $label"
}

# ---------------------------------------------------------------- hướng 1 ---
start_tftpd
"$PYTHON" -m tftp.tftp_client --server "127.0.0.1:$PORT" --get pattern.bin \
  --save "$ROOT_B/from-tftpd.bin" --blocksize 1468 --overwrite
assert_same "$ROOT_A/pattern.bin" "$ROOT_B/from-tftpd.bin" "project client RRQ <- tftpd-hpa (OACK 1468)"

cp "$ROOT_A/pattern.bin" "$ROOT_B/to-tftpd.bin"
"$PYTHON" -m tftp.tftp_client --server "127.0.0.1:$PORT" --put "$ROOT_B/to-tftpd.bin" \
  --remote uploaded-by-project.bin --blocksize 1468
assert_same "$ROOT_A/pattern.bin" "$ROOT_A/uploaded-by-project.bin" "project client WRQ -> tftpd-hpa"

# Không option, blksize 512 thuần RFC 1350 (đường dễ lệch nhất).
"$PYTHON" -m tftp.tftp_client --server "127.0.0.1:$PORT" --get pattern.bin \
  --save "$ROOT_B/plain.bin" --no-options --overwrite
assert_same "$ROOT_A/pattern.bin" "$ROOT_B/plain.bin" "project client RRQ <- tftpd-hpa (không OACK, 512)"

stop_tftpd

# ---------------------------------------------------------------- hướng 2 ---
start_project_server

atftp --option "blksize 1468" --get --remote-file pattern.bin \
  --local-file "$ROOT_A/from-atftp.bin" 127.0.0.1 "$PORT"
assert_same "$ROOT_A/pattern.bin" "$ROOT_A/from-atftp.bin" "atftp RRQ <- project server (OACK 1468)"

atftp --option "blksize 512" --put --local-file "$ROOT_A/pattern.bin" \
  --remote-file by-atftp.bin 127.0.0.1 "$PORT"
assert_same "$ROOT_A/pattern.bin" "$ROOT_B/by-atftp.bin" "atftp WRQ -> project server (512)"

# File rỗng: đường dễ hỏng của block-number/độ dài.
: > "$ROOT_B/empty.txt"
atftp --get --remote-file empty.txt --local-file "$ROOT_A/empty-back.txt" 127.0.0.1 "$PORT"
assert_same "$ROOT_B/empty.txt" "$ROOT_A/empty-back.txt" "atftp RRQ <- project server (file rỗng)"

stop_project_server
echo "== interop OK =="
