"""CLI run flags retain their pre-retention request semantics."""
import json
from io import BytesIO
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
from flowly.cli.cron_cmd import cron_app


@pytest.mark.parametrize(('flags', 'force'), [([], False), (['-f'], True), (['--force'], True), (['--no-force'], False)])
def test_run_force_flags(flags, force):
    requests = []
    def respond(request, **kwargs):
        requests.append(request)
        return BytesIO(b'{"ok":true}')
    with patch('urllib.request.urlopen', side_effect=respond):
        result = CliRunner().invoke(cron_app, ['run', 'existing-job', *flags])
    assert result.exit_code == 0, result.output
    assert len(requests) == 1
    assert json.loads(requests[0].data) == {'job_id': 'existing-job', 'force': force}
