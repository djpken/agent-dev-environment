import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from ade.environment import dashboard_html


class EnvironmentDashboardTests(unittest.TestCase):
    def test_dashboard_selects_registration_or_login_from_allowlist(self):
        registration = dashboard_html(SimpleNamespace(authorized_wallets=[]))
        login = dashboard_html(SimpleNamespace(authorized_wallets=["0x" + "1" * 40]))

        self.assertIn('data-registration-required="true"', registration)
        self.assertIn('data-registration-required="false"', login)
        self.assertEqual(registration.count('id="wallet"'), 1)
        self.assertEqual(login.count('id="wallet"'), 1)

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
