import shutil
import subprocess
import unittest
from pathlib import Path

from ade.environment import dashboard_html


class EnvironmentDashboardTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute dashboard JavaScript")
    def test_session_and_controls_in_browser_runtime(self):
        # The anonymous shell intentionally has no config-dependent content.
        page = dashboard_html(None)
        script = page.split("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run(
            [shutil.which("node"), str(Path(__file__).with_name("environment_dashboard.cjs"))],
            input=script, text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
