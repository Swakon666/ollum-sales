"""Minimal SOCKS5 stdio tunnel for OpenSSH ProxyCommand.

The proxy URL is read from OLLUM_DEPLOY_PROXY. Credentials are never stored in
this file or printed to stdout.
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import threading
from urllib.parse import unquote, urlparse


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("SOCKS5 proxy closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _connect(proxy_url: str, target_host: str, target_port: int) -> socket.socket:
    proxy = urlparse(proxy_url)
    if proxy.scheme not in {"socks5", "socks5h"} or not proxy.hostname or not proxy.port:
        raise ValueError("OLLUM_DEPLOY_PROXY must be a socks5:// or socks5h:// URL")

    sock = socket.create_connection((proxy.hostname, proxy.port), timeout=20)
    sock.settimeout(20)
    methods = b"\x02" if proxy.username is not None else b"\x00"
    sock.sendall(b"\x05\x01" + methods)
    version, method = _recv_exact(sock, 2)
    if version != 5 or method == 0xFF:
        raise ConnectionError("SOCKS5 proxy rejected authentication methods")

    if method == 2:
        username = unquote(proxy.username or "").encode("utf-8")
        password = unquote(proxy.password or "").encode("utf-8")
        if len(username) > 255 or len(password) > 255:
            raise ValueError("SOCKS5 credentials are too long")
        sock.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
        auth_version, status = _recv_exact(sock, 2)
        if auth_version != 1 or status != 0:
            raise PermissionError("SOCKS5 proxy authentication failed")
    elif method != 0:
        raise ConnectionError(f"Unsupported SOCKS5 authentication method: {method}")

    host_bytes = target_host.encode("idna")
    if len(host_bytes) > 255:
        raise ValueError("Target hostname is too long")
    request = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + struct.pack("!H", target_port)
    sock.sendall(request)
    version, reply, reserved, address_type = _recv_exact(sock, 4)
    if version != 5 or reserved != 0 or reply != 0:
        raise ConnectionError(f"SOCKS5 CONNECT failed with reply code {reply}")
    address_lengths = {1: 4, 4: 16}
    if address_type == 3:
        address_length = _recv_exact(sock, 1)[0]
    elif address_type in address_lengths:
        address_length = address_lengths[address_type]
    else:
        raise ConnectionError(f"Unsupported SOCKS5 address type: {address_type}")
    _recv_exact(sock, address_length + 2)
    sock.settimeout(None)
    return sock


def _stdin_to_socket(sock: socket.socket) -> None:
    try:
        while chunk := os.read(sys.stdin.fileno(), 65536):
            sock.sendall(chunk)
    except (BrokenPipeError, ConnectionError, OSError):
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: socks5_proxy_command.py HOST PORT", file=sys.stderr)
        return 2
    proxy_url = os.environ.get("OLLUM_DEPLOY_PROXY", "")
    if not proxy_url:
        print("OLLUM_DEPLOY_PROXY is not set", file=sys.stderr)
        return 2

    try:
        sock = _connect(proxy_url, sys.argv[1], int(sys.argv[2]))
        threading.Thread(target=_stdin_to_socket, args=(sock,), daemon=True).start()
        while chunk := sock.recv(65536):
            os.write(sys.stdout.fileno(), chunk)
        return 0
    except (ConnectionError, OSError, ValueError) as exc:
        print(f"SOCKS5 tunnel failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
