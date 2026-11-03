"""Thin authenticated loopback HTTP/SSE transport; OpenCode owns all agent execution."""
import base64
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

DISPATCH_GRACE_SECONDS = 10


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def snapshot_due(event, last_snapshot):
    """Reconcile lifecycle and question changes immediately; throttle other events to once a second."""
    kind = event.get('type', '')
    return (kind in {'server.connected', 'session.status', 'session.idle', 'session.error', 'message.updated'}
            or kind.startswith('question.') or time.monotonic() - last_snapshot >= 1)


def request_assistants(messages, message_id):
    """Follow only pinned native auto-compaction markers until the next explicit turn."""
    ordered = sorted(messages, key=lambda message: message['info']['id'])
    start = next((index for index, message in enumerate(ordered) if message['info']['id'] == message_id), None)
    if start is None:
        return []  # No ancestry proof: never borrow an arbitrary later assistant answer.
    parents, compactions, completed_compactions = {message_id}, set(), set()
    last_user = message_id
    candidates = []
    for message in ordered[start + 1:]:
        info, parts = message['info'], message['parts']
        if info.get('role') == 'user':
            automatic = any(part.get('type') == 'compaction' and part.get('auto') is True for part in parts)
            continuation = any(part.get('type') == 'text' and part.get('synthetic') is True
                and (part.get('metadata') or {}).get('compaction_continue') is True for part in parts)
            if automatic and last_user in parents | compactions:
                compactions.add(info['id'])
            elif continuation and last_user in completed_compactions:
                parents.add(info['id'])
            else:
                break  # A manual follow-up is a distinct request, even if its text is identical.
            last_user = info['id']
        elif info.get('role') == 'assistant':
            if info.get('summary'):
                if info.get('parentID') in compactions:
                    if info.get('error'):
                        candidates.append(message)  # Owned compaction failure, never a final answer.
                    elif info.get('finish') == 'stop' and info.get('time', {}).get('completed'):
                        completed_compactions.add(info['parentID'])
                continue  # A compaction summary is not the delegated task's final answer.
            if info.get('parentID') in parents:
                candidates.append(message)
    return candidates


class NativeClient:
    def __init__(self, url, password, directory):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.username or parsed.path:
            raise ValueError('Native endpoint must be an authenticated HTTP loopback origin')
        self.url = url
        self.directory = str(directory)
        self.authorization = 'Basic ' + base64.b64encode(('opencode:' + password).encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.dispatch_context = {}

    def note_dispatch(self, session_id, message_id, acknowledged_at, error=None):
        """Attach an acknowledgement and any observed session error to this exact request."""
        context = self.dispatch_context.setdefault((session_id, message_id), {})
        context['acknowledged_at'] = acknowledged_at
        if error is not None:
            context['error'] = error

    def observe_dispatch(self, session_id, message_id, event):
        """Retain matching session errors without treating an event as dispatch acknowledgement."""
        props = event.get('properties', {})
        context = self.dispatch_context.get((session_id, message_id))
        if (context is not None and event.get('type') == 'session.error'
                and props.get('sessionID') == session_id
                and props.get('messageID') == message_id and props.get('error')):
            context['error'] = props['error']

    def open(self, method, path, body=None, timeout=15):
        query = urllib.parse.urlencode({'directory': self.directory})
        url = self.url + path + ('&' if '?' in path else '?') + query
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': self.authorization, 'Content-Type': 'application/json'})
        return self.opener.open(request, timeout=timeout)

    def request(self, method, path, body=None):
        with self.open(method, path, body) as response:
            data = response.read()
            return json.loads(data) if data else None

    def create_session(self, title='Delegated worker'):
        return self.request('POST', '/session', {'title': title})['id']

    def stage(self, session_id, task, model, reasoning):
        provider, model_id = model.split('/', 1)
        payload = {'agent': 'worker', 'model': {'providerID': provider, 'modelID': model_id},
                   'variant': reasoning, 'parts': [{'type': 'text', 'text': task}]}
        # Obtain the native ID without generating an ID or starting provider work.
        receipt = self.request('POST', f'/session/{session_id}/message', {**payload, 'noReply': True})
        payload['parts'] = [{'id': p['id'], 'type': 'text', 'text': p['text']}
                            for p in receipt['parts'] if p.get('type') == 'text']
        return receipt['info']['id'], payload

    def dispatch(self, session_id, message_id, payload):
        # The same native message ID is updated, not appended as a second user task.
        result = self.request('POST', f'/session/{session_id}/prompt_async', {**payload, 'messageID': message_id})
        self.note_dispatch(session_id, message_id, time.time())
        return result

    def questions(self, session_id):
        return [q for q in self.request('GET', '/question') if q['sessionID'] == session_id]

    def reply(self, session_id, question_id, answers):
        if question_id not in {q['id'] for q in self.questions(session_id)}:
            raise ValueError('Question is not pending for this worker session')
        return self.request('POST', f'/question/{question_id}/reply', {'answers': answers})

    def abort(self, session_id):
        return self.request('POST', f'/session/{session_id}/abort', {})

    def messages_since(self, session_id, message_id):
        path = f'/session/{session_id}/message?limit=100'
        messages, seen = {}, set()
        while True:
            with self.open('GET', path) as response:
                page = json.load(response)
                cursor = response.headers.get('X-Next-Cursor')
            messages.update((message['info']['id'], message) for message in page)
            if message_id in messages or not cursor:
                return list(messages.values())
            if cursor in seen:
                raise ValueError('Native message pagination did not advance; request ancestry is unknown')
            seen.add(cursor)
            path = f'/session/{session_id}/message?limit=100&before=' + urllib.parse.quote(cursor, safe='')

    def snapshot(self, session_id, message_id):
        messages = self.messages_since(session_id, message_id)
        questions = self.questions(session_id)
        native = self.request('GET', '/session/status').get(session_id, {'type': 'idle'})['type']
        if native == 'idle':
            # Idle observed after the first read can belong to a newly finished
            # compaction/continuation. Reconcile once behind that observation.
            messages = self.messages_since(session_id, message_id)
            questions = self.questions(session_id)
            native = self.request('GET', '/session/status').get(session_id, {'type': 'idle'})['type']
        candidates = request_assistants(messages, message_id)
        result = {'session_id': session_id, 'request_id': message_id, 'status': 'running' if native != 'idle' else 'queued',
                  'native_status': native, 'text': '', 'questions': questions,
                  'execution_observed': any(not item['info'].get('summary') for item in candidates)}
        if questions:
            result['status'] = 'needs_input'
        if not candidates:
            context = self.dispatch_context.get((session_id, message_id))
            if not questions and native == 'idle' and context is not None:
                error = context.get('error')
                if error or time.time() - context['acknowledged_at'] >= DISPATCH_GRACE_SECONDS:
                    result.update(status='incomplete', incomplete_reason='dispatched_without_execution')
                    if error:
                        result['error'] = error
                        result['error_attribution'] = 'explicit_message_id'
            return result  # Staging/initial idle without acknowledgement is never completion.
        latest = max(candidates, key=lambda item: item['info']['id'])
        info, parts = latest['info'], latest['parts']
        if not info.get('summary'):
            result['text'] = '\n\n'.join(p.get('text', '') for p in parts if p.get('type') == 'text')
        if questions:
            return result
        if info.get('error'):
            error = info['error']
            result.update(status='interrupted' if error.get('name') in ('MessageAbortedError', 'AbortedError') else 'failed', error=error)
            return result
        finish = info.get('finish')
        result['finish_reason'] = finish
        if not info.get('time', {}).get('completed'):
            return result
        tools = [p.get('state', {}) for p in parts if p.get('type') == 'tool']
        if finish == 'length':
            result.update(status='incomplete', incomplete_reason='output_limit_reached')
        elif any(s.get('status') in ('pending', 'running') for s in tools):
            if native == 'idle':
                result.update(status='incomplete', incomplete_reason='unfinished_tool_call')
        elif any(s.get('status') == 'error' and 'abort' in str(s.get('error', '')).lower() for s in tools):
            if native == 'idle':
                result.update(status='incomplete', incomplete_reason='aborted_tool_call')
        elif finish == 'stop' and not tools and result['text'].strip():
            # Native auto-compaction may continue after a completed stop message.
            # That text is provisional until the native runner is quiescent.
            if native == 'idle':
                result['status'] = 'complete'
        elif native == 'idle' and not questions:
            result.update(status='incomplete', incomplete_reason='missing_terminal_text_or_stop')
        return result

    def events(self):
        with self.open('GET', '/event', timeout=45) as response:
            data = []
            for raw in response:
                line = raw.decode().rstrip('\r\n')
                if line.startswith('data:'):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    yield json.loads('\n'.join(data))
                    data = []

    def wait(self, session_id, message_id, observer=None, timeout=None):
        started = time.monotonic()
        terminal = {'complete', 'incomplete', 'failed', 'interrupted', 'needs_input'}
        # Connect before snapshot so a completion between attach and snapshot is retained.
        while True:
            try:
                stream = self.events()
                next(stream)  # Native server.connected establishes the SSE subscription.
                try:
                    result = self.snapshot(session_id, message_id)
                    if observer:
                        observer(result)
                    if result['status'] in terminal:
                        return result
                    last_snapshot = time.monotonic()
                    for event in stream:
                        self.observe_dispatch(session_id, message_id, event)
                        if observer:
                            observer(event)
                        if timeout is not None and time.monotonic() - started >= timeout:
                            return {**result, 'wait_timed_out': True}
                        if not snapshot_due(event, last_snapshot):
                            continue
                        # Reconcile retained native state; no replay on transport reconnect.
                        result = self.snapshot(session_id, message_id)
                        last_snapshot = time.monotonic()
                        if result['status'] in terminal:
                            if observer:
                                observer(result)
                            return result
                finally:
                    stream.close()
            except (ConnectionError, socket.timeout, urllib.error.URLError):
                result = self.snapshot(session_id, message_id)  # Raises if the backend is gone.
                if result['status'] in terminal:
                    return result
                time.sleep(0.2)


def live_settings(settings, *, allow_commands=False, long_context=False, shell_plugin=None):
    settings = json.loads(json.dumps(settings))
    settings['permission']['question'] = 'allow'
    settings['agent']['worker']['permission']['question'] = 'allow'
    if allow_commands:
        if not shell_plugin:
            raise ValueError('Command capability requires the controlled shell environment hook')
        settings['permission']['bash'] = 'allow'
        settings['agent']['worker']['permission']['bash'] = 'allow'
        settings['plugin'] = [shell_plugin]
    model_id = settings['model'].split('/', 1)[1]
    limits = settings['provider']['openai']['models'][model_id]['limit']
    limits['input'] = limits['context']  # Truthful model-context input upper bound, not a smaller capacity claim.
    if long_context:
        settings['compaction'].pop('reserved', None)
    else:
        threshold = min(250000, max(1, limits['context'] - 32000))
        settings['compaction']['reserved'] = limits['input'] - threshold
    return settings
