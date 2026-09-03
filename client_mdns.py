#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""client_mdns — 對區網廣播 ``aicode web`` 的位址(RFC 6762 的最小子集)。

只用標準函式庫。做兩件事:

1. **主動宣告**:啟動時、以及之後每隔一段時間,對 224.0.0.251:5353 送一份
   unsolicited response,帶 PTR / SRV / TXT / A 四筆。
2. **回答查詢**:收到對 ``_http._tcp.local``(PTR)、自己的服務實例名
   (SRV/TXT)或 ``<host>.local``(A)的查詢時回同一份記錄。

刻意**不**做的事:conflict probing、unicast-response 旗標、IPv6/AAAA、
service subtype。這是一個「讓同網段的人找得到網址」的便利功能,不是通用的
mDNS 實作;做不到的部分寧可不宣告,也不要宣告錯的東西。

安全:廣播的內容只有主機名、port 與服務名 —— **不含密碼、不含專案路徑**。
廣播本身等於把服務暴露到區網,所以 `client_web.enforce_bind_policy` 要求
它必須有密碼,而且不得配 loopback bind(廣播一個別人連不到的位址只會製造
困惑)。
"""
from __future__ import annotations

import ipaddress
import socket
import struct
import threading
import time

GROUP = "224.0.0.251"
PORT = 5353
SERVICE = "_http._tcp.local"
TTL_SECONDS = 120
#: 每次重新宣告的間隔。比 TTL 短,讓記錄不會在正常運作時過期。
ANNOUNCE_INTERVAL_SECONDS = 60

TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_SRV = 33
TYPE_ANY = 255
CLASS_IN = 1
#: cache-flush bit:告訴對方用這份取代舊記錄,而不是併進去。
CACHE_FLUSH = 0x8000


class MdnsError(RuntimeError):
    """廣播的參數不合法(例如 loopback 位址)。"""


def encode_name(name: str) -> bytes:
    """DNS wire 格式的名字。不做壓縮指標 —— 記錄很少,省不了多少。"""
    out = bytearray()
    for label in name.strip(".").split("."):
        raw = label.encode("utf-8")
        if not 0 < len(raw) < 64:
            raise MdnsError(f"mDNS label 長度必須是 1..63: {label!r}")
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    return bytes(out)


def decode_name(payload: bytes, offset: int) -> tuple[str, int]:
    """讀一個名字,回 (名字, 下一個 offset)。支援壓縮指標(查詢端會用)。"""
    labels: list[str] = []
    cursor = offset
    after: int | None = None
    hops = 0
    while cursor < len(payload):
        length = payload[cursor]
        if length == 0:
            cursor += 1
            break
        if length & 0xC0 == 0xC0:
            if cursor + 1 >= len(payload):
                raise MdnsError("截斷的壓縮指標")
            pointer = ((length & 0x3F) << 8) | payload[cursor + 1]
            if after is None:
                after = cursor + 2
            hops += 1
            if hops > 16:
                raise MdnsError("壓縮指標繞圈")
            cursor = pointer
            continue
        start = cursor + 1
        end = start + length
        if end > len(payload):
            raise MdnsError("截斷的 label")
        labels.append(payload[start:end].decode("utf-8", errors="replace"))
        cursor = end
    return ".".join(labels), (after if after is not None else cursor)


def _record(name: str, rtype: int, rdata: bytes, *, flush: bool = True) -> bytes:
    rclass = CLASS_IN | (CACHE_FLUSH if flush else 0)
    return (
        encode_name(name)
        + struct.pack("!HHIH", rtype, rclass, TTL_SECONDS, len(rdata))
        + rdata
    )


def _txt(pairs: dict[str, str]) -> bytes:
    out = bytearray()
    for key, value in pairs.items():
        item = f"{key}={value}".encode("utf-8")
        if len(item) > 255:
            raise MdnsError(f"TXT 項目過長: {key}")
        out.append(len(item))
        out.extend(item)
    return bytes(out) or b"\x00"


def build_response(instance: str, hostname: str, address: str, port: int) -> bytes:
    """一份完整的 mDNS response(PTR + SRV + TXT + A)。"""
    service_instance = f"{instance}.{SERVICE}"
    host_name = f"{hostname}.local"
    answers = [
        _record(SERVICE, TYPE_PTR, encode_name(service_instance), flush=False),
        _record(
            service_instance,
            TYPE_SRV,
            struct.pack("!HHH", 0, 0, port) + encode_name(host_name),
        ),
        _record(service_instance, TYPE_TXT, _txt({"path": "/"})),
        _record(host_name, TYPE_A, ipaddress.IPv4Address(address).packed),
    ]
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, len(answers), 0, 0)
    return header + b"".join(answers)


def parse_questions(payload: bytes) -> list[tuple[str, int]]:
    """回 (名字, 類型)。不是查詢(QR=1)或格式壞掉時回空 list。"""
    if len(payload) < 12:
        return []
    _ident, flags, qdcount = struct.unpack("!HHH", payload[:6])
    if flags & 0x8000:
        return []
    questions: list[tuple[str, int]] = []
    offset = 12
    try:
        for _ in range(qdcount):
            name, offset = decode_name(payload, offset)
            if offset + 4 > len(payload):
                return questions
            qtype, _qclass = struct.unpack("!HH", payload[offset:offset + 4])
            offset += 4
            questions.append((name.lower(), qtype))
    except (MdnsError, struct.error):
        return questions
    return questions


def wants_us(questions: list[tuple[str, int]], instance: str, hostname: str) -> bool:
    service_instance = f"{instance}.{SERVICE}".lower()
    host_name = f"{hostname}.local".lower()
    for name, qtype in questions:
        if name == SERVICE and qtype in (TYPE_PTR, TYPE_ANY):
            return True
        if name == service_instance and qtype in (TYPE_SRV, TYPE_TXT, TYPE_ANY):
            return True
        if name == host_name and qtype in (TYPE_A, TYPE_ANY):
            return True
    return False


def instance_name(address: str, port: int) -> str:
    return f"CodeTrail on {address}-{port}"


class Advertiser:
    """背景 daemon thread:宣告 + 回答查詢。失敗一律不擋住 server 啟動。"""

    def __init__(self, address: str, port: int, *, hostname: str | None = None) -> None:
        parsed = ipaddress.ip_address(address)
        if parsed.is_loopback:
            raise MdnsError(
                f"--mdns 綁在 {address} 沒有意義:廣播出去的位址別人連不到。"
                "請一併指定區網可達的 --hostname。"
            )
        self.address = str(parsed)
        self.port = int(port)
        self.hostname = (hostname or socket.gethostname().split(".")[0] or "codetrail")
        self.instance = instance_name(self.address, self.port)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    # -- lifecycle --
    def start(self) -> bool:
        try:
            self._socket = self._open()
        except OSError as exc:
            print(f"[codetrail] mDNS 廣播啟動失敗({exc});web 服務照常執行。", flush=True)
            return False
        self._thread = threading.Thread(target=self._serve, name="codetrail-mdns", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass

    # -- internals --
    def _open(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", PORT))
        membership = socket.inet_aton(GROUP) + socket.inet_aton(self.address)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.address))
        sock.settimeout(1.0)
        return sock

    def _announce(self) -> None:
        if self._socket is None:
            return
        try:
            self._socket.sendto(
                build_response(self.instance, self.hostname, self.address, self.port),
                (GROUP, PORT),
            )
        except OSError:
            pass

    def _serve(self) -> None:
        self._announce()
        last = time.monotonic()
        while not self._stop.is_set():
            try:
                payload, _sender = self._socket.recvfrom(9000)
            except socket.timeout:
                payload = b""
            except OSError:
                break
            if payload and wants_us(parse_questions(payload), self.instance, self.hostname):
                self._announce()
            now = time.monotonic()
            if now - last >= ANNOUNCE_INTERVAL_SECONDS:
                self._announce()
                last = now
