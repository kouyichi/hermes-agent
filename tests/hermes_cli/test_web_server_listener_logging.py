"""Building a listener's ``uvicorn.Config`` must leave Hermes's own file logging alone.

uvicorn's default ``log_config`` runs ``logging.config.dictConfig``, which calls
``logging.shutdown()`` on every live handler. A multiplexed gateway installs its profile log
routers before the API listener starts, and a closed router stays closed, so agent.log,
errors.log and gateway.log went silent for every home until restart. Real handlers and files;
nothing binds.
"""

import logging

import pytest

import hermes_cli.web_server as ws_mod
import hermes_logging
from hermes_cli import web_server_lifecycle as lifecycle
from hermes_cli.web_server_lifecycle import build_listener_server
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


def _clear_uvicorn_handlers() -> None:
    """Force the "first listener in this process" branch: the console install is a no-op while a
    handler from an earlier listener is still attached."""
    for name in _UVICORN_LOGGERS:
        logging.getLogger(name).handlers[:] = []


@pytest.fixture(autouse=True)
def _isolated_logging():
    """Undo the queued file handlers, root level and uvicorn logger state a test sets up."""
    root = logging.getLogger()
    saved_root_level = root.level
    saved = {name: (lg.level, lg.propagate, list(lg.handlers))
             for name in _UVICORN_LOGGERS for lg in (logging.getLogger(name),)}
    hermes_logging._reset_queued_handlers()
    yield
    hermes_logging._reset_queued_handlers()
    hermes_logging._logging_initialized = False
    root.setLevel(saved_root_level)
    for name, (level, propagate, handlers) in saved.items():
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _gateway_listener(_monkeypatch):
    return build_listener_server(lambda scope, receive, send: None, "127.0.0.1", 0, auth_required=False)


def _dashboard_listener(monkeypatch):
    monkeypatch.setattr(ws_mod.app.state, "auth_required", False, raising=False)
    return ws_mod._build_uvicorn_server("127.0.0.1", 0)


@pytest.mark.parametrize("build", [_gateway_listener, _dashboard_listener], ids=["gateway", "dashboard"])
def test_listener_config_keeps_every_profile_log_file_writing(tmp_path, monkeypatch, build):
    root = tmp_path / "root"
    beta = root / "profiles" / "beta"
    beta.mkdir(parents=True)
    (beta / "config.yaml").write_text("{}", encoding="utf-8")
    hermes_logging.setup_logging(hermes_home=root, mode="gateway", force=True)
    assert hermes_logging.enable_profile_log_routing([root, beta])
    logger = logging.getLogger("gateway.listener_logging")

    def log_in_every_home(marker):
        for home in (root, beta):
            token = set_hermes_home_override(str(home))
            try:
                logger.warning(marker)
            finally:
                reset_hermes_home_override(token)
        hermes_logging.flush_log_queue()

    log_in_every_home("before-listener")
    build(monkeypatch)
    log_in_every_home("after-listener")
    for home in (root, beta):
        for name in ("agent.log", "errors.log", "gateway.log"):
            text = (home / "logs" / name).read_text(encoding="utf-8-sig")
            assert "before-listener" in text
            assert "after-listener" in text, f"building the listener silenced {home / 'logs' / name}"


def test_listener_config_keeps_uvicorn_on_the_console_and_out_of_the_hermes_logs(tmp_path, monkeypatch, capsys):
    """Without uvicorn's ``dictConfig`` its loggers still print where they did: ``uvicorn.error``
    to stderr, access lines nowhere at ``log_level="warning"``, neither into agent.log."""
    root = tmp_path / "root"
    hermes_logging.setup_logging(hermes_home=root, mode="gateway", force=True)
    _gateway_listener(monkeypatch)
    _dashboard_listener(monkeypatch)  # a second listener in the process adds no second console handler

    logging.getLogger("uvicorn.error").warning("uvicorn-console-warning")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", "/access-line", "1.1", 200)
    logging.getLogger("gateway.listener_logging").warning("hermes-file-warning")
    hermes_logging.flush_log_queue()

    out, err = capsys.readouterr()
    assert logging.getLogger("uvicorn").level == logging.INFO  # LOGGING_CONFIG's, not the root's
    assert err.count("uvicorn-console-warning") == 1
    assert "/access-line" not in out + err
    agent_log = (root / "logs" / "agent.log").read_text(encoding="utf-8-sig")
    assert "hermes-file-warning" in agent_log
    assert "uvicorn-console-warning" not in agent_log and "/access-line" not in agent_log


def test_a_listener_builds_with_no_console_at_all(monkeypatch):
    """Windows ``pythonw`` / a detached service has ``sys.stdout is None`` and ``sys.stderr is None``
    (CPython), and uvicorn's formatters read ``sys.stdout.isatty()`` at construction while
    ``logging.StreamHandler(None)`` adopts the same None stream and raises on the first emit. Both
    used to kill the gateway in a crash loop before any adapter started (#144932)."""
    import sys as _sys

    _clear_uvicorn_handlers()
    monkeypatch.setattr(_sys, "stdout", None)
    monkeypatch.setattr(_sys, "stderr", None)

    lifecycle._uvicorn_console_logging()

    for name in ("uvicorn", "uvicorn.access"):
        logger = logging.getLogger(name)
        assert logger.handlers == [], f"{name} kept a handler on a stream that does not exist"
        assert logger.propagate is False, f"{name} must not fall through into the Hermes log files"
    logging.getLogger("uvicorn.error").warning("no-console-warning")  # the emit-time trap


def test_a_listener_builds_with_one_console_stream(monkeypatch):
    """Only the missing stream is skipped: ``pythonw`` can leave one of the two unauthored, and the
    surviving logger keeps its console handler (on the stream it was built for)."""
    import io
    import sys as _sys

    _clear_uvicorn_handlers()
    stdout = io.StringIO()
    monkeypatch.setattr(_sys, "stdout", stdout)
    monkeypatch.setattr(_sys, "stderr", None)

    lifecycle._uvicorn_console_logging()

    assert logging.getLogger("uvicorn").handlers == []
    access = logging.getLogger("uvicorn.access")
    assert len(access.handlers) == 1
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", "/access-line", "1.1", 200)
    assert "/access-line" in stdout.getvalue()
