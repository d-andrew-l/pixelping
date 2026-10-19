#!/usr/bin/env python3
"""pixelping: live network dashboard for this machine and LAN servers.

Starts an agent per host (local subprocess or over SSH), keeps ~15 minutes of
per-second history in memory, detects incidents, logs them to ./logs, and serves
a dashboard at http://127.0.0.1:8765

The agent also reports system stats (sysstats.py is sent along with it), or a
host can point stats.url at a sysstats HTTP agent instead. If configured, log
watchdog reports are accepted on a separate LAN-only listener: POST
/api/watchdog with a Bearer token.

Usage: python3 pixelping.py [--config PATH]   (default: config.json next to this file)
"""
import argparse
import collections
import csv
import functools
import hmac
import json
import os
import queue
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
STATE_DIR = HERE / "state"
STATIC = HERE / "static"
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
HISTORY = 900  # seconds kept in memory
WATCHDOG_FILE = STATE_DIR / "watchdog.json"

CONFIG, CONFIG_DIR = {}, HERE   # set by load_config()
STATS_EVERY = 3
lock = threading.Lock()
hosts = {}                      # name -> HostState
events = collections.deque(maxlen=500)
subscribers = set()             # queues of SSE clients
host_names = {}                 # ip -> host name, for peer labels
target_labels = {}              # ping target -> display label
rdns_cache, rdns_queue = {}, queue.Queue()
watchdog = None                 # latest watchdog report, persisted in WATCHDOG_FILE


def load_config(path):
    global CONFIG, CONFIG_DIR, STATS_EVERY, host_names, target_labels
    path = Path(path).resolve()
    CONFIG, CONFIG_DIR = json.loads(path.read_text()), path.parent
    STATS_EVERY = CONFIG.get("stats_interval", 3)
    host_names = {h["ip"]: h["name"] for h in CONFIG["hosts"] if h.get("ip")}
    target_labels = {h["ip"]: h.get("label", h["name"]) for h in CONFIG["hosts"] if h.get("ip")}
    target_labels.update(CONFIG.get("labels", {}))


@functools.lru_cache(maxsize=None)
def agent_script():
    """agent.py with sysstats.py injected as a module, so one stdin stream carries both."""
    stats_src = (HERE / "sysstats.py").read_text()
    prelude = ("import sys as _s, types as _t\n"
               "_m = _t.ModuleType('sysstats')\n"
               f"exec(compile({stats_src!r}, 'sysstats.py', 'exec'), _m.__dict__)\n"
               "_s.modules['sysstats'] = _m\n")
    return prelude + (HERE / "agent.py").read_text()


def now_iso(ts=None):
    return datetime.fromtimestamp(ts or time.time()).isoformat(timespec="seconds")


def broadcast(msg):
    data = json.dumps(msg, separators=(",", ":"))
    for q in list(subscribers):
        try:
            q.put_nowait(data)
        except queue.Full:
            pass


def log_event(host, kind, severity, text, **extra):
    ev = {"ts": time.time(), "host": host, "kind": kind, "severity": severity,
          "text": text, **extra}
    with lock:
        events.append(ev)
    with open(LOG_DIR / f"events-{datetime.now():%Y-%m-%d}.jsonl", "a") as f:
        f.write(json.dumps({**ev, "time": now_iso(ev["ts"])}) + "\n")
    print(f"[{now_iso()}] {severity.upper():8} {host}: {text}", flush=True)
    broadcast({"type": "event", "event": ev})


# ---------------------------------------------------------------- reverse DNS
def label_peer(addr):
    ip = addr.rsplit(":", 1)[0].strip("[]")
    if ip in host_names:
        return host_names[ip]
    if ip not in rdns_cache:
        rdns_cache[ip] = None
        rdns_queue.put(ip)
    return rdns_cache[ip]


def rdns_worker():
    while True:
        ip = rdns_queue.get()
        try:
            rdns_cache[ip] = socket.gethostbyaddr(ip)[0]
        except OSError:
            rdns_cache[ip] = ""


# ---------------------------------------------------------------- detection
class TargetTracker:
    """Tracks one ping target on one host: outages and latency spikes."""

    def __init__(self, host, target):
        self.host, self.target = host, target
        self.window = collections.deque(maxlen=60)   # (ok, lost) per second
        self.baseline = collections.deque(maxlen=300)
        self.down_since = None
        self.good_streak = 0
        self.spike_since = None
        self.spike_peak = 0.0

    def update(self, r, ts):
        if r is None:
            return
        self.window.append((r["ok"], r["lost"]))
        name = f"{self.target} ping"
        if r["lost"] and not r["ok"]:
            self.good_streak = 0
            if self.down_since is None:
                self.down_since = ts
                log_event(self.host, "loss", "critical", f"{name}: no replies (outage started)",
                          target=self.target)
        elif r["ok"]:
            self.good_streak += 1
            if self.down_since is not None and self.good_streak >= 3:
                dur = ts - self.down_since
                log_event(self.host, "loss", "good",
                          f"{name}: recovered after {dur:.0f}s without replies",
                          target=self.target, duration=dur)
                self.down_since = None

        rtt = r["rtt"]
        if rtt is None:
            return
        base = statistics.median(self.baseline) if len(self.baseline) >= 30 else None
        spiking = base is not None and rtt > max(base * 4, base + 40)
        if spiking:
            self.spike_peak = max(self.spike_peak, rtt)
            if self.spike_since is None:
                self.spike_since = ts
        else:
            self.baseline.append(rtt)
            if self.spike_since is not None:
                dur = ts - self.spike_since
                if dur >= 2:
                    log_event(self.host, "latency", "serious",
                              f"{name}: latency spike for {dur:.0f}s, peak {self.spike_peak:.0f} ms "
                              f"(normal ~{base:.1f} ms)", target=self.target, duration=dur)
                self.spike_since, self.spike_peak = None, 0.0

    def summary(self):
        ok = sum(o for o, _ in self.window)
        lost = sum(l for _, l in self.window)
        total = ok + lost
        return {
            "loss_pct": 100.0 * lost / total if total else None,
            "baseline": statistics.median(self.baseline) if self.baseline else None,
            "down": self.down_since is not None,
        }


class HostState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.samples = collections.deque(maxlen=HISTORY)
        self.online = False
        self.error = None
        self.targets = {}
        self.dns_bad_since = None
        self.saturated_since = None
        self.minute = []
        self.stats = None           # latest sysstats.collect() result
        self.stats_error = None

    def ingest(self, rec):
        ts = rec["ts"]
        main = rec["ifaces"].get(rec["iface"]) or {}
        for label, r in rec["ping"].items():
            tr = self.targets.setdefault(label, TargetTracker(self.name, label))
            tr.update(r, ts)

        d = rec.get("dns")
        if d is not None:
            bad = (not d["ok"]) or d["ms"] > 1000
            if bad and self.dns_bad_since is None:
                self.dns_bad_since = ts
                msg = "DNS lookup failed" if not d["ok"] else f"DNS lookup slow ({d['ms']:.0f} ms)"
                log_event(self.name, "dns", "serious", msg)
            elif not bad and self.dns_bad_since is not None:
                log_event(self.name, "dns", "good",
                          f"DNS healthy again after {ts - self.dns_bad_since:.0f}s")
                self.dns_bad_since = None

        speed = main.get("speed")
        if speed:
            util = max(main["rx_bps"], main["tx_bps"]) / (speed * 1e6)
            if util > 0.8 and self.saturated_since is None:
                self.saturated_since = ts
                log_event(self.name, "saturation", "warning",
                          f"{rec['iface']} above 80% of its {speed} Mbps link")
            elif util < 0.6 and self.saturated_since is not None:
                self.saturated_since = None
        errs = main.get("rx_err", 0) + main.get("tx_err", 0)
        if errs > 0:
            log_event(self.name, "errors", "warning", f"{rec['iface']}: {errs} interface errors")

        for c in rec["conns"]:
            c["peer_name"] = label_peer(c["peer"])

        if rec.get("sys"):
            self.set_stats(rec["sys"].get("stats"), rec["sys"].get("error"))

        slim = {"ts": ts, "rx": main.get("rx_bps", 0), "tx": main.get("tx_bps", 0),
                "retrans": rec["retrans_pct"], "load": rec["load"],
                "ping": {k: v["rtt"] if v else None for k, v in rec["ping"].items()},
                "lost": {k: v["lost"] if v else 0 for k, v in rec["ping"].items()},
                "dns": d["ms"] if d and d["ok"] else (-1 if d else None)}
        with lock:
            self.samples.append(slim)
            self.last = rec
        self.write_csv(slim, rec)
        broadcast({"type": "sample", "host": self.name, "sample": slim, "live": self.live()})

    def write_csv(self, slim, rec):
        """Aggregate to one CSV row per minute for later review."""
        if self.minute and int(slim["ts"] // 60) != int(self.minute[0]["ts"] // 60):
            rows = self.minute
            self.minute = []
            path = LOG_DIR / f"minutes-{datetime.now():%Y-%m-%d}.csv"
            new = not path.exists()
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                labels = sorted({k for r in rows for k in r["ping"]})
                if new:
                    w.writerow(["time", "host", "rx_avg_mbps", "rx_max_mbps", "tx_avg_mbps",
                                "tx_max_mbps", "retrans_pct_max", "pings(label:max_ms/lost)"])
                pings = []
                for lab in labels:
                    vals = [r["ping"].get(lab) for r in rows if r["ping"].get(lab) is not None]
                    lost = sum(r["lost"].get(lab, 0) for r in rows)
                    pings.append(f"{lab}:{max(vals) if vals else 'na'}/{lost}")
                rx = [r["rx"] / 1e6 for r in rows]
                tx = [r["tx"] / 1e6 for r in rows]
                w.writerow([now_iso(rows[0]["ts"]), self.name,
                            f"{sum(rx)/len(rx):.2f}", f"{max(rx):.2f}",
                            f"{sum(tx)/len(tx):.2f}", f"{max(tx):.2f}",
                            f"{max(r['retrans'] for r in rows):.2f}", " ".join(pings)])
        self.minute.append(slim)

    def live(self):
        rec = getattr(self, "last", None)
        return {
            "online": self.online, "error": self.error,
            "iface": rec and rec["iface"],
            "speed": rec and (rec["ifaces"].get(rec["iface"]) or {}).get("speed"),
            "conns": rec["conns"] if rec else [],
            "targets": {k: t.summary() for k, t in self.targets.items()},
        }

    def agent_cmd(self):
        args = CONFIG["ping_targets"] + self.cfg.get("extra_targets", [])
        if self.stats_via_agent():
            args = ["--stats", json.dumps(self.cfg["stats"]), "--stats-every", str(STATS_EVERY), *args]
        if self.cfg.get("ssh"):
            # ssh joins the remote command into one shell string: quote every argument
            return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                    "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                    self.cfg["ssh"], "python3", "-", *map(shlex.quote, args)]
        return [sys.executable, "-", *args]

    def run(self):
        cmd = self.agent_cmd()
        backoff = 2
        while True:
            started = time.time()
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
                p.stdin.write(agent_script())
                p.stdin.close()
                for line in p.stdout:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not self.online:
                        self.online, self.error = True, None
                        log_event(self.name, "agent", "good", "collector connected")
                    self.ingest(rec)
                p.wait()
                err = p.stderr.read().strip().splitlines()
                self.error = err[-1] if err else f"agent exited ({p.returncode})"
            except OSError as e:
                self.error = str(e)
            if self.online:
                log_event(self.name, "agent", "critical", f"collector disconnected: {self.error}")
            self.online = False
            broadcast({"type": "sample", "host": self.name, "sample": None, "live": self.live()})
            if self.stats_via_agent():
                self.set_stats(self.stats, "network collector offline")
            backoff = 2 if time.time() - started > 60 else min(backoff * 2, 30)
            time.sleep(backoff)

    def stats_live(self):
        return {"stats": self.stats, "error": self.stats_error}

    def stats_via_agent(self):
        cfg = self.cfg.get("stats")
        return bool(cfg) and not cfg.get("url")

    def set_stats(self, st, err):
        """Store a stats result (from the agent or an HTTP poll) and log state changes."""
        failing = self.stats_error is not None
        if err:
            self.stats_error = err
            if not failing:
                log_event(self.name, "stats", "warning", f"system stats unavailable: {err}")
        else:
            self.stats, self.stats_error = st, None
            if failing:
                log_event(self.name, "stats", "good", "system stats reachable again")
        broadcast({"type": "stats", "host": self.name, **self.stats_live()})

    def run_stats(self):
        """Poll system stats from the host's sysstats HTTP agent, if stats.url is set."""
        url = (self.cfg.get("stats") or {}).get("url")
        if not url:
            return
        while True:
            try:
                with urllib.request.urlopen(url, timeout=4) as r:
                    self.set_stats(json.loads(r.read(1_000_000)), None)
            except (OSError, ValueError) as e:
                self.set_stats(None, str(getattr(e, "reason", None) or e))
            time.sleep(STATS_EVERY)


# ---------------------------------------------------------------- watchdog reports
SEVERITIES = ("High", "Medium", "Low", "Quiet")


def clean_text(v, limit):
    if not isinstance(v, str):
        raise ValueError("expected a string")
    return v[:limit]


def validate_watchdog(p):
    """Return a cleaned copy of a watchdog payload or raise ValueError."""
    if not isinstance(p, dict):
        raise ValueError("payload must be a JSON object")
    ts = clean_text(p.get("ts"), 64)
    datetime.fromisoformat(ts.replace("Z", "+00:00"))   # must parse as ISO 8601
    if p.get("severity") not in SEVERITIES:
        raise ValueError("severity must be one of " + "|".join(SEVERITIES))
    hosts_, rules = p.get("hosts", []), p.get("rules", [])
    if not isinstance(hosts_, list) or not isinstance(rules, list):
        raise ValueError("hosts and rules must be lists")
    sup = p.get("suppressed", 0)
    if not isinstance(sup, int) or isinstance(sup, bool) or sup < 0:
        raise ValueError("suppressed must be a non-negative integer")
    return {"ts": ts, "severity": p["severity"],
            "hosts": [clean_text(h, 100) for h in hosts_[:50]],
            "summary": clean_text(p.get("summary", ""), 20_000),
            "rules": [clean_text(r, 2_000) for r in rules[:300]],
            "rules_total": len(rules), "suppressed": sup}


def load_watchdog():
    global watchdog
    try:
        watchdog = json.loads(WATCHDOG_FILE.read_text())
    except (OSError, ValueError):
        watchdog = None


def save_watchdog(report):
    global watchdog
    tmp = WATCHDOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(report))
    os.replace(tmp, WATCHDOG_FILE)   # atomic: never leaves a half-written file
    watchdog = report
    broadcast({"type": "watchdog", "watchdog": report})


class WatchdogHandler(BaseHTTPRequestHandler):
    """LAN listener that accepts only POST /api/watchdog from the watchdog host."""
    timeout = 10
    allow, token = set(), b""

    def log_message(self, *a):
        pass

    def reply(self, code, msg):
        body = json.dumps({"ok": code == 200, "message": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        ip = self.client_address[0]
        if ip not in self.allow:
            return self.reply(403, "forbidden")
        if self.path != "/api/watchdog":
            return self.reply(404, "not found")
        auth = self.headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and
                hmac.compare_digest(auth[7:].strip().encode(), self.token)):
            log_event("watchdog", "push", "warning", f"rejected report from {ip}: bad token")
            return self.reply(401, "unauthorized")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.reply(411, "Content-Length required")
        if not 0 < n <= 256_000:
            return self.reply(413, "payload too large")
        try:
            report = validate_watchdog(json.loads(self.rfile.read(n)))
        except (ValueError, RecursionError) as e:
            return self.reply(400, f"invalid payload: {e}")
        report["received"] = time.time()
        save_watchdog(report)
        print(f"[{now_iso()}] watchdog report: {report['severity']}, {len(report['rules'])} rules", flush=True)
        self.reply(200, "stored")

    def do_GET(self):
        self.reply(405, "POST only")

    do_PUT = do_DELETE = do_PATCH = do_GET


def start_watchdog_listener():
    cfg = CONFIG.get("watchdog")
    if not cfg:
        return
    try:
        # relative paths resolve next to the config file
        token = (CONFIG_DIR / cfg.get("token_file", "watchdog.token")).read_text().strip()
    except OSError as e:
        print(f"watchdog listener disabled: cannot read token ({e})", flush=True)
        return
    if len(token) < 32:
        print("watchdog listener disabled: token shorter than 32 characters", flush=True)
        return
    WatchdogHandler.allow = set(cfg["allow"])
    WatchdogHandler.token = token.encode()
    addr = (cfg["bind"], cfg.get("port", 8766))

    def serve():
        while True:   # the LAN address may not be up yet right after boot
            try:
                srv = ThreadingHTTPServer(addr, WatchdogHandler)
                break
            except OSError as e:
                print(f"watchdog listener: cannot bind {addr[0]}:{addr[1]} ({e}), retrying", flush=True)
                time.sleep(10)
        srv.daemon_threads = True
        print(f"watchdog listener: http://{addr[0]}:{addr[1]}/api/watchdog "
              f"(allow: {', '.join(cfg['allow'])})", flush=True)
        srv.serve_forever()

    threading.Thread(target=serve, daemon=True).start()


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_body(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_body((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/static/"):
            # fonts + pixel art engine, bundled so the dashboard works with the internet down
            f = (STATIC / self.path[len("/static/"):].split("?")[0]).resolve()
            if f.is_file() and STATIC in f.parents and f.suffix in STATIC_TYPES:
                self.send_body(f.read_bytes(), STATIC_TYPES[f.suffix])
            else:
                self.send_error(404)
        elif self.path == "/api/snapshot":
            with lock:
                snap = {
                    "hosts": [{"name": h.name, "label": h.cfg.get("label", h.name),
                               "samples": list(h.samples), "live": h.live(),
                               "sys": h.stats_live() if h.cfg.get("stats") else None}
                              for h in hosts.values()],
                    "events": list(events),
                    "targets": CONFIG["ping_targets"],
                    "labels": target_labels,
                    "watchdog": watchdog,
                    "watchdog_enabled": bool(CONFIG.get("watchdog")),
                }
            self.send_body(json.dumps(snap).encode(), "application/json")
        elif self.path == "/api/stream":
            q = queue.Queue(maxsize=2000)
            subscribers.add(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f"data: {data}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                subscribers.discard(q)
        else:
            self.send_error(404)


# ---------------------------------------------------------------- --check
SSH_HINTS = (
    ("Permission denied", "set up key login from this machine: ssh-copy-id {ssh}"),
    ("Host key verification failed", "connect once by hand to accept the host key: ssh {ssh}"),
    ("Could not resolve", "check the ssh name, or add it to ~/.ssh/config"),
    ("timed out", "is the host up and reachable on port 22?"),
    ("python3", "install python3 on the host"),
)


def run_agent_briefly(h, seconds):
    """Run a host's agent for a few seconds. Returns (records, last stderr line)."""
    p = subprocess.Popen(h.agent_cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, bufsize=1)
    lines = queue.Queue()

    def pump():
        for line in p.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    try:
        p.stdin.write(agent_script())
        p.stdin.close()
    except OSError:
        pass
    recs, deadline = [], time.time() + seconds
    while time.time() < deadline:
        try:
            line = lines.get(timeout=max(0.1, deadline - time.time()))
        except queue.Empty:
            break
        if line is None:
            break
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
        if len(recs) >= 4 and (not h.stats_via_agent() or any("sys" in r for r in recs)):
            break
    p.kill()
    p.wait()
    err = p.stderr.read().strip().splitlines()
    return recs, (err[-1] if err else "")


def check_host(h):
    """Returns [(level, message)] for one host."""
    try:
        recs, err = run_agent_briefly(h, max(8, STATS_EVERY + 5))
    except OSError as e:
        return [("FAIL", f"cannot start the agent: {e}")]
    if not recs:
        out = [("FAIL", f"agent did not start: {err or 'no output'}")]
        for needle, hint in SSH_HINTS if h.cfg.get("ssh") else ():
            if needle in err:
                out.append(("HINT", hint.format(ssh=h.cfg["ssh"])))
        return out
    first = recs[0]
    out = [("OK", f"agent running, default route via {first['iface']} to {first['gateway']}")]
    for tool, found in (first.get("tools") or {}).items():
        if not found:
            out.append(("FAIL", f"'{tool}' not found on the host (Debian: apt install "
                                f"{'iputils-ping' if tool == 'ping' else 'iproute2'})"))
    labels = CONFIG["ping_targets"] + h.cfg.get("extra_targets", [])
    for lab in labels:
        rs = [r["ping"][lab] for r in recs if r["ping"].get(lab)]
        if not rs:
            out.append(("WARN", f"ping {lab}: not started (no default gateway?)"))
            continue
        ok, lost = sum(r["ok"] for r in rs), sum(r["lost"] for r in rs)
        name = target_labels.get(lab, lab)
        if ok:
            out.append(("OK", f"ping {name}: {ok} replies, {lost} lost"))
        else:
            out.append(("WARN", f"ping {name}: no replies yet ({lost} lost)"))
    dns = [r["dns"] for r in recs if r.get("dns")]
    if dns:
        out.append(("OK", f"DNS lookup {dns[-1]['ms']:.0f} ms") if dns[-1]["ok"] else ("WARN", "DNS lookup failed"))
    if h.stats_via_agent():
        sys_recs = [r["sys"] for r in recs if r.get("sys")]
        if not sys_recs:
            out.append(("FAIL", "no system stats from the agent"))
        else:
            out += stats_report(sys_recs[-1].get("stats"), sys_recs[-1].get("error"))
    elif (h.cfg.get("stats") or {}).get("url"):
        try:
            with urllib.request.urlopen(h.cfg["stats"]["url"], timeout=4) as r:
                out += stats_report(json.loads(r.read(1_000_000)), None)
        except (OSError, ValueError) as e:
            out.append(("FAIL", f"stats url: {getattr(e, 'reason', None) or e}"))
    return out


def stats_report(st, err):
    if err or not st:
        return [("FAIL", f"system stats: {err or 'empty'}")]
    cpu = "?" if st.get("cpu") is None else f"{st['cpu']:.0f}"
    out = [("OK", f"system stats: CPU {cpu}%, {len(st['disks'])} disks, "
                  f"{len(st['services'])} services")]
    for d in st["disks"]:
        if d.get("error"):
            out.append(("WARN", f"disk {d['mount']}: {d['error']}"))
    for s in st["services"]:
        if s["state"] not in ("active", "online"):
            out.append(("WARN", f"service {s['name']}: {s['state']}"))
    return out


def check():
    """--check: validate the config and try every host once. Exit 1 on any FAIL."""
    results = [("config", [])]
    cfg_out = results[0][1]
    names = [h.get("name") for h in CONFIG["hosts"]]
    if not names or not all(names) or len(set(names)) != len(names):
        cfg_out.append(("FAIL", "every host needs a unique \"name\""))
    if not CONFIG.get("ping_targets"):
        cfg_out.append(("WARN", "ping_targets is empty"))
    if any(h.get("ssh") for h in CONFIG["hosts"]) and not shutil.which("ssh"):
        cfg_out.append(("FAIL", "ssh client not found on this machine"))
    port = CONFIG.get("port", 8765)
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
        cfg_out.append(("OK", f"dashboard port {port} is free"))
    except OSError:
        cfg_out.append(("WARN", f"port {port} is in use (pixelping already running?)"))
    wd = CONFIG.get("watchdog")
    if wd:
        try:
            ok = len((CONFIG_DIR / wd.get("token_file", "watchdog.token")).read_text().strip()) >= 32
            cfg_out.append(("OK", "watchdog token found") if ok else ("FAIL", "watchdog token shorter than 32 characters"))
        except OSError as e:
            cfg_out.append(("FAIL", f"watchdog token: {e}"))
        if not wd.get("bind") or not wd.get("allow"):
            cfg_out.append(("FAIL", "watchdog needs \"bind\" and \"allow\""))
    if not cfg_out or all(lvl == "OK" for lvl, _ in cfg_out):
        cfg_out.insert(0, ("OK", f"{len(names)} hosts, {len(CONFIG.get('ping_targets', []))} ping targets"))

    host_states = [HostState(c) for c in CONFIG["hosts"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for h, out in zip(host_states, pool.map(check_host, host_states)):
            where = f"ssh {h.cfg['ssh']}" if h.cfg.get("ssh") else "this machine"
            results.append((f"{h.cfg.get('label', h.name)} ({where})", out))
    fails = 0
    for title, out in results:
        print(f"\n{title}")
        for level, msg in out:
            print(f"  {level:4}  {msg}")
            fails += level == "FAIL"
    print(f"\n{'all checks passed' if not fails else f'{fails} problem(s) found'}")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="live LAN network dashboard")
    ap.add_argument("--config", default=str(HERE / "config.json"),
                    help="config file (default: config.json next to pixelping.py)")
    ap.add_argument("--check", action="store_true",
                    help="test the config and every host once, print a report and exit")
    args = ap.parse_args()
    try:
        load_config(args.config)
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"pixelping: cannot load config {args.config}: {e}. "
                 "Copy config.example.json to config.json and edit it.")
    if args.check:
        sys.exit(check())
    LOG_DIR.mkdir(exist_ok=True)
    STATE_DIR.mkdir(exist_ok=True)
    threading.Thread(target=rdns_worker, daemon=True).start()
    load_watchdog()
    for cfg in CONFIG["hosts"]:
        h = HostState(cfg)
        hosts[h.name] = h
        threading.Thread(target=h.run, daemon=True).start()
        threading.Thread(target=h.run_stats, daemon=True).start()
    start_watchdog_listener()
    port = CONFIG.get("port", 8765)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"pixelping dashboard: http://127.0.0.1:{port}  (logs in {LOG_DIR})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
