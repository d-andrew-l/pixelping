#!/usr/bin/env python3
"""pixelping agent: emits one JSON line per second describing this host's network.

pixelping.py runs it locally, or on a remote host via `ssh host python3 - <args>`
with this file (and sysstats.py) on stdin. Stdlib only, no root required
(process names for other users' sockets need root).

Args: [--stats JSON] [--stats-every SECONDS] ping targets...
The literal target "gateway" is replaced by the default gateway. With --stats
(e.g. '{"mounts": ["/"], "services": ["nginx"], "pm2": false}') records also
carry system stats from sysstats.collect() every few seconds.
"""
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

INTERVAL = 1.0
DNS_EVERY = 5.0
TOP_CONNS = 12


def default_route():
    """Return (iface, gateway_ip) for the IPv4 default route."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if p[1] == "00000000":
                    gw = socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))
                    return p[0], gw
    except OSError:
        pass
    return None, None


def read_ifaces():
    out = {}
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            name = name.strip()
            if name == "lo" or name.startswith("veth"):
                continue
            v = [int(x) for x in data.split()]
            out[name] = {"rx": v[0], "rx_err": v[2], "rx_drop": v[3],
                         "tx": v[8], "tx_err": v[10], "tx_drop": v[11]}
    return out


def link_speed(iface):
    try:
        with open(f"/sys/class/net/{iface}/speed") as f:
            s = int(f.read().strip())
            return s if s > 0 else None
    except (OSError, ValueError):
        return None


def read_tcp():
    with open("/proc/net/snmp") as f:
        lines = [l.split() for l in f if l.startswith("Tcp:")]
    d = dict(zip(lines[0][1:], lines[1][1:]))
    return int(d["OutSegs"]), int(d["RetransSegs"])


USERS_RE = re.compile(r'users:\(\("([^"]+)"')
ACK_RE = re.compile(r"bytes_acked:(\d+)")
RCV_RE = re.compile(r"bytes_received:(\d+)")


def read_conns():
    """{(local, peer): (proc, bytes_rx, bytes_tx)} for established TCP sockets."""
    try:
        txt = subprocess.run(["ss", "-Htinpn", "state", "established"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    conns = {}
    cur = None
    for line in txt.splitlines():
        if not line:
            continue
        if not line[0].isspace():
            p = line.split()
            if len(p) < 4:
                cur = None
                continue
            m = USERS_RE.search(line)
            cur = (p[2], p[3], m.group(1) if m else None)
        elif cur:
            a, r = ACK_RE.search(line), RCV_RE.search(line)
            local, peer, proc = cur
            host = peer.rsplit(":", 1)[0].strip("[]")
            if not (host.startswith("127.") or host == "::1"):
                conns[(local, peer)] = (proc, int(r.group(1)) if r else 0,
                                        int(a.group(1)) if a else 0)
            cur = None
    return conns


class Pinger(threading.Thread):
    """Runs `ping` continuously; accumulates replies/losses between drains."""

    LINE_RE = re.compile(r"icmp_seq=(\d+).*time=([\d.]+) ms")
    MISS_RE = re.compile(r"no answer yet for icmp_seq=(\d+)")

    def __init__(self, target):
        super().__init__(daemon=True)
        self.target = target
        self.lock = threading.Lock()
        self.rtts, self.lost = [], 0

    def run(self):
        while True:
            try:
                p = subprocess.Popen(["ping", "-n", "-O", "-i", "1", "-W", "2", self.target],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1)
                for line in p.stdout:
                    m = self.LINE_RE.search(line)
                    with self.lock:
                        if m:
                            self.rtts.append(float(m.group(2)))
                        elif self.MISS_RE.search(line):
                            self.lost += 1
                p.wait()
            except OSError:
                pass
            # ping exited (e.g. network unreachable): count as loss and retry
            with self.lock:
                self.lost += 1
            time.sleep(1)

    def drain(self):
        with self.lock:
            r, l = self.rtts, self.lost
            self.rtts, self.lost = [], 0
        return {"rtt": max(r) if r else None, "ok": len(r), "lost": l}


def nameserver():
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and p[0] == "nameserver" and ":" not in p[1]:
                    return p[1]
    except OSError:
        pass
    return None


def dns_probe(server):
    """Uncached A query (random label) straight to the resolver. Returns ms or None."""
    qid = random.randint(0, 65535)
    label = "nm%08x" % random.getrandbits(32)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for part in (label, "google", "com"):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        t0 = time.monotonic()
        s.sendto(q, (server, 53))
        while True:
            data, _ = s.recvfrom(512)
            if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == qid:
                return (time.monotonic() - t0) * 1000
    except OSError:
        return None
    finally:
        s.close()


class StatsCollector(threading.Thread):
    """Runs sysstats.collect() in the background: systemctl and pm2 can be slow."""

    def __init__(self, cfg, every):
        super().__init__(daemon=True)
        self.cfg, self.every = cfg, every
        self.result = None  # latest {"stats", "error"}, consumed by drain

    def run(self):
        try:
            import sysstats   # injected by pixelping.py, or next to this file
            sysstats.cpu_percent()   # prime the CPU delta
        except Exception as e:
            self.result = {"stats": None, "error": f"sysstats unavailable: {e}"[:300]}
            return
        while True:
            time.sleep(self.every)
            try:
                st = sysstats.collect(self.cfg.get("mounts", ["/"]), self.cfg.get("services", []),
                                      self.cfg.get("pm2", False))
                self.result = {"stats": st, "error": None}
            except Exception as e:
                self.result = {"stats": None, "error": str(e)[:300]}

    def drain(self):
        r, self.result = self.result, None
        return r


class DnsProber(threading.Thread):
    def __init__(self, server):
        super().__init__(daemon=True)
        self.server = server
        self.result = None  # latest (ms or None), consumed by drain
        self.fresh = False

    def run(self):
        while True:
            ms = dns_probe(self.server)
            self.result, self.fresh = ms, True
            time.sleep(DNS_EVERY)

    def drain(self):
        if not self.fresh:
            return None
        self.fresh = False
        return {"ms": self.result, "ok": self.result is not None}


def parse_args(argv):
    """Return (ping targets, stats config or None, stats interval)."""
    targets, stats, every = [], None, 3.0
    it = iter(argv)
    for a in it:
        if a == "--stats":
            stats = json.loads(next(it))
        elif a == "--stats-every":
            every = float(next(it))
        else:
            targets.append(a)
    return targets, stats, every


def main():
    labels, stats_cfg, stats_every = parse_args(sys.argv[1:])
    iface, gw = default_route()
    targets = [gw if t == "gateway" else t for t in labels]
    pingers = {lab: Pinger(t) for lab, t in zip(labels, targets) if t}
    for p in pingers.values():
        p.start()
    ns = nameserver()
    dns = DnsProber(ns) if ns else None
    if dns:
        dns.start()
    stats = StatsCollector(stats_cfg, stats_every) if stats_cfg is not None else None
    if stats:
        stats.start()

    tools = {t: shutil.which(t) is not None for t in ("ping", "ss")}   # sent once, for --check
    speeds = {}
    prev_if, prev_tcp, prev_conns = read_ifaces(), read_tcp(), read_conns()
    prev_t = time.monotonic()
    while True:
        time.sleep(max(0.0, INTERVAL - (time.monotonic() - prev_t)))
        now = time.monotonic()
        dt = now - prev_t
        cur_if, cur_tcp, cur_conns = read_ifaces(), read_tcp(), read_conns()

        ifaces = {}
        for name, c in cur_if.items():
            p = prev_if.get(name)
            if not p:
                continue
            if name not in speeds:
                speeds[name] = link_speed(name)
            ifaces[name] = {
                "rx_bps": max(0, c["rx"] - p["rx"]) * 8 / dt,
                "tx_bps": max(0, c["tx"] - p["tx"]) * 8 / dt,
                "rx_drop": c["rx_drop"] - p["rx_drop"], "tx_drop": c["tx_drop"] - p["tx_drop"],
                "rx_err": c["rx_err"] - p["rx_err"], "tx_err": c["tx_err"] - p["tx_err"],
                "speed": speeds[name],
            }

        conns = []
        for key, (proc, rx, tx) in cur_conns.items():
            p = prev_conns.get(key)
            if not p:
                continue
            drx, dtx = max(0, rx - p[1]) * 8 / dt, max(0, tx - p[2]) * 8 / dt
            if drx + dtx > 0:
                conns.append({"local": key[0], "peer": key[1], "proc": proc,
                              "rx_bps": drx, "tx_bps": dtx})
        conns.sort(key=lambda c: c["rx_bps"] + c["tx_bps"], reverse=True)

        out_segs, retrans = cur_tcp[0] - prev_tcp[0], cur_tcp[1] - prev_tcp[1]
        with open("/proc/loadavg") as f:
            load = float(f.read().split()[0])

        rec = {
            "ts": time.time(),
            "iface": iface,
            "gateway": gw,
            "ifaces": ifaces,
            "retrans_pct": 100.0 * retrans / out_segs if out_segs > 0 else 0.0,
            "load": load,
            "ping": {lab: p.drain() for lab, p in pingers.items()},
            "dns": dns.drain() if dns else None,
            "conns": conns[:TOP_CONNS],
        }
        sys_rec = stats.drain() if stats else None
        if sys_rec:
            rec["sys"] = sys_rec
        if tools:
            rec["tools"], tools = tools, None
        sys.stdout.write(json.dumps(rec, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        prev_if, prev_tcp, prev_conns, prev_t = cur_if, cur_tcp, cur_conns, now


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
