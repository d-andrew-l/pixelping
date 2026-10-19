"""pixelping: watchdog payload validation, config loading and the agent bundle."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import agent  # noqa: E402
import pixelping  # noqa: E402

GOOD = {"ts": "2026-01-02T03:04:05+00:00", "severity": "Medium", "hosts": ["web", "db"],
        "summary": "Two failed logins.", "rules": ["web: Medium: sshd auth failure"], "suppressed": 3}


class ValidateWatchdogTest(unittest.TestCase):
    def check(self, **change):
        return pixelping.validate_watchdog({**GOOD, **change})

    def rejects(self, payload):
        with self.assertRaises(ValueError):
            pixelping.validate_watchdog(payload)

    def test_accepts_full_payload(self):
        r = self.check()
        self.assertEqual(r["severity"], "Medium")
        self.assertEqual(r["hosts"], ["web", "db"])
        self.assertEqual(r["rules_total"], 1)
        self.assertEqual(r["suppressed"], 3)

    def test_accepts_minimal_payload(self):
        r = pixelping.validate_watchdog({"ts": "2026-01-02T03:04:05Z", "severity": "Quiet"})
        self.assertEqual((r["hosts"], r["rules"], r["summary"], r["suppressed"]), ([], [], "", 0))

    def test_truncates_long_fields(self):
        r = self.check(summary="x" * 30_000, rules=["r"] * 500, hosts=["h" * 200] * 60)
        self.assertEqual(len(r["summary"]), 20_000)
        self.assertEqual(len(r["rules"]), 300)
        self.assertEqual(r["rules_total"], 500)
        self.assertEqual(len(r["hosts"]), 50)
        self.assertEqual(len(r["hosts"][0]), 100)

    def test_drops_unknown_keys(self):
        self.assertNotIn("extra", self.check(extra="nope"))

    def test_rejects(self):
        self.rejects([GOOD])
        self.rejects("text")
        for bad in ({"ts": None}, {"ts": "yesterday"}, {"ts": 1700000000},
                    {"severity": "Critical"}, {"severity": None},
                    {"hosts": "web"}, {"rules": {"a": 1}}, {"hosts": [1]}, {"rules": [None]},
                    {"summary": 5}, {"suppressed": -1}, {"suppressed": True}, {"suppressed": 1.5}):
            with self.subTest(bad=bad):
                self.rejects({**GOOD, **bad})


class ConfigTest(unittest.TestCase):
    def test_example_config_loads(self):
        pixelping.load_config(ROOT / "config.example.json")
        self.assertEqual(pixelping.CONFIG_DIR, ROOT)
        self.assertEqual(pixelping.target_labels["192.168.1.20"], "NAS")
        self.assertEqual(pixelping.target_labels["1.1.1.1"], "Cloudflare")
        self.assertEqual(pixelping.host_names["192.168.1.10"], "server")
        for h in pixelping.CONFIG["hosts"]:
            self.assertTrue(h["name"] and h["label"])

    def test_agent_cmd_quotes_ssh_args(self):
        pixelping.load_config(ROOT / "config.example.json")
        nas = pixelping.HostState(pixelping.CONFIG["hosts"][1])
        cmd = nas.agent_cmd()
        self.assertEqual(cmd[cmd.index("nas") + 1:cmd.index("nas") + 3], ["python3", "-"])
        stats_arg = cmd[cmd.index("--stats") + 1]
        self.assertTrue(stats_arg.startswith("'"))
        url_host = pixelping.HostState(pixelping.CONFIG["hosts"][3])
        self.assertNotIn("--stats", url_host.agent_cmd())


class AgentTest(unittest.TestCase):
    def test_parse_args(self):
        targets, stats, every = agent.parse_args(
            ["gateway", "--stats", '{"mounts": ["/"]}', "1.1.1.1", "--stats-every", "5"])
        self.assertEqual(targets, ["gateway", "1.1.1.1"])
        self.assertEqual(stats, {"mounts": ["/"]})
        self.assertEqual(every, 5.0)
        self.assertEqual(agent.parse_args(["8.8.8.8"]), (["8.8.8.8"], None, 3.0))

    def test_bundle_compiles(self):
        compile(pixelping.agent_script(), "agent-bundle", "exec")

    @unittest.skipUnless(sys.platform.startswith("linux"), "agent reads /proc")
    def test_bundle_reports_stats(self):
        # run the bundle the way pixelping does, from an empty dir so sysstats must come from stdin
        p = subprocess.Popen([sys.executable, "-", "--stats", '{"mounts": ["/"]}', "--stats-every", "0.5"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd="/")
        try:
            p.stdin.write(pixelping.agent_script())
            p.stdin.close()
            deadline = time.time() + 15
            while time.time() < deadline:
                rec = json.loads(p.stdout.readline())
                if "sys" in rec:
                    break
            else:
                self.fail("no stats record from the agent")
            self.assertIsNone(rec["sys"]["error"])
            self.assertEqual(rec["sys"]["stats"]["disks"][0]["mount"], "/")
            for key in ("ifaces", "ping", "conns", "load"):
                self.assertIn(key, rec)
        finally:
            p.kill()
            p.stdout.close()
            p.stderr.close()
            p.wait()


@unittest.skipUnless(sys.platform.startswith("linux"), "agent reads /proc")
class CheckTest(unittest.TestCase):
    def run_check(self, hosts):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"port": 0, "stats_interval": 1, "ping_targets": ["127.0.0.1"],
                                       "hosts": hosts}))
            return subprocess.run([sys.executable, str(ROOT / "pixelping.py"), "--config", str(cfg), "--check"],
                                  capture_output=True, text=True, timeout=60)

    def test_local_host_passes(self):
        r = self.run_check([{"name": "local", "stats": {"mounts": ["/"]}}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("agent running", r.stdout)
        self.assertIn("system stats: CPU", r.stdout)
        self.assertIn("all checks passed", r.stdout)

    def test_unreachable_ssh_host_fails(self):
        r = self.run_check([{"name": "gone", "ssh": "nobody@host.invalid"}])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("FAIL  agent did not start", r.stdout)

    def test_duplicate_names_fail(self):
        r = self.run_check([{"name": "a"}, {"name": "a"}])
        self.assertEqual(r.returncode, 1)
        self.assertIn("unique", r.stdout)


if __name__ == "__main__":
    unittest.main()
