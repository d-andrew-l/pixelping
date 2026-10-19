<!--
  This file is for anyone who wants an AI coding agent (Claude Code, Codex,
  Cursor, Copilot and the like) to get PixelPing up and running for them.
  Open the repo in your agent and ask: "Set up PixelPing for me, following AGENTS.md."
  People can follow it by hand too. README.md is the full reference.
-->

# Setting up PixelPing with an AI agent

> **Who this is for:** people who want an AI agent to install and configure
> PixelPing for them. If you are the agent, follow the steps below in order.
> If you are a person, you can follow them too, or just point your agent here.

PixelPing is a live LAN dashboard: one Python process (`pixelping.py`) on a
Linux machine streams a small agent over ssh to each watched host and serves a
web UI on `http://127.0.0.1:8765`. Standard library only, nothing to install on
the watched hosts.

## Rules for the agent

- **Ask, don't guess,** which machines to watch, their IPs and ssh names.
- **Ask before any `sudo`,** before installing system packages, and before
  enabling services. Show the user the command first.
- **Never ask for, type or store passwords.** When a step needs one (such as
  `ssh-copy-id`), give the user the command and let them run it.
- **Keep the dashboard on 127.0.0.1.** Don't change the bind address or open
  firewall ports for it. Remote viewing goes through an ssh tunnel.
- **PixelPing is read-only** on watched hosts. Don't change anything on them
  beyond what a step below says (copying an ssh key, the optional stats agent).
- **No pip packages.** It's standard library only. Don't add a venv or requirements file.
- **Keep secrets out of git.** `config.json` and `*.token` are already ignored.

## 1. Ask the user

1. Which Linux machine runs the dashboard? (Usually an always-on box: a
   server, NAS or Raspberry Pi. On Windows only, see "Windows" below.)
2. Which hosts to watch? For each: a short name, a display label, its LAN IP,
   and how to ssh to it (an alias in `~/.ssh/config`, or `user@ip`).
3. For hosts that should show system stats: which disks (mount points) and
   which systemd services? Any pm2 apps?
4. Which ping targets? The default is the router (`gateway`), `1.1.1.1` and `8.8.8.8`.
5. Should it start on boot (systemd or pm2)? Show on a monitor (kiosk)?
6. Is there a log watchdog that will push reports? If not, leave it out.

## 2. Check requirements on the dashboard machine

```bash
python3 --version          # needs 3.9 or newer
command -v ssh ping ss     # all three must print a path
```

If any are missing on Debian, Ubuntu or Raspberry Pi OS (ask first):
`sudo apt install python3 openssh-client iputils-ping iproute2`.
Every watched host needs `python3`, `ping` and `ss` too. Step 5 checks them.

## 3. Get the code

```bash
git clone https://github.com/d-andrew-l/pixelping.git ~/pixelping
cd ~/pixelping
```

The UI looks for optional fonts and pixel art in `static/`, which are not in the
repo. It works without them, so don't go looking for them.

## 4. Write config.json

```bash
cp config.example.json config.json
```

Edit `config.json` with the user's answers:

- Replace the example `hosts` with the real ones. Leave out `ssh` for the
  dashboard machine itself. Leave out `stats` for hosts that don't need it.
- Use `stats.url` only if the user installs the optional HTTP stats agent.
- Remove the whole `watchdog` block unless the user has a watchdog.
- The `_comment` keys can stay or go. They are ignored.

Every key is described in README.md under "Configuration".

## 5. Set up ssh and run the check

For each host with `ssh`, test passwordless login from the dashboard machine:

```bash
ssh -o BatchMode=yes HOST true && echo ok
```

If that fails because there is no key yet, create one (no passphrase, so
PixelPing can connect unattended) and have the **user** copy it, since it asks
for their password:

```bash
test -f ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id HOST           # the user runs this one
```

Then run the built-in check. It tests the config and runs the agent on every
host for a few seconds:

```bash
python3 pixelping.py --check
```

Each line is `OK`, `WARN`, `FAIL` or `HINT`, and it exits non-zero if anything
failed. Fix every `FAIL` (the `HINT` lines say how) and run it again. Explain
any `WARN` lines to the user. A stopped service or an unreachable ping target
is often expected.

| Problem | Fix |
| --- | --- |
| `Permission denied` | Key not installed: user runs `ssh-copy-id HOST`. |
| `Host key verification failed` | User runs `ssh HOST` once and accepts the key. |
| `python3: command not found` | Install python3 on that host (ask first). |
| `'ss' not found` / `'ping' not found` | Install `iproute2` / `iputils-ping` on that host. |
| `service X: inactive` | Not an install problem. Tell the user. |
| `port 8765 is in use` | PixelPing is already running, or change `port`. |

## 6. Start it

```bash
python3 pixelping.py
```

Open http://127.0.0.1:8765 on the dashboard machine. From another computer:

```bash
ssh -L 8765:127.0.0.1:8765 DASHBOARD_MACHINE
```

then open http://127.0.0.1:8765 there. The page should show "Live" in the
header, and each host's graphs should move every second.

## 7. Optional extras (ask first)

- **Start on boot with systemd:** copy `deploy/systemd/pixelping.service` to
  `/etc/systemd/system/`, set `User=` and the paths to match this install, then
  `sudo systemctl daemon-reload && sudo systemctl enable --now pixelping`.
- **Start on boot with pm2:** `pm2 start pixelping.py --interpreter python3 --name pixelping && pm2 save`.
- **Kiosk on an attached monitor:** see `deploy/kiosk/` (needs `xinit`,
  `firefox-esr`, `x11-utils`, plus getty autologin on tty1).
- **HTTP stats agent** for a host the dashboard shouldn't ssh into:
  `sudo deploy/install-agent.sh --bind HOST_IP --allow DASHBOARD_IP ...`, run on
  that host. Try `--dry-run` first. Then set `stats.url` for it.
- **Watchdog token:**
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))" > watchdog.token && chmod 600 watchdog.token`.
  Give the token to the user for their watchdog sender. Never commit it.

## Windows

PixelPing's server and agents need Linux (they read `/proc` and use Linux
`ping` and `ss`). If the user only has Windows:

- **Best:** run PixelPing on any Linux machine on the LAN (even a Raspberry
  Pi) and view it from Windows over the ssh tunnel above. Windows 10 and 11
  include `ssh`.
- **WSL2 works for trying it out.** Inside WSL2 it can watch Linux hosts over ssh,
  but the "local" host is the WSL virtual machine, not Windows itself.
- **Desktop window:** `desktop/` is an Electron shell for Windows that starts
  PixelPing on the Linux server and shows it in a window. Set the environment
  variable `PIXELPING_SSH` to the server's ssh alias.

## Done when

- `python3 pixelping.py --check` reports no `FAIL`.
- The dashboard loads and shows "Live", with every host online.
- You've told the user how to open it, how to view it remotely, where the logs
  are (`logs/`), and how it starts on boot (if set up).
