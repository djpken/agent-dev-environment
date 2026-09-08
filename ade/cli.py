"""CLI: stdout is JSON except when forwarding a provider's protocol."""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

from . import core
from . import hosts


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def active(root):
    release = core.generation(root)
    core.check(release is not None, "run apply first")
    return release, core.read(release / "config.json")


def doctor(root):
    release, config = active(root)
    results = {}
    for name, enabled in config["providers"].items():
        if not enabled:
            results[name] = {"status": "disabled"}
            continue
        try:
            core.authorize(config, name)
            core.health(config["manifests"][name], release)
            results[name] = {"status": "ready"}
        except (core.Error, OSError, subprocess.SubprocessError) as exc:
            results[name] = {"status": "blocked", "reason": str(exc)}
    return results


def supervise(provider, release):
    """Foreground owner: no stale PID files; always reap the process we spawned."""
    core.check(provider["lifecycle"] == "shared-local", "provider is not shared-local")
    # Refuse to claim an already-running endpoint.
    url = urlparse(provider["url"])
    try:
        socket.create_connection((url.hostname, url.port), timeout=2).close()
    except OSError:
        pass
    else:
        raise core.Error("shared endpoint is already occupied")
    child = subprocess.Popen(core.argv(provider, release), env=core.clean_env(provider),
                             start_new_session=True)
    previous = signal.signal(signal.SIGTERM, lambda *_: child.terminate())
    try:
        deadline = time.monotonic() + 20
        while child.poll() is None and time.monotonic() < deadline:
            try:
                urllib.request.urlopen(provider["url"], timeout=1).close()
                print("shared provider ready: " + provider["id"], file=sys.stderr)
                return child.wait()
            except OSError:
                time.sleep(0.1)
        raise core.Error("shared provider failed readiness check")
    finally:
        signal.signal(signal.SIGTERM, previous)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def review(args, release, config):
    provider = core.authorize(config, "ocr")
    core.health(provider, release)
    core.check(args.model, "review requires --model")
    revisions = []
    for ref in (args.base, args.head):
        result = subprocess.run(["git", "-C", str(args.workspace), "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
                                capture_output=True, text=True, check=True)
        revisions.append(result.stdout.strip())
    result = subprocess.run(["git", "-C", str(args.workspace), "merge-base", "--is-ancestor", *revisions])
    core.check(result.returncode == 0, "base must be an ancestor of head")
    env = core.clean_env(provider)
    core.check(args.key_env in os.environ, "missing credential environment variable: " + args.key_env)
    env.update(OCR_LLM_URL=config["llm_endpoint"], OCR_LLM_MODEL=args.model,
               OCR_LLM_TOKEN=os.environ[args.key_env], OCR_LLM_PROTOCOL=args.protocol)
    command = core.argv(provider, release) + ["review", "--from", revisions[0], "--to", revisions[1],
                                            "--format", "json"]
    return subprocess.call(command, cwd=args.workspace, env=env)


def main():
    parser = argparse.ArgumentParser(description="ADE composition manager")
    parser.add_argument("--root", type=Path, default=Path.home() / ".local/share/ade")
    sub = parser.add_subparsers(dest="operation", required=True)
    for command in ("plan", "apply"):
        p = sub.add_parser(command)
        p.add_argument("--lock", type=Path, default=Path("ade.lock.json"))
        p.add_argument("--user", type=Path)
        p.add_argument("--workspace-config", type=Path)
        p.add_argument("--workflow-repo", type=Path)
        if command == "apply":
            p.add_argument("--plan-id", required=True)
    sub.add_parser("rollback")
    sub.add_parser("doctor")
    sub.add_parser("status")
    p = sub.add_parser("attach")
    p.add_argument("--host", choices=list(hosts.HOSTS), required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("provider")
    p.add_argument("name")
    p.add_argument("--shared", action="store_true")
    p = sub.add_parser("review")
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    p.add_argument("--base", required=True)
    p.add_argument("--head", default="HEAD")
    p.add_argument("--model", required=True)
    p.add_argument("--protocol", choices=["anthropic", "openai", "openai-responses"], default="openai")
    p.add_argument("--key-env", default="OCR_LLM_TOKEN")
    args = parser.parse_args()
    try:
        if args.operation in ("plan", "apply"):
            lock = core.read(args.lock)
            config = core.compose(lock, core.read(args.user) if args.user else {},
                                  core.read(args.workspace_config) if args.workspace_config else {})
            repository = args.workflow_repo or args.lock.resolve().parent
            if args.operation == "plan":
                output(core.plan(lock, config, args.root, repository))
            else:
                output(core.install(lock, config, args.root, repository, args.plan_id))
        elif args.operation == "rollback":
            output(core.rollback(args.root))
        elif args.operation == "status":
            output({"generation": str(core.generation(args.root))})
        elif args.operation == "attach":
            output(hosts.attach(args.root, args.workspace, args.host, args.apply))
        elif args.operation == "doctor":
            results = doctor(args.root)
            output(results)
            return int(any(r["status"] == "blocked" for r in results.values()))
        else:
            release, config = active(args.root)
            if args.operation == "review":
                return review(args, release, config)
            provider = core.authorize(config, args.name)
            core.health(provider, release)
            if args.shared:
                return supervise(provider, release)
            core.check(provider["lifecycle"] == "host-spawned", "use --shared for ADE-owned services")
            core.check(provider["transport"] != "cli", "use the typed review command for OCR")
            command = core.argv(provider, release)
            os.execvpe(command[0], command, core.clean_env(provider))
    except (core.Error, OSError, subprocess.SubprocessError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print("ade: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
