"""CLI: stdout is JSON except when forwarding a provider's protocol."""

import argparse
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import core, environment, environment_artifacts, hosts, publish as html_publish, scheduler, sync, trending


DEFAULT_WEB_ARTIFACT_BASE_URL = "http://172.16.240.41:80"
DEFAULT_WEB_ARTIFACT_ROOT = Path("/var/lib/ade/web-artifacts")


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def active(root):
    release = core.generation(root)
    core.check(release is not None, "run apply first")
    return release, core.read(release / "config.json")


def doctor(root, env_file=None):
    release, config = active(root)
    results = {}
    for name, enabled in config["providers"].items():
        if not enabled:
            results[name] = {"status": "disabled"}
            continue
        try:
            core.authorize(config, name)
            if name == sync.SYNC_PROVIDER:
                sync.PlaneLinearConfig.from_environment(sync_environment(config["manifests"][name], env_file))
                results[name] = {"status": "ready", "network": "not checked"}
                continue
            core.health(config["manifests"][name], release)
            results[name] = {"status": "ready"}
        except (core.Error, sync.SyncError, OSError, subprocess.SubprocessError) as exc:
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
    result = subprocess.run(["git", "-C", str(args.workspace), "merge-base", "--is-ancestor", *revisions], check=False)
    core.check(result.returncode == 0, "base must be an ancestor of head")
    env = core.clean_env(provider)
    core.check(args.key_env in os.environ, "missing credential environment variable: " + args.key_env)
    env.update(OCR_LLM_URL=config["llm_endpoint"], OCR_LLM_MODEL=args.model,
               OCR_LLM_TOKEN=os.environ[args.key_env], OCR_LLM_PROTOCOL=args.protocol)
    command = core.argv(provider, release) + ["review", "--from", revisions[0], "--to", revisions[1],
                                            "--format", "json"]
    return subprocess.call(command, cwd=args.workspace, env=env)


def sync_environment(provider, env_file=None):
    environment = dict(os.environ)
    if env_file:
        environment.update(sync.load_env_file(env_file))
    return core.clean_env(provider, environment)


def run_sync(root, provider_name, env_file=None):
    _release, config = active(root)
    provider = config["manifests"].get(provider_name)
    core.check(provider is not None, "unknown sync provider: " + provider_name)
    core.authorize(config, provider_name)
    result = sync.run_from_environment(root, sync_environment(provider, env_file))
    output(result)
    return 0 if result["status"] == "completed" else 1


def install_schedule(root, provider_name, env_file=None, enable=False):
    _release, config = active(root)
    provider = config["manifests"].get(provider_name)
    core.check(provider is not None, "unknown sync provider: " + provider_name)
    core.authorize(config, provider_name)
    if env_file:
        sync.load_env_file(env_file)
    times = scheduler.manifest_schedule(provider)
    return scheduler.install(root, platform.system().lower(), provider_name, times, env_file, sys.executable, enable=enable)


def publisher_ready(base_url=DEFAULT_WEB_ARTIFACT_BASE_URL):
    return html_publish.publisher_ready(base_url)


def publish_html(args):
    artifact_root = args.artifact_root or Path(
        os.environ.get("ADE_WEB_ARTIFACT_ROOT", DEFAULT_WEB_ARTIFACT_ROOT))
    if args.publish_operation == "delete":
        return html_publish.delete(args.name, artifact_root)
    base_url = args.base_url or os.environ.get("ADE_WEB_ARTIFACT_BASE_URL") or DEFAULT_WEB_ARTIFACT_BASE_URL
    try:
        html_publish.validate_base_url(base_url)
        publisher_ready(base_url)
        return html_publish.publish(args.source, args.name, artifact_root, base_url, args.entrypoint)
    except core.Error as exc:
        if str(exc).startswith("publish blocked:"):
            raise
        raise core.Error("publish blocked: " + str(exc)) from exc
    except OSError as exc:
        raise core.Error("publish blocked: " + str(exc)) from exc


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
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--env-file", type=Path)
    sub.add_parser("status")
    p = sub.add_parser("attach")
    p.add_argument("--host", choices=list(hosts.HOSTS), required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("provider")
    p.add_argument("name")
    p.add_argument("--shared", action="store_true")
    p = sub.add_parser("publish-html")
    publish_sub = p.add_subparsers(dest="publish_operation", required=True)
    publish_parser = publish_sub.add_parser("publish")
    publish_parser.add_argument("source", type=Path)
    publish_parser.add_argument("--name", required=True)
    publish_parser.add_argument("--entrypoint")
    publish_parser.add_argument("--artifact-root", type=Path)
    publish_parser.add_argument("--base-url")
    delete_parser = publish_sub.add_parser("delete")
    delete_parser.add_argument("name")
    delete_parser.add_argument("--artifact-root", type=Path)
    p = sub.add_parser("review")
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    p.add_argument("--base", required=True)
    p.add_argument("--head", default="HEAD")
    p.add_argument("--model", required=True)
    p.add_argument("--protocol", choices=["anthropic", "openai", "openai-responses"], default="openai")
    p.add_argument("--key-env", default="OCR_LLM_TOKEN")
    p = sub.add_parser("sync")
    p.add_argument("--provider", choices=[sync.SYNC_PROVIDER], default=sync.SYNC_PROVIDER)
    p.add_argument("--env-file", type=Path)
    p = sub.add_parser("schedule")
    schedule = p.add_subparsers(dest="schedule_operation", required=True)
    install_parser = schedule.add_parser("install")
    install_parser.add_argument("--provider", choices=[sync.SYNC_PROVIDER], default=sync.SYNC_PROVIDER)
    install_parser.add_argument("--env-file", type=Path)
    install_parser.add_argument("--enable", action="store_true")
    env_parser = sub.add_parser("environment")
    env_sub = env_parser.add_subparsers(dest="environment_operation", required=True)
    env_service = env_sub.add_parser("service")
    env_service.add_argument("--config", type=Path, default=environment.DEFAULT_CONFIG_PATH)
    env_service.add_argument("--host")
    env_service.add_argument("--port", type=int)
    env_service.add_argument("--tls-cert", type=Path)
    env_service.add_argument("--tls-key", type=Path)
    env_update = env_sub.add_parser("update")
    env_update.add_argument("--config", type=Path, default=environment.DEFAULT_CONFIG_PATH)
    env_update.add_argument("--trigger", choices=("scheduled", "manual"), default="scheduled")
    env_update.add_argument("--run-id")
    env_update.add_argument("--requested-by", default="system")
    env_update.add_argument("--component", action="append", dest="components")
    env_status = env_sub.add_parser("status")
    env_status.add_argument("--config", type=Path, default=environment.DEFAULT_CONFIG_PATH)
    env_enroll = env_sub.add_parser("enroll")
    env_enroll.add_argument("--config", type=Path, default=environment.DEFAULT_CONFIG_PATH)
    env_enroll.add_argument("--address", required=True)
    env_enroll.add_argument("--role", choices=environment.ROLES, default="admin")
    env_enroll_signed = env_sub.add_parser("enroll-signed")
    env_enroll_signed.add_argument("--config", type=Path, default=environment.DEFAULT_CONFIG_PATH)
    env_enroll_signed.add_argument("--request-json", required=True)
    env_authorize = env_sub.add_parser("authorize")
    env_authorize.add_argument("--config", type=Path, default=environment.DEFAULT_CONFIG_PATH)
    env_trending = env_sub.add_parser("trending")
    env_trending.add_argument("--config", type=Path, default=environment.DEFAULT_CONFIG_PATH)
    env_trending.add_argument("--source", choices=("github", "trendshift"), default="github")
    env_trending.add_argument("--since", choices=("daily", "weekly", "monthly"), default="daily")
    env_artifacts = env_sub.add_parser("artifacts")
    env_artifacts.add_argument("--config", type=Path, default=environment.DEFAULT_CONFIG_PATH)
    env_artifacts.add_argument("artifact_operation", choices=("list", "upload", "delete"))
    env_artifacts.add_argument("--name")
    env_fail_run = env_sub.add_parser("fail-run")
    env_fail_run.add_argument("--config", type=Path, default=environment.DEFAULT_CONFIG_PATH)
    env_fail_run.add_argument("--run-id", required=True)
    env_fail_run.add_argument("--error", required=True)
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
        elif args.operation == "publish-html":
            output(publish_html(args))
        elif args.operation == "attach":
            output(hosts.attach(args.root, args.workspace, args.host, args.apply))
        elif args.operation == "doctor":
            results = doctor(args.root, args.env_file)
            output(results)
            return int(any(r["status"] == "blocked" for r in results.values()))
        elif args.operation == "sync":
            return run_sync(args.root, args.provider, args.env_file)
        elif args.operation == "schedule":
            if args.schedule_operation == "install":
                output(install_schedule(args.root, args.provider, args.env_file, args.enable))
                return 0
            raise core.Error("unknown schedule operation: " + args.schedule_operation)
        elif args.operation == "environment":
            config = environment.EnvironmentConfig.load(args.config)
            if args.environment_operation == "authorize":
                from .environment_auth import EnvironmentAuthority

                if os.geteuid() != 0 or config.path.stat().st_uid != 0 or config.path.stat().st_mode & 0o022:
                    raise core.Error("environment authorization requires root and a root-owned config")
                try:
                    raw_request = sys.stdin.read(65537)
                    if not raw_request or len(raw_request) > 65536:
                        raise environment.EnvironmentError("authorization request is empty or too large")
                    request = json.loads(raw_request)
                    output(EnvironmentAuthority(config).dispatch(request))
                except (environment.EnvironmentError, json.JSONDecodeError) as exc:
                    output({"error": str(exc)})
                return 0
            if args.environment_operation == "service":
                if args.host:
                    config = environment.EnvironmentConfig(
                        **{**config.__dict__, "listen_host": args.host}
                    )
                if args.port:
                    config = environment.EnvironmentConfig(
                        **{**config.__dict__, "listen_port": args.port}
                    )
                if args.tls_cert or args.tls_key:
                    config = environment.EnvironmentConfig(
                        **{
                            **config.__dict__,
                            "tls_cert": args.tls_cert or config.tls_cert,
                            "tls_key": args.tls_key or config.tls_key,
                        }
                    )
                environment.EnvironmentHTTPServer(config).serve_forever()
                return 0
            store = environment.RunStore(config.state_root)
            if args.environment_operation == "status":
                output({
                    "config": config.public_config(),
                    "health": environment.environment_health(config),
                    "runs": store.list(),
                    "policy": environment._policy_public(environment.read_environment_policy(config), config),
                })
                return 0
            if args.environment_operation == "trending":
                output(trending.TrendingService().get(args.source, args.since))
                return 0
            if args.environment_operation == "artifacts":
                try:
                    if args.artifact_operation == "list":
                        output(environment_artifacts.listing(config.artifacts))
                        return 0
                    if args.artifact_operation == "upload":
                        raw_request = sys.stdin.buffer.read(environment_artifacts.MAX_REQUEST_BYTES + 1)
                        if not raw_request or len(raw_request) > environment_artifacts.MAX_REQUEST_BYTES:
                            raise core.Error("artifact request is empty or too large")
                        try:
                            payload = json.loads(raw_request.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise core.Error("artifact request must be valid JSON") from exc
                        if not isinstance(payload, dict):
                            raise core.Error("artifact request must be a JSON object")
                        output(environment_artifacts.upload(config.artifacts, payload))
                        return 0
                    if not args.name:
                        raise core.Error("artifact name is required")
                    output(environment_artifacts.remove(config.artifacts, args.name))
                    return 0
                except core.Error as exc:
                    message = str(exc)
                    status_code = 503 if args.artifact_operation == "list" or message.startswith("publish blocked:") else 400
                    if args.artifact_operation == "delete" and message.startswith("artifact not found:"):
                        status_code = 404
                    output({"error": message, "status_code": status_code})
                    return 1
                except OSError:
                    output({"error": "web artifact storage is unavailable", "status_code": 503})
                    return 1
            if args.environment_operation == "fail-run":
                run = store.update(
                    args.run_id,
                    status="failed",
                    finished_at=environment.iso_now(),
                    error=environment._redact(args.error),
                )
                output(run)
                return 0
            if args.environment_operation == "enroll":
                if os.geteuid() != 0:
                    raise core.Error("environment wallet enrollment must run as root")
                raw = environment._read_json(config.path, None)
                if not isinstance(raw, dict):
                    raise core.Error("environment config must be a JSON object")
                args.address = environment.normalize_wallet_address(args.address)
                wallets = raw.setdefault("wallets", {}).setdefault("authorized", [])
                if not isinstance(wallets, list):
                    raise core.Error("wallets.authorized must be a list")
                wallets = [item for item in wallets if not isinstance(item, dict) or str(item.get("address", "")).lower() != args.address]
                wallets.append({"address": args.address, "role": args.role})
                raw["wallets"]["authorized"] = wallets
                mode = config.path.stat().st_mode & 0o777
                owner = config.path.stat()
                environment._write_json(config.path, raw, mode or 0o640)
                os.chown(config.path, owner.st_uid, owner.st_gid)
                output({"address": args.address, "role": args.role, "config": str(config.path)})
                return 0
            if args.environment_operation == "enroll-signed":
                if os.geteuid() != 0:
                    raise core.Error("signed wallet enrollment must run as root")
                try:
                    request = json.loads(args.request_json)
                except json.JSONDecodeError as exc:
                    raise core.Error("signed enrollment request must be valid JSON") from exc
                if not isinstance(request, dict) or set(request) != {"address", "message", "signature"}:
                    raise core.Error("signed enrollment request fields are invalid")
                if not all(isinstance(request.get(key), str) for key in ("address", "message", "signature")):
                    raise core.Error("signed enrollment request values are invalid")
                output(environment.enroll_signed_wallet(
                    config,
                    request["address"],
                    request["message"],
                    request["signature"],
                ))
                return 0
            if args.environment_operation == "update":
                coordinator = environment.UpdateCoordinator(config, store)
                run = store.get(args.run_id) if args.run_id else coordinator.create_run(
                    args.trigger, args.components, args.requested_by
                )
                if run is None:
                    raise core.Error("update run not found: " + args.run_id)
                result = coordinator.execute(run["id"], expected_trigger=args.trigger)
                output(result)
                return 0 if result["status"] in {"completed", "blocked"} else 1
            raise core.Error("unknown environment operation: " + args.environment_operation)
        else:
            release, config = active(args.root)
            if args.operation == "review":
                return review(args, release, config)
            provider = core.authorize(config, args.name)
            core.health(provider, release)
            if args.shared:
                return supervise(provider, release)
            if provider.get("builtin"):
                return run_sync(args.root, args.name)
            core.check(provider["lifecycle"] == "host-spawned", "use --shared for ADE-owned services")
            core.check(provider["transport"] != "cli", "use the typed review command for OCR")
            command = core.argv(provider, release)
            os.execvpe(command[0], command, core.clean_env(provider))
    except (core.Error, environment.EnvironmentError, sync.SyncError, scheduler.ScheduleError, OSError, subprocess.SubprocessError,
            KeyError, TypeError, json.JSONDecodeError) as exc:
        print("ade: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
