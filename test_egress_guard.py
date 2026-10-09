"""Self-tests for the process-level egress guard.

These tests deliberately trigger blocked egress, so every deny probe is wrapped
in :func:`egress_guard.expecting_blocks` -- otherwise the guard would (correctly)
treat the probe as an unexpected egress attempt and fail the run.

Safe to run standalone without the guard pre-installed: the guard is installed
by ``setUpModule``.
"""

import socket
import unittest

import egress_guard
from egress_guard import EgressBlocked


def setUpModule():
    egress_guard.install()


class LoopbackClassificationTests(unittest.TestCase):
    """Pure classification logic -- no sockets involved."""

    def test_loopback_hosts_are_permitted(self):
        for host in (
            None,
            "",
            "127.0.0.1",
            "127.0.0.53",
            "::1",
            "0:0:0:0:0:0:0:1",
            "::ffff:127.0.0.1",
            "localhost",
            "LOCALHOST",
            "ip6-localhost",
            "0.0.0.0",
            "::",
        ):
            with self.subTest(host=host):
                self.assertTrue(egress_guard.is_loopback(host))

    def test_non_loopback_hosts_are_rejected(self):
        for host in (
            "example.com",
            "1.1.1.1",
            "8.8.8.8",
            "192.0.2.1",
            "2001:4860:4860::8888",
            "api.telegram.org",
            "not-an-ip",
            1234,
        ):
            with self.subTest(host=host):
                self.assertFalse(egress_guard.is_loopback(host))


class EgressDenyTests(unittest.TestCase):
    """The guard must reject everything that is not loopback."""

    def setUp(self):
        self.guard = egress_guard.install()

    def test_external_dns_is_rejected(self):
        with egress_guard.expecting_blocks():
            with self.assertRaises(EgressBlocked) as ctx:
                socket.getaddrinfo("example.com", 80)
        self.assertIn("getaddrinfo", str(ctx.exception))

    def test_external_tcp_connect_is_rejected(self):
        with egress_guard.expecting_blocks():
            with self.assertRaises(EgressBlocked) as ctx:
                socket.create_connection(("192.0.2.1", 80), timeout=3)
        # create_connection resolves the name first, so the guard normally
        # rejects at getaddrinfo; accept connect as well in case a platform
        # resolves numeric literals without calling getaddrinfo.
        self.assertRegex(str(ctx.exception), r"(getaddrinfo|connect)")
        self.assertIn("192.0.2.1", str(ctx.exception))

    def test_external_connect_ex_is_rejected(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with egress_guard.expecting_blocks():
                with self.assertRaises(EgressBlocked):
                    sock.connect_ex(("192.0.2.1", 80))
        finally:
            sock.close()

    def test_external_udp_sendto_is_rejected(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with egress_guard.expecting_blocks():
                with self.assertRaises(EgressBlocked):
                    sock.sendto(b"probe", ("192.0.2.1", 53))
        finally:
            sock.close()

    def test_block_message_does_not_echo_payload(self):
        with egress_guard.expecting_blocks():
            with self.assertRaises(EgressBlocked) as ctx:
                socket.getaddrinfo("example.com", 80)
        # Only kind + host are recorded; nothing else may leak into the message.
        self.assertNotIn("80", str(ctx.exception).split("host=")[0])


class EgressAllowTests(unittest.TestCase):
    """Loopback traffic must keep working -- aiohttp test servers depend on it."""

    def setUp(self):
        self.guard = egress_guard.install()

    def test_loopback_ipv4_is_permitted(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            conn = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                self.assertGreaterEqual(conn.fileno(), 0)
            finally:
                conn.close()
        finally:
            server.close()

    def test_localhost_name_is_permitted(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            conn = socket.create_connection(("localhost", port), timeout=5)
            conn.close()
        finally:
            server.close()

    def test_loopback_does_not_increment_violation_counter(self):
        before = self.guard.block_count
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            conn = socket.create_connection(("127.0.0.1", server.getsockname()[1]), timeout=5)
            conn.close()
        finally:
            server.close()
        self.assertEqual(self.guard.block_count, before)


class CounterAndReportTests(unittest.TestCase):
    def setUp(self):
        self.guard = egress_guard.install()

    def test_deliberate_probe_is_not_counted_as_violation(self):
        violations = self.guard.block_count
        deliberate = self.guard.expected_count
        with egress_guard.expecting_blocks():
            with self.assertRaises(EgressBlocked):
                socket.getaddrinfo("example.com", 80)
        self.assertEqual(self.guard.block_count, violations)
        self.assertEqual(self.guard.expected_count, deliberate + 1)

    def test_unexpected_block_is_counted_as_violation(self):
        before = self.guard.block_count
        with self.assertRaises(EgressBlocked):
            socket.getaddrinfo("example.com", 80)
        self.assertEqual(self.guard.block_count, before + 1)
        # Keep the run honest: remove the violation we just created on purpose.
        with self.guard._lock:
            self.guard.blocked.pop()

    def test_report_reflects_zero_violations(self):
        self.assertIn("0 unexpected egress attempts", self.guard.report())

    def test_report_lists_violation_hosts(self):
        with self.assertRaises(EgressBlocked):
            socket.getaddrinfo("example.com", 80)
        try:
            report = self.guard.report()
            self.assertIn("UNEXPECTED", report)
            self.assertIn("example.com", report)
        finally:
            with self.guard._lock:
                self.guard.blocked.pop()


class InstallLifecycleTests(unittest.TestCase):
    def test_uninstall_restores_original_socket_behaviour(self):
        active = egress_guard.install()
        patched_connect = socket.socket.connect
        active.uninstall()
        try:
            self.assertIsNot(socket.socket.connect, patched_connect)
        finally:
            egress_guard.install()
        self.assertTrue(egress_guard.guard().installed)

    def test_install_is_idempotent(self):
        first = egress_guard.install()
        second = egress_guard.install()
        self.assertIs(first, second)
        self.assertTrue(second.installed)

    def test_guard_singleton_is_stable(self):
        # Regression guard: a second module object for egress_guard would give
        # the tests a different instance than the CLI reports on, silently
        # disabling the "unexpected egress fails the run" guarantee.
        self.assertIs(egress_guard.guard(), egress_guard.guard())
        self.assertIs(egress_guard.guard(), egress_guard.install())

    def test_self_check_passes_in_this_environment(self):
        self.assertTrue(egress_guard.self_check(verbose=False))


if __name__ == "__main__":
    unittest.main()
