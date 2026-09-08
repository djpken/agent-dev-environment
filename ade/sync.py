"""Plane-to-Linear synchronization with injectable provider boundaries.

The module keeps the business rule independent from HTTP.  ``PlaneApiClient``
and ``LinearApiClient`` are the production adapters; tests can provide objects
with the same small methods without credentials or network access.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import tempfile
import urllib.request
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

SYNC_PROVIDER = "plane-linear-sync"
SYNC_VERSION = "0.1.0"
SOURCE_MARKER_PREFIX = "ade:plane-linear-sync"


class SyncError(ValueError):
    """Base error for configuration, provider, and synchronization failures."""


class ConfigurationError(SyncError):
    """The local, non-secret sync configuration is invalid or incomplete."""


class ApiError(SyncError):
    """A provider API request failed without retaining a response secret."""


class NotFoundError(ApiError):
    """The target Linear issue no longer exists."""


class MappingError(SyncError):
    """A source field cannot be mapped safely to a Linear field."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _normalise(value: Any) -> str:
    return re.sub(r"[\s_-]+", " ", str(value or "").strip().casefold())


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("name", "title", "value", "display_name", "displayName"):
            if value.get(key) is not None:
                return str(value[key]).strip()
    return str(value).strip()


def _entity_value(value: Any, *keys: str) -> str:
    if isinstance(value, Mapping):
        for key in keys:
            if value.get(key) is not None:
                return str(value[key]).strip()
    elif value is not None:
        return str(value).strip()
    return ""


def _date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise MappingError("Plane due date is not an ISO date") from exc


def _priority(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("name", value.get("value", value.get("priority")))
    if isinstance(value, int):
        return {0: "none", 1: "urgent", 2: "high", 3: "medium", 4: "low"}.get(
            value, "none"
        )
    normalised = _normalise(value)
    aliases = {
        "no priority": "none",
        "none": "none",
        "urgent": "urgent",
        "high": "high",
        "medium": "medium",
        "low": "low",
    }
    return aliases.get(normalised, normalised or "none")


def _status_type(value: Any) -> str:
    normalised = _normalise(value)
    aliases = {
        "backlog": "backlog",
        "unstarted": "unstarted",
        "todo": "unstarted",
        "to do": "unstarted",
        "open": "unstarted",
        "started": "started",
        "in progress": "started",
        "doing": "started",
        "active": "started",
        "completed": "completed",
        "complete": "completed",
        "done": "completed",
        "closed": "completed",
        "resolved": "completed",
        "canceled": "canceled",
        "cancelled": "canceled",
        "rejected": "canceled",
    }
    return aliases.get(normalised, normalised)


def _validate_endpoint(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a valid URL") from exc
    local_http = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ConfigurationError(f"{name} must be an HTTPS URL without credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError(f"{name} must not contain query or fragment data")
    if parsed.scheme != "https" and not local_http:
        raise ConfigurationError(f"{name} must use HTTPS unless it targets localhost")
    return value.rstrip("/")


def _json_object(value: str | None, name: str) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{name} must be a JSON object") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ConfigurationError(f"{name} must be a JSON object of strings")
    return parsed


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read a user-owned env file without putting its values in ADE state.

    The file must be outside the repository in normal use and have no group or
    other permissions.  Its path may be recorded in a scheduler command; its
    contents never are.
    """

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ConfigurationError("credential env file must be a regular file")
    path = candidate.resolve()
    repository = Path(__file__).resolve().parent.parent
    if path.is_relative_to(repository):
        raise ConfigurationError("credential env file must be outside the repository")
    if not path.is_file():
        raise ConfigurationError("credential env file must be a regular file")
    if path.stat().st_mode & 0o077:
        raise ConfigurationError(
            "credential env file must be readable by the owner only"
        )
    values: dict[str, str] = {}
    key_pattern = re.compile(r"^[A-Z][A-Z0-9_]*$")
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key_pattern.fullmatch(key.strip()):
            raise ConfigurationError(f"invalid env file entry at line {line_number}")
        value = value.strip()
        if value[:1] in {"'", '"'}:
            try:
                parsed = shlex.split(value, comments=False, posix=True)
            except ValueError as exc:
                raise ConfigurationError(
                    f"invalid quoted env value at line {line_number}"
                ) from exc
            if len(parsed) != 1:
                raise ConfigurationError(f"invalid env value at line {line_number}")
            value = parsed[0]
        values[key.strip()] = value
    return values


@dataclass(frozen=True, repr=False)
class PlaneLinearConfig:
    """Non-persistent sync configuration plus in-memory credentials."""

    plane_api_url: str
    plane_api_token: str
    plane_workspace: str
    plane_assignee_id: str | None
    plane_assignee_email: str | None
    plane_issues_url: str | None
    plane_project_id: str | None
    linear_api_url: str
    linear_api_key: str
    linear_team_id: str
    linear_assignee_id: str | None
    linear_project_id: str | None
    project_map: dict[str, str]
    status_map: dict[str, str]
    timeout: float

    def __repr__(self) -> str:
        return (
            f"PlaneLinearConfig(plane_api_url={self.plane_api_url!r}, plane_workspace={self.plane_workspace!r}, "
            f"linear_api_url={self.linear_api_url!r}, linear_team_id={self.linear_team_id!r}, credentials=<redacted>)"
        )

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> PlaneLinearConfig:
        env = dict(os.environ if environment is None else environment)
        required = {
            "PLANE_API_URL": env.get("PLANE_API_URL"),
            "PLANE_API_TOKEN": env.get("PLANE_API_TOKEN"),
            "PLANE_WORKSPACE_SLUG": env.get("PLANE_WORKSPACE_SLUG"),
            "LINEAR_API_KEY": env.get("LINEAR_API_KEY"),
            "LINEAR_TEAM_ID": env.get("LINEAR_TEAM_ID"),
        }
        missing = sorted(name for name, value in required.items() if not value)
        if not env.get("PLANE_ASSIGNEE_ID") and not env.get("PLANE_ASSIGNEE_EMAIL"):
            missing.append("PLANE_ASSIGNEE_ID or PLANE_ASSIGNEE_EMAIL")
        if missing:
            raise ConfigurationError(
                "missing sync configuration: " + ", ".join(missing)
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", required["PLANE_WORKSPACE_SLUG"]):
            raise ConfigurationError(
                "PLANE_WORKSPACE_SLUG must be a Plane workspace slug"
            )
        try:
            timeout = float(env.get("PLANE_LINEAR_TIMEOUT", "30"))
        except ValueError as exc:
            raise ConfigurationError("PLANE_LINEAR_TIMEOUT must be a number") from exc
        if not 1 <= timeout <= 120:
            raise ConfigurationError(
                "PLANE_LINEAR_TIMEOUT must be between 1 and 120 seconds"
            )
        plane_api_url = _validate_endpoint(required["PLANE_API_URL"], "PLANE_API_URL")
        linear_api_url = _validate_endpoint(
            env.get("LINEAR_API_URL", "https://api.linear.app/graphql"),
            "LINEAR_API_URL",
        )
        plane_issues_url = env.get("PLANE_ISSUES_URL") or None
        if plane_issues_url:
            plane_issues_url = _validate_endpoint(plane_issues_url, "PLANE_ISSUES_URL")
        return cls(
            plane_api_url=plane_api_url,
            plane_api_token=required["PLANE_API_TOKEN"],
            plane_workspace=required["PLANE_WORKSPACE_SLUG"],
            plane_assignee_id=env.get("PLANE_ASSIGNEE_ID") or None,
            plane_assignee_email=env.get("PLANE_ASSIGNEE_EMAIL") or None,
            plane_issues_url=plane_issues_url,
            plane_project_id=env.get("PLANE_PROJECT_ID") or None,
            linear_api_url=linear_api_url,
            linear_api_key=required["LINEAR_API_KEY"],
            linear_team_id=required["LINEAR_TEAM_ID"],
            linear_assignee_id=env.get("LINEAR_ASSIGNEE_ID") or None,
            linear_project_id=env.get("LINEAR_PROJECT_ID") or None,
            project_map=_json_object(
                env.get("PLANE_LINEAR_PROJECT_MAP"), "PLANE_LINEAR_PROJECT_MAP"
            ),
            status_map=_json_object(
                env.get("PLANE_LINEAR_STATUS_MAP"), "PLANE_LINEAR_STATUS_MAP"
            ),
            timeout=timeout,
        )


@dataclass(frozen=True)
class PlaneProject:
    id: str
    name: str
    identifier: str
    url: str | None = None


@dataclass(frozen=True)
class PlaneIssue:
    id: str
    identifier: str
    title: str
    description: str = ""
    state_name: str = ""
    state_group: str = ""
    priority: str = "none"
    due_date: str | None = None
    labels: tuple[str, ...] = ()
    project: PlaneProject | None = None
    assignee_id: str | None = None
    assignee_email: str | None = None
    assignee_ids: tuple[str, ...] = ()
    assignee_emails: tuple[str, ...] = ()
    url: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], base_url: str) -> PlaneIssue:
        issue_id = _entity_value(payload, "id", "issue_id", "issueId")
        title = _entity_value(payload, "name", "title")
        if not issue_id or not title:
            raise MappingError("Plane issue must contain id and title")
        identifier = (
            _entity_value(payload, "identifier", "sequence_id", "sequenceId")
            or issue_id
        )
        state = (
            payload.get("state_detail") or payload.get("state") or payload.get("status")
        )
        state_name = _entity_value(state, "name", "title", "value") or _text(state)
        state_group = _entity_value(state, "group", "type", "group_name", "groupName")
        project_value = payload.get("project_detail") or payload.get("project")
        project = None
        if isinstance(project_value, Mapping):
            project_id = _entity_value(project_value, "id", "project_id", "projectId")
            project_name = _entity_value(project_value, "name", "title")
            project_identifier = _entity_value(
                project_value, "identifier", "key", "project_identifier"
            )
            if project_id or project_name or project_identifier:
                project = PlaneProject(
                    project_id,
                    project_name,
                    project_identifier,
                    project_value.get("url"),
                )
        elif project_value:
            project = PlaneProject(_text(project_value), "", "")
        assignee = payload.get("assignee_detail") or payload.get("assignee")
        assignees = payload.get("assignees")
        if assignee is not None:
            assignees = [assignee]
        elif isinstance(assignees, Mapping):
            assignees = [assignees]
        elif not isinstance(assignees, (list, tuple)):
            assignees = []
        assignee_ids = tuple(
            value
            for value in (
                _entity_value(item, "id", "user_id", "userId")
                if isinstance(item, Mapping)
                else str(item).strip()
                for item in assignees
            )
            if value
        )
        assignee_emails = tuple(
            value
            for value in (
                _entity_value(item, "email")
                for item in assignees
                if isinstance(item, Mapping)
            )
            if value
        )
        assignee_id = assignee_ids[0] if assignee_ids else None
        assignee_email = assignee_emails[0] if assignee_emails else None
        labels_value = payload.get("label_details") or payload.get("labels") or ()
        if isinstance(labels_value, (str, bytes, Mapping)):
            labels_value = (labels_value,)
        labels = tuple(sorted({_text(label) for label in labels_value if _text(label)}))
        issue_url = (
            _entity_value(payload, "url", "issue_url", "issueUrl")
            or f"{base_url}/issues/{issue_id}/"
        )
        description = _entity_value(
            payload, "description_str", "description", "description_html"
        )
        return cls(
            id=issue_id,
            identifier=identifier,
            title=title,
            description=description,
            state_name=state_name,
            state_group=state_group,
            priority=_priority(payload.get("priority")),
            due_date=_date(
                payload.get("due_date")
                or payload.get("dueDate")
                or payload.get("target_date")
                or payload.get("targetDate")
            ),
            labels=labels,
            project=project,
            assignee_id=assignee_id,
            assignee_email=assignee_email,
            assignee_ids=assignee_ids,
            assignee_emails=assignee_emails,
            url=issue_url,
            updated_at=_entity_value(payload, "updated_at", "updatedAt") or None,
        )

    def source_key(self, workspace: str) -> str:
        return f"plane:{workspace}:{self.id}"

    def matches_assignee(self, config: PlaneLinearConfig) -> bool:
        ids = self.assignee_ids or ((self.assignee_id,) if self.assignee_id else ())
        emails = self.assignee_emails or (
            (self.assignee_email,) if self.assignee_email else ()
        )
        if ids and config.plane_assignee_id:
            return config.plane_assignee_id in ids
        if emails and config.plane_assignee_email:
            return any(
                email.casefold() == config.plane_assignee_email.casefold()
                for email in emails
            )
        return True


@dataclass(frozen=True)
class LinearIssueInput:
    title: str
    description: str
    priority: int
    due_date: str | None
    state_id: str
    label_ids: tuple[str, ...]
    project_id: str | None
    assignee_id: str | None

    def as_graphql_input(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "dueDate": self.due_date,
            "stateId": self.state_id,
            "labelIds": list(self.label_ids),
        }
        if self.project_id:
            value["projectId"] = self.project_id
        if self.assignee_id:
            value["assigneeId"] = self.assignee_id
        return value


@dataclass(frozen=True)
class LinearIssueRef:
    id: str
    identifier: str = ""
    description: str = ""


class PlaneIssueSource(Protocol):
    def list_assigned_issues(self) -> list[PlaneIssue]: ...


class LinearIssueTarget(Protocol):
    def find_by_source_key(self, source_key: str) -> LinearIssueRef | None: ...

    def create_issue(self, value: LinearIssueInput) -> LinearIssueRef: ...

    def update_issue(
        self, issue_id: str, value: LinearIssueInput
    ) -> LinearIssueRef: ...

    def resolve_state_id(
        self, state_name: str, state_group: str, mapping: Mapping[str, str]
    ) -> str: ...

    def resolve_label_ids(self, labels: tuple[str, ...]) -> tuple[str, ...]: ...

    def viewer_id(self) -> str: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, new_url):
        raise ApiError("provider redirect refused")


class JsonHttpTransport:
    """Small HTTP boundary used by both adapters and easy to replace in tests."""

    def request(
        self,
        url: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=data, headers=dict(headers or {}), method=method
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=timeout) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except HTTPError as exc:
            if exc.code == 404:
                raise NotFoundError(f"provider returned HTTP 404 for {url}") from None
            raise ApiError(f"provider returned HTTP {exc.code} for {url}") from None
        except URLError as exc:
            raise ApiError(f"provider request failed for {url}: {exc.reason}") from None
        if status >= 400:
            raise ApiError(f"provider returned HTTP {status} for {url}")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(f"provider returned invalid JSON for {url}") from exc


def _with_query(url: str, values: Mapping[str, str]) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(values.items())
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _same_origin(url: str, base: str) -> bool:
    current = urlsplit(url)
    origin = urlsplit(base)
    current_port = current.port or (443 if current.scheme == "https" else 80)
    origin_port = origin.port or (443 if origin.scheme == "https" else 80)
    return (
        current.scheme == origin.scheme
        and current.hostname == origin.hostname
        and current_port == origin_port
    )


def _page(payload: Any) -> tuple[list[Mapping[str, Any]], Any]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)], None
    if not isinstance(payload, Mapping):
        raise ApiError("provider returned an unexpected list response")
    items: Any = payload.get("results", payload.get("issues", payload.get("items", [])))
    if isinstance(items, Mapping):
        items = items.get("nodes", items.get("results", []))
    if not isinstance(items, list):
        raise ApiError("provider returned an unexpected issue collection")
    continuation = (
        payload.get("next") or payload.get("next_page") or payload.get("next_cursor")
    )
    if not continuation and payload.get("next_page_results"):
        raise ApiError("Plane indicated another page without a continuation cursor")
    return [item for item in items if isinstance(item, Mapping)], continuation


class PlaneApiClient:
    """Plane REST adapter; it only returns issues assigned to the configured user."""

    def __init__(
        self, config: PlaneLinearConfig, transport: JsonHttpTransport | None = None
    ):
        self.config = config
        self.transport = transport or JsonHttpTransport()

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "X-API-Key": self.config.plane_api_token}

    def _append_issues(
        self,
        items: list[Mapping[str, Any]],
        issues: list[PlaneIssue],
        seen_ids: set[str],
    ) -> None:
        for item in items:
            issue = PlaneIssue.from_payload(item, self.config.plane_api_url)
            if issue.id in seen_ids or not issue.matches_assignee(self.config):
                continue
            seen_ids.add(issue.id)
            issues.append(issue)

    def _list_get(self, endpoint: str, params: Mapping[str, str]) -> list[PlaneIssue]:
        next_url: Any = _with_query(endpoint, params)
        seen_urls: set[str] = set()
        issues: list[PlaneIssue] = []
        seen_ids: set[str] = set()
        while next_url:
            if isinstance(next_url, str) and not next_url.startswith(
                ("https://", "http://")
            ):
                next_url = self.config.plane_api_url + "/" + next_url.lstrip("/")
            if not _same_origin(next_url, endpoint):
                raise ApiError("Plane pagination pointed to a different origin")
            if next_url in seen_urls:
                raise ApiError("Plane pagination repeated the same page")
            seen_urls.add(next_url)
            payload = self.transport.request(
                next_url, headers=self._headers(), timeout=self.config.timeout
            )
            items, continuation = _page(payload)
            self._append_issues(items, issues, seen_ids)
            if isinstance(continuation, Mapping):
                next_url = continuation.get("url") or continuation.get("next")
            elif isinstance(continuation, str) and continuation.startswith(
                ("http://", "https://", "/")
            ):
                next_url = continuation
            elif continuation:
                next_url = _with_query(
                    endpoint, {**params, "cursor": str(continuation)}
                )
            else:
                next_url = None
        return issues

    def _list_advanced(self) -> list[PlaneIssue]:
        if not self.config.plane_assignee_id:
            raise ConfigurationError(
                "PLANE_ASSIGNEE_ID is required for workspace-wide Plane search"
            )
        endpoint = f"{self.config.plane_api_url}/api/v1/workspaces/{quote(self.config.plane_workspace, safe='')}/work-items/advanced-search/"
        body: dict[str, Any] = {
            "filters": {"assignees": [self.config.plane_assignee_id]},
            "limit": 100,
            "workspace_search": True,
        }
        issues: list[PlaneIssue] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        while True:
            payload = self.transport.request(
                endpoint,
                method="POST",
                headers={**self._headers(), "Content-Type": "application/json"},
                body=body,
                timeout=self.config.timeout,
            )
            items, continuation = _page(payload)
            self._append_issues(items, issues, seen_ids)
            if isinstance(continuation, Mapping):
                continuation = (
                    continuation.get("cursor")
                    or continuation.get("next_cursor")
                    or continuation.get("url")
                )
            if not continuation:
                return issues
            cursor = str(continuation)
            if cursor in seen_cursors:
                raise ApiError("Plane pagination repeated the same cursor")
            seen_cursors.add(cursor)
            body["cursor"] = continuation

    def list_assigned_issues(self) -> list[PlaneIssue]:
        if self.config.plane_issues_url:
            assignee = (
                self.config.plane_assignee_id or self.config.plane_assignee_email or ""
            )
            params = {"assignee": assignee, "per_page": "100"}
            if self.config.plane_project_id:
                params["project"] = self.config.plane_project_id
            return self._list_get(self.config.plane_issues_url, params)
        if self.config.plane_project_id:
            endpoint = (
                f"{self.config.plane_api_url}/api/v1/workspaces/{quote(self.config.plane_workspace, safe='')}"
                f"/projects/{quote(self.config.plane_project_id, safe='')}/work-items/"
            )
            return self._list_get(endpoint, {"per_page": "100", "expand": "assignees"})
        return self._list_advanced()


class LinearApiClient:
    """Linear GraphQL adapter for issue lookup, creation, and updates."""

    def __init__(
        self, config: PlaneLinearConfig, transport: JsonHttpTransport | None = None
    ):
        self.config = config
        self.transport = transport or JsonHttpTransport()
        self._states: list[Mapping[str, Any]] | None = None
        self._labels: list[Mapping[str, Any]] | None = None
        self._viewer: str | None = None
        self._source_index: dict[str, LinearIssueRef] | None = None

    def _graphql(self, query: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = self.transport.request(
            self.config.linear_api_url,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": self.config.linear_api_key,
            },
            body={"query": query, "variables": variables},
            timeout=self.config.timeout,
        )
        errors = payload.get("errors") if isinstance(payload, Mapping) else None
        if errors:
            message = "; ".join(
                _text(error.get("message"))
                for error in errors
                if isinstance(error, Mapping)
            )
            if "not found" in message.casefold():
                raise NotFoundError("Linear issue was not found")
            raise ApiError(
                "Linear GraphQL request failed" + (": " + message if message else "")
            )
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise ApiError("Linear returned no GraphQL data")
        return data

    def find_by_source_key(self, source_key: str) -> LinearIssueRef | None:
        if self._source_index is None:
            self._source_index = {}
            after: str | None = None
            seen_cursors: set[str] = set()
            while True:
                data = self._graphql(
                    """
                    query FindPlaneSyncIssues($teamId: ID!, $after: String) {
                      team(id: $teamId) {
                        issues(first: 100, after: $after) {
                          nodes { id identifier description }
                          pageInfo { hasNextPage endCursor }
                        }
                      }
                    }
                    """,
                    {"teamId": self.config.linear_team_id, "after": after},
                )
                issues = data.get("team", {}).get("issues", {})
                nodes = issues.get("nodes", []) if isinstance(issues, Mapping) else []
                for node in nodes if isinstance(nodes, list) else ():
                    if not isinstance(node, Mapping):
                        continue
                    description = _text(node.get("description"))
                    match = re.search(
                        rf"<!-- {re.escape(SOURCE_MARKER_PREFIX)} source=([^ ]+) -->",
                        description,
                    )
                    if match and node.get("id"):
                        ref = LinearIssueRef(
                            _text(node.get("id")),
                            _text(node.get("identifier")),
                            description,
                        )
                        previous = self._source_index.get(match.group(1))
                        if previous and previous.id != ref.id:
                            raise ApiError(
                                "multiple Linear issues contain the same Plane source marker"
                            )
                        self._source_index[match.group(1)] = ref
                page_info = (
                    issues.get("pageInfo", {}) if isinstance(issues, Mapping) else {}
                )
                if not page_info.get("hasNextPage"):
                    break
                after = page_info.get("endCursor")
                if not after or after in seen_cursors:
                    raise ApiError("Linear pagination repeated the same cursor")
                seen_cursors.add(after)
        return self._source_index.get(source_key)

    def create_issue(self, value: LinearIssueInput) -> LinearIssueRef:
        data = self._graphql(
            """
            mutation CreatePlaneSyncIssue($input: IssueCreateInput!) {
              issueCreate(input: $input) { success issue { id identifier description } }
            }
            """,
            {
                "input": {
                    "teamId": self.config.linear_team_id,
                    **value.as_graphql_input(),
                }
            },
        )
        result = data.get("issueCreate", {})
        issue = result.get("issue") if isinstance(result, Mapping) else None
        if (
            not result.get("success")
            or not isinstance(issue, Mapping)
            or not issue.get("id")
        ):
            raise ApiError("Linear did not create the issue")
        return LinearIssueRef(
            _text(issue.get("id")),
            _text(issue.get("identifier")),
            _text(issue.get("description")),
        )

    def update_issue(self, issue_id: str, value: LinearIssueInput) -> LinearIssueRef:
        data = self._graphql(
            """
            mutation UpdatePlaneSyncIssue($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success issue { id identifier description } }
            }
            """,
            {"id": issue_id, "input": value.as_graphql_input()},
        )
        result = data.get("issueUpdate", {})
        issue = result.get("issue") if isinstance(result, Mapping) else None
        if (
            not result.get("success")
            or not isinstance(issue, Mapping)
            or not issue.get("id")
        ):
            raise ApiError("Linear did not update the issue")
        return LinearIssueRef(
            _text(issue.get("id")),
            _text(issue.get("identifier")),
            _text(issue.get("description")),
        )

    def _workflow_states(self) -> list[Mapping[str, Any]]:
        if self._states is None:
            data = self._graphql(
                """
                query PlaneSyncWorkflowStates($teamId: ID!) {
                  workflowStates(filter: {team: {id: {eq: $teamId}}}, first: 100) {
                    nodes { id name type }
                  }
                }
                """,
                {"teamId": self.config.linear_team_id},
            )
            nodes = data.get("workflowStates", {}).get("nodes", [])
            self._states = [node for node in nodes if isinstance(node, Mapping)]
        return self._states

    def resolve_state_id(
        self, state_name: str, state_group: str, mapping: Mapping[str, str]
    ) -> str:
        states = self._workflow_states()
        normalised_mapping = {_normalise(key): value for key, value in mapping.items()}
        source_name = _normalise(state_name)
        source_group = _status_type(state_group or state_name)
        target = normalised_mapping.get(source_name) or normalised_mapping.get(
            source_group
        )
        if target:
            target_normalised = _normalise(target)
            for state in states:
                if (
                    _text(state.get("id")) == target
                    or _normalise(state.get("name")) == target_normalised
                ):
                    return _text(state.get("id"))
            if target_normalised in {
                "backlog",
                "unstarted",
                "started",
                "completed",
                "canceled",
            }:
                candidates = [
                    state
                    for state in states
                    if _status_type(state.get("type")) == target_normalised
                ]
                if len(candidates) == 1:
                    return _text(candidates[0].get("id"))
        for state in states:
            if source_name and _normalise(state.get("name")) == source_name:
                return _text(state.get("id"))
        candidates = [
            state for state in states if _status_type(state.get("type")) == source_group
        ]
        if source_group == "started":
            preferred = {"in progress", "started", "doing"}
            candidates.sort(
                key=lambda state: (
                    0 if _normalise(state.get("name")) in preferred else 1,
                    _normalise(state.get("name")),
                )
            )
        else:
            candidates.sort(key=lambda state: _normalise(state.get("name")))
        if candidates and _text(candidates[0].get("id")):
            return _text(candidates[0].get("id"))
        raise MappingError(
            f"no Linear workflow state matches Plane state {state_name or state_group!r}"
        )

    def _issue_labels(self) -> list[Mapping[str, Any]]:
        if self._labels is None:
            data = self._graphql(
                """
                query PlaneSyncIssueLabels($teamId: ID!) {
                  issueLabels(filter: {team: {id: {eq: $teamId}}}, first: 250) {
                    nodes { id name }
                  }
                }
                """,
                {"teamId": self.config.linear_team_id},
            )
            nodes = data.get("issueLabels", {}).get("nodes", [])
            self._labels = [node for node in nodes if isinstance(node, Mapping)]
        return self._labels

    def resolve_label_ids(self, labels: tuple[str, ...]) -> tuple[str, ...]:
        lookup = {
            _normalise(label.get("name")): _text(label.get("id"))
            for label in self._issue_labels()
        }
        return tuple(
            sorted(
                {
                    lookup[_normalise(label)]
                    for label in labels
                    if _normalise(label) in lookup
                }
            )
        )

    def viewer_id(self) -> str:
        if self._viewer is None:
            data = self._graphql("query PlaneSyncViewer { viewer { id } }", {})
            self._viewer = _text(data.get("viewer", {}).get("id"))
            if not self._viewer:
                raise ApiError("Linear viewer response did not contain an id")
        return self._viewer


class SyncStateStore:
    """ADE-owned local state.  It never writes credentials or source payloads."""

    def __init__(self, root: str | Path):
        self.directory = Path(root).expanduser().resolve() / "state" / SYNC_PROVIDER
        self.mappings_path = self.directory / "mappings.json"
        self.runs_directory = self.directory / "runs"
        self.lock_path = self.directory / "run.lock"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)

    @contextmanager
    def lock(self):
        with self.lock_path.open("a+") as stream:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _read_mappings(self) -> dict[str, dict[str, Any]]:
        if not self.mappings_path.exists():
            return {}
        try:
            value = json.loads(self.mappings_path.read_text())
        except json.JSONDecodeError as exc:
            raise SyncError("Plane/Linear mapping state is invalid JSON") from exc
        if (
            not isinstance(value, Mapping)
            or value.get("version") != 1
            or not isinstance(value.get("mappings"), Mapping)
        ):
            raise SyncError("unsupported Plane/Linear mapping state")
        return {
            str(key): dict(item)
            for key, item in value["mappings"].items()
            if isinstance(item, Mapping)
        }

    def mapping(self, source_key: str) -> dict[str, Any] | None:
        return self._read_mappings().get(source_key)

    def save_mapping(self, source_key: str, value: Mapping[str, Any]) -> None:
        mappings = self._read_mappings()
        mappings[source_key] = dict(value)
        self._atomic_json(self.mappings_path, {"version": 1, "mappings": mappings})

    def append_run(self, value: Mapping[str, Any]) -> None:
        self.runs_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runs_directory, 0o700)
        path = self.runs_directory / (datetime.now(UTC).date().isoformat() + ".jsonl")
        if not path.exists():
            path.touch(mode=0o600)
        os.chmod(path, 0o600)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=".state-", delete=False, encoding="utf-8"
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)


def source_marker(source_key: str) -> str:
    return f"<!-- {SOURCE_MARKER_PREFIX} source={source_key} -->"


def _source_description(issue: PlaneIssue, source_key: str) -> str:
    project = ""
    if issue.project:
        project = issue.project.identifier or issue.project.name or issue.project.id
    lines = [issue.description.strip()] if issue.description.strip() else []
    lines.extend(
        [
            "",
            "---",
            "來源追溯",
            f"Plane issue：[{issue.identifier}]({issue.url})"
            if issue.url
            else f"Plane issue：{issue.identifier}",
            f"Plane project：{project}" if project else "Plane project：未提供",
            source_marker(source_key),
        ]
    )
    return "\n".join(lines).strip()


def _linear_priority(value: str) -> int:
    return {"urgent": 1, "high": 2, "medium": 3, "low": 4, "none": 0}.get(
        _normalise(value), 0
    )


class SyncEngine:
    """Orchestrate one full run while isolating failures per Plane issue."""

    def __init__(
        self,
        config: PlaneLinearConfig,
        plane: PlaneIssueSource,
        linear: LinearIssueTarget,
        state: SyncStateStore,
    ):
        self.config = config
        self.plane = plane
        self.linear = linear
        self.state = state
        self._linear_assignee_id: str | None = config.linear_assignee_id
        self.secrets = tuple(
            secret
            for secret in (config.plane_api_token, config.linear_api_key)
            if secret
        )

    def run(self) -> dict[str, Any]:
        started_at = _now()
        result: dict[str, Any] = {
            "run_id": str(uuid.uuid4()),
            "provider": SYNC_PROVIDER,
            "started_at": started_at,
            "status": "failed",
            "discovered": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [],
            "items": [],
        }
        with self.state.lock():
            try:
                source_issues = self.plane.list_assigned_issues()
                result["discovered"] = len(source_issues)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(self._error("list", None, exc))
                return self._finish(result)
            seen: set[str] = set()
            for issue in source_issues:
                source_key = issue.source_key(self.config.plane_workspace)
                if source_key in seen:
                    result["skipped"] += 1
                    continue
                seen.add(source_key)
                try:
                    item = self._sync_issue(issue, source_key)
                    result["items"].append(item)
                    result[item["action"]] += 1
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(self._error("sync", source_key, exc))
            return self._finish(result)

    def _finish(self, result: dict[str, Any]) -> dict[str, Any]:
        result["finished_at"] = _now()
        if result["errors"]:
            result["status"] = (
                "partial" if result["created"] or result["updated"] else "failed"
            )
        else:
            result["status"] = "completed"
        self.state.append_run(result)
        return result

    def _sync_issue(self, issue: PlaneIssue, source_key: str) -> dict[str, Any]:
        value = self._linear_input(issue, source_key)
        mapping = self.state.mapping(source_key)
        target: LinearIssueRef | None = None
        action = "updated"
        if mapping and mapping.get("linear_id"):
            try:
                target = self.linear.update_issue(str(mapping["linear_id"]), value)
            except NotFoundError:
                target = None
        if target is None:
            target = self.linear.find_by_source_key(source_key)
            if target:
                target = self.linear.update_issue(target.id, value)
            else:
                target = self.linear.create_issue(value)
                action = "created"
        self.state.save_mapping(
            source_key,
            {
                "linear_id": target.id,
                "linear_identifier": target.identifier,
                "plane_updated_at": issue.updated_at,
                "synced_at": _now(),
            },
        )
        return {
            "source_key": source_key,
            "linear_id": target.id,
            "linear_identifier": target.identifier,
            "action": action,
        }

    def _linear_input(self, issue: PlaneIssue, source_key: str) -> LinearIssueInput:
        state_id = self.linear.resolve_state_id(
            issue.state_name, issue.state_group, self.config.status_map
        )
        labels = self.linear.resolve_label_ids(issue.labels)
        if self._linear_assignee_id is None:
            self._linear_assignee_id = self.linear.viewer_id()
        project_id = self.config.linear_project_id
        if project_id is None and issue.project:
            for key in (issue.project.id, issue.project.identifier, issue.project.name):
                if key and key in self.config.project_map:
                    project_id = self.config.project_map[key]
                    break
        return LinearIssueInput(
            title=issue.title,
            description=_source_description(issue, source_key),
            priority=_linear_priority(issue.priority),
            due_date=issue.due_date,
            state_id=state_id,
            label_ids=labels,
            project_id=project_id,
            assignee_id=self._linear_assignee_id,
        )

    def _error(
        self, stage: str, source_key: str | None, exc: Exception
    ) -> dict[str, str | None]:
        message = str(exc)
        for secret in self.secrets:
            message = message.replace(secret, "<redacted>")
        return {
            "stage": stage,
            "source_key": source_key,
            "error": type(exc).__name__,
            "message": message,
        }


def run_from_environment(
    root: str | Path, environment: Mapping[str, str] | None = None
) -> dict[str, Any]:
    config = PlaneLinearConfig.from_environment(environment)
    state = SyncStateStore(root)
    return SyncEngine(
        config, PlaneApiClient(config), LinearApiClient(config), state
    ).run()


def main(argv: list[str] | None = None) -> int:
    """Small provider health entry point for ADE's builtin provider manifest."""

    import argparse

    parser = argparse.ArgumentParser(
        description="Synchronize assigned Plane issues to Linear"
    )
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(SYNC_VERSION)
        return 0
    print(
        json.dumps(
            run_from_environment(Path.home() / ".local/share/ade"), ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
