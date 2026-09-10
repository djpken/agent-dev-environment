import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from ade import cli, core, publish


class PublishTests(unittest.TestCase):
    def test_publish_single_html_returns_browser_access_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "report.html"
            source.write_text("<html><body>report</body></html>")

            result = publish.publish(
                source,
                "architecture",
                base / "public",
                "http://172.16.240.41:80",
            )

            self.assertEqual(
                result["access_url"],
                "http://172.16.240.41:80/artifacts/architecture/index.html",
            )
            self.assertEqual(result["entrypoint"], "index.html")
            self.assertEqual(
                (base / "public" / "artifacts" / "architecture").stat().st_mode & 0o777,
                0o755,
            )
            self.assertEqual(
                (base / "public" / "artifacts" / "architecture" / "index.html").read_text(),
                "<html><body>report</body></html>",
            )

    def test_publish_bundle_overwrites_fixed_name_and_removes_old_files(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "bundle"
            (source / "assets").mkdir(parents=True)
            (source / "index.html").write_text("old")
            (source / "assets" / "old.png").write_bytes(b"old image")

            first = publish.publish(
                source,
                "architecture",
                base / "public",
                "http://172.16.240.41:80",
            )

            (source / "index.html").write_text("new")
            (source / "assets" / "old.png").unlink()
            (source / "assets" / "new.png").write_bytes(b"new image")
            second = publish.publish(
                source,
                "architecture",
                base / "public",
                "http://172.16.240.41:80",
            )

            destination = base / "public" / "artifacts" / "architecture"
            self.assertEqual(first["access_url"], second["access_url"])
            self.assertEqual((destination / "index.html").read_text(), "new")
            self.assertEqual((destination / "assets" / "new.png").read_bytes(), b"new image")
            self.assertFalse((destination / "assets" / "old.png").exists())

    def test_invalid_bundle_preserves_previous_fixed_name(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "public"
            valid = base / "valid"
            valid.mkdir()
            (valid / "index.html").write_text("old")
            publish.publish(valid, "architecture", root, "http://172.16.240.41:80")

            invalid = base / "invalid"
            invalid.mkdir()
            (invalid / "readme.txt").write_text("missing entrypoint")
            with self.assertRaisesRegex(core.Error, "entrypoint"):
                publish.publish(invalid, "architecture", root, "http://172.16.240.41:80")

            self.assertEqual(
                (root / "artifacts" / "architecture" / "index.html").read_text(),
                "old",
            )

    def test_publish_directories_are_readable_with_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "bundle"
            (source / "assets" / "nested").mkdir(parents=True)
            (source / "assets" / "nested" / "index.html").write_text("report")
            root = base / "public"
            previous_umask = os.umask(0o077)
            try:
                publish.publish(source, "architecture", root,
                                "http://172.16.240.41:80",
                                "assets/nested/index.html")
            finally:
                os.umask(previous_umask)

            for path in (root, root / "artifacts", root / "artifacts" / "architecture",
                         root / "artifacts" / "architecture" / "assets",
                         root / "artifacts" / "architecture" / "assets" / "nested"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_publish_rejects_artifacts_root_symlink_without_writing_outside_root(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "report.html"
            source.write_text("report")
            root = base / "public"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (root / "artifacts").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(core.Error, "artifact root may not contain symbolic links"):
                publish.publish(source, "architecture", root, "http://172.16.240.41:80")
            with self.assertRaisesRegex(core.Error, "artifact root may not contain symbolic links"):
                publish.delete("architecture", root)
            self.assertFalse(list(outside.iterdir()))

    def test_delete_removes_named_artifact_and_rejects_missing_name(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "report.html"
            source.write_text("<html></html>")
            root = base / "public"
            publish.publish(source, "architecture", root, "http://172.16.240.41:80")

            self.assertEqual(publish.delete("architecture", root),
                             {"name": "architecture", "deleted": True})
            self.assertFalse((root / "artifacts" / "architecture").exists())
            with self.assertRaisesRegex(core.Error, "artifact not found: architecture"):
                publish.delete("architecture", root)

    def test_publish_rejects_https_and_does_not_change_existing_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "report.html"
            source.write_text("old")
            root = base / "public"
            publish.publish(source, "architecture", root, "http://172.16.240.41:80")

            source.write_text("must not replace")
            with self.assertRaisesRegex(core.Error, "HTTP port 80"):
                publish.publish(source, "architecture", root, "https://reports.example.internal")

            self.assertEqual(
                (root / "artifacts" / "architecture" / "index.html").read_text(),
                "old",
            )

    def test_publish_rejects_symlink_sources_and_unsafe_entrypoints(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            real_source = base / "real.html"
            real_source.write_text("report")
            link = base / "link.html"
            link.symlink_to(real_source)
            with self.assertRaisesRegex(core.Error, "symbolic link"):
                publish.publish(link, "architecture", base / "public",
                                "http://172.16.240.41:80")

            bundle = base / "bundle"
            bundle.mkdir()
            (bundle / "index.html").write_text("report")
            with self.assertRaisesRegex(core.Error, "invalid HTML entrypoint"):
                publish.publish(bundle, "architecture", base / "public",
                                "http://172.16.240.41:80", "../index.html")
            with self.assertRaisesRegex(core.Error, "invalid HTML entrypoint"):
                publish.publish(bundle, "architecture", base / "public",
                                "http://172.16.240.41:80", ".well-known/index.html")

            linked_bundle = base / "linked-bundle"
            linked_bundle.mkdir()
            (linked_bundle / "index.html").symlink_to(real_source)
            with self.assertRaisesRegex(core.Error, "symbolic link"):
                publish.publish(linked_bundle, "architecture", base / "public",
                                "http://172.16.240.41:80")


class PublishCliTests(unittest.TestCase):
    def run_cli(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["ade", *arguments]), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main()
        output = json.loads(stdout.getvalue()) if stdout.getvalue() else None
        return code, output, stderr.getvalue()

    def test_publish_html_cli_publishes_and_returns_json_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "report.html"
            source.write_text("<html></html>")

            with patch("ade.cli.publisher_ready"):
                code, output, error = self.run_cli([
                    "publish-html", "publish", str(source), "--name", "architecture",
                    "--artifact-root", str(base / "public"),
                    "--base-url", "http://172.16.240.41:80",
                ])

            self.assertEqual(code, 0)
            self.assertEqual(error, "")
            self.assertEqual(output["name"], "architecture")
            self.assertEqual(output["entrypoint"], "index.html")

    def test_publish_html_cli_deletes_by_name(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "report.html"
            source.write_text("<html></html>")
            root = base / "public"
            publish.publish(source, "architecture", root, "http://172.16.240.41:80")

            code, output, error = self.run_cli([
                "publish-html", "delete", "architecture", "--artifact-root", str(root),
            ])

            self.assertEqual(code, 0)
            self.assertEqual(error, "")
            self.assertEqual(output, {"name": "architecture", "deleted": True})

    def test_publish_html_cli_uses_default_browser_origin(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            base = Path(temp)
            source = base / "report.html"
            source.write_text("<html></html>")

            with patch("ade.cli.publisher_ready"):
                code, output, error = self.run_cli([
                    "publish-html", "publish", str(source), "--name", "architecture",
                    "--artifact-root", str(base / "public"),
                ])

            self.assertEqual(code, 0)
            self.assertEqual(error, "")
            self.assertEqual(output["access_url"],
                             "http://172.16.240.41:80/artifacts/architecture/index.html")

    def test_publish_html_cli_rejects_non_http_base_url_before_readiness_check(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "report.html"
            source.write_text("<html></html>")
            with patch("ade.cli.publisher_ready") as ready:
                code, output, error = self.run_cli([
                    "publish-html", "publish", str(source), "--name", "architecture",
                    "--artifact-root", str(base / "public"),
                    "--base-url", "https://artifact.internal.example",
                ])

            self.assertEqual(code, 1)
            self.assertIsNone(output)
            self.assertIn("HTTP port 80", error)
            ready.assert_not_called()
            self.assertFalse((base / "public").exists())

    def test_publish_html_cli_prefixes_publish_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with patch("ade.cli.publisher_ready"):
                code, output, error = self.run_cli([
                    "publish-html", "publish", str(base / "missing.html"), "--name", "architecture",
                    "--artifact-root", str(base / "public"),
                    "--base-url", "http://172.16.240.41:80",
                ])

            self.assertEqual(code, 1)
            self.assertIsNone(output)
            self.assertIn("publish blocked: HTML source does not exist", error)

    def test_publish_html_cli_blocks_when_http_publisher_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "report.html"
            source.write_text("<html></html>")
            with patch("ade.cli.publisher_ready", side_effect=core.Error(
                    "publish blocked: HTTP publisher unavailable at 127.0.0.1:80")):
                code, output, error = self.run_cli([
                    "publish-html", "publish", str(source), "--name", "architecture",
                    "--artifact-root", str(base / "public"),
                    "--base-url", "http://172.16.240.41:80",
                ])

            self.assertEqual(code, 1)
            self.assertIsNone(output)
            self.assertIn("HTTP publisher unavailable", error)
            self.assertFalse((base / "public").exists())

    def test_publish_html_cli_uses_publisher_environment_defaults(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
                "ADE_WEB_ARTIFACT_ROOT": str(Path(temp) / "public"),
                "ADE_WEB_ARTIFACT_BASE_URL": "http://172.16.240.41:80",
        }, clear=True):
            base = Path(temp)
            source = base / "report.html"
            source.write_text("<html></html>")
            with patch("ade.cli.publisher_ready"):
                code, output, error = self.run_cli([
                    "publish-html", "publish", str(source), "--name", "architecture",
                ])

            self.assertEqual(code, 0)
            self.assertEqual(error, "")
            self.assertEqual(output["access_url"],
                             "http://172.16.240.41:80/artifacts/architecture/index.html")

    def test_publisher_ready_requires_local_health_endpoint(self):
        with patch("ade.cli.urllib.request.urlopen") as urlopen:
            cli.publisher_ready("http://artifact.internal.example:80")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:80/healthz")
        self.assertEqual(request.get_header("Host"), "artifact.internal.example")
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 2})

        with patch("ade.cli.urllib.request.urlopen", side_effect=OSError):
            with self.assertRaisesRegex(core.Error, "HTTP publisher unavailable"):
                cli.publisher_ready()


if __name__ == "__main__":
    unittest.main()
