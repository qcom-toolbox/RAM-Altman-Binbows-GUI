"""Exercises ram_altman_core with simulated memory and a fake mail sender.

Run with:  python -m unittest test_ram_altman -v
No inboxes are harmed.
"""

import os
import re
import tempfile
import unittest
from pathlib import Path

import ram_altman_core as core


class RamAltmanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.state = self.work / "state"
        self.meminfo = self.work / "meminfo"
        self.env = {
            "RAM_ALTMAN_TO": "to@example.com",
            "RAM_ALTMAN_FROM": "from@example.com",
            "RAM_ALTMAN_STATE_DIR": str(self.state),
            "RAM_ALTMAN_MEMINFO": str(self.meminfo),
        }
        self.old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.outbox = []
        self.fail = False

        def fake_send(cfg, msg):
            if self.fail:
                raise core.RamAltmanError("fake SMTP is down")
            self.outbox.append(msg)

        self.old_send = core.send_email
        core.send_email = fake_send

    def tearDown(self):
        core.send_email = self.old_send
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def mem(self, total, free, available, buffers, cached, slab, shmem):
        avail = f"MemAvailable:   {available} kB\n" if available != "" else ""
        self.meminfo.write_text(
            f"MemTotal:       {total} kB\nMemFree:        {free} kB\n{avail}"
            f"Buffers:        {buffers} kB\nCached:         {cached} kB\n"
            f"SwapCached:     999999 kB\nSReclaimable:   {slab} kB\nShmem:          {shmem} kB\n")

    def set_last_sent(self, day):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "last_sent").write_text(day + "\n")

    def test_daily_flow(self):
        # Mostly page cache: little is free, but plenty is available.
        self.mem(16000000, 300000, 9000000, 200000, 8000000, 500000, 100000)
        core.run_once()
        self.assertEqual(len(self.outbox), 1, "cache counts as available: email sent")
        core.run_once()
        core.run_once()
        self.assertEqual(len(self.outbox), 1, "second and third run same day: no more email")
        rows = core.read_log()
        self.assertEqual(rows[-1]["used_kb"], 7000000, "used = total - available")
        self.assertEqual(len(rows), 3, "three samples logged")

        # Next day, tight on memory.
        self.set_last_sent("2000-01-01")
        self.mem(16000000, 300000, 1000000, 100000, 800000, 100000, 100000)
        core.run_once()
        self.assertEqual(len(self.outbox), 1, "6.2% available: no email")

        # Exactly 10% is not over 10%.
        self.mem(16000000, 300000, 1600000, 100000, 800000, 100000, 100000)
        core.run_once()
        self.assertEqual(len(self.outbox), 1, "exactly 10%: no email")
        self.mem(16000000, 300000, 1600001, 100000, 800000, 100000, 100000)
        core.run_once()
        self.assertEqual(len(self.outbox), 2, "just over 10%: email sent")

    def test_several_a_day(self):
        import datetime as dt
        self.mem(16000000, 300000, 9000000, 200000, 8000000, 500000, 100000)
        os.environ["RAM_ALTMAN_PER_DAY"] = "2"
        try:
            day = dt.datetime(2030, 5, 1, 8, 0)
            core.run_once(now=day)
            self.assertEqual(len(self.outbox), 1, "first of two sent")
            self.assertIn("at most twice a day", self.outbox[0].get_content())
            core.run_once(now=day + dt.timedelta(hours=2, minutes=59))
            self.assertEqual(len(self.outbox), 1, "too soon: waits for the gap")
            core.run_once(now=day + dt.timedelta(hours=3))
            self.assertEqual(len(self.outbox), 2, "second sent after the gap")
            core.run_once(now=day + dt.timedelta(hours=9))
            self.assertEqual(len(self.outbox), 2, "two a day is the limit")
            core.run_once(now=day + dt.timedelta(days=1))
            self.assertEqual(len(self.outbox), 3, "next day starts again")

            os.environ["RAM_ALTMAN_PER_DAY"] = "50"
            self.assertEqual(core.load_config()["per_day"], core.MAX_PER_DAY, "capped")
        finally:
            os.environ.pop("RAM_ALTMAN_PER_DAY")

    def test_old_last_sent_file_still_counts(self):
        # Files written before this option hold just the date: that's one email today.
        import datetime as dt
        self.mem(16000000, 300000, 9000000, 200000, 8000000, 500000, 100000)
        self.set_last_sent("2030-05-01")
        core.run_once(now=dt.datetime(2030, 5, 1, 20, 0))
        self.assertEqual(len(self.outbox), 0)
        self.assertIn("at most once a day", core.compose_email(
            core.read_memory(), "", dry_run=True)[1])

    def test_failed_send_is_retried(self):
        self.mem(16000000, 300000, 9000000, 200000, 8000000, 500000, 100000)
        self.set_last_sent("2000-01-01")
        self.fail = True
        with self.assertRaises(core.RamAltmanError):
            core.run_once()
        self.assertEqual(core.last_sent(), "2000-01-01", "failed send not recorded")
        self.fail = False
        core.run_once()
        self.assertEqual(len(self.outbox), 1, "retry succeeds")

    def test_fallback_estimate(self):
        # Old kernel with no MemAvailable: free + buffers + cached + slab - shmem.
        # SwapCached in the fixture must not be picked up as Cached.
        self.mem(16000000, 1000000, "", 200000, 3000000, 500000, 700000)
        status = core.format_status(core.read_memory())
        self.assertRegex(status, r"Available:\s+3\.8 GiB")

    def test_variety(self):
        # 10 bodies should all appear across 10 days, with no back-to-back repeat.
        self.mem(16000000, 300000, 9000000, 200000, 8000000, 500000, 100000)
        for _ in range(40):
            self.set_last_sent("2000-01-01")
            core.run_once()
        bodies = [m.get_content().split("\n")[2] for m in self.outbox]
        self.assertEqual(len(set(bodies[:10])), 10, "all opening lines used")
        repeats = sum(1 for a, b in zip(bodies, bodies[1:]) if a == b)
        self.assertEqual(repeats, 0, "no back-to-back repeat")

    def test_message_headers(self):
        self.mem(16000000, 300000, 9000000, 200000, 8000000, 500000, 100000)
        os.environ["RAM_ALTMAN_FROM_NAME"] = "Ram Owner"
        try:
            core.run_once()
        finally:
            os.environ.pop("RAM_ALTMAN_FROM_NAME")
        msg = self.outbox[0]
        self.assertEqual(msg["To"], "to@example.com")
        self.assertEqual(msg["From"], "Ram Owner <from@example.com>")
        self.assertEqual(msg["Auto-Submitted"], "auto-generated")
        self.assertIn("Reply if you'd like it to stop.", msg.get_content())

    def test_missing_recipient_refused(self):
        self.mem(16000000, 300000, 9000000, 200000, 8000000, 500000, 100000)
        os.environ["RAM_ALTMAN_TO"] = ""
        with self.assertRaises(core.RamAltmanError):
            core.run_once()
        self.assertEqual(self.outbox, [])

    def test_dry_run_changes_nothing(self):
        self.mem(16000000, 300000, 9000000, 200000, 8000000, 500000, 100000)
        subject, body = core.compose_email(core.read_memory(), "", dry_run=True)
        self.assertTrue(subject and body.startswith("Hi Sam,"))
        self.assertFalse(self.state.exists(), "dry run wrote nothing to disk")

    def test_config_round_trip(self):
        cfg = dict(core.DEFAULTS, to="a@example.com", smtp_host="smtp.example.com")
        core.save_config(cfg)
        saved = core.load_saved_config()
        self.assertEqual(saved["smtp_host"], "smtp.example.com")
        self.assertNotIn("password", saved)

    @unittest.skipUnless(core.IS_WINDOWS, "Windows only")
    def test_real_windows_memory(self):
        os.environ.pop("RAM_ALTMAN_MEMINFO")
        mem = core.read_memory()
        self.assertGreater(mem.total_kb, 0)
        self.assertTrue(0 < mem.available_kb <= mem.total_kb)
        if mem.free_kb is not None:
            self.assertEqual(mem.free_kb + mem.cache_kb, mem.available_kb)


if __name__ == "__main__":
    unittest.main(verbosity=2)
