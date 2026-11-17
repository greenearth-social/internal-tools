"""Stream graph rows through an explicitly selected SSH/admin connection.

SQL comes from the extractor's fixed templates with encoded values, never
from a user-supplied query. In particular, callers must not interpolate raw
values that could introduce SQL statements or psql backslash commands.
"""

import json
import re
import shlex
import subprocess
import tempfile
from collections.abc import Generator
from threading import Event, Timer


class GraphSourceError(RuntimeError):
    """The remote graph query failed or returned an unexpected result."""


_TRANSACTION_START = """BEGIN READ ONLY;
SET LOCAL ROLE bridgeu_readonly;
DO $graph_source$
BEGIN
    IF current_user <> 'bridgeu_readonly'
       OR current_setting('transaction_read_only') <> 'on' THEN
        RAISE EXCEPTION 'Graph extraction requires bridgeu_readonly in a read-only transaction';
    END IF;
END;
$graph_source$;
"""


class SshGraphSource:
    def __init__(self, host: str, database: str, statement_timeout_ms: int):
        if not host or host.startswith("-") or any(char.isspace() for char in host):
            raise ValueError("host must be an SSH destination without whitespace or a leading dash")
        # psql also accepts connection strings as --dbname. Only allow a name,
        # so this argument cannot replace the local socket or postgres login.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
            raise ValueError("database must be a simple PostgreSQL database name")
        if statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be positive")
        self.host = host
        self.database = database
        self.statement_timeout_ms = statement_timeout_ms

    def query(self, sql: str) -> Generator[dict]:
        """Yield one JSON object per row from a trusted SELECT statement.

        Consume the iterator fully to check the remote exit status. Call
        ``close()`` (or use ``contextlib.closing``) when stopping early so the
        SSH process is terminated and reaped promptly.
        """
        options = (
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={self.statement_timeout_ms} "
            "-c lock_timeout=1000 -c idle_in_transaction_session_timeout=15000 "
            "-c application_name=dev-fixture-extractor -c timezone=UTC"
        )
        remote = shlex.join(
            [
                "sudo",
                "-n",
                "-u",
                "postgres",
                "env",
                f"PGOPTIONS={options}",
                "psql",
                "-X",
                "-qAt",
                "--no-password",
                "--set=ON_ERROR_STOP=1",
                "--host=/var/run/postgresql",
                "--username=postgres",
                f"--dbname={self.database}",
                "-f",
                "-",
            ]
        )
        command = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            self.host,
            remote,
        ]
        # Files avoid a blocked stdin write or a full stderr pipe while the
        # caller consumes stdout. Only stdout is streamed through a pipe.
        with (
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as script,
            tempfile.TemporaryFile() as errors,
        ):
            script.write(_TRANSACTION_START)
            script.write(sql)
            script.write("\n;\nCOMMIT;\n")
            script.seek(0)
            try:
                process = subprocess.Popen(
                    command,
                    stdin=script,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                    encoding="utf-8",
                )
            except OSError as exc:
                raise GraphSourceError(f"Could not start graph SSH query: {exc}") from exc
            timed_out = Event()
            deadline_seconds = self.statement_timeout_ms / 1000 + 30

            def expire() -> None:
                # Database and SSH keepalive timeouts cannot bound every kind
                # of network stall. Killing SSH also unblocks its stdout pipe.
                if process.poll() is None:
                    timed_out.set()
                    process.kill()

            def check_deadline() -> None:
                if timed_out.is_set():
                    raise GraphSourceError(
                        f"Graph query timed out after {deadline_seconds:g} seconds"
                    )

            deadline = Timer(deadline_seconds, expire)
            deadline.daemon = True
            deadline.start()
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    check_deadline()
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise GraphSourceError("Graph query returned invalid JSON") from exc
                    if not isinstance(row, dict):
                        raise GraphSourceError("Graph query must return one JSON object per row")
                    yield row
                returncode = process.wait()
                check_deadline()
                if returncode:
                    # Bound diagnostics even when a remote failure is noisy.
                    end = errors.seek(0, 2)
                    errors.seek(max(0, end - 8192))
                    detail = errors.read().decode("utf-8", errors="replace").strip()
                    raise GraphSourceError(f"Graph query failed (exit {returncode}): {detail}")
            finally:
                deadline.cancel()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if process.stdout is not None:
                    process.stdout.close()
