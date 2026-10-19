# Changelog

## Unreleased

First public release. Renamed from netmon to PixelPing.

### Added
- `python3 pixelping.py --check`: tests the config and runs the agent on every
  host once, with fix-it hints. Exits non-zero on failure.
- `AGENTS.md`: setup guide for installing PixelPing with an AI coding agent.
- README sections: step-by-step Linux install, supported platforms, Windows use.
- `--config` flag for `pixelping.py` (default: `config.json` next to it).
- System stats over ssh: the agent now carries `sysstats.py` and reports CPU,
  memory, load, disks, uptime and services. No install needed on hosts.
- Optional `labels` map in the config for ping target display names.
- `config.example.json` documenting every key.
- `deploy/install-agent.sh` and generic templates for the optional HTTP stats
  agent (`deploy/stats-agent/`).
- `deploy/systemd/pixelping.service`, `deploy/kiosk/` (Firefox kiosk on tty1).
- Unit tests and a GitHub Actions workflow.
- README, MIT license, contributing guide.

### Changed
- Ping target names come from the config instead of being hard-coded.
- The watchdog pane is hidden when no watchdog listener is configured.
- The desktop launcher reads the server from `PIXELPING_SSH` (no default).
- `config.json` is no longer tracked.
- Renamed: `netmon.py` is now `pixelping.py`; services, env vars
  (`PIXELPING_SSH`, `PIXELPING_DIR`, `PIXELPING_URL`) and paths follow.
- Watchdog token paths are relative to the config file.

## 2026-10 (pre-release)
- System stats panes, sysstats HTTP agent and the watchdog push endpoint.
- Electron desktop launcher.
- Initial network dashboard.
