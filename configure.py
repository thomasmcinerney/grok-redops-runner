#!/usr/bin/env python3
"""Connect this Grok project to an existing local RedOps installation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redops-dir", required=True, type=Path)
    args = parser.parse_args()

    redops = args.redops_dir.expanduser().resolve()
    python = redops / "venv" / "bin" / "python"
    server = redops / "redops" / "mcp_server.py"
    for path in (python, server):
        if not path.is_file():
            parser.error(f"missing RedOps component: {path}")

    config = Path(__file__).resolve().parent / ".grok" / "config.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        "[mcp_servers.redops]\n"
        f"command = {json.dumps(str(python))}\n"
        f"args = [{json.dumps(str(server))}]\n"
        "startup_timeout_sec = 30\n"
        "tool_timeout_sec = 1900\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    print(f"Wrote {config}")


if __name__ == "__main__":
    main()
