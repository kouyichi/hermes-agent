"""A custom-endpoint Test must probe with the credential the endpoint already has (#147501).

The Desktop settings card sends ``api_key: undefined`` whenever the key is stored — the ``key_env``
slot a Save writes, or a hand-written ``${VAR}`` — so the probe used to go out with no Authorization
header at all. A llama-style server answers that with 401 JSON on ``/v1/models``; the probe's second
candidate strips the ``/v1`` and the server's web UI catch-all answers 200 ``text/html``, which the
handler then reported as "Connected to <root>/models, but it returned text/html instead of a JSON
model list" — naming a URL that is not in the config and hiding the real cause.

The fix has two halves, both asserted here: send the stored credential, and never let a fallback
candidate's model-less answer outrank the URL the user typed.
"""

from __future__ import annotations

import asyncio

import hermes_yaml as yaml

from hermes_cli.config import custom_endpoint_key_env, get_config_path, save_env_value


class _ProbeHost:
    """A llama-style server: ``/v1/models`` wants the key, everything else is the web UI's
    catch-all (200 text/html). Records every GET it answers."""

    def __init__(self, expected_bearer: str = ""):
        self.expected_bearer = expected_bearer
        self.gets: list[tuple[str, dict]] = []

    class _Resp:
        def __init__(self, status, content_type, body):
            self.status_code = status
            self.is_success = status < 400
            self.headers = {"content-type": content_type}
            self._body = body

        def json(self):
            if isinstance(self._body, str):
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return self._body

    def client(self):
        host = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, *a, headers=None, **k):
                host.gets.append((url, dict(headers or {})))
                if url.endswith("/v1/models") and (
                    host.expected_bearer
                    and (headers or {}).get("Authorization") == f"Bearer {host.expected_bearer}"
                ):
                    return host._Resp(200, "application/json", {"data": [{"id": "llama-local"}]})
                if url.endswith("/v1/models"):
                    return host._Resp(401, "application/json", {"error": "unauthorized"})
                return host._Resp(200, "text/html", "<html><body><div id=root></div></body></html>")

            async def post(self, url, *a, json=None, headers=None, **k):
                return host._Resp(400, "application/json", {"error": "bad request"})

        return _Client()


def _write_providers_entry(base_url: str, key_env: str) -> None:
    get_config_path().write_text(
        yaml.safe_dump({"providers": {"strata": {"name": "strata", "base_url": base_url,
                                                 "model": "llama-local", "key_env": key_env}}}),
        encoding="utf-8",
    )


def _probe(monkeypatch, host: _ProbeHost, body_kwargs: dict):
    import hermes_cli.web_routers.config_env as mod
    from hermes_cli.web_models import CustomEndpointUpdate

    monkeypatch.setattr(mod, "_endpoint_probe_client", lambda url, timeout: host.client())
    body = CustomEndpointUpdate(**body_kwargs)
    return asyncio.run(mod.validate_custom_endpoint(body))


def test_probe_sends_the_saved_key_env_credential(monkeypatch):
    """The stored ``key_env`` credential is the one the runtime uses, so the Test must use it too:
    without it a healthy endpoint answers 401 and the panel blames the URL."""
    base_url = "http://127.0.0.1:39999/v1"
    _write_providers_entry(base_url, custom_endpoint_key_env("strata"))
    save_env_value(custom_endpoint_key_env("strata"), "sk-stored-in-dotenv")

    host = _ProbeHost(expected_bearer="sk-stored-in-dotenv")
    data = _probe(monkeypatch, host, {"id": "strata", "name": "strata", "base_url": base_url,
                                      "model": "llama-local", "api_key": ""})

    assert data["ok"] is True, data["message"]
    assert data["models"] == ["llama-local"]
    assert host.gets[0] == (f"{base_url}/models", {"Accept": "application/json",
                                                   "Authorization": "Bearer sk-stored-in-dotenv"})


def test_probe_sends_a_hand_written_env_ref_key(monkeypatch):
    """The reporter's install kept the key as ``api_key: ${STRATA_API_KEY}`` on a legacy
    ``custom_providers:`` row — the row the panel lists and tests by its slug."""
    base_url = "http://127.0.0.1:39999/v1"
    monkeypatch.setenv("STRATA_API_KEY", "sk-from-the-environment")
    get_config_path().write_text(
        yaml.safe_dump({"custom_providers": [{"name": "strata", "base_url": base_url,
                                              "model": "llama-local",
                                              "api_key": "${STRATA_API_KEY}"}]}),
        encoding="utf-8",
    )

    host = _ProbeHost(expected_bearer="sk-from-the-environment")
    data = _probe(monkeypatch, host, {"id": "strata", "name": "strata", "base_url": base_url,
                                      "model": "llama-local"})

    assert data["ok"] is True, data["message"]
    assert host.gets[0][1].get("Authorization") == "Bearer sk-from-the-environment"


def test_probe_never_hands_a_saved_key_to_a_different_url(monkeypatch):
    """A URL that is not the endpoint's own gets no stored secret — the panel probes what the user
    is about to point the endpoint at, and that must not become a credential-exfiltration path."""
    _write_providers_entry("http://127.0.0.1:39999/v1", custom_endpoint_key_env("strata"))
    save_env_value(custom_endpoint_key_env("strata"), "sk-stored-in-dotenv")

    host = _ProbeHost(expected_bearer="sk-stored-in-dotenv")
    data = _probe(monkeypatch, host, {"id": "strata", "name": "strata",
                                      "base_url": "http://127.0.0.1:39999/elsewhere/v1",
                                      "model": "llama-local", "api_key": ""})

    assert data["ok"] is False
    assert all("Authorization" not in headers for _url, headers in host.gets)


def test_auth_rejection_is_not_masked_by_the_v1_stripped_spa_fallback(monkeypatch):
    """``/v1/models`` says 401 (the credential) while the ``/v1``-stripped catch-all says 200
    text/html. The verdict must be the configured URL's answer: reporting the stripped URL's HTML
    sends the user hunting for a base URL that is not the one in their config."""
    host = _ProbeHost()
    data = _probe(monkeypatch, host, {"name": "strata", "base_url": "http://127.0.0.1:39999/v1",
                                      "model": "llama-local", "api_key": ""})

    assert data["ok"] is False and data["reachable"] is True
    assert data["message"] == "The endpoint rejected the API key.", data["message"]
    assert "text/html" not in data["message"]
