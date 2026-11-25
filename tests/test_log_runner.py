import tempfile
import unittest
from pathlib import Path

from log_runner import RotatingLogWriter, normalize_existing_log


class LogRunnerTest(unittest.TestCase):
    def test_watchdog_closes_its_lock_fd_before_background_launch(self):
        ensure_script = (Path(__file__).parents[1] / "ops" / "ensure.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('setsid "$RUN" 9>&-', ensure_script)
        self.assertIn('RUNNER="$ROOT/log_runner.py"', ensure_script)
        self.assertIn('START_TIMEOUT=45', ensure_script)

    def test_normalizes_oversized_closed_log_to_bounded_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.log"
            path.write_bytes(b"0123456789ABCDEFGHIJklmnopqrstUV")

            self.assertTrue(normalize_existing_log(path, max_bytes=10, backups=3))
            self.assertEqual(path.read_bytes(), b"")
            self.assertEqual((Path(tmp) / "service.log.1").read_bytes(), b"mnopqrstUV")
            self.assertEqual((Path(tmp) / "service.log.2").read_bytes(), b"CDEFGHIJkl")
            self.assertEqual((Path(tmp) / "service.log.3").read_bytes(), b"23456789AB")

    def test_writer_rotates_and_keeps_backup_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.log"
            writer = RotatingLogWriter(path, max_bytes=8, backups=2)
            for line in ("one\n", "two\n", "three\n", "four\n"):
                writer.write(line)
            writer.close()

            self.assertLessEqual(path.stat().st_size, 8)
            self.assertTrue((Path(tmp) / "service.log.1").exists())
            self.assertTrue((Path(tmp) / "service.log.2").exists())
            self.assertFalse((Path(tmp) / "service.log.3").exists())

    def test_writer_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.log"
            writer = RotatingLogWriter(path, max_bytes=1024, backups=2)
            writer.write("MANAGER_BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz\n")
            writer.close()

            content = path.read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", content)
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", content)

    def test_normalizer_redacts_retained_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.log"
            secret = "123456:abcdefghijklmnopqrstuvwxyz"
            path.write_text(("prefix\n" * 8) + f"token={secret}\n", encoding="utf-8")

            self.assertTrue(normalize_existing_log(path, max_bytes=32, backups=2))

            retained = b"".join(
                candidate.read_bytes()
                for candidate in sorted(Path(tmp).glob("service.log.*"))
            ).decode("utf-8", "replace")
            self.assertIn("[REDACTED]", retained)
            self.assertNotIn(secret, retained)


if __name__ == "__main__":
    unittest.main()
