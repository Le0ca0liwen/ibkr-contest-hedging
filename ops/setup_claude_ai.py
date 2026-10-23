"""Masked, local-only Claude key entry. Never puts a key in shell history or the browser.

    .venv/bin/python ops/setup_claude_ai.py                 # asks for the key (hidden) and workspace
    .venv/bin/python ops/setup_claude_ai.py --key-stdin --workspace wrkspc_...   # key piped on stdin

Saves the key to .runtime/anthropic-api-key and the workspace ID (needed when the key is not scoped
to a workspace) to .runtime/anthropic-workspace-id, both readable by this user only, and selects
Claude Sonnet 5.5 (medium effort) as the app's AI model. Restart the app to use it.
"""

import argparse
import getpass
import json
import os
import sys
from pathlib import Path


def private_write(path, text):
    if path.is_symlink():
        raise ValueError(f"Refusing a symbolic-link file: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(text + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-stdin", action="store_true", help="Read the key from standard input")
    parser.add_argument("--workspace", help="Claude Console workspace ID (wrkspc_...)")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.key_stdin:
        key = sys.stdin.readline().strip()
    else:
        key = getpass.getpass("Anthropic API key (hidden; saved only on this Mac): ").strip()
    if not key or any(c.isspace() for c in key) or not key.startswith("sk-ant-"):
        raise ValueError("Enter an Anthropic API key (it starts with sk-ant-) without whitespace")
    workspace = args.workspace
    if workspace is None and not args.key_stdin:
        workspace = input("Workspace ID (wrkspc_..., Enter if the key is scoped to one): ").strip()
    workspace = (workspace or "").strip()
    if workspace and (not workspace.startswith("wrkspc_") or any(c.isspace() for c in workspace)):
        raise ValueError("A workspace ID starts with wrkspc_")
    folder = root / ".runtime"
    folder.mkdir(exist_ok=True)
    private_write(folder / "anthropic-api-key", key)
    if workspace:
        private_write(folder / "anthropic-workspace-id", workspace)
    settings = root / "config/local.ai.json"
    try:
        current = json.loads(settings.read_text())
    except (OSError, ValueError):
        current = {}
    settings.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"model": "claude-sonnet-5-5", "enabled": current.get("enabled", True)}) + "\n"
    )
    temporary.replace(settings)
    print(
        "Claude key saved privately"
        + (" with its workspace" if workspace else "")
        + "; AI model set to Claude Sonnet 5.5 (medium effort). Restart the app. No trading enabled."
    )


if __name__ == "__main__":
    main()
