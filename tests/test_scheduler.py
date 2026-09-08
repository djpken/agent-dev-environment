import plistlib
import tempfile
import unittest
from pathlib import Path

from ade.scheduler import (
    DEFAULT_TIMES,
    SERVICE_NAME,
    install,
    manifest_schedule,
    render_launchd,
    render_systemd,
)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_systemd_has_the_three_local_time_triggers_and_no_secret(self):
        result = render_systemd(
            self.temp.name, env_file="/home/test/.config/ade/plane.env"
        )
        timer = result[f"{SERVICE_NAME}.timer"]
        service = result[f"{SERVICE_NAME}.service"]
        for time in DEFAULT_TIMES:
            self.assertIn(f"OnCalendar=*-*-* {time}:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("--env-file /home/test/.config/ade/plane.env", service)
        self.assertNotIn("secret", service)

    def test_launchd_has_three_calendar_intervals(self):
        value = plistlib.loads(render_launchd(self.temp.name))
        self.assertEqual(
            value["StartCalendarInterval"],
            [
                {"Hour": 8, "Minute": 0},
                {"Hour": 12, "Minute": 0},
                {"Hour": 17, "Minute": 0},
            ],
        )
        self.assertFalse(value["RunAtLoad"])

    def test_install_is_idempotent_in_an_operator_destination(self):
        destination = Path(self.temp.name) / "systemd"
        first = install(self.temp.name, "linux", destination=destination)
        second = install(self.temp.name, "linux", destination=destination)
        self.assertEqual(first["files"], second["files"])
        self.assertTrue((destination / f"{SERVICE_NAME}.timer").is_file())

    def test_manifest_schedule_defaults_and_rejects_bad_time(self):
        self.assertEqual(manifest_schedule({}), DEFAULT_TIMES)
        with self.assertRaises(ValueError):
            manifest_schedule({"schedule": {"times": ["25:00"]}})


if __name__ == "__main__":
    unittest.main()
