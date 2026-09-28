"""A narrow authenticated RPC facade; the shared Board remains the task owner."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from flowly.board.store import BoardError
from flowly.live_voice.access import VoicePrincipal
from flowly.live_voice.authority import RequestOwner
from flowly.live_voice.sessions import VoiceError, VoiceSessions, bounded_text, identity, integer
from flowly.profile import ensure_profile_bot_id, profile_exists, validate_profile_name

METHODS = frozenset({
    'voice.open', 'voice.get', 'voice.history', 'voice.append', 'voice.end', 'voice.delete', 'voice.list',
    'voice.chats.open', 'voice.tasks.dispatch', 'voice.tasks.events', 'voice.notice', 'voice.work.list', 'voice.language',
    'voice.tasks.get', 'voice.tasks.prepare', 'voice.tasks.steer', 'voice.tasks.cancel',
    'voice.tasks.requests', 'voice.tasks.respond', 'voice.focus', 'voice.exec', 'voice.attachments',
    'voice.tools.record', 'voice.attachments.read',
})


def target(params: dict) -> tuple[str, str]:
    profile = bounded_text(params.get('profile'), 'profile', maximum=64)
    bot_id = bounded_text(params.get('botId'), 'botId', maximum=128)
    try:
        if profile != 'default':
            validate_profile_name(profile)
        if not profile_exists(profile):
            raise ValueError('Unknown agent')
        info = ensure_profile_bot_id(profile)
    except (ValueError, FileNotFoundError) as exc:
        raise VoiceError('TARGET_NOT_FOUND', 'Select an available agent before starting voice.') from exc
    if info.bot_id != bot_id:
        raise VoiceError('TARGET_CONFLICT', 'The selected agent identity has changed. Select it again.')
    return profile, bot_id


class LiveVoiceService:
    def __init__(self, sessions: VoiceSessions, board: Callable[[], tuple[Any, Any]],
                 worker: Callable[[], Any] | None = None, executor: Callable[[], Any] | None = None):
        self.sessions = sessions
        self.board = board
        self.worker = worker
        self.executor = executor

    def for_principal(self, principal: VoicePrincipal | None) -> LiveVoiceService:
        """Keep simultaneous account requests out of each other's mutable state."""
        return LiveVoiceService(self.sessions.for_principal(principal), self.board, self.worker, self.executor)

    def for_owner(self, owner: RequestOwner) -> LiveVoiceService:
        return LiveVoiceService(self.sessions.for_owner(owner), self.board, self.worker, self.executor)

    def call(self, method: str, params: dict) -> dict | Awaitable[dict]:
        if method == 'voice.exec':
            return self._exec(params)
        if method == 'voice.attachments':
            from flowly.live_voice.attachments import store_attachments

            return store_attachments(self.sessions, params)
        if method == 'voice.attachments.read':
            from flowly.live_voice.attachments import read_attachment

            return read_attachment(self.sessions, params)
        if method == 'voice.tools.record':
            from flowly.live_voice.tool_records import record_tool

            return record_tool(self.sessions, params)
        if method == 'voice.chats.open':
            return self._open_chat(params)
        if method == 'voice.tasks.prepare':
            return self._prepare_task_chat(params)
        if method in {'voice.tasks.requests', 'voice.tasks.respond'}:
            return self._requests(method, params)
        if method == 'voice.list':
            return self.sessions.list(params)
        if method == 'voice.delete':
            conversation_id = identity(params.get('conversationId'), 'conversationId')
            store, _ = self.board()
            preserve_work = False
            if store is not None:
                try:
                    preserve_work = bool(store.voice_events(conversation_id, after=0, limit=1)['cards'])
                except BoardError as exc:
                    raise VoiceError('UNAVAILABLE', 'Task history is not available.') from exc
            return self.sessions.delete(params, preserve_work=preserve_work)
        if method == 'voice.work.list':
            offset = integer(params.get('offset', 0), 'offset', maximum=100_000)
            limit = integer(params.get('limit', 100), 'limit', minimum=1, maximum=100)
            store, _ = self.board()
            if store is None:
                raise VoiceError('UNAVAILABLE', 'Chat discovery is not ready on this agent host.')
            cards = store.list_voice_work(offset=offset, limit=limit + 1)
            conversations = {}
            entries = []
            for card in cards[:limit]:
                conversation_id = card.voice_conversation_id
                if conversation_id not in conversations:
                    try:
                        conversations[conversation_id] = self.sessions.get_for_work(conversation_id)
                    except VoiceError as error:
                        if error.code != 'NOT_FOUND':
                            raise
                        conversations[conversation_id] = None
                conversation = conversations[conversation_id]
                if conversation is not None:
                    public = card.to_dict()
                    summary = {key: public[key] for key in (
                        'id', 'title', 'status', 'revision', 'sessionKey', 'assigneeProfile',
                        'assigneeBotId', 'updatedAt', 'voiceConversationId',
                    )}
                    entries.append({'card': summary, 'conversation': conversation})
            return {'workChats': entries,
                    'nextOffset': offset + limit if len(cards) > limit else None}
        if method == 'voice.open':
            profile, bot_id = target(params)
            return self.sessions.open(params, profile=profile, bot_id=bot_id)
        if method == 'voice.language':
            return self.sessions.set_language(params)
        if method == 'voice.get':
            return self.sessions.get(params.get('conversationId'))
        if method == 'voice.history':
            return self._history(params)
        if method == 'voice.append':
            return self.sessions.append(params)
        if method == 'voice.end':
            return self.sessions.end(params)
        if method == 'voice.focus':
            self.sessions.require_connection(params)
            task_id = params.get('taskId')
            if task_id is not None:
                task_id = bounded_text(task_id, 'taskId', maximum=128)
                store, _ = self.board()
                if store is None:
                    raise VoiceError('UNAVAILABLE', 'Task history is not available.')
                try:
                    store.voice_commands.list(params['conversationId'], task_id)
                except BoardError as exc:
                    raise VoiceError('TASK_CONFLICT', str(exc)) from exc
            return self.sessions.focus(params, task_id)
        if method == 'voice.notice':
            self.sessions.get_for_work(params.get('conversationId'))
            event_id = integer(params.get('eventId'), 'eventId', minimum=1)
            store, _ = self.board()
            if store is None:
                raise VoiceError('UNAVAILABLE', 'Task history is not available.')
            event = store.voice_event(params['conversationId'], event_id)
            if event is None or event['kind'] != 'run_finished':
                raise VoiceError('NOT_FOUND', 'Task completion notice not found in this conversation.')
            card = store.get_card(event['cardId'])
            expected_revision = params.get('expectedRevision')
            if expected_revision is not None:
                expected_revision = integer(expected_revision, 'expectedRevision', minimum=0)
            if params.get('delivery') == 'started' and (
                card.status not in {'done', 'cancelled', 'blocked', 'review'}
                or event['payload'].get('attempt') != card.attempt_count
                or event['payload'].get('status') != card.status
                or (expected_revision is not None and expected_revision != card.revision)
            ):
                return {'accepted': False, 'reason': 'superseded'}
            return self.sessions.notice(params, task_id=event['cardId'], event_id=event_id)
        if method == 'voice.tasks.dispatch':
            self.sessions.require_connection(params)
            profile, bot_id = target(params)
            command_id = identity(params.get('commandId'), 'commandId')
            title = bounded_text(params.get('title'), 'title', maximum=200)
            body = bounded_text(params.get('body', ''), 'body', maximum=30000, empty=True)
            store, orchestrator = self.board()
            if store is None or orchestrator is None:
                raise VoiceError('UNAVAILABLE', 'Task execution is not ready on this agent host.')
            try:
                card = orchestrator.dispatch_voice(
                    conversation_id=params['conversationId'], command_id=command_id,
                    profile=profile, title=title, body=body,
                    expected_bot_id=bot_id,
                )
            except BoardError as exc:
                raise VoiceError('TASK_CONFLICT', str(exc)) from exc
            # This is a durable acceptance receipt, never a completion claim.
            return {'status': 'accepted', 'card': card.to_dict()}
        if method == 'voice.tasks.events':
            self.sessions.get_for_work(params.get('conversationId'))
            after = integer(params.get('after', 0), 'after')
            limit = integer(params.get('limit', 100), 'limit', minimum=1, maximum=200)
            store, _ = self.board()
            if store is None:
                raise VoiceError('UNAVAILABLE', 'Task history is not ready on this agent host.')
            return store.voice_events(params['conversationId'], after=after, limit=limit)
        if method in {'voice.tasks.get', 'voice.tasks.steer', 'voice.tasks.cancel'}:
            self.sessions.get_for_work(params.get('conversationId'))
            card_id = bounded_text(params.get('taskId'), 'taskId', maximum=128)
            store, orchestrator = self.board()
            if store is None:
                raise VoiceError('UNAVAILABLE', 'Task history is not ready on this agent host.')
            try:
                # The ledger verifies conversation membership before disclosing
                # the card or accepting any instruction.
                commands = store.voice_commands.list(params['conversationId'], card_id)
                if method == 'voice.tasks.get':
                    from flowly.live_voice.work_readiness import work_session_state

                    card = store.get_card(card_id)
                    return {'card': card.to_dict(), 'commands': commands, 'workSessionState': work_session_state(card)}
                self.sessions.require_connection(params)
                if orchestrator is None:
                    raise VoiceError('UNAVAILABLE', 'Task execution is not ready on this agent host.')
                if method == 'voice.tasks.cancel':
                    receipt = store.voice_commands.cancel(
                        conversation_id=params['conversationId'], card_id=card_id,
                        command_id=identity(params.get('commandId'), 'commandId'),
                        expected_revision=integer(params.get('expectedRevision'), 'expectedRevision'),
                    )
                    orchestrator.wake_dispatcher()
                    return {'command': receipt, 'card': store.get_card(card_id).to_dict()}
                receipt = store.voice_commands.enqueue(
                    conversation_id=params['conversationId'], card_id=card_id,
                    command_id=identity(params.get('commandId'), 'commandId'),
                    expected_revision=integer(params.get('expectedRevision'), 'expectedRevision'),
                    text=bounded_text(params.get('text'), 'text', maximum=30000),
                )
                orchestrator.wake_dispatcher()
                return {'command': receipt, 'card': store.get_card(card_id).to_dict()}
            except BoardError as exc:
                raise VoiceError('TASK_CONFLICT', str(exc)) from exc
        raise VoiceError('UNKNOWN_METHOD', 'Unknown voice operation.')

    def _history(self, params: dict) -> dict:
        from flowly.exec.approval_manager import get_approval_manager
        from flowly.exec.wire import approval_to_wire

        result = self.sessions.history(params)
        runner = self.executor() if self.executor is not None else None
        conversation_id = result['conversation']['conversationId']
        for record in result['tools']:
            # Only this process can execute voice commands. A running record
            # it does not own was lost with a previous process.
            if record['status'] == 'running' and (runner is None or not runner.running(conversation_id, record['id'])):
                record['status'] = 'interrupted'
        # Catch-up for every transport (Relay has no approval list RPC). The
        # manager hides requests the calling owner may not answer.
        key = result['conversation']['sessionKey']
        result['pendingApprovals'] = [approval_to_wire(pending) for pending in get_approval_manager().list_pending()
                                      if pending.session_key == key][:20] if not params.get('offset') else []
        return result

    async def _exec(self, params: dict) -> dict:
        """Run one command with the selected agent's ordinary exec tool and policy."""
        from flowly.live_voice.exec import exec_arguments

        conversation = self.sessions.require_connection(params)
        profile, bot_id = target(params)
        if (conversation['profile'], conversation['botId']) != (profile, bot_id):
            raise VoiceError('TARGET_CONFLICT', 'Commands run on this conversation\'s agent.')
        runner = self.executor() if self.executor is not None else None
        if runner is None:
            raise VoiceError('UNAVAILABLE', 'Command execution is not ready on this agent host.')
        if runner.profile() != profile:
            # Voice conversations are owned by the primary runtime. A named
            # profile's worker has no ownership record to scope approvals.
            raise VoiceError('UNSUPPORTED_TARGET', 'Voice commands run only on the primary agent. Dispatch a task instead.')
        arguments = exec_arguments(params)
        record, created = self.sessions.begin_tool(params, name='exec', arguments=arguments)
        return await runner.run(self.sessions, params['conversationId'], record, created)

    async def _prepare_task_chat(self, params: dict) -> dict:
        """Open an existing owned chat independently of worker capacity.

        Only the authorized ledger selects the runtime and canonical session.
        This idempotent reservation neither claims work nor sends a user turn;
        it also works after the originating voice connection has ended.
        """
        from flowly.live_voice.work_readiness import work_session_state
        from flowly.profile_host_contract import ProfileHostError

        self.sessions.get_for_work(params.get('conversationId'))
        card_id = bounded_text(params.get('taskId'), 'taskId', maximum=128)
        store, _ = self.board()
        if store is None:
            raise VoiceError('UNAVAILABLE', 'Chat preparation is not ready on this agent host.')
        try:
            store.voice_commands.list(params['conversationId'], card_id)
            card = store.get_card(card_id)
            state = work_session_state(card)
            if state == 'not_created':
                host = self.worker() if self.worker is not None else None
                if host is None:
                    raise VoiceError('UNAVAILABLE', 'The selected chat runtime is unavailable.')
                result = await host._target_rpc(card.assignee_profile, 'runtime.voice.reserve', {
                    'sessionKey': card.session_key, 'expectedBotId': card.assignee_bot_id,
                }, 15)
                if not isinstance(result, dict) or result.get('reserved') is not True or result.get('sessionKey') != card.session_key:
                    raise VoiceError('UNAVAILABLE', 'Chat reservation could not be confirmed.')
            # Revalidate ownership and identity after the asynchronous hop.
            self.sessions.get_for_work(params.get('conversationId'))
            commands = store.voice_commands.list(params['conversationId'], card_id)
            fresh = store.get_card(card_id)
            if (fresh.assignee_profile, fresh.assignee_bot_id, fresh.session_key) != (
                card.assignee_profile, card.assignee_bot_id, card.session_key,
            ):
                raise VoiceError('TARGET_CONFLICT', 'The selected agent identity has changed.')
            return {'card': fresh.to_dict(), 'commands': commands, 'workSessionState': work_session_state(fresh)}
        except BoardError as exc:
            raise VoiceError('TASK_CONFLICT', str(exc)) from exc
        except (ProfileHostError, asyncio.TimeoutError) as exc:
            raise VoiceError('UNAVAILABLE', 'The selected chat runtime is unavailable.') from exc

    async def _open_chat(self, params: dict) -> dict:
        """Reserve an owned empty conversation. Never send a synthetic user turn.

        The Board's idempotency record is persisted before reservation. A lost
        receipt retries that same reservation, without queuing work or charging
        an LLM. A later explicit steer uses the ordinary instruction ledger.
        """
        from flowly.profile_host_contract import ProfileHostError

        self.sessions.require_connection(params)
        profile, bot_id = target(params)
        command_id = identity(params.get('commandId'), 'commandId')
        title = bounded_text(params.get('title'), 'title', maximum=200)
        store, orchestrator = self.board()
        host = self.worker() if self.worker is not None else None
        if store is None or orchestrator is None or host is None:
            raise VoiceError('UNAVAILABLE', 'Chat creation is not ready on this agent host.')
        try:
            card = orchestrator.dispatch_voice(
                conversation_id=params['conversationId'], command_id=command_id,
                profile=profile, title=title, body='', expected_bot_id=bot_id, open_only=True,
            )
            result = await host._target_rpc(profile, 'runtime.voice.reserve', {
                'sessionKey': card.session_key, 'expectedBotId': bot_id,
            }, 15)
            if not isinstance(result, dict) or result.get('reserved') is not True or result.get('sessionKey') != card.session_key:
                raise VoiceError('UNAVAILABLE', 'Chat reservation could not be confirmed.')
            self.sessions.require_connection(params)
            target(params)
            return {'status': 'opened', 'card': store.get_card(card.id).to_dict()}
        except BoardError as exc:
            raise VoiceError('TASK_CONFLICT', str(exc)) from exc
        except ProfileHostError as exc:
            raise VoiceError('UNAVAILABLE', 'The selected chat runtime is unavailable.') from exc

    async def _requests(self, method: str, params: dict) -> dict:
        from flowly.profile_host_contract import ProfileHostError

        self.sessions.get_for_work(params.get('conversationId'))
        store, _ = self.board()
        host = self.worker() if self.worker is not None else None
        if store is None or host is None:
            raise VoiceError('UNAVAILABLE', 'Task requests are not available on this agent host.')
        task_id = bounded_text(params.get('taskId'), 'taskId', maximum=128)
        try:
            store.voice_commands.list(params['conversationId'], task_id)
            card = store.get_card(task_id)
            worker_params = {'sessionKey': card.session_key, 'expectedBotId': card.assignee_bot_id}
            if method == 'voice.tasks.requests':
                snapshot = await host._target_rpc(card.assignee_profile, 'chat.inflight', worker_params, 10)
                run = snapshot.get('inflight')
                requests = []
                if run and run.get('runId'):
                    result = await host._target_rpc(card.assignee_profile, 'chat.requests',
                                                    {**worker_params, 'runId': run['runId']}, 10)
                    requests = result['requests']
                return {'card': store.get_card(task_id).to_dict(), 'requests': requests}
            self.sessions.require_connection(params)
            payload = {
                'runId': bounded_text(params.get('runId'), 'runId', maximum=256),
                'requestId': bounded_text(params.get('requestId'), 'requestId', maximum=128),
                'requestRevision': bounded_text(params.get('requestRevision'), 'requestRevision', maximum=64),
                'requestType': params.get('requestType'),
            }
            if payload['requestType'] == 'clarify':
                payload['answer'] = bounded_text(params.get('answer'), 'answer', maximum=4000)
            elif payload['requestType'] == 'approval' and params.get('decision') in {'allow-once', 'deny'}:
                payload['decision'] = params['decision']
            else:
                raise VoiceError('INVALID_PARAMS', 'A valid question answer or one-time approval decision is required.')
            command_id = identity(params.get('commandId'), 'commandId')
            created, receipt = store.voice_commands.response_intent(
                conversation_id=params['conversationId'], card_id=task_id, command_id=command_id,
                expected_revision=integer(params.get('expectedRevision'), 'expectedRevision'), payload=payload,
            )
            if not created:
                return {'command': receipt, 'card': store.get_card(task_id).to_dict()}
            try:
                result = await host._target_rpc(card.assignee_profile, 'chat.respond', {**worker_params, **payload}, 10)
                status = 'applied' if result.get('status') == 'applied' else 'status_unknown'
            except ProfileHostError as exc:
                status = 'rejected' if exc.code in {'STALE_REQUEST', 'INVALID_PARAMS', 'TASK_TARGET_CHANGED'} else 'status_unknown'
            except asyncio.CancelledError:
                store.voice_commands.settle_response(command_id, 'status_unknown')
                raise
            except Exception:
                status = 'status_unknown'
            receipt = store.voice_commands.settle_response(command_id, status)
            return {'command': receipt, 'card': store.get_card(task_id).to_dict()}
        except BoardError as exc:
            raise VoiceError('TASK_CONFLICT', str(exc)) from exc
        except ProfileHostError as exc:
            raise VoiceError(exc.code, str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise VoiceError('UNAVAILABLE', 'The worker did not respond. Check the work conversation.') from exc
