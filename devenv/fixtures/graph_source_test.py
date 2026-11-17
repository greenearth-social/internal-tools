import io
import shlex
import subprocess

import graph_source
import pytest
from graph_source import GraphSourceError, SshGraphSource


@pytest.fixture(autouse=True)
def timers(monkeypatch):
    created = []

    class FakeTimer:
        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback
            self.daemon = False
            self.started = False
            self.cancelled = False
            created.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def fire(self):
            if not self.cancelled:
                self.callback()

    monkeypatch.setattr(graph_source, "Timer", FakeTimer)
    return created


class StreamingOutput(io.StringIO):
    def __init__(self, value):
        super().__init__(value)
        self.lines_read = 0

    def __next__(self):
        line = super().__next__()
        self.lines_read += 1
        return line

    def read(self, *args):
        raise AssertionError("stdout must be consumed one line at a time")


class FakeProcess:
    def __init__(self, output, exit_code=0, hangs_on_terminate=False):
        self.stdout = StreamingOutput(output)
        self.returncode = None
        self.exit_code = exit_code
        self.hangs_on_terminate = hangs_on_terminate
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if timeout is not None and self.hangs_on_terminate:
            raise subprocess.TimeoutExpired("ssh", timeout)
        self.waited = True
        self.returncode = self.exit_code
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def install_process(monkeypatch, process, stderr=b""):
    captured = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured["script"] = kwargs["stdin"].read()
        captured["stdin"] = kwargs["stdin"]
        captured["stderr"] = kwargs["stderr"]
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] != subprocess.PIPE
        kwargs["stderr"].write(stderr)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    return captured


def test_query_uses_noninteractive_ssh_and_checks_role_before_reading(monkeypatch, timers):
    process = FakeProcess('{"did":"did:plc:a"}\n')
    captured = install_process(monkeypatch, process)
    sql = "SELECT json_build_object('did', did) FROM graph.actor LIMIT 1;"

    assert list(SshGraphSource("reader@graph.example", "bridgeu", 12345).query(sql)) == [
        {"did": "did:plc:a"}
    ]

    assert captured["command"][:-1] == [
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
        "reader@graph.example",
    ]
    remote = shlex.split(captured["command"][-1])
    assert remote[:5] == ["sudo", "-n", "-u", "postgres", "env"]
    assert remote[5] == (
        "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=12345 "
        "-c lock_timeout=1000 -c idle_in_transaction_session_timeout=15000 "
        "-c application_name=dev-fixture-extractor -c timezone=UTC"
    )
    assert remote[6:] == [
        "psql",
        "-X",
        "-qAt",
        "--no-password",
        "--set=ON_ERROR_STOP=1",
        "--host=/var/run/postgresql",
        "--username=postgres",
        "--dbname=bridgeu",
        "-f",
        "-",
    ]
    script = captured["script"]
    assert script.startswith("BEGIN READ ONLY;\nSET LOCAL ROLE bridgeu_readonly;\n")
    assertion, query = script.split(sql)
    assert "current_user <> 'bridgeu_readonly'" in assertion
    assert "current_setting('transaction_read_only') <> 'on'" in assertion
    assert "RAISE EXCEPTION" in assertion
    assert query == "\n;\nCOMMIT;\n"
    assert process.waited and not process.terminated
    assert process.stdout.closed and captured["stdin"].closed and captured["stderr"].closed
    assert timers[0].interval == 42.345
    assert timers[0].started and timers[0].daemon and timers[0].cancelled
    timers[0].fire()
    assert not process.killed


def test_query_streams_without_waiting_for_all_rows(monkeypatch):
    process = FakeProcess('{"id":1}\n{"id":2}\n')
    install_process(monkeypatch, process)
    rows = SshGraphSource("graph.example", "bridgeu", 10000).query("SELECT ...")

    assert next(rows) == {"id": 1}
    assert process.stdout.lines_read == 1
    assert not process.waited
    assert list(rows) == [{"id": 2}]
    assert process.waited


@pytest.mark.parametrize("output", ["", '{"id":1}\n'])
def test_remote_failure_is_reported_even_after_partial_rows(monkeypatch, output):
    process = FakeProcess(output, exit_code=3)
    install_process(monkeypatch, process, stderr=b"psql: permission denied\n")
    rows = SshGraphSource("graph.example", "bridgeu", 10000).query("SELECT ...")
    if output:
        assert next(rows) == {"id": 1}

    with pytest.raises(GraphSourceError, match="exit 3.*permission denied"):
        list(rows)
    assert process.stdout.closed


@pytest.mark.parametrize("hangs", [False, True])
def test_closing_early_reaps_the_process_and_closes_files(monkeypatch, hangs, timers):
    process = FakeProcess('{"id":1}\n{"id":2}\n', hangs_on_terminate=hangs)
    captured = install_process(monkeypatch, process)
    rows = SshGraphSource("graph.example", "bridgeu", 10000).query("SELECT ...")
    next(rows)

    rows.close()

    assert process.terminated and process.waited
    assert process.killed == hangs
    assert process.stdout.closed and captured["stdin"].closed and captured["stderr"].closed
    assert timers[0].cancelled


@pytest.mark.parametrize("remaining_output", ["", '{"id":2}\n'])
def test_wall_deadline_kills_ssh_and_rejects_partial_results(monkeypatch, timers, remaining_output):
    process = FakeProcess('{"id":1}\n' + remaining_output, exit_code=-9)
    captured = install_process(monkeypatch, process)
    rows = SshGraphSource("graph.example", "bridgeu", 10000).query("SELECT ...")
    assert next(rows) == {"id": 1}

    timers[0].fire()

    assert process.killed
    with pytest.raises(GraphSourceError, match="timed out after 40 seconds"):
        list(rows)
    assert process.waited and timers[0].cancelled
    assert process.stdout.closed and captured["stdin"].closed and captured["stderr"].closed


def test_deadline_does_not_kill_an_already_finished_process(monkeypatch, timers):
    process = FakeProcess('{"id":1}\n')
    install_process(monkeypatch, process)
    rows = SshGraphSource("graph.example", "bridgeu", 10000).query("SELECT ...")
    assert next(rows) == {"id": 1}
    process.returncode = 0

    timers[0].fire()

    assert list(rows) == []
    assert not process.killed
    assert timers[0].cancelled


@pytest.mark.parametrize("output", ["not JSON\n", "[]\n", "null\n"])
def test_unexpected_stdout_fails_and_terminates_query(monkeypatch, output):
    process = FakeProcess(output)
    install_process(monkeypatch, process)

    with pytest.raises(GraphSourceError, match="JSON"):
        list(SshGraphSource("graph.example", "bridgeu", 10000).query("SELECT ..."))
    assert process.terminated and process.waited


def test_start_failure_is_reported(monkeypatch):
    def popen(*args, **kwargs):
        raise FileNotFoundError("ssh executable unavailable")

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(GraphSourceError, match="Could not start graph SSH query"):
        list(SshGraphSource("graph.example", "bridgeu", 10000).query("SELECT ..."))


@pytest.mark.parametrize("host", ["", "-oProxyCommand=bad", "graph example", "graph\nexample"])
def test_invalid_ssh_destination_is_rejected(host):
    with pytest.raises(ValueError, match="host"):
        SshGraphSource(host, "bridgeu", 10000)


@pytest.mark.parametrize(
    "database", ["", "host=elsewhere dbname=graph", "postgres://elsewhere/g", "g;id"]
)
def test_database_cannot_override_the_connection(database):
    with pytest.raises(ValueError, match="database"):
        SshGraphSource("graph.example", database, 10000)


@pytest.mark.parametrize("timeout", [0, -1])
def test_timeout_cannot_disable_the_query_deadline(timeout):
    with pytest.raises(ValueError, match="positive"):
        SshGraphSource("graph.example", "bridgeu", timeout)
