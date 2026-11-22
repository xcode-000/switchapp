"""Switch status probes: is a switch answering? (no external package)

check(host) pings the switch: with the system `ping` command when it exists (always on Windows, no rights needed),
otherwise with an ICMP socket, otherwise (or when ICMP is blocked) with a TCP connection to a port.
The background loop that calls it for every switch lives in app.py (monitor_round).
"""
import os
import platform
import random
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

WINDOWS = platform.system() == "Windows"
_PING = shutil.which("ping")
_TIME_RE = re.compile(r"[=<]\s*([\d.,]+)\s*ms", re.I)


def _ping_command(host, count, timeout):
    if WINDOWS:
        cmd = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(max(1, int(round(timeout)))), "-i", "0.2", host]
    kw = {"capture_output": True, "timeout": count * (timeout + 1) + 2}
    if WINDOWS:
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        out = subprocess.run(cmd, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.stdout.decode("latin-1", "replace")  # console code page: only "TTL=" and the times are read
    replies = [ln for ln in text.splitlines() if "ttl=" in ln.lower()]  # any language; not "unreachable" lines
    times = []
    for ln in replies:
        m = _TIME_RE.search(ln)
        if m:
            try:
                times.append(float(m.group(1).replace(",", ".")))
            except ValueError:
                pass
    rtt = (sum(times) / len(times)) if times else (0.5 if replies else None)
    return len(replies), rtt


def _checksum(data):
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def _icmp(host, count, timeout):
    """Own ICMP echo (needs rights for a raw socket, or Linux unprivileged ICMP sockets)."""
    try:
        addr = socket.gethostbyname(host)
    except OSError:
        return 0, None
    sock = None
    for kind in (socket.SOCK_DGRAM, socket.SOCK_RAW):
        try:
            sock = socket.socket(socket.AF_INET, kind, socket.IPPROTO_ICMP)
            raw = kind == socket.SOCK_RAW
            break
        except OSError:
            sock = None
    if sock is None:
        return None
    ident = random.randint(1, 0xFFFF)
    got, times = 0, []
    try:
        for seq in range(1, count + 1):
            body = struct.pack("!d", time.time()) + b"switchdoc-status"
            hdr = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
            pkt = struct.pack("!BBHHH", 8, 0, _checksum(hdr + body), ident, seq) + body
            start = time.time()
            try:
                sock.sendto(pkt, (addr, 0))
            except OSError:
                continue
            while True:
                left = timeout - (time.time() - start)
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    data, src = sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    break
                if src[0] != addr:
                    continue
                icmp = data[(data[0] & 0x0F) * 4:] if raw else data
                if len(icmp) < 8:
                    continue
                typ, _code, _ck, rid, rseq = struct.unpack("!BBHHH", icmp[:8])
                if typ == 0 and rseq == seq and (rid == ident or not raw):
                    got += 1
                    times.append((time.time() - start) * 1000)
                    break
    finally:
        sock.close()
    return got, (sum(times) / len(times)) if times else None


def tcp_check(host, port, timeout=1.0):
    start = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return (time.time() - start) * 1000
    except OSError:
        return None


def check(host, count=2, timeout=1.0, tcp_port=None):
    """-> {"ok": bool, "rtt": ms or None, "loss": percent, "method": "ping" | "icmp" | "tcp"}"""
    res, method = None, "ping"
    if _PING:
        res = _ping_command(host, count, timeout)
    if res is None:
        method = "icmp"
        res = _icmp(host, count, timeout)
    if res is not None and res[0] > 0:
        got, rtt = res
        return {"ok": True, "rtt": round(rtt, 1) if rtt is not None else None,
                "loss": round(100 * (count - got) / count), "method": method}
    if tcp_port:
        rtt = tcp_check(host, tcp_port, timeout)
        if rtt is not None:
            return {"ok": True, "rtt": round(rtt, 1), "loss": 0, "method": "tcp"}
    return {"ok": False, "rtt": None, "loss": 100, "method": method if res is not None else "tcp"}


class Loop:
    """Background thread calling tick() every few seconds (one per SwitchDoc process)."""

    def __init__(self, tick, every=10):
        self.tick, self.every = tick, every
        self.thread = None
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            if os.environ.get("SWITCHDOC_NO_MONITOR"):
                return
            self.thread = threading.Thread(target=self._run, name="switchdoc-monitor", daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # never let the loop die
                print("monitor:", repr(e))
            self.stop.wait(self.every)
