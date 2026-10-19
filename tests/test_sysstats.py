"""sysstats parsing against fixture /proc files."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sysstats  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "proc"


class SysstatsTest(unittest.TestCase):
    def setUp(self):
        self.proc = Path(tempfile.mkdtemp())
        for f in FIXTURES.iterdir():
            shutil.copy(f, self.proc / f.name)
        self._saved = sysstats.PROC
        sysstats.PROC = str(self.proc)
        sysstats._cpu_prev = None

    def tearDown(self):
        sysstats.PROC = self._saved
        sysstats._cpu_prev = None
        shutil.rmtree(self.proc)

    def test_memory(self):
        self.assertEqual(sysstats.memory(), {"total": 8_000_000 * 1024, "used": 2_000_000 * 1024})

    def test_memory_without_memavailable(self):
        lines = [l for l in (self.proc / "meminfo").read_text().splitlines() if "MemAvailable" not in l]
        (self.proc / "meminfo").write_text("\n".join(lines) + "\n")
        self.assertEqual(sysstats.memory()["used"], 7_500_000 * 1024)

    def test_cpu_first_call_is_since_boot(self):
        # total 10000, idle + iowait 8500
        self.assertAlmostEqual(sysstats.cpu_percent(), 15.0)

    def test_cpu_delta(self):
        sysstats.cpu_percent()
        (self.proc / "stat").write_text("cpu  1600 0 700 8600 600 0 0 0 0 0\n")
        # +1500 total, +700 idle
        self.assertAlmostEqual(sysstats.cpu_percent(), 100 * (1 - 700 / 1500))

    def test_cpu_no_change_falls_back_to_since_boot(self):
        sysstats.cpu_percent()
        self.assertAlmostEqual(sysstats.cpu_percent(), 15.0)

    def test_disks(self):
        root, missing = sysstats.disks(["/", "/no/such/mount"])
        self.assertEqual(root["mount"], "/")
        self.assertGreater(root["total"], 0)
        self.assertTrue(0 <= root["pct"] <= 100)
        self.assertEqual(missing["mount"], "/no/such/mount")
        self.assertIn("error", missing)

    def test_services_empty(self):
        self.assertEqual(sysstats.services([]), [])

    def test_collect(self):
        st = sysstats.collect(["/"], [])
        self.assertEqual(st["load"], [0.52, 0.48, 0.40])
        self.assertEqual(st["uptime"], 93784.25)
        self.assertAlmostEqual(st["cpu"], 15.0)
        self.assertEqual(st["services"], [])
        self.assertEqual(len(st["disks"]), 1)
        for key in ("ts", "mem", "cores"):
            self.assertIn(key, st)


if __name__ == "__main__":
    unittest.main()
