#!/usr/bin/env python3
"""Run a bounded Codex efficiency experiment in disposable repository snapshots.

Uses existing Codex authentication; never enables personal feature defaults.
Removes only its temporary trust registrations. Raw responses stay in the
private output directory, not the repository or the summary report.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import stat
import statistics
import subprocess
import tarfile
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = ("baseline", "context", "instructions", "combined")
OVERLAY = ("AGENTS.md", "docs/agents/code-search.md", "docs/agents/workflow-pack.md")
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")


def config_fingerprint(path, output):
    config = tomllib.loads(path.read_text()) if path.exists() else {}
    # Codex registers disposable cwd paths as trusted during exec. Ignore only
    # this run's entries, not unrelated configuration edits.
    projects = config.get("projects", {})
    for name in list(projects):
        if Path(name).is_relative_to(output):
            del projects[name]
    if not projects:
        config.pop("projects", None)
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def remove_trial_trust(path, workspaces):
    """Remove only exact Codex-generated trusted entries owned by this run."""
    if not path.exists():
        return 0
    path = path.resolve()
    original = path.read_text()
    parsed = tomllib.loads(original)
    updated = original
    removed = 0
    for workspace in workspaces:
        name = str(workspace.resolve())
        entry = parsed.get("projects", {}).get(name)
        if entry is None:
            continue
        if entry != {"trust_level": "trusted"}:
            raise ValueError("trial trust entry changed; refusing to remove it")
        header = "[projects." + json.dumps(name, ensure_ascii=False) + "]"
        updated, count = re.subn(r"(?m)^" + re.escape(header) +
                                r'\ntrust_level = "trusted"\n\n?', "", updated)
        if count != 1:
            raise ValueError("unrecognized trial trust serialization; refusing to rewrite config")
        del parsed["projects"][name]
        removed += 1
    if not removed:
        return 0
    reparsed = tomllib.loads(updated)
    for value in (parsed, reparsed):
        if not value.get("projects"):
            value.pop("projects", None)
    if reparsed != parsed:
        raise ValueError("trust cleanup would change unrelated configuration")
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        handle.write(updated)
        staged = Path(handle.name)
    try:
        staged.chmod(path.stat().st_mode & 0o777)
        if path.read_text() != original:
            raise ValueError("config changed during trust cleanup")
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    return removed


def events_result(text):
    """Incomplete/error streams must never look like a zero-token success."""
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    completed = [e for e in events if e.get("type") == "turn.completed"]
    if len(completed) != 1 or any(e.get("type") in ("error", "turn.failed") for e in events):
        raise ValueError("expected exactly one successful turn.completed")
    usage = completed[0].get("usage", {})
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in USAGE_KEYS):
        raise ValueError("missing or invalid actual token usage")
    if usage["cached_input_tokens"] > usage["input_tokens"]:
        raise ValueError("cached input exceeds input tokens")
    if usage["input_tokens"] == 0:
        raise ValueError("zero input usage does not establish a measured model request")
    messages = [e["item"]["text"] for e in events
                if e.get("type") == "item.completed"
                and e.get("item", {}).get("type") == "agent_message"]
    thread = next((e["thread_id"] for e in events if e.get("type") == "thread.started"), None)
    calls = sum(e.get("type") == "item.completed" and e.get("item", {}).get("type")
                in ("command_execution", "mcp_tool_call", "web_search") for e in events)
    return {"usage": {k: usage[k] for k in USAGE_KEYS}, "thread": thread,
            "answer": messages[-1] if messages else "", "tool_calls": calls}


def session_usage(turns):
    """Codex 0.154.0 emits cumulative thread usage, including on resume."""
    if not turns:
        raise ValueError("no measured turns")
    for previous, current in zip(turns, turns[1:]):
        if any(current["usage"][k] < previous["usage"][k] for k in USAGE_KEYS):
            raise ValueError("session token counters decreased; accounting semantics changed")
    return turns[-1]["usage"].copy()


def decision(trials, repeats):
    """Require the complete paired battery; cached input is a subset of input."""
    stats = {}
    for variant in VARIANTS:
        rows = [r for r in trials if r["variant"] == variant]
        valid = (len(rows) == repeats and {r["repeat"] for r in rows} == set(range(repeats))
                 and all(r.get("quality_pass") and r.get("usage")
                         and r.get("metrics_complete") for r in rows))
        stats[variant] = {"complete": valid}
        if valid:
            stats[variant].update(
                median_tokens=statistics.median(r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in rows),
                median_seconds=statistics.median(r["seconds"] for r in rows),
                median_cached_input=statistics.median(r["usage"]["cached_input_tokens"] for r in rows),
            )
    winners = []
    base = stats["baseline"]
    if repeats >= 3 and base["complete"] and base["median_tokens"] > 0 and base["median_seconds"] > 0:
        for name, stat in stats.items():
            if name == "baseline" or not stat["complete"]:
                continue
            stat["token_reduction"] = 1 - stat["median_tokens"] / base["median_tokens"]
            stat["time_ratio"] = stat["median_seconds"] / base["median_seconds"]
            if stat["token_reduction"] >= 0.15 and stat["time_ratio"] <= 1.10:
                winners.append(name)
    winner = min(winners, key=lambda n: (stats[n]["median_tokens"], stats[n]["median_seconds"])) if winners else None
    return {"variants": stats, "eligible_variant": winner,
            "personal_defaults_changed": False}


def run_process(command, cwd, timeout, stem, prompt=None):
    started = time.monotonic()
    with stem.with_suffix(".stdout").open("w") as stdout, stem.with_suffix(".stderr").open("w") as stderr:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                                   stdout=stdout, stderr=stderr, text=True, start_new_session=True)
        try:
            process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return 124, time.monotonic() - started
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
    return process.returncode, time.monotonic() - started


def workspace_snapshot(workspace):
    """Record all entries without following links or reading special files."""
    entries = {}
    pending = [workspace]
    while pending:
        path = pending.pop()
        mode = path.lstat().st_mode
        permissions = stat.S_IMODE(mode)
        name = path.relative_to(workspace).as_posix()
        if stat.S_ISLNK(mode):
            entries[name] = ("symlink", permissions, os.readlink(path))
        elif stat.S_ISDIR(mode):
            entries[name] = ("directory", permissions)
            pending.extend(path.iterdir())
        elif stat.S_ISREG(mode):
            entries[name] = ("file", permissions, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            entries[name] = ("special", mode)
    return entries


def scope_changes(before, after, *, allow_edit):
    unexpected = []
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if (allow_edit and name == "_efficiency_task/events.py" and old and new
                and old[0] == new[0] == "file" and old[1] == new[1]):
            continue
        # Only the tested module's normal Python bytecode is an allowed side effect.
        kind = None
        if name == "_efficiency_task/__pycache__":
            kind = "directory"
        elif re.fullmatch(r"_efficiency_task/__pycache__/events\.cpython-\d+(?:\.opt-[12])?\.pyc", name):
            kind = "file"
        if kind and all(value is None or value[0] == kind for value in (old, new)):
            continue
        unexpected.append(name)
    return unexpected


def check_scope(trial, workspace, baseline, stage, *, allow_edit):
    changed = scope_changes(baseline, workspace_snapshot(workspace), allow_edit=allow_edit)
    trial["scope_checks"].append({"stage": stage, "unexpected_paths": changed})
    if changed:
        trial["scope_pass"] = False
        raise ValueError(f"{stage} changed unauthorized paths: {', '.join(changed)}")


def fixtures(workspace):
    directory = workspace / "_efficiency_task"
    directory.mkdir()
    (directory / "events.py").write_text(
        'def summarize(records):\n    return {"count": len(records)}\n')
    lines = [f"test_fixture_{i:05d} ... ok" for i in range(6000)]
    for position, entry in ((37, "FAIL test_auth expected=200 actual=401"),
                            (2931, "FAIL test_retry expected=3 actual=4"),
                            (5989, "ERROR test_storage error=PermissionError")):
        lines.insert(position, entry)
    (directory / "suite.log").write_text("\n".join(lines) + "\n")


FIRST = """這是隔離的 ADE 效率試驗。只處理本訊息任務，不新增依賴或連外。
此試驗未提供 MCP 圖譜；結構查核請使用原始碼並在結果標示降級限制，不宣稱圖譜驗證。
本輪只讀：
1. 查 ade.lock.json 的 headroom version 與 defaults 啟用值。
2. 從 ade/hosts.py 和 ade/core.py 確認 Codex attach 的 config 相對路徑、export 檔名、MCP 頂層 namespace，分別使用 HOSTS 的 codex tuple 前三個原始字串，不加 workspace 前綴或 provider 後綴。附兩個檔案的行號證據。
3. 找出 _efficiency_task/suite.log 的全部 FAIL/ERROR，不省略 expected/actual 或 error。
跨輪保留決策：後續 summarize 函式只用標準函式庫、不修改輸入 records；空輸入回傳零筆，輸出 key 固定為 count、failed、error。
只輸出 JSON：headroom_version、headroom_enabled、config_target、export、namespace、evidence（path:line 字串陣列）、failures（每筆包含完整錯誤行的字串陣列）、limitation（說明未用圖譜）。
"""
SECOND = """實作上一輪決定的 summarize(records)，只改 _efficiency_task/events.py。
records 是包含 status 的 dict 陣列。count 計算所有筆數，failed 計算 status=failed，error 計算 status=error。
請自行執行包含空輸入及 mixed records 的檢查，保留跨輪約束。回報實際驗證結果。
"""
THIRD = """延續既有實作與先前全部約束：records 的元素可能缺少 status，仍計入 count，但不要計入 failed 或 error。
只改 _efficiency_task/events.py，實作後驗證缺少 status、空輸入、mixed records 和輸入未被修改。不得新增輸出 key。
"""


def check_answer(answer, workspace):
    value = json.loads(answer)
    expected = {"headroom_version": "0.36.5", "headroom_enabled": False,
                "config_target": ".codex/config.toml", "export": "codex.toml", "namespace": "mcp_servers"}
    checks = {k: type(value.get(k)) is type(v) and value.get(k) == v for k, v in expected.items()}
    checks["failures"] = sorted(value.get("failures", [])) == sorted([
        "FAIL test_auth expected=200 actual=401", "FAIL test_retry expected=3 actual=4",
        "ERROR test_storage error=PermissionError"])
    checks["fallback_disclosed"] = bool(value.get("limitation"))
    refs = value.get("evidence", [])
    # Validate a real line containing the relevant host/export evidence, not just a filename.
    for name, needle in (("ade/hosts.py", "codex"), ("ade/core.py", "codex.toml")):
        matches = []
        for ref in refs:
            if not isinstance(ref, str) or not ref.startswith(name + ":"):
                continue
            try:
                number = int(ref.rsplit(":", 1)[1])
                lines = (workspace / name).read_text().splitlines()
                matches.append(0 < number <= len(lines) and needle in lines[number - 1])
            except (ValueError, IndexError):
                matches.append(False)
        checks[name] = any(matches)
    return checks


def check_code(workspace, log):
    code = '''import ast, copy, importlib.util, pathlib, sys
tree = ast.parse(pathlib.Path("_efficiency_task/events.py").read_text())
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        assert node.level == 0 and node.module.split(".")[0] in sys.stdlib_module_names
spec = importlib.util.spec_from_file_location("subject", "_efficiency_task/events.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for records, expected in [([], {"count":0,"failed":0,"error":0}),
    ([{"status":"ok"},{"status":"failed"},{"status":"error"},{"status":"failed"}], {"count":4,"failed":2,"error":1}),
    ([{}, {"status":"error"}, {"status":"unknown"}], {"count":3,"failed":0,"error":1})]:
    original = copy.deepcopy(records)
    assert module.summarize(records) == expected
    assert records == original
print("quality checks passed")
'''
    return run_process(["python3", "-c", code], workspace, 10, log)[0] == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning", default="medium")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    if args.repeats < 1 or args.timeout < 1:
        parser.error("repeats and timeout must be positive")
    os.umask(0o077)
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error("output must be outside the repository; raw sessions are private")
    output.mkdir(parents=True, exist_ok=False)
    revision = subprocess.check_output(["git", "rev-parse", args.revision], cwd=ROOT, text=True).strip()
    archive = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
    overlays = {name: (ROOT / name).read_bytes() for name in OVERLAY}
    config_path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest() if config_path.exists() else None
    report = {"revision": revision, "model": args.model, "reasoning": args.reasoning,
              "codex_version": subprocess.check_output(["codex", "--version"], text=True).strip(),
              "config_sha256": config_hash, "repeats": args.repeats,
              "overlay_sha256": {p: hashlib.sha256(b).hexdigest() for p, b in overlays.items()},
              "scope": "repository instructions; inherited global instructions unchanged; MCP disabled in all arms",
              "trials": []}
    # MCP access is not needed for these disposable fixtures. Keep external services out of the experiment.
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    report["effective_config_sha256"] = config_fingerprint(config_path, output)
    common = ["-m", args.model, "-c", f'model_reasoning_effort="{args.reasoning}"']
    for name in config.get("mcp_servers", {}):
        common += ["-c", f"mcp_servers.{name}.enabled=false"]
    trial_workspaces = []
    try:
        for repeat in range(args.repeats):
            # Rotate arm order to reduce warm-cache/order bias; no concurrent timed runs.
            for variant in VARIANTS[repeat % 4:] + VARIANTS[:repeat % 4]:
                trial_dir = output / f"{repeat + 1}-{variant}"
                workspace = trial_dir / "workspace"
                workspace.mkdir(parents=True)
                if str(workspace) not in config.get("projects", {}):
                    trial_workspaces.append(workspace)
                with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                    tar.extractall(workspace, filter="data")
                if variant in ("instructions", "combined"):
                    for name, content in overlays.items():
                        target = workspace / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                fixtures(workspace)
                baseline = workspace_snapshot(workspace)
                options = common + ["-c", "features.context_management.experimental_mode=" +
                                     str(variant in ("context", "combined")).lower()]
                trial = {"variant": variant, "repeat": repeat, "turns": [], "quality_pass": False,
                         "metrics_complete": False, "seconds": 0, "scope_pass": True, "scope_checks": []}
                thread = None
                try:
                    for turn, prompt in enumerate((FIRST, SECOND, THIRD)):
                        if turn == 0:
                            command = ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--json"] + options + ["-"]
                        else:
                            command = ["codex", "exec", "resume", "--skip-git-repo-check", "--json"] + options + [thread, "-"]
                        stem = trial_dir / f"turn-{turn + 1}"
                        rc, seconds = run_process(command, workspace, args.timeout, stem, prompt)
                        trial["seconds"] += seconds
                        check_scope(trial, workspace, baseline, f"turn-{turn + 1}", allow_edit=turn > 0)
                        if rc:
                            raise ValueError(f"turn {turn + 1} exited {rc}; see private stderr")
                        parsed = events_result(stem.with_suffix(".stdout").read_text())
                        if turn == 0:
                            thread = parsed["thread"]
                            if not thread:
                                raise ValueError("missing thread id")
                            trial["lookup_checks"] = check_answer(parsed["answer"], workspace)
                        trial["turns"].append({k: v for k, v in parsed.items() if k not in ("answer", "thread")})
                    trial["code_pass"] = check_code(workspace, trial_dir / "quality")
                    check_scope(trial, workspace, baseline, "quality", allow_edit=True)
                    trial["quality_pass"] = all(trial["lookup_checks"].values()) and trial["code_pass"] and trial["scope_pass"]
                    trial["usage"] = session_usage(trial["turns"])
                    if config_fingerprint(config_path, output) != report["effective_config_sha256"]:
                        raise ValueError("effective personal configuration changed during the experiment")
                    trial["metrics_complete"] = True
                except (ValueError, OSError, KeyError, TypeError) as error:
                    trial["error"] = str(error)
                report["trials"].append(trial)
                report["decision"] = decision(report["trials"], args.repeats)
                (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps({"variant": variant, "repeat": repeat + 1,
                                  "quality_pass": trial["quality_pass"], "seconds": round(trial["seconds"], 2),
                                  "usage": trial.get("usage"), "error": trial.get("error")}), flush=True)
        print(json.dumps(report["decision"], ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        report["interrupted"] = True
        raise
    finally:
        report["decision"] = decision(report["trials"], args.repeats)
        try:
            report["trial_trust_entries_removed"] = remove_trial_trust(config_path, trial_workspaces)
        except (OSError, ValueError) as error:
            report["trial_trust_cleanup_error"] = str(error)
            raise
        finally:
            (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if all(t["metrics_complete"] for t in report["trials"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
