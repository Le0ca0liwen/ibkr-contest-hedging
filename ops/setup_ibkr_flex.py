"""Masked, local-only IBKR Flex Web Service entry, so the app can fetch the daily Activity Statement
(the contest's scored NAV) itself. Never puts the token in shell history or the browser.

    .venv/bin/python ops/setup_ibkr_flex.py                                    # asks (token hidden)
    .venv/bin/python ops/setup_ibkr_flex.py --token-stdin --query-id 123456    # token piped on stdin

Saves the token and Activity Flex Query ID to .runtime/ibkr-flex.json, readable by this user only.
Makes no network call.
"""

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

GUIDE = """Where to find them: Client Portal (log in to the PAPER account) > Performance & Reports
> Flex Queries.
  Token: the Flex Web Service Configuration gear. Turn the service on and generate a token; set its
    expiry to 1 year if allowed (if it offers an IP restriction, leave it empty).
  Query: create an Activity Flex Query with the sections Net Asset Value (NAV) in Base, Change in NAV
    and Open Positions (Summary); Format XML; Period Last Business Day; keep the Date Format at
    its default yyyyMMdd. Save it.
  Query ID: under the query's info (i) icon."""


def private_write(path, text):
    if path.is_symlink():
        raise ValueError(f"Refusing a symbolic-link file: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(text + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(epilog=GUIDE, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token-stdin", action="store_true", help="Read the Flex token from standard input")
    parser.add_argument("--query-id", help="Activity Flex Query ID (digits)")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.token_stdin:
        token = sys.stdin.readline().strip()
    else:
        print(GUIDE + "\n")
        token = getpass.getpass("Flex Web Service token (hidden; saved only on this Mac): ").strip()
    if not re.fullmatch(r"[0-9]{10,}", token):
        raise ValueError("A Flex Web Service token is 10 or more digits")
    query_id = args.query_id
    if query_id is None:
        try:
            query_id = input("Activity Flex Query ID: ")
        except EOFError:
            query_id = ""
    query_id = query_id.strip()
    if not re.fullmatch(r"[0-9]+", query_id):
        raise ValueError("A Flex Query ID is digits only")
    folder = root / ".runtime"
    folder.mkdir(exist_ok=True)
    private_write(folder / "ibkr-flex.json", json.dumps({"token": token, "query_id": query_id}))
    print(
        "IBKR Flex token and query ID saved privately to .runtime/ibkr-flex.json. "
        "No network call made; no trading enabled."
    )


if __name__ == "__main__":
    main()
