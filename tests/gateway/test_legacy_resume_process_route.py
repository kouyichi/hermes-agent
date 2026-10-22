"""#142779: a row last billed by an external-process provider resumes on that provider.

Such a provider declares its own fixed transport (``process://<name>``); the per-turn usage stamp
copies it into ``billing_base_url``. ``legacy_launch_params`` resurrected that stamp as a ``base_url``
launch pin, and ``_apply_launch_overrides`` (http(s)-only since v0.22.3) refused it, so every resume of
such a chat failed with ``invalid_params field=base_url``. The stamp records which endpoint billed the
last turn; the row's provider name is what restores the route.
"""
import json

import pytest

from gateway.session_local_migration import legacy_launch_params
from gateway.session_policy import build_policy

PROVIDER = 'claude-subscription-directsdk-experimental'
PSEUDO = f'process://{PROVIDER}'
PROFILE = {'model': {'default': 'profile-model', 'provider': 'custom',
                     'base_url': 'https://profile.example.com/v1'}}


def _row(billing_base_url, provider, cwd):
    return {'id': '20261019_221959_016823', 'source': 'cli', 'model': 'claude-sonnet-4-5',
            'cwd': str(cwd), 'model_config': json.dumps({}), 'billing_provider': provider,
            'billing_base_url': billing_base_url}


def _params(billing_base_url, provider, cwd):
    params, _ = legacy_launch_params(_row(billing_base_url, provider, cwd))
    params['source'] = 'cli'
    return params


def test_process_endpoint_stamp_is_not_a_launch_pin(tmp_path):
    params = _params(PSEUDO, PROVIDER, tmp_path)
    assert params['provider'] == PROVIDER
    assert 'base_url' not in params


def test_resume_of_a_process_route_row_builds_a_policy(tmp_path):
    policy = build_policy(_params(PSEUDO, PROVIDER, tmp_path), json.loads(json.dumps(PROFILE)))
    request = json.loads(policy.request_json)
    assert request['provider'] == PROVIDER
    assert 'base_url' not in request


@pytest.mark.parametrize('stamp', ['https://integrate.api.nvidia.com/v1', 'http://127.0.0.1:1234/v1'])
def test_http_stamp_without_a_saved_route_is_still_a_launch_pin(tmp_path, stamp):
    """The resurrection this guard narrows: a named provider plus an http(s) stamp keeps its endpoint,
    and the bare-custom heal still sees a ``custom`` provider for it."""
    params = _params(stamp, 'nvidia', tmp_path)
    assert params['base_url'] == stamp
    assert params['provider'] == 'nvidia'
