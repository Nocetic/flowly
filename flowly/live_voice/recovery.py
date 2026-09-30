"""Passive task recovery from the selected profile's durable local stores."""
from pathlib import Path

from flowly.goals.store import GoalStore
from flowly.session.commands import ChatCommandStore
from flowly.session.manager import SessionManager


def _binding(receipt: dict | None) -> dict | None:
    if not isinstance(receipt, dict):
        return None
    value = receipt.get('goalBinding')
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != 1 or value.get('state') not in {'none', 'goal'}):
        return None
    if value['state'] == 'goal' and (
        not isinstance(value.get('goalId'), str) or not 1 <= len(value['goalId']) <= 512
        or type(value.get('revision')) is not int or value['revision'] < 0
    ):
        return None
    return value


def read_task_result(home: Path, session_key: str, run_id: str) -> dict:
    """Verify the complete command/goal chain; never dispatch or resume a turn."""
    unknown = {'runId': run_id, 'status': 'status_unknown'}
    commands_path = home / 'sessions' / 'chat_commands.sqlite3'
    receipt = ChatCommandStore.read_existing(commands_path, session_key, run_id)
    binding = _binding(receipt)
    if binding is None or receipt['status'] not in {'completed', 'aborted', 'error'}:
        return unknown
    final_run_id = run_id
    goal = None
    goals = None
    if binding['state'] == 'goal':
        if not (home / 'goals').is_dir():
            return unknown
        goals = GoalStore(home, lock_timeout=1)
        goal = goals.get_generation(session_key, binding['goalId'])
        if not goal or goal['revision'] < binding['revision']:
            return unknown
        if goal['status'] in {'active', 'paused'}:
            return {'runId': run_id, 'status': goal['status']}
        if goal['status'] == 'cleared':
            return {'runId': run_id, 'status': 'aborted'}
        if receipt['status'] != 'completed':
            return unknown
        final_run_id = goal.get('lastRunId')
        if not isinstance(final_run_id, str) or not final_run_id:
            return unknown
        final_receipt = ChatCommandStore.read_existing(commands_path, session_key, final_run_id)
        final_binding = _binding(final_receipt)
        if (not final_binding or final_receipt['status'] != 'completed'
                or final_binding.get('goalId') != binding['goalId'] or final_binding['state'] != 'goal'):
            return unknown
    elif receipt['status'] != 'completed':
        return {'runId': run_id, 'status': receipt['status']}

    message = SessionManager.read_run_result(home, session_key, final_run_id)
    if (not message or message.get('aborted') or message.get('failed')
            or isinstance(message.get('error'), dict)):
        return unknown
    content = message.get('content')
    if isinstance(content, str):
        response = content
    elif isinstance(content, list):
        response = '\n'.join(part['text'] for part in content if isinstance(part, dict)
                             and part.get('type') == 'text' and isinstance(part.get('text'), str))
    else:
        response = ''
    if not response.strip():
        return unknown
    if goals is not None and goals.get_generation(session_key, binding['goalId']) != goal:
        return unknown
    return {'runId': run_id, 'completedRunId': final_run_id, 'status': 'completed', 'response': response}
