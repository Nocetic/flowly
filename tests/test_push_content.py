"""What a phone notification says.

A notification reads like a message from the agent (its name, then the
question, the command, the action, the plan or the result), and nothing
that leaves the machine carries a credential or a disguised command.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from flowly.config.loader import load_config
from flowly.push import approval_push, notifications, relay_push
from flowly.push.display import has_hidden_characters, safe_text

SECRETS = [
    ('curl -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.longtoken.sig" https://api.example.com',
     ['eyJhbGci', 'longtoken'], ['curl', 'https://api.example.com']),
    ('API_SECRET="sk-abc123456789012345678" python script.py', ['sk-abc123'], ['python script.py']),
    ('export OPENAI_API_KEY=sk-proj-AbCdEf1234567890 && npm start', ['AbCdEf'], ['OPENAI_API_KEY=', 'npm start']),
    ('git clone https://ghp_1234567890abcdefghij1234567890abcdef@github.com/user/repo', ['ghp_'], ['git clone', 'github.com/user/repo']),
    ('psql --password=hunter2secret -h db', ['hunter2'], ['psql --password=', '-h db']),
    ('gh auth login --token ghs_abcdefghijklmnop', ['ghs_abc'], ['gh auth login --token']),
    ('curl -u admin:S3cretPass https://x.io', ['S3cret'], ['curl -u admin:', 'https://x.io']),
    ('mysql -u root -pS3cret mydb', ['S3cret'], ['mysql -u root -p', 'mydb']),
    ('sshpass -p hunter2 ssh me@host', ['hunter2'], ['ssh me@host']),
    ('docker run -e POSTGRES_PASSWORD=pw123 postgres', ['pw123'], ['POSTGRES_PASSWORD=', 'postgres']),
    ('curl -X POST https://hooks.slack.com/services/T000/B000/AbCdEfGh12345678 -d x', ['AbCdEfGh'], ['hooks.slack.com/services/']),
    ('curl https://discord.com/api/webhooks/123456/abcDEF-xyz', ['abcDEF'], ['discord.com/api/webhooks/123456/']),
    ('psql postgres://app:Sup3rS3cret@db.internal/app', ['Sup3r'], ['db.internal/app']),
    ('aws configure set aws_access_key_id AKIAABCDEFGHIJKLMNOP', ['AKIAABCD'], ['aws configure set']),
    ('echo "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ" > key',
     ['b3BlbnNzaC'], ['echo']),
    ('deploy --webhook Xk9mQ2vL8pR4tY7wZ1nB5cF3hJ6sD0aE', ['Xk9mQ2'], ['deploy --webhook']),
]

ORDINARY = [
    'ls -la ~/Desktop',
    'git push origin main',
    'git checkout 4efe59e8a1b2c3d4e5f60718293a4b5c6d7e8f90',
    'npm install --save-dev typescript',
    'python3 -m pytest tests -q -k "push or approval"',
    'rm -rf ./build',
]


@pytest.mark.parametrize('command,hidden_parts,kept_parts', SECRETS)
def test_a_credential_in_a_command_never_leaves_the_machine(command, hidden_parts, kept_parts):
    shown = safe_text(command, 500, command=True)
    for part in hidden_parts:
        assert part not in shown, shown
    for part in kept_parts:
        assert part in shown, shown


@pytest.mark.parametrize('command', ORDINARY)
def test_an_ordinary_command_reads_exactly_as_typed(command):
    assert safe_text(command, 500, command=True) == command
    assert not has_hidden_characters(command)


def test_redaction_happens_before_the_text_is_cut():
    # Cut first, and the 16-character minimum of the key shape would no longer
    # match the prefix left at the edge.
    command = 'echo ' + 'x' * 160 + ' sk-' + 'a1B2c3D4' * 4
    shown = safe_text(command, 180, command=True)
    assert 'sk-a1' not in shown and 'a1B2' not in shown
    assert len(shown) <= 180


@pytest.mark.parametrize('splice', ['\u200b', '\u00a0', '\u202f', '\u2060', '\ufeff'])
def test_a_secret_split_by_an_invisible_character_is_still_redacted(splice):
    shown = safe_text(f'echo sk-abc123{splice}456789012345678 remainder', 500, command=True)
    assert 'abc123' not in shown and '456789012345678' not in shown
    assert shown.endswith('remainder')


@pytest.mark.parametrize('disguise', ['\u202e', '\u200b', '\u00a0', '\x07', '\u2066'])
def test_a_command_with_hidden_characters_is_never_shown(disguise):
    pending = _Pending(command=f'rm -rf /tmp/x{disguise}/y', kind='exec')
    assert approval_push.approval_body(pending) == approval_push.HIDDEN_COMMAND_BODY


def test_a_multi_line_command_stays_on_one_readable_line():
    assert safe_text('cd ~/proj && npm test\nnpm run build\r\n', 500, command=True) == 'cd ~/proj && npm test ↵ npm run build'


def test_prose_keeps_its_unusual_spaces_as_spaces():
    assert safe_text('Toplantıyı yarın\u00a010\'a mı alayım?', 200) == "Toplantıyı yarın 10'a mı alayım?"


@dataclass
class _Request:
    command: str


@dataclass
class _Pending:
    command: str
    kind: str = 'exec'
    id: str = 'a_1'
    request: _Request = field(init=False)

    def __post_init__(self):
        self.request = _Request(self.command)


def test_a_command_approval_names_the_command():
    assert approval_push.approval_body(_Pending('git push origin main')) == 'Needs your OK to run: git push origin main'


def test_an_action_approval_shows_what_not_the_whole_payload():
    body = approval_push.approval_body(_Pending(
        '📧 Send email to ali@example.com\nSubject: Offer\n\nHi Ali, the price is 12.000 EUR', kind='action'))
    assert body == 'Needs your OK: 📧 Send email to ali@example.com'


def test_an_empty_approval_still_says_something():
    assert approval_push.approval_body(_Pending('   ', kind='action')) == approval_push.EMPTY_BODY


@dataclass
class _Question:
    question: str
    choices: list | None = None
    id: str = 'q_1'


def test_a_question_lists_short_choices_and_drops_long_ones():
    assert approval_push.question_body(_Question('Move the meeting to 10?', ['Yes', 'No'])) == 'Move the meeting to 10? (Yes / No)'
    many = _Question('Which one?', ['A', 'B', 'C', 'D', 'E'])
    assert approval_push.question_body(many) == 'Which one?'
    long = _Question('Which one?', ['The first option that is far too long to list', 'B'])
    assert approval_push.question_body(long) == 'Which one?'
    assert approval_push.question_body(_Question('  ')) == approval_push.QUESTION_FALLBACK


@dataclass
class _Plan:
    title: str
    steps: list
    goal: str = ''


@dataclass
class _Approval:
    id: str = 'pa_1'


def test_a_plan_names_its_title_and_size():
    plan = _Plan('Move the blog to Next.js', [1, 2, 3, 4, 5])
    notice = approval_push.plan_notice(_Approval(), 'plan-1', lookup=lambda plan_id: plan)
    assert notice.body == 'Plan ready for your OK: Move the blog to Next.js (5 steps)'
    single = approval_push.plan_body(_Plan('Rename the repo', [1]))
    assert single == 'Plan ready for your OK: Rename the repo (1 step)'


def test_a_plan_that_cannot_be_read_still_notifies():
    def broken(plan_id):
        raise OSError('store unavailable')

    assert approval_push.plan_notice(_Approval(), 'plan-1', lookup=broken).body == approval_push.PLAN_FALLBACK


@pytest.fixture
def phones(monkeypatch):
    sent: list[dict] = []

    async def push(title, body, **kwargs):
        sent.append({'title': title, 'body': body, **kwargs})

    monkeypatch.setattr(relay_push, 'notify_devices', push)
    return sent


@pytest.mark.asyncio
async def test_every_notification_is_redacted_whoever_built_it(phones):
    await notifications.deliver(notifications.Notice(
        kind='board', key='board:c1:done', title='Board · Rotate keys',
        body='done: new key is sk-live-AbCdEfGhIjKlMnOpQrSt and the old one ghp_1234567890abcdefghij',
    ))
    assert 'AbCdEf' not in repr(phones) and 'ghp_123' not in repr(phones)
    assert phones[0]['body'].startswith('done: new key is [redacted]')


@pytest.mark.asyncio
async def test_titles_and_bodies_are_bounded(phones):
    await notifications.deliver(notifications.Notice(kind='flowlet', key='flowlet:f', title='t' * 300, body='b' * 900))
    assert len(phones[0]['title']) <= notifications.TITLE_LIMIT
    assert len(phones[0]['body']) <= notifications.BODY_LIMIT


@pytest.mark.asyncio
async def test_a_named_bot_signs_its_notifications(phones, monkeypatch):
    import flowly.profile

    monkeypatch.setattr(flowly.profile, 'current_profile_display_name', lambda: 'Researcher')
    await approval_push.notify_approval_requested(_Pending('git status'))
    assert phones[0]['title'] == 'Researcher'


@pytest.mark.parametrize('kind,body', [
    ('approval', 'Needs your OK to run: cat ~/.ssh/config'),
    ('clarify', 'Send the contract to ali@example.com?'),
    ('plan', 'Plan ready for your OK: Fire the vendor'),
    ('cron', 'Revenue is down 12% this week'),
    ('chat', 'Here is the summary of the call'),
])
@pytest.mark.asyncio
async def test_a_minimal_preview_carries_no_content(phones, monkeypatch, tmp_path, kind, body):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'notifications': {'preview': 'minimal'}}), encoding='utf-8')
    monkeypatch.setattr('flowly.config.loader.load_config', lambda *a, **k: load_config(config))
    await notifications.deliver(notifications.Notice(kind=kind, key=f'{kind}:1', title='Weekly report', body=body))
    assert phones[0]['title'] == 'Flowly'
    assert phones[0]['body'] == notifications.MINIMAL_BODY[kind]
    assert body not in repr(phones) and 'Weekly report' not in repr(phones)


@pytest.mark.asyncio
async def test_a_minimal_preview_leaves_content_out_of_the_payload_too(phones, monkeypatch, tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'notifications': {'preview': 'minimal'}}), encoding='utf-8')
    monkeypatch.setattr('flowly.config.loader.load_config', lambda *a, **k: load_config(config))
    await notifications.deliver(notifications.Notice(
        kind='cron', key='cron:j:r', title='Payroll export', body='3 people unpaid',
        data={'jobId': 'j', 'jobName': 'Payroll export', 'runId': 'r'},
    ))
    assert phones[0]['data'] == {'type': 'cron', 'jobId': 'j', 'runId': 'r', 'eventKey': 'cron:j:r'}
    assert 'Payroll' not in repr(phones)


def test_an_unknown_preview_setting_falls_back_to_full(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'notifications': {'preview': 'off'}}), encoding='utf-8')
    assert load_config(config).notifications.preview == 'full'
