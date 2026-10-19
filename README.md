# PixelPing

[![tests](https://github.com/d-andrew-l/pixelping/actions/workflows/test.yml/badge.svg)](https://github.com/d-andrew-l/pixelping/actions/workflows/test.yml)

_A pixel-art window onto your LAN._

A small, live network and system dashboard for a home or office LAN. One Python
process on one machine watches every host you list: per-second throughput, ping
latency and loss to your router and the internet, DNS lookup times, TCP
retransmits, the busiest connections, plus CPU, RAM, disks and service status.
Incidents (outages, latency spikes, slow DNS, saturated links) are detected,
logged and shown in a pixel-art web UI that works fine on a wall-mounted kiosk.

- **Python 3.9+ standard library only.** No pip, no database, no Node.
- **Nothing to install on monitored hosts.** The agent is streamed over ssh.
- **Read-only.** PixelPing never changes anything on the hosts it watches.

<!-- TODO: add a screenshot at docs/screenshot.png and link it here -->

## Quickstart

```bash
git clone https://github.com/d-andrew-l/pixelping.git
cd pixelping
cp config.example.json config.json   # then edit hosts and ping targets
python3 pixelping.py --check         # tests the config and every host
python3 pixelping.py
```

Open http://127.0.0.1:8765. With no `ssh` hosts in the config, PixelPing only
watches the machine it runs on. To watch it from another computer, use an ssh
tunnel: `ssh -L 8765:127.0.0.1:8765 your-server`.

Use another config file with `python3 pixelping.py --config /path/to/config.json`.

### Installing with an AI agent

[AGENTS.md](AGENTS.md) is a step-by-step setup guide written for AI coding
agents (Claude Code, Codex, Cursor and others). Open the repo in your agent and
ask it to "set up PixelPing following AGENTS.md". It asks you which machines to
watch, sets up ssh keys with you, writes `config.json`, runs `--check` until it
passes, and can set up start on boot. It works as a manual checklist too.

### Fonts and pixel art

The UI loads its fonts and pixel-art engine from `static/` (`static/pixel.js` and
`static/fonts/*.woff2`), which are not part of this repository. Without them the
dashboard still works, with system fonts and no planet artwork. Fonts used:
Pixelify Sans and Atkinson Hyperlegible (both under the SIL Open Font License).

## Requirements

- **Dashboard host:** Linux with Python 3.9+ and an ssh client.
- **Monitored hosts:** Linux with `python3`, iputils `ping` and `ss` (iproute2).
  The dashboard host needs key-based ssh access to each one (PixelPing uses
  `BatchMode=yes`, so password prompts fail instead of hanging).
- Service status uses `systemctl`; pm2 apps use `pm2 jlist` (pm2 must be on the
  `PATH` of a non-interactive ssh session).

## Install step by step (Linux)

Tested on Debian, Ubuntu and Raspberry Pi OS. Run these on the machine that
will host the dashboard (an always-on box is best).

1. **Install the basics** (most systems already have them):
   `sudo apt install git python3 openssh-client iputils-ping iproute2`
2. **Get the code:** `git clone https://github.com/d-andrew-l/pixelping.git ~/pixelping && cd ~/pixelping`
3. **Configure:** `cp config.example.json config.json`, then edit it. Replace
   the example hosts with yours, leave out `ssh` for this machine, and delete
   the `watchdog` block if you don't use one.
4. **ssh keys** for each remote host: `ssh-keygen -t ed25519` (once, empty
   passphrase), then `ssh-copy-id HOST`. Check with `ssh -o BatchMode=yes HOST true`.
   Each remote host needs `python3`, `ping` and `ss`.
5. **Check:** `python3 pixelping.py --check` runs the agent on every host for a
   few seconds and reports `OK`, `WARN` or `FAIL` with hints. Fix any `FAIL`.
6. **Run:** `python3 pixelping.py` and open http://127.0.0.1:8765.
7. **Keep it running:** see "Running it permanently" below.

## Supported platforms

| Role | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Dashboard server (`pixelping.py`) | yes | no | WSL2 only |
| Watched host (agent over ssh) | yes | no | no |
| Viewing the dashboard | browser | browser via ssh tunnel | browser via ssh tunnel, or `desktop/` app |

The server and agent read `/proc` and use Linux `ping` and `ss`, so watched
hosts must be Linux (including Raspberry Pi OS and most NAS systems with ssh).

## Using it from Windows

Run PixelPing on a Linux machine on your LAN, then on Windows (10 and 11 include
OpenSSH):

1. Make sure `ssh SERVER` works from PowerShell, ideally with a key
   (`ssh-keygen`, then add `%USERPROFILE%\.ssh\id_ed25519.pub` to the server's
   `~/.ssh/authorized_keys`).
2. **In a browser:** run `ssh -N -L 8765:127.0.0.1:8765 SERVER` in PowerShell,
   keep it open, and browse to http://127.0.0.1:8765.
3. **As a desktop app:** `desktop/` is a small Electron shell that starts
   PixelPing on the server if needed, opens the tunnel and shows the dashboard
   in its own window. Download an Electron release zip from
   https://github.com/electron/electron/releases, set the user environment
   variable `PIXELPING_SSH` to your server's ssh alias (and `PIXELPING_DIR` if
   PixelPing is not in `~/pixelping` there), then run
   `electron.exe path\to\pixelping\desktop`. Make a shortcut to that command
   to pin it.

Trying it without a Linux box: install WSL2 (`wsl --install`) and follow the
Linux steps inside it. The "local" host then shows the WSL virtual machine's
network, not Windows itself, but remote Linux hosts work normally.

## How it works

```
                     ssh (agent.py + sysstats.py on stdin)
 pixelping.py  ──────────────────────────────────────────▶  host A: python3 -
   │   ▲         one JSON line per second on stdout          host B: python3 -
   │   └───────────────────────────────────────────────────  (no files, no ports)
   │
   ├── keeps 15 min of history in memory, detects incidents
   ├── logs/events-YYYY-MM-DD.jsonl and logs/minutes-YYYY-MM-DD.csv
   ├── http://127.0.0.1:8765  index.html, /api/snapshot, /api/stream (SSE)
   └── optional LAN listener: POST /api/watchdog (log watchdog reports)
```

- **agent.py** runs on each host (locally for hosts without `ssh`). It reads
  `/proc`, runs one `ping` per target and `ss` for connections, and prints one
  JSON record per second. PixelPing sends it over ssh on every connect, so updating
  PixelPing updates every agent.
- **sysstats.py** collects CPU, memory, load, disks, uptime and services. It is
  bundled into the agent stream, and every `stats_interval` seconds the agent's
  record includes a `sys` block.
- **Optional HTTP stats agent.** For hosts where you would rather not allow ssh
  from the dashboard, run `sysstats.py` as a hardened read-only HTTP service and
  set `stats.url`. `deploy/install-agent.sh` installs it with systemd sandboxing
  and an nftables rule (see below).
- **Incidents** are written to `logs/` and shown in the UI: ping outages and
  recoveries, latency spikes (4x baseline and +40 ms), DNS failures or lookups
  over 1 s, links above 80% of their speed, interface errors, agents and stats
  going away and coming back.

## Configuration

Copy `config.example.json` to `config.json` (git-ignored). Keys starting with
`_` are comments and are ignored.

| Key | Default | Meaning |
| --- | --- | --- |
| `port` | `8765` | Dashboard port. Always bound to `127.0.0.1`. |
| `stats_interval` | `3` | Seconds between system stats samples. |
| `ping_targets` | required | Targets pinged from every host. `"gateway"` is each host's default gateway. |
| `labels` | `{}` | Display names for plain ping targets, e.g. `{"1.1.1.1": "Cloudflare"}`. Host IPs get their host label automatically. |
| `hosts[].name` | required | Unique id, used in logs. |
| `hosts[].label` | `name` | Display name. |
| `hosts[].ip` | none | LAN address. Labels pings and connections to this host. |
| `hosts[].ssh` | none | ssh destination or `~/.ssh/config` alias. Omit to run the agent locally. |
| `hosts[].extra_targets` | `[]` | Extra ping targets for this host only. |
| `hosts[].stats` | none | Omit to skip system stats for this host. |
| `hosts[].stats.mounts` | `["/"]` | Mount points to show disk usage for. |
| `hosts[].stats.services` | `[]` | systemd units to show the state of. |
| `hosts[].stats.pm2` | `false` | Also list the ssh user's pm2 apps. |
| `hosts[].stats.url` | none | Poll a `sysstats.py` HTTP agent instead of reading stats over ssh. |
| `watchdog` | none | Omit to disable the watchdog listener and hide its pane. |
| `watchdog.bind` | required | LAN address to listen on. |
| `watchdog.port` | `8766` | Listener port. |
| `watchdog.allow` | required | Client IPs allowed to push reports. |
| `watchdog.token_file` | `watchdog.token` | File with the Bearer token (at least 32 characters), relative to the config file. |

Hosts with `stats` get a system pane in the overview at the top of the page;
every host gets a network pane further down.

## Running it permanently

- **systemd:** `deploy/systemd/pixelping.service` (edit the user and paths).
- **pm2:** `pm2 start pixelping.py --interpreter python3 --name pixelping`.
- **Kiosk:** `deploy/kiosk/` has an `xinitrc` that opens the dashboard in a
  full-screen Firefox sized to the attached monitor, in dark mode, and a
  `bash_profile` hook that starts X on tty1 (with getty autologin).
- **Desktop window:** `desktop/` is a dependency-free Electron shell that starts
  PixelPing on a server over ssh, tunnels the port and shows the dashboard. Set
  `PIXELPING_SSH` to the server's ssh alias.

### Optional HTTP stats agent

```bash
# on the monitored host, from a copy of this repo
sudo deploy/install-agent.sh --bind 192.168.1.20 --allow 192.168.1.10 \
    --mounts "/ /srv" --services "nginx rsyslog"
```

This creates a `pixelping-stats` system user, copies `sysstats.py` to
`/opt/pixelping-stats`, and fills in `deploy/stats-agent/` templates: a sandboxed
systemd unit and an nftables table that only lets the `--allow` addresses reach
the port. Use `--dry-run` to print the files instead. Then set
`"stats": {"url": "http://192.168.1.20:9105/stats"}` for that host.

## Security model

- **The dashboard listens on 127.0.0.1 only.** It has no authentication, so it
  is never exposed to the network. Reach it through an ssh tunnel or run the
  browser on the same machine.
- **Agents open no ports.** They run as your ssh user for the life of the ssh
  session, read `/proc`, and run `ping`, `ss`, `systemctl is-active` and
  optionally `pm2 jlist`. No root needed. Process names for other users'
  sockets are only visible to root, and appear blank otherwise.
- **The HTTP stats agent is read-only** (GET `/stats` only), checks the client
  IP, is fenced by nftables and systemd `IPAddressAllow`, and runs as an
  unprivileged user under `ProtectSystem=strict` and a syscall filter.
- **The watchdog listener** binds a LAN address you choose, accepts only
  `POST /api/watchdog` from the `allow` list, requires a Bearer token of 32+
  characters (compared in constant time), caps bodies at 256 kB, validates and
  truncates every field, and the UI renders report text as plain text only.
  Keep the token file out of git (`*.token` is ignored) and readable only by
  the PixelPing user.

## Watchdog push API

PixelPing can show the latest report from a log watchdog (any script that reads
logs and decides how worried to be). The sender POSTs JSON:

```http
POST /api/watchdog HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json
```

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `ts` | string | yes | ISO 8601 time of the run, e.g. `2026-01-02T03:04:05+01:00`. |
| `severity` | string | yes | One of `High`, `Medium`, `Low`, `Quiet`. |
| `hosts` | array of strings | no | Hosts with alerts. First 50 kept, 100 chars each. |
| `summary` | string | no | Free text summary. Up to 20,000 chars. |
| `rules` | array of strings | no | One line per fired rule. First 300 kept, 2,000 chars each. |
| `suppressed` | integer >= 0 | no | Alerts hidden by ignore rules. |

```json
{
  "ts": "2026-01-02T03:04:05+01:00",
  "severity": "Medium",
  "hosts": ["web"],
  "summary": "Repeated ssh login failures from one address.",
  "rules": ["web: Medium: sshd: 40 failed logins from 203.0.113.7"],
  "suppressed": 2
}
```

Responses are JSON `{"ok": bool, "message": str}`: 200 stored, 400 invalid
payload, 401 bad token, 403 address not allowed, 411/413 missing or oversized
body. The newest report is kept in `state/watchdog.json` and survives restarts.
The pane warns when no report has arrived for two hours.

`deploy/watchdog/watchdog-pixelping-push.patch` is an example: a diff adding a
best-effort push to an existing Python watchdog script (not included here),
configured with `WATCHDOG_PUSH_URL` and `WATCHDOG_PUSH_TOKEN`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT, see [LICENSE](LICENSE).
