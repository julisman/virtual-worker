"""The SSRF guard (agent/tools._is_private_host) must fail CLOSED — an
unparseable/empty/unresolvable host is blocked — and custom_tools must reuse the
same guard so the two can't drift. DNS is monkeypatched so tests never hit the
network."""

import socket

import pytest

from agent.tools import _is_private_host
from agent.custom_tools import _is_blocked_host


def _fake_addrinfo(ip: str):
    def _f(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
    return _f


def test_localhost_blocked():
    assert _is_private_host("http://localhost/x") is True
    assert _is_private_host("http://localhost.localdomain/") is True


def test_empty_and_garbage_fail_closed():
    assert _is_private_host("") is True          # no host -> blocked
    assert _is_private_host("not a url") is True  # unparseable -> blocked


def test_unresolvable_host_fails_closed(monkeypatch):
    def _boom(*a, **k):
        raise OSError("name resolution failed")
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    # The old guard returned False here (fail open); it must now be blocked.
    assert _is_private_host("http://totally-bogus.invalid/") is True


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254",
                                "192.168.1.1", "::1", "0.0.0.0"])
def test_private_and_reserved_ips_blocked(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo(ip))
    assert _is_private_host(f"http://host.example/") is True


def test_public_ip_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("93.184.216.34"))
    assert _is_private_host("http://example.com/") is False


def test_custom_tools_guard_delegates(monkeypatch):
    # Same input -> same verdict as the canonical guard (no drift).
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("10.1.2.3"))
    assert _is_blocked_host("http://internal/") is True
    monkeypatch.setattr(socket, "getaddrinfo", _fake_addrinfo("8.8.8.8"))
    assert _is_blocked_host("http://public/") is False
