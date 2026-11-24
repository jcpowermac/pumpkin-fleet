#!/usr/bin/env python3
"""Minecraft status ping — proves a game server is really answering.

Protocol 777 (Java 26.3) framing: varint length prefix + varint packet id.
Flow: handshake (next state=1, status) -> status request (id 0) -> status JSON.
The server never acks the handshake; it waits for the status request.

Usage: python3 mc-ping.py [host] [port]   # default 127.0.0.1 25565
"""
import json
import socket
import struct
import sys

PROTOCOL = 777  # Java 26.3; the version is advisory in the handshake


def varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


def rd_varint(sock: socket.socket) -> int:
    n = 0
    shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connection closed")
        b = b[0]
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n


def recv_packet(sock: socket.socket) -> bytes:
    length = rd_varint(sock)
    buf = b""
    while len(buf) < length:
        chunk = sock.recv(length - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 25565
    with socket.create_connection((host, port), timeout=5) as s:
        s.settimeout(5)
        # Handshake: protocol varint, server address, port, next state=1 (status)
        payload = (
            varint(PROTOCOL)
            + varint(len(host)) + host.encode()
            + struct.pack("!H", port)
            + varint(1)
        )
        s.sendall(varint(len(payload) + 1) + varint(0) + payload)
        # Status request: length=1, packet id 0, no payload
        s.sendall(varint(1) + varint(0))
        body = recv_packet(s)
    # body: varint packet id, then the JSON as a length-prefixed string
    _, i1 = rd_varint_from(body)        # packet id
    _, i2 = rd_varint_from(body[i1:])   # string length
    status = json.loads(body[i1 + i2:])
    desc = status["description"]
    print(json.dumps({
        "version": status["version"],
        "players": status["players"],
        "description": desc.get("text") if isinstance(desc, dict) else desc,
    }, indent=2))


def rd_varint_from(data: bytes):
    n = 0
    shift = 0
    i = 0
    while True:
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


if __name__ == "__main__":
    main()
