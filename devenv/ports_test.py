"""Tests for per-instance port allocation.

The property that matters most here isn't any single number — it's that two
instances can never be handed the same port, including across *different*
services. Base ports were chosen for readability, not spacing, so the gap
between two of them being a multiple of STRIDE would silently give one
instance's Auth emulator another instance's Emulator UI. test_no_cross_service_collisions
is the guard on that.
"""

import socket

import ports
import pytest


@pytest.fixture
def devenv(tmp_path, monkeypatch):
    """A scratch devenv directory where every port reads as free.

    Allocation asks the operating system whether a port can be bound, which
    would otherwise make these tests depend on what happens to be running on
    the developer's machine — including a real dev environment, which occupies
    exactly the ports being tested. test_port_free_reports_a_bound_port covers
    the real check.
    """
    monkeypatch.setattr(ports, "port_free", lambda port: port not in BUSY)
    BUSY.clear()
    return tmp_path


# Ports the fake `port_free` should report as taken; tests add to it.
BUSY: set[int] = set()


def test_default_instance_gets_no_offset(devenv):
    offset, allocated = ports.allocate(devenv, "dev", ports.BASE_PORTS)
    assert offset == 0
    assert allocated == ports.BASE_PORTS
    # Nothing written: the default instance's ports are the documented ones,
    # so there is no allocation to remember.
    assert not (devenv / ".runtime" / "ports.json").exists()


def test_named_instance_gets_an_offset_and_remembers_it(devenv):
    offset, allocated = ports.allocate(devenv, "alpha", ports.BASE_PORTS)
    assert offset == ports.STRIDE
    assert allocated["API"] == ports.BASE_PORTS["API"] + ports.STRIDE
    assert (devenv / ".runtime.alpha" / "ports.json").exists()

    # Second call is a lookup, not a new allocation.
    assert ports.allocate(devenv, "alpha", ports.BASE_PORTS) == (offset, allocated)


def test_second_instance_gets_a_different_block(devenv):
    first, _ = ports.allocate(devenv, "alpha", ports.BASE_PORTS)
    second, _ = ports.allocate(devenv, "beta", ports.BASE_PORTS)
    assert first != second


def test_running_instance_keeps_its_ports(devenv):
    """A held port must not read as a conflict for the instance holding it.

    This is the case that makes re-checking on every call wrong: an instance
    that is up is listening on the ports it was given.
    """
    offset, allocated = ports.allocate(devenv, "alpha", ports.BASE_PORTS)
    BUSY.update(allocated.values())
    assert ports.allocate(devenv, "alpha", ports.BASE_PORTS) == (offset, allocated)


def test_occupied_block_is_skipped(devenv):
    """Something unrelated on one port pushes the whole block along."""
    BUSY.add(ports.BASE_PORTS["FRONTEND"] + ports.STRIDE)
    offset, _ = ports.allocate(devenv, "alpha", ports.BASE_PORTS)
    assert offset == ports.STRIDE * 2


def test_port_free_reports_a_bound_port(tmp_path):
    """The real check, unmocked, against a socket we actually hold."""
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    try:
        assert not ports.port_free(held.getsockname()[1])
    finally:
        held.close()


def test_exhaustion_is_an_error_naming_the_fix(devenv):
    for n in range(ports.MAX_OFFSET // ports.STRIDE):
        ports.allocate(devenv, f"i{n}", ports.BASE_PORTS)
    with pytest.raises(SystemExit) as excinfo:
        ports.allocate(devenv, "one-too-many", ports.BASE_PORTS)
    assert "devctl nuke" in str(excinfo.value)


def test_no_cross_service_collisions():
    """No offset can map one service onto another service's port.

    Checked across every allocatable offset *pair*, which is the case a single
    instance can't reveal: instance A's Firestore landing on instance B's api.
    """
    offsets = [0, *range(ports.STRIDE, ports.MAX_OFFSET + 1, ports.STRIDE)]
    seen: dict[int, tuple[int, str]] = {}
    for offset in offsets:
        for name, port in ports.ports_for(offset, ports.BASE_PORTS).items():
            assert port not in seen, (
                f"offset {offset} {name} collides with "
                f"offset {seen[port][0]} {seen[port][1]} on port {port}"
            )
            seen[port] = (offset, name)


def test_local_overrides_shift_the_whole_series(devenv):
    bases = ports.resolve_bases({"GE_DEV_PORT_API": "9500"})
    assert bases["API"] == 9500
    assert bases["ES"] == ports.BASE_PORTS["ES"]
    _, allocated = ports.allocate(devenv, "alpha", bases)
    assert allocated["API"] == 9500 + ports.STRIDE


def test_list_instances_reports_names_and_offsets(devenv):
    (devenv / ".runtime").mkdir()
    ports.allocate(devenv, "alpha", ports.BASE_PORTS)
    assert ports.list_instances(devenv) == [("dev", 0), ("alpha", ports.STRIDE)]


def test_read_only_refuses_an_unknown_instance(devenv):
    with pytest.raises(SystemExit) as excinfo:
        ports.read_only(devenv, "typo", ports.BASE_PORTS)
    assert "no such instance" in str(excinfo.value)
    # And it allocated nothing on the way out.
    assert not (devenv / ".runtime.typo").exists()


def test_read_only_returns_base_ports_for_the_default_instance(devenv):
    assert ports.read_only(devenv, "dev", ports.BASE_PORTS) == (0, ports.BASE_PORTS)


def test_unreadable_port_record_does_not_crash_listing(devenv):
    runtime = devenv / ".runtime.broken"
    runtime.mkdir()
    (runtime / "ports.json").write_text("{ truncated")
    assert ports.list_instances(devenv) == [("broken", 0)]
