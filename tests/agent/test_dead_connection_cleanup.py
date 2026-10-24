"""Regression tests for stale provider sockets in agent-owned client pools."""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from agent.agent_runtime_helpers import _iter_pool_sockets
from agent.agent_runtime_helpers_dead_connections import (
    _socket_is_dead,
    cleanup_dead_connections,
)


def _client_with_socket(sock):
    stream = SimpleNamespace(_sock=sock)
    http11 = SimpleNamespace(_network_stream=stream)
    pool_entry = SimpleNamespace(_connection=http11)
    pool = SimpleNamespace(_connections=[pool_entry])
    transport = SimpleNamespace(_pool=pool)
    http_client = SimpleNamespace(_transport=transport)
    return SimpleNamespace(_client=http_client)


@pytest.fixture
def plain_pair():
    """A connected plain-socket pair; closing either end is the peer's FIN."""
    local, peer = socket.socketpair()
    try:
        yield local, peer
    finally:
        for sock in (local, peer):
            with contextlib.suppress(OSError):
                sock.close()


def _tls_contexts(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dead-connection-probe")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_pem = tmp_path / "probe.pem"
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem = tmp_path / "probe.key"
    key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_pem, key_pem)
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    return client, server


@pytest.fixture
def tls_pair(tmp_path):
    """A handshaken TLS socket pair over an in-process socketpair (no server, no network)."""
    client_ctx, server_ctx = _tls_contexts(tmp_path)
    local, peer = socket.socketpair()
    server_side = []

    def handshake() -> None:
        try:
            wrapped = server_ctx.wrap_socket(peer, server_side=True)
            wrapped.do_handshake()
            server_side.append(wrapped)
        except (OSError, ssl.SSLError) as exc:  # surfaced by the assertion below
            server_side.append(exc)

    thread = threading.Thread(target=handshake, daemon=True)
    thread.start()
    client = client_ctx.wrap_socket(local, server_hostname="dead-connection-probe")
    client.do_handshake()
    thread.join(timeout=10)
    assert not thread.is_alive(), "TLS handshake thread did not finish"
    assert isinstance(server_side[0], ssl.SSLSocket), server_side
    try:
        yield client, server_side[0]
    finally:
        for sock in (client, server_side[0]):
            with contextlib.suppress(OSError):
                sock.close()


def _close_gracefully(sock: ssl.SSLSocket) -> None:
    """Send close_notify and close; the peer's acknowledgement is not needed."""
    sock.settimeout(0.5)
    with contextlib.suppress(TimeoutError, OSError):
        sock.unwrap()
    sock.close()


def test_live_socket_is_not_reported_dead(plain_pair) -> None:
    local, _peer = plain_pair
    assert _socket_is_dead(local) is False


def test_closed_peer_is_reported_dead(plain_pair) -> None:
    local, peer = plain_pair
    peer.close()
    assert _socket_is_dead(local) is True


def test_probe_does_not_depend_on_msg_dontwait(plain_pair, monkeypatch) -> None:
    """Windows exposes no ``socket.MSG_DONTWAIT``: the probe must not need it."""
    monkeypatch.delattr(socket, "MSG_DONTWAIT")
    local, peer = plain_pair
    assert _socket_is_dead(local) is False
    peer.close()
    assert _socket_is_dead(local) is True


def test_probe_preserves_the_socket_timeout(plain_pair) -> None:
    """The probe must hand the socket back exactly as it found it."""
    local, peer = plain_pair
    local.settimeout(7.0)
    assert _socket_is_dead(local) is False
    assert local.gettimeout() == 7.0
    peer.close()
    assert _socket_is_dead(local) is True
    assert local.gettimeout() == 7.0


def test_closed_descriptor_is_reported_dead(plain_pair) -> None:
    local, _peer = plain_pair
    local.close()
    assert _socket_is_dead(local) is True


def test_tls_socket_is_probed_without_raising(tls_pair) -> None:
    client, _server = tls_pair
    assert _socket_is_dead(client) is False


def test_tls_peer_close_notify_is_reported_dead(tls_pair) -> None:
    client, server = tls_pair
    _close_gracefully(server)
    assert _socket_is_dead(client) is True


def test_cleanup_retires_dead_cached_request_client(plain_pair) -> None:
    """A half-closed request pool must not be hidden by a healthy primary."""
    primary_sock, _primary_peer = plain_pair
    request_sock, request_peer = socket.socketpair()
    request_peer.close()
    agent = SimpleNamespace(
        client=_client_with_socket(primary_sock),
        _request_client_cache={"client": _client_with_socket(request_sock)},
        _replace_primary_openai_client=MagicMock(),
        _close_cached_request_openai_client=MagicMock(),
    )
    try:
        assert cleanup_dead_connections(agent) is True
    finally:
        request_sock.close()
    agent._replace_primary_openai_client.assert_not_called()
    agent._close_cached_request_openai_client.assert_called_once_with(
        reason="dead_connection_cleanup"
    )


def test_cleanup_rebuilds_dead_primary_client(plain_pair) -> None:
    """The existing primary-client recovery remains the cleanup path."""
    primary_sock, primary_peer = plain_pair
    primary_peer.close()
    request_sock, _request_peer = socket.socketpair()
    agent = SimpleNamespace(
        client=_client_with_socket(primary_sock),
        _request_client_cache={"client": _client_with_socket(request_sock)},
        _replace_primary_openai_client=MagicMock(),
        _close_cached_request_openai_client=MagicMock(),
    )
    try:
        assert cleanup_dead_connections(agent) is True
    finally:
        request_sock.close()
    agent._replace_primary_openai_client.assert_called_once_with(
        reason="dead_connection_cleanup"
    )
    agent._close_cached_request_openai_client.assert_not_called()


def test_cleanup_detects_a_dead_tls_pool(tls_pair) -> None:
    """The sweep reaches HTTPS pools too: a closed TLS peer is a dead connection."""
    client, server = tls_pair
    agent = SimpleNamespace(
        client=_client_with_socket(client),
        _request_client_cache=None,
        _replace_primary_openai_client=MagicMock(),
        _close_cached_request_openai_client=MagicMock(),
    )
    assert cleanup_dead_connections(agent) is False
    _close_gracefully(server)
    assert cleanup_dead_connections(agent) is True
    agent._replace_primary_openai_client.assert_called_once_with(
        reason="dead_connection_cleanup"
    )


def test_cleanup_detects_a_real_https_pool_whose_peer_dropped(tmp_path):
    """End to end over a real httpx pool: the sweep fires for an HTTPS endpoint.

    The unit cases above hand-build the httpcore pool shape; this one proves the socket
    ``_iter_pool_sockets`` actually yields from a live client is the one the probe judges.
    """
    import select

    import httpx

    _client_ctx, server_ctx = _tls_contexts(tmp_path)
    peer_sockets: list[socket.socket] = []
    body = b'{"ok":true}'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            peer_sockets.append(self.connection)  # keep-alive: the server end stays open

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http = httpx.Client(verify=False, timeout=5)
    agent = SimpleNamespace(
        client=http,
        _request_client_cache=None,
        _replace_primary_openai_client=MagicMock(),
        _close_cached_request_openai_client=MagicMock(),
    )
    try:
        assert http.get(f"https://127.0.0.1:{server.server_port}/").status_code == 200
        assert cleanup_dead_connections(agent) is False  # live keep-alive pool
        pooled = list(_iter_pool_sockets(http))
        assert len(pooled) == 1 and isinstance(pooled[0], ssl.SSLSocket), pooled
        # The peer drops the idle keep-alive connection: FIN, no close_notify.
        assert peer_sockets, "server handler never recorded its socket"
        peer_sockets[0].shutdown(socket.SHUT_RDWR)
        assert select.select([pooled[0]], [], [], 5)[0], "peer FIN never reached the client"
        assert cleanup_dead_connections(agent) is True
        agent._replace_primary_openai_client.assert_called_once_with(
            reason="dead_connection_cleanup"
        )
    finally:
        http.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
