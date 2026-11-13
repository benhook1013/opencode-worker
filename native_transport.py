"""Thin authenticated loopback HTTP/SSE transport; OpenCode owns all agent execution."""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

DISPATCH_GRACE_SECONDS = 10
REQUEST_MARKER = 'opencode_worker_request_id'


def boot_id():
    """Identify the monotonic clock epoch; unavailable identity must never imply comparability."""
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except (OSError, UnicodeError):
        return None


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
    overflow_compactions = set()
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
                if any(part.get('type') == 'compaction' and part.get('auto') is True
                       and part.get('overflow') is True for part in parts):
                    overflow_compactions.add(info['id'])
            elif continuation and last_user in completed_compactions:
                parents.add(info['id'])
            elif (last_user in completed_compactions & overflow_compactions
                  and any(part.get('type') == 'text'
                          and (part.get('metadata') or {}).get(REQUEST_MARKER) == message_id for part in parts)):
                # Pinned native overflow replay copies original part metadata, not IDs.
                parents.add(info['id'])
            else:
                break  # A manual follow-up is a distinct request, even if its text is identical.
            last_user = info['id']
        elif info.get('role') == 'assistant':
            if info.get('summary'):
                if info.get('parentID') in compactions:
                    if info.get('error'):
                        candidates.append(message)  # Owned compaction failure, never a final answer.
                    elif info.get('finish') in {'stop', 'length'} and info.get('time', {}).get('completed'):
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
        self.deadline = None
        self.idle_unfinished = {}
        self.restored_idle_requests = set()

    def restore_idle_evidence(self, session_id, message_id, result):
        """Reuse request-specific stability only within the same boot clock epoch."""
        key = (session_id, message_id)
        if key in self.restored_idle_requests or key in self.idle_unfinished:
            return
        self.restored_idle_requests.add(key)
        evidence = result.get('idle_unfinished_evidence') or {}
        epoch = boot_id()
        if (result.get('session_id') == session_id and result.get('request_id') == message_id
                and result.get('native_status') == 'idle' and epoch
                and evidence.get('boot_id') == epoch
                and isinstance(evidence.get('signature'), str)
                and isinstance(evidence.get('since'), (int, float))
                and 0 <= evidence['since'] <= time.monotonic()):
            self.idle_unfinished[(session_id, message_id)] = (evidence['signature'], evidence['since'])

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
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Native observation deadline expired')
            timeout = min(timeout, remaining)
        return self.opener.open(request, timeout=timeout)

    def request(self, method, path, body=None):
        try:
            with self.open(method, path, body) as response:
                data = response.read()
                return json.loads(data) if data else None
        except (http.client.HTTPException, UnicodeError, json.JSONDecodeError) as error:
            raise ConnectionError('Native JSON response was incomplete or invalid') from error

    def create_session(self, title='Delegated worker'):
        return self.request('POST', '/session', {'title': title})['id']

    def stage(self, session_id, task, model, reasoning):
        provider, model_id = model.split('/', 1)
        payload = {'agent': 'worker', 'model': {'providerID': provider, 'modelID': model_id},
                   'variant': reasoning, 'parts': [{'type': 'text', 'text': task}]}
        # Obtain the native ID without generating an ID or starting provider work.
        receipt = self.request('POST', f'/session/{session_id}/message', {**payload, 'noReply': True})
        payload['parts'] = [{'id': p['id'], 'type': 'text', 'text': p['text'],
                             'metadata': {**(p.get('metadata') or {}), REQUEST_MARKER: receipt['info']['id']}}
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
            try:
                with self.open('GET', path) as response:
                    page = json.load(response)
                    cursor = response.headers.get('X-Next-Cursor')
            except (http.client.HTTPException, UnicodeError, json.JSONDecodeError) as error:
                raise ConnectionError('Native message response was incomplete or invalid') from error
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
        observed_at_ns = time.monotonic_ns()
        candidates = request_assistants(messages, message_id)
        result = {'session_id': session_id, 'request_id': message_id, 'status': 'running' if native != 'idle' else 'queued',
                  'native_status': native, 'text': '', 'questions': questions, 'observed_at_ns': observed_at_ns, 'observed_boot_id': boot_id(),
                  'execution_observed': bool(candidates)}
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
        key = (session_id, message_id)
        if not info.get('time', {}).get('completed'):
            if native == 'idle':
                signature = hashlib.sha256(json.dumps(latest, sort_keys=True).encode()).hexdigest()
                previous = self.idle_unfinished.get(key)
                if previous is None or previous[0] != signature:
                    self.idle_unfinished[key] = (signature, time.monotonic())
                elif time.monotonic() - previous[1] >= DISPATCH_GRACE_SECONDS:
                    result.update(status='incomplete', incomplete_reason='idle_unfinished_assistant')
                result['idle_unfinished_evidence'] = {
                    'signature': signature, 'since': self.idle_unfinished[key][1],
                    'boot_id': result['observed_boot_id']}
            else:
                self.idle_unfinished.pop(key, None)
            return result
        self.idle_unfinished.pop(key, None)
        tools = [p.get('state', {}) for p in parts if p.get('type') == 'tool']
        if finish == 'length':
            if native == 'idle':
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

    def events(self, timeout=45):
        with self.open('GET', '/event', timeout=min(timeout, 1) if self.deadline else timeout) as response:
            data = []
            try:
                for raw in response:
                    if self.deadline is not None and time.monotonic() >= self.deadline:
                        raise TimeoutError('Native observation deadline expired')
                    line = raw.decode().rstrip('\r\n')
                    if line.startswith('data:'):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        yield json.loads('\n'.join(data))
                        data = []
            except http.client.HTTPException as error:
                raise ConnectionError(f'Native event stream ended: {error!r}') from error

    def wait(self, session_id, message_id, observer=None, timeout=None):
        old_deadline = self.deadline
        self.deadline = time.monotonic() + timeout if timeout is not None else None
        terminal = {'complete', 'incomplete', 'failed', 'interrupted', 'needs_input'}
        result = {'session_id': session_id, 'request_id': message_id, 'status': 'running'}
        try:
            while True:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    return {**result, 'wait_timed_out': True}
                stream = None
                try:
                    stream = self.events()
                    if next(stream, None) is None:
                        raise ConnectionError('Native event stream closed before subscription')
                    result = self.snapshot(session_id, message_id)
                    if observer:
                        observer(result)
                    if result['status'] in terminal:
                        return result
                    last_snapshot = time.monotonic()
                    for event in stream:
                        if self.deadline is not None and time.monotonic() >= self.deadline:
                            return {**result, 'wait_timed_out': True}
                        self.observe_dispatch(session_id, message_id, event)
                        if observer:
                            observer(event)
                        if not snapshot_due(event, last_snapshot):
                            continue
                        result = self.snapshot(session_id, message_id)
                        last_snapshot = time.monotonic()
                        if result['status'] in terminal:
                            if observer:
                                observer(result)
                            return result
                    raise ConnectionError('Native event stream closed')
                except (ConnectionError, socket.timeout, urllib.error.URLError):
                    if self.deadline is not None and time.monotonic() >= self.deadline:
                        return {**result, 'wait_timed_out': True}
                    result = self.snapshot(session_id, message_id)
                    if result['status'] in terminal:
                        return result
                    remaining = self.deadline - time.monotonic() if self.deadline else 0.2
                    time.sleep(max(0, min(0.2, remaining)))
                finally:
                    if stream is not None:
                        stream.close()
        finally:
            self.deadline = old_deadline


def compaction_reserved(limits):
    """Keep the 250k trigger while scaling headroom for smaller model windows."""
    context = limits['context']
    # Native 1.19.5 subtracts reserved from limit.input and compares total tokens.
    # Never let fixed headroom consume a small context window.
    headroom = min(32000, limits['output'], context // 4)
    return context - min(250000, context - headroom)


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
        settings['compaction']['reserved'] = compaction_reserved(limits)
    return settings
