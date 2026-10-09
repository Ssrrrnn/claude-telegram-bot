"""Process-level egress guard for hermetic test runs.

Installs an in-process guard that rejects every network operation which is not
directed at a loopback address, and counts each rejection.

READ THIS BEFORE RELYING ON IT
------------------------------
This is **defence in depth and a detector -- NOT an operating-system level
network isolation mechanism**.  It runs inside the CPython interpreter and can
only observe traffic that passes through the ``socket`` module's Python-level
entry points.  It CANNOT stop:

* a child process spawned through ``subprocess`` / ``os.system`` / ``os.exec*``;
* native code that opens its own socket without the ``socket`` module
  (for example a C extension calling ``connect(2)`` directly);
* a ``_socket.socket`` instance created directly, which bypasses the patched
  ``socket.socket`` subclass;
* Unix-domain sockets, files, pipes, or any other non-IP channel;
* any connection that was already established before :func:`install` ran.

Use it as a second line of defence *underneath* a real isolation boundary such
as a container started with ``--network none``.  Never use it as a replacement
for that boundary.

Coverage notes
--------------
* ``socket.create_connection`` is covered indirectly: it calls
  ``socket.getaddrinfo`` and then ``socket.socket.connect``, both patched.
* TLS sockets are covered indirectly: ``ssl.SSLSocket.connect`` delegates to
  ``socket.socket.connect``, which is patched.
* UDP egress is covered through ``socket.socket.sendto``.

Credential safety
-----------------
Blocked entries record only the operation kind and the destination *host*.
No payload, header, URL query, or credential is ever read, stored or printed.

Usage
-----
    # run unittest modules from the repository root, guard installed first
    python egress_guard.py test_continuity test_qq

    # guard-only self-check (no test modules executed)
    python egress_guard.py --selfcheck
"""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
import unittest

__all__ = [
    "EgressBlocked",
    "EgressGuard",
    "expecting_blocks",
    "guard",
    "install",
    "main",
    "self_check",
    "uninstall",
]

# Hosts that are treated as loopback / local-only and therefore permitted.
_LOOPBACK_NAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }
)


class EgressBlocked(RuntimeError):
    """Raised when the guard rejects a non-loopback network operation."""


def _host_of(address):
    """Return the host component of a socket address, or the value itself."""
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    return address


def is_loopback(host) -> bool:
    """True when *host* denotes a local-only destination or an unspecified bind."""
    if host is None:
        return True
    if not isinstance(host, str):
        return False
    if host == "":
        return True
    # Unspecified addresses -- used for listening, never a remote destination.
    if host in ("0.0.0.0", "::"):
        return True
    if host.lower() in _LOOPBACK_NAMES:
        return True
    if host.startswith("127."):
        return True
    if host == "::1" or host == "0:0:0:0:0:0:0:1":
        return True
    # IPv4-mapped IPv6 loopback, e.g. ::ffff:127.0.0.1
    if host.lower().startswith("::ffff:127."):
        return True
    return False


class EgressGuard:
    """In-process guard around the ``socket`` module."""

    def __init__(self) -> None:
        self.blocked: list[tuple[str, object]] = []
        self.expected: list[tuple[str, object]] = []
        self._expect_depth = 0
        self._lock = threading.Lock()
        self._installed = False
        self._orig_connect = None
        self._orig_connect_ex = None
        self._orig_sendto = None
        self._orig_getaddrinfo = None

    # -- counters ---------------------------------------------------------
    @property
    def block_count(self) -> int:
        """Number of *unexpected* (i.e. violation) egress attempts."""
        with self._lock:
            return len(self.blocked)

    @property
    def expected_count(self) -> int:
        """Number of egress attempts made deliberately by the guard self-check."""
        with self._lock:
            return len(self.expected)

    @property
    def installed(self) -> bool:
        return self._installed

    # -- internals --------------------------------------------------------
    def _reject(self, kind: str, address) -> None:
        host = _host_of(address)
        with self._lock:
            if self._expect_depth > 0:
                self.expected.append((kind, host))
                total = len(self.expected)
                tag = "expected"
            else:
                self.blocked.append((kind, host))
                total = len(self.blocked)
                tag = "UNEXPECTED"
        raise EgressBlocked(
            "egress-guard rejected %s to host=%r (%s #%d)"
            % (kind, host, tag, total)
        )

    @contextlib.contextmanager
    def expecting_blocks(self):
        """Mark blocks raised inside this block as deliberate, not violations."""
        with self._lock:
            self._expect_depth += 1
        try:
            yield self
        finally:
            with self._lock:
                self._expect_depth -= 1

    # -- patching ---------------------------------------------------------
    def install(self) -> "EgressGuard":
        if self._installed:
            return self
        cls = socket.socket
        self._orig_connect = cls.connect
        self._orig_connect_ex = cls.connect_ex
        self._orig_sendto = cls.sendto
        self._orig_getaddrinfo = socket.getaddrinfo

        def connect(sock, address):
            if not is_loopback(_host_of(address)):
                self._reject("connect", address)
            return self._orig_connect(sock, address)

        def connect_ex(sock, address):
            if not is_loopback(_host_of(address)):
                self._reject("connect_ex", address)
            return self._orig_connect_ex(sock, address)

        def sendto(sock, data, *args):
            address = args[-1] if args else None
            if isinstance(address, (tuple, list)) and not is_loopback(_host_of(address)):
                self._reject("sendto", address)
            return self._orig_sendto(sock, data, *args)

        def getaddrinfo(host, port, *args, **kwargs):
            if not is_loopback(host):
                self._reject("getaddrinfo", host)
            return self._orig_getaddrinfo(host, port, *args, **kwargs)

        cls.connect = connect
        cls.connect_ex = connect_ex
        cls.sendto = sendto
        socket.getaddrinfo = getaddrinfo
        self._installed = True
        return self

    def uninstall(self) -> None:
        if not self._installed:
            return
        socket.socket.connect = self._orig_connect
        socket.socket.connect_ex = self._orig_connect_ex
        socket.socket.sendto = self._orig_sendto
        socket.getaddrinfo = self._orig_getaddrinfo
        self._installed = False

    # -- reporting --------------------------------------------------------
    def report(self) -> str:
        with self._lock:
            violations = list(self.blocked)
            deliberate = len(self.expected)
        if not violations:
            return (
                "egress-guard: 0 unexpected egress attempts "
                "(deliberate self-check probes: %d)" % deliberate
            )
        lines = ["egress-guard: %d UNEXPECTED egress attempt(s):" % len(violations)]
        lines.extend("  - %s -> host=%r" % (kind, host) for kind, host in violations)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_GUARD: EgressGuard | None = None
_GUARD_LOCK = threading.Lock()


def install() -> EgressGuard:
    """Install (once) and return the process-wide guard."""
    global _GUARD
    with _GUARD_LOCK:
        if _GUARD is None:
            _GUARD = EgressGuard()
        return _GUARD.install()


def uninstall() -> None:
    """Restore the original ``socket`` behaviour."""
    with _GUARD_LOCK:
        if _GUARD is not None:
            _GUARD.uninstall()


def guard() -> EgressGuard:
    """Return the process-wide guard, installing it if necessary."""
    return install()


def expecting_blocks():
    """Context manager marking blocks inside it as deliberate."""
    return guard().expecting_blocks()


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------
def self_check(verbose: bool = True) -> bool:
    """Verify the guard denies non-loopback traffic and permits loopback.

    Returns True when every probe behaved as required.  Intended for use as a
    fast pre-flight check; it also proves that a loopback listener can be
    reached, which is what the aiohttp test servers need.
    """
    active = install()
    results: list[tuple[str, bool, str]] = []

    # 1. non-loopback DNS must be rejected
    with active.expecting_blocks():
        try:
            socket.getaddrinfo("example.com", 80)
            results.append(("deny:dns", False, "getaddrinfo(external) was NOT blocked"))
        except EgressBlocked as exc:
            results.append(("deny:dns", True, str(exc)))
        except OSError as exc:
            results.append(("deny:dns", False, "unexpected OS error: %r" % (exc,)))

    # 2. non-loopback TCP connect must be rejected before any packet is sent
    with active.expecting_blocks():
        try:
            socket.create_connection(("192.0.2.1", 80), timeout=3)
            results.append(("deny:tcp", False, "connect(192.0.2.1) was NOT blocked"))
        except EgressBlocked as exc:
            results.append(("deny:tcp", True, str(exc)))
        except OSError as exc:
            results.append(("deny:tcp", False, "unexpected OS error: %r" % (exc,)))

    # 3. loopback must be permitted
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.close()
        results.append(("allow:loopback-ipv4", True, "127.0.0.1:%d reachable" % port))
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        results.append(("allow:loopback-ipv4", False, "%r" % (exc,)))
    finally:
        server.close()

    # 4. IPv6 loopback, when the platform provides it
    if socket.has_ipv6:
        server6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            server6.bind(("::1", 0))
            server6.listen(1)
            port6 = server6.getsockname()[1]
            conn6 = socket.create_connection(("::1", port6), timeout=5)
            conn6.close()
            results.append(("allow:loopback-ipv6", True, "::1:%d reachable" % port6))
        except Exception as exc:  # noqa: BLE001
            results.append(("allow:loopback-ipv6", False, "%r" % (exc,)))
        finally:
            server6.close()

    ok = all(passed for _, passed, _ in results)
    if verbose:
        for name, passed, detail in results:
            print("  [%s] %-20s %s" % ("PASS" if passed else "FAIL", name, detail))
        print("egress-guard self-check: %s" % ("OK" if ok else "FAILED"))
    return ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _usage(stream) -> None:
    print(
        "usage: python egress_guard.py [--selfcheck] [module ...]\n"
        "\n"
        "Installs the process-level egress guard, then runs the named unittest\n"
        "modules from the repository root.  Exits non-zero when a test fails or\n"
        "when any unexpected egress attempt was blocked.\n",
        file=stream,
    )


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("-h", "--help"):
        _usage(sys.stdout)
        return 0

    active = install()

    if args and args[0] == "--selfcheck":
        ok = self_check()
        print(active.report())
        return 0 if ok else 1

    if not args:
        _usage(sys.stderr)
        return 2

    print(
        "egress-guard installed: only loopback destinations are permitted",
        flush=True,
    )
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    load_errors = []
    for name in args:
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except Exception as exc:  # noqa: BLE001 - surface as a load failure
            load_errors.append((name, exc))
            print("egress-guard: failed to load %r: %r" % (name, exc), file=sys.stderr)

    result = unittest.TextTestRunner(verbosity=2).run(suite)

    print(active.report(), flush=True)
    print(
        "EGRESS-GUARD-SUMMARY unexpected=%d deliberate=%d tests=%d failures=%d errors=%d skipped=%d"
        % (
            active.block_count,
            active.expected_count,
            result.testsRun,
            len(result.failures),
            len(result.errors),
            len(result.skipped),
        ),
        flush=True,
    )

    if load_errors:
        return 1
    if active.block_count:
        print(
            "egress-guard: FAILING the run because unexpected egress was blocked",
            file=sys.stderr,
        )
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    # Running this file as a script would otherwise create a SECOND module
    # object when a test module does `import egress_guard`, which would give the
    # tests a different guard instance than the one main() reports on -- and the
    # "unexpected egress fails the run" guarantee would silently break.
    # Registering the running module under its import name keeps one instance.
    sys.modules.setdefault("egress_guard", sys.modules["__main__"])
    sys.exit(main())
