"""Project-local PostgreSQL; no launch agent, root access, or system configuration."""

import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote

import psycopg

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / ".runtime/Postgres.app/Contents/Versions/17/bin"
PGDATA = ROOT / "data/postgres"
SOCKET = ROOT / "data/postgres-socket"
PORT = 55432


def start_database(database="hedge"):
    if not (BIN / "pg_ctl").exists():
        raise RuntimeError("Install the project-local PostgreSQL runtime or provide HEDGE_DATABASE_URL")
    password_file = ROOT / ".runtime/db-password"
    if not password_file.exists():
        password_file.write_text(secrets.token_hex(24))
        password_file.chmod(0o600)
    password = password_file.read_text().strip()
    PGDATA.parent.mkdir(exist_ok=True)
    SOCKET.mkdir(exist_ok=True)
    if not (PGDATA / "PG_VERSION").exists():
        subprocess.run(
            [
                str(BIN / "initdb"),
                "-D",
                str(PGDATA),
                "-U",
                "hedge",
                "--encoding=UTF8",
                "--locale=C",
                "--auth-host=scram-sha-256",
                "--auth-local=scram-sha-256",
                "--pwfile=" + str(password_file),
            ],
            check=True,
        )
    status = subprocess.run([str(BIN / "pg_ctl"), "-D", str(PGDATA), "status"], capture_output=True)
    if status.returncode:
        subprocess.run(
            [
                str(BIN / "pg_ctl"),
                "-D",
                str(PGDATA),
                "-l",
                str(PGDATA.parent / "postgres.log"),
                "-o",
                f'-h 127.0.0.1 -p {PORT} -k "{SOCKET}"',
                "-w",
                "start",
            ],
            check=True,
        )
    with psycopg.connect(
        host="127.0.0.1", port=PORT, dbname="postgres", user="hedge", password=password, autocommit=True
    ) as connection:
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            connection.execute(f'CREATE DATABASE "{database}"')
    url = f"postgresql+psycopg://hedge:{quote(password, safe='')}@127.0.0.1:{PORT}/{database}"
    env = os.environ.copy()
    env["HEDGE_DATABASE_URL"] = url
    subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True
    )
    return env


if __name__ == "__main__":
    start_database()
    print("Local PostgreSQL ready on 127.0.0.1:55432. Credentials remain in .runtime/db-password.")
