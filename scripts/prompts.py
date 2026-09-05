#!/usr/bin/env python3
"""Build local prompts or inspect upstream changes without adopting them."""

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def github(repo, endpoint, raw=False):
    command = ["gh", "api", f"repos/{repo}/{endpoint}"]
    if raw:
        command += ["-H", "Accept: application/vnd.github.raw+json"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return result.stdout if raw else json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "check", "upstream"])
    args = parser.parse_args()
    manifest = json.loads((ROOT / "prompt/sources.json").read_text(encoding="utf-8"))
    if args.action in ("build", "check"):
        # Read every input before writing either output.
        parts = [(ROOT / source["local"]).read_text(encoding="utf-8").rstrip()
                 for source in manifest["sources"]]
        expected = ("\n\n---\n\n".join(parts) + "\n").encode("utf-8")
        stale = []
        for output in manifest["outputs"]:
            path = ROOT / output
            if args.action == "build":
                path.write_bytes(expected)
            elif not path.exists() or path.read_bytes() != expected:
                stale.append(output)
        if stale:
            print("STALE: " + ", ".join(stale))
            return 1
        print("OK: " + ", ".join(manifest["outputs"]))
        return 0

    incomplete = False
    for source in manifest["sources"]:
        upstream = source.get("upstream")
        if upstream is None:
            print(f"LOCAL: {source['id']} ({source['origin']})")
            continue
        baseline = upstream.get("baseline")
        if baseline is None:
            print(f"UNKNOWN: {source['id']} has no upstream baseline")
            incomplete = True
            continue
        try:
            commit = github(upstream["repo"], f"commits/{upstream['branch']}")["sha"]
            for item in baseline["files"]:
                snapshot = (ROOT / item["snapshot"]).read_bytes()
                if hashlib.sha256(snapshot).hexdigest() != item["sha256"]:
                    raise ValueError(f"snapshot checksum mismatch: {item['snapshot']}")
                previous = snapshot.decode("utf-8")
                current = github(upstream["repo"],
                                 f"contents/{item['path']}?ref={commit}", raw=True)
                print(f"{source['id']}: {item['path']} {baseline['commit']} -> {commit}")
                if previous == current:
                    print("UNCHANGED")
                else:
                    print("".join(difflib.unified_diff(
                        previous.splitlines(keepends=True), current.splitlines(keepends=True),
                        fromfile=f"{baseline['commit']}/{item['path']}",
                        tofile=f"{commit}/{item['path']}")), end="")
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
            print(f"ERROR: {source['id']}: {detail}")
            incomplete = True
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
