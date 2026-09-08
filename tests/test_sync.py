import json
import tempfile
import unittest
from pathlib import Path

from ade.sync import (
    ConfigurationError,
    LinearApiClient,
    LinearIssueRef,
    MappingError,
    NotFoundError,
    PlaneApiClient,
    PlaneIssue,
    PlaneLinearConfig,
    PlaneProject,
    SyncEngine,
    SyncStateStore,
    load_env_file,
    source_marker,
)

ENV = {
    "PLANE_API_URL": "https://plane.example.test",
    "PLANE_API_TOKEN": "plane-test-value",
    "PLANE_WORKSPACE_SLUG": "company",
    "PLANE_ASSIGNEE_ID": "plane-user",
    "PLANE_ISSUES_URL": "https://plane.example.test/api/issues",
    "PLANE_LINEAR_PROJECT_MAP": '{"project-1": "linear-project"}',
    "PLANE_LINEAR_STATUS_MAP": '{"in progress": "In Progress"}',
    "LINEAR_API_URL": "https://linear.example.test/graphql",
    "LINEAR_API_KEY": "linear-test-value",
    "LINEAR_TEAM_ID": "linear-team",
    "LINEAR_ASSIGNEE_ID": "linear-user",
}


def issue(issue_id="plane-1", title="Fix the build", state_name="In Progress"):
    return PlaneIssue(
        id=issue_id,
        identifier="ENG-1" if issue_id == "plane-1" else "ENG-2",
        title=title,
        description="Source description",
        state_name=state_name,
        state_group="started",
        priority="high",
        due_date="2026-09-08",
        labels=("backend",),
        project=PlaneProject("project-1", "Platform", "PLAT"),
        assignee_id="plane-user",
        url=f"https://plane.example.test/issues/{issue_id}/",
        updated_at="2026-09-06T08:00:00Z",
    )


class FakePlane:
    def __init__(self, issues):
        self.issues = issues

    def list_assigned_issues(self):
        return list(self.issues)


class FakeLinear:
    def __init__(self):
        self.issues = {}
        self.created = []
        self.updated = []

    def find_by_source_key(self, source_key):
        marker = source_marker(source_key)
        for ref, value in self.issues.items():
            if marker in value.description:
                return ref
        return None

    def create_issue(self, value):
        ref = LinearIssueRef(
            f"linear-{len(self.issues) + 1}",
            f"KEN-{len(self.issues) + 1}",
            value.description,
        )
        self.issues[ref] = value
        self.created.append(value)
        return ref

    def update_issue(self, issue_id, value):
        for ref in self.issues:
            if ref.id == issue_id:
                self.issues[ref] = value
                self.updated.append((issue_id, value))
                return LinearIssueRef(ref.id, ref.identifier, value.description)
        raise NotFoundError("Linear issue was not found")

    def resolve_state_id(self, state_name, state_group, mapping):
        if state_name == "Broken state":
            raise MappingError(
                "no Linear workflow state matches Plane state 'Broken state'"
            )
        return {"In Progress": "state-started", "Done": "state-completed"}.get(
            state_name, "state-started"
        )

    def resolve_label_ids(self, labels):
        return tuple(f"label-{label}" for label in labels)

    def viewer_id(self):
        return "linear-user"


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.config = PlaneLinearConfig.from_environment(ENV)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_configuration_requires_credentials_without_echoing_them(self):
        missing = dict(ENV)
        del missing["PLANE_API_TOKEN"]
        with self.assertRaisesRegex(ConfigurationError, "PLANE_API_TOKEN"):
            PlaneLinearConfig.from_environment(missing)
        self.assertNotIn("plane-test-value", repr(self.config))
        self.assertNotIn("linear-test-value", repr(self.config))

    def test_create_then_update_is_idempotent_and_persists_traceability(self):
        linear = FakeLinear()
        state = SyncStateStore(self.temp.name)
        first = SyncEngine(self.config, FakePlane([issue()]), linear, state).run()
        second = SyncEngine(
            self.config, FakePlane([issue(title="Updated title")]), linear, state
        ).run()

        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(len(linear.created), 1)
        self.assertEqual(len(linear.updated), 1)
        value = linear.updated[0][1]
        self.assertEqual(value.title, "Updated title")
        self.assertEqual(value.priority, 2)
        self.assertEqual(value.due_date, "2026-09-08")
        self.assertEqual(value.state_id, "state-started")
        self.assertEqual(value.project_id, "linear-project")
        self.assertIn("https://plane.example.test/issues/plane-1/", value.description)
        self.assertIn(source_marker("plane:company:plane-1"), value.description)
        self.assertEqual((state.mappings_path.stat().st_mode & 0o077), 0)

    def test_missing_local_mapping_recovers_existing_linear_issue_by_marker(self):
        linear = FakeLinear()
        state = SyncStateStore(self.temp.name)
        SyncEngine(self.config, FakePlane([issue()]), linear, state).run()
        state.mappings_path.unlink()

        result = SyncEngine(self.config, FakePlane([issue()]), linear, state).run()

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(linear.created), 1)

    def test_one_mapping_error_does_not_stop_sibling_issues(self):
        linear = FakeLinear()
        state = SyncStateStore(self.temp.name)
        result = SyncEngine(
            self.config,
            FakePlane([issue("plane-bad", "Bad", "Broken state"), issue("plane-1")]),
            linear,
            state,
        ).run()

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["source_key"], "plane:company:plane-bad")
        self.assertEqual(len(linear.created), 1)
        records = list((state.runs_directory).glob("*.jsonl"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())["status"], "partial")

    def test_env_file_is_owner_only_and_values_stay_out_of_state(self):
        path = Path(self.temp.name) / "credentials.env"
        path.write_text("PLANE_API_TOKEN='file-test-value'\nLINEAR_API_KEY=linear\n")
        path.chmod(0o600)
        self.assertEqual(load_env_file(path)["PLANE_API_TOKEN"], "file-test-value")
        path.chmod(0o640)
        with self.assertRaisesRegex(ConfigurationError, "owner only"):
            load_env_file(path)


class PlaneAdapterTests(unittest.TestCase):
    def test_plane_adapter_filters_assignee_and_follows_next_page(self):
        config_values = dict(ENV)
        config_values["PLANE_ASSIGNEE_ID"] = "plane-user"
        config = PlaneLinearConfig.from_environment(config_values)

        class Transport:
            def __init__(self):
                self.calls = []

            def request(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "cursor=next" in url:
                    return {
                        "results": [
                            {
                                "id": "two",
                                "name": "Keep",
                                "assignee": {"id": "plane-user"},
                            }
                        ]
                    }
                return {
                    "results": [
                        {"id": "one", "name": "Keep", "assignee": {"id": "plane-user"}},
                        {
                            "id": "skip",
                            "name": "Skip",
                            "assignee": {"id": "someone-else"},
                        },
                    ],
                    "next_cursor": "next",
                }

        transport = Transport()
        issues = PlaneApiClient(config, transport).list_assigned_issues()
        self.assertEqual([value.id for value in issues], ["one", "two"])
        self.assertEqual(
            transport.calls[0][1]["headers"]["X-API-Key"], "plane-test-value"
        )
        self.assertEqual(len(transport.calls), 2)

    def test_default_plane_adapter_uses_workspace_advanced_search(self):
        config_values = dict(ENV)
        del config_values["PLANE_ISSUES_URL"]
        config = PlaneLinearConfig.from_environment(config_values)

        class Transport:
            def __init__(self):
                self.call = None

            def request(self, url, **kwargs):
                self.call = (url, kwargs)
                return {
                    "results": [
                        {
                            "id": "one",
                            "name": "Keep",
                            "assignees": [{"id": "plane-user"}],
                        },
                        {
                            "id": "skip",
                            "name": "Skip",
                            "assignees": [{"id": "someone-else"}],
                        },
                    ]
                }

        transport = Transport()
        issues = PlaneApiClient(config, transport).list_assigned_issues()
        self.assertEqual([value.id for value in issues], ["one"])
        self.assertEqual(transport.call[1]["method"], "POST")
        self.assertEqual(
            transport.call[1]["body"]["filters"]["assignees"], ["plane-user"]
        )
        self.assertIn(
            "/api/v1/workspaces/company/work-items/advanced-search/", transport.call[0]
        )


class LinearAdapterTests(unittest.TestCase):
    def test_source_lookup_scans_team_pages_without_putting_key_in_payload(self):
        config = PlaneLinearConfig.from_environment(ENV)
        marker = source_marker("plane:company:plane-1")

        class Transport:
            def __init__(self):
                self.calls = []

            def request(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return {
                    "data": {
                        "team": {
                            "issues": {
                                "nodes": [
                                    {
                                        "id": "linear-1",
                                        "identifier": "KEN-1",
                                        "description": f"{marker}",
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }

        transport = Transport()
        found = LinearApiClient(config, transport).find_by_source_key(
            "plane:company:plane-1"
        )
        self.assertEqual(found.id, "linear-1")
        self.assertEqual(
            transport.calls[0][1]["headers"]["Authorization"], "linear-test-value"
        )
        self.assertNotIn("linear-test-value", json.dumps(transport.calls[0][1]["body"]))


if __name__ == "__main__":
    unittest.main()
