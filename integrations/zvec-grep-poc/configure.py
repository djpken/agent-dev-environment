#!/usr/bin/env python3
"""Write an ignored, machine-specific ADE user config for the zvec-grep POC."""

import json
import os
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ZG = HERE / "node_modules" / ".bin" / "zg"
CONFIG = ROOT / ".ade" / "zvec-grep-poc-user.json"
PERMISSIONS = [
    "workspace-read",
    "workspace-write",
    "local-state",
    "external-network",
    "listen-loopback",
]


def main():
    if not ZG.is_file():
        raise SystemExit("zvec-grep is not installed; run npm ci in integrations/zvec-grep-poc")
    if CONFIG.exists():
        raise SystemExit(f"refusing to overwrite existing config: {CONFIG}")

    manifest = {
        "id": "zvec-grep",
        "version": "0.2.2",
        "capabilities": ["workspace-retrieval"],
        "lifecycle": "shared-local",
        "transport": "http",
        "command": [str(ZG), "server", "run"],
        "health": [str(ZG), "version"],
        "url": "http://127.0.0.1:7999/mcp",
        "permissions": PERMISSIONS,
    }
    config = {
        "providers": {
            "ocr": False,
            "headroom": False,
            "plane-linear-sync": False,
            "zvec-grep": True,
        },
        "grants": {"zvec-grep": PERMISSIONS},
        "extensions": [manifest],
    }

    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    os.chmod(CONFIG, 0o600)
    print(CONFIG)


if __name__ == "__main__":
    main()
