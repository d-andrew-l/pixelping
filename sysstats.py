#!/usr/bin/env python3
"""pixelping system stats: CPU, RAM, load, disks, uptime and service status.

pixelping.py sends this file along with agent.py over ssh (the default), so most
hosts need nothing installed. It can also run standalone as a tiny read-only
HTTP agent:

  python3 sysstats.py --bind 192.168.1.20 --port 9105 --allow 192.168.1.10 \
      --mounts / /srv --services nginx rsyslog

GET /stats returns one JSON object. Stdlib only, no root required.
"""
import argparse
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROC = "/proc"   # tests point this at fixture files
_cpu_lock = threading.Lock()
_cpu_prev = None
_pm2_cache = (0.0, None)


def cpu_percent():
    """Busy % across all CPUs since the previous call (first call: since boot)."""
    global _cpu_prev
    with open(f"{PROC}/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle, total = v[3] + v[4], sum(v[:8])   # idle + iowait; skip guest columns
    with _cpu_lock:
        prev, _cpu_prev = _cpu_prev, (idle, total)
    if prev is None or total == prev[1]:
        return 100.0 * (1 - idle / total) if total else None
    return 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))


def memory():
    m = {}
    with open(f"{PROC}/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0]) * 1024
    total = m["MemTotal"]
    return {"total": total, "used": total - m.get("MemAvailable", m["MemFree"])}


def disks(mounts):
    out = []
    for mnt in mounts:
        try:
            s = os.statvfs(mnt)
        except OSError as e:
            out.append({"mount": mnt, "error": e.strerror})
            continue
        total = s.f_blocks * s.f_frsize
        used = total - s.f_bfree * s.f_frsize
        avail = s.f_bavail * s.f_frsize
        # same % as df: used / (used + available to users)
        out.append({"mount": mnt, "total": total, "used": used,
                    "pct": 100.0 * used / (used + avail) if used + avail else 0.0})
    return out


def services(names):
    """[{name, state}] via `systemctl is-active`; on failure the reason goes in "error"."""
    if not names:
        return []
    err = None
    try:
        r = subprocess.run(["/usr/bin/systemctl", "is-active", *names], capture_output=True,
                           text=True, timeout=5)
        states = r.stdout.split()
        if len(states) != len(names):
            err = (r.stderr.strip() or f"systemctl exited {r.returncode}")[:300]
    except (OSError, subprocess.TimeoutExpired) as e:
        states, err = [], str(e)[:300]
    out = [{"name": n, "state": states[i] if i < len(states) else "unknown"}
           for i, n in enumerate(names)]
    if err:
        for s in out:
            s["error"] = err
    return out


def pm2_apps(max_age=10.0):
    """[{name, state}] from `pm2 jlist` for the current user, cached a few seconds."""
    global _pm2_cache
    ts, apps = _pm2_cache
    if apps is not None and time.time() - ts < max_age:
        return apps
    try:
        r = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=10)
        # keep only name + status: jlist also carries each app's environment
        apps = [{"name": "pm2: " + p["name"], "state": p["pm2_env"]["status"]}
                for p in json.loads(r.stdout)]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        apps = [{"name": "pm2", "state": "unknown"}]
    _pm2_cache = (time.time(), apps)
    return apps


def collect(mounts=(), service_names=(), pm2=False):
    with open(f"{PROC}/loadavg") as f:
        load = [float(x) for x in f.read().split()[:3]]
    with open(f"{PROC}/uptime") as f:
        uptime = float(f.read().split()[0])
    svcs = services(list(service_names))
    if pm2:
        svcs += pm2_apps()
    return {"ts": time.time(), "cpu": cpu_percent(), "mem": memory(), "load": load,
            "uptime": uptime, "cores": os.cpu_count(), "disks": disks(mounts),
            "services": svcs}


def serve(args):
    allowed = set(args.allow)

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, *a):
            pass

        def do_GET(self):
            if allowed and self.client_address[0] not in allowed:
                self.send_error(403)
                return
            if self.path != "/stats":
                self.send_error(404)
                return
            body = json.dumps(collect(args.mounts, args.services)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    cpu_percent()   # prime the CPU delta
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    print(f"sysstats: http://{args.bind}:{args.port}/stats (allow: {', '.join(allowed) or 'any'})",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="read-only system stats agent for pixelping")
    ap.add_argument("--bind", required=True, help="address to listen on")
    ap.add_argument("--port", type=int, default=9105)
    ap.add_argument("--allow", nargs="*", default=[], help="client IPs allowed to connect")
    ap.add_argument("--mounts", nargs="*", default=["/"])
    ap.add_argument("--services", nargs="*", default=[])
    try:
        serve(ap.parse_args())
    except KeyboardInterrupt:
        pass
