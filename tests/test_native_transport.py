"""Real pinned OpenCode against an isolated loopback fake Responses provider only."""
import importlib.util
import argparse
import contextlib
import io
import json
import multiprocessing
import os
from unittest import mock
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

worker = module('native_fixture_worker', ROOT / 'worker.py')
transport = module('native_fixture_transport', ROOT / 'native_transport.py')
live = module('native_fixture_live', ROOT / 'live_worker.py')
router = module('native_fixture_router', ROOT / 'cli_router.py')


class FakeProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(body)
        if self.server.mode == 'delayed_headers':
            self.server.request_started.set()
            self.server.release.wait(30)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        def emit(kind, **fields):
            value = {'type': kind, **fields}
            self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps(value) + '\n\n').encode())
            self.wfile.flush()
        try:
            response = {'id': 'resp_fixture', 'object': 'response', 'created_at': 1, 'model': 'gpt-6-luna',
                        'status': 'in_progress', 'output': []}
            emit('response.created', response=response)
            if self.server.mode == 'delayed_output':
                self.server.request_started.set()
                self.server.release.wait(30)
            if self.server.mode == 'wait':
                self.server.release.wait(10)
            first = len(self.server.requests) == 1
            if self.server.mode == 'post_stop_compaction' and len(self.server.requests) == 2:
                self.server.compaction_started.set()
                self.server.release.wait(10)
            if self.server.mode in ('question', 'command', 'compaction') and first:
                name = {'question': 'question', 'command': 'bash', 'compaction': 'read'}[self.server.mode]
                args = {'questions': [{'question': 'Continue the fixture?', 'header': 'Fixture',
                        'options': [{'label': 'Proceed', 'description': 'Finish fixture'}]}]} if name == 'question' else {
                        'command': "test -z \"$OPENCODE_SERVER_PASSWORD\" && test -z \"$OPENAI_API_KEY\" && git status --short && git branch --show-current && python3 -c 'print(42)'",
                        'description': 'Read-only fixture Git and Python checks'}
                if name == 'read':
                    args = {'filePath': self.server.read_path}
                item = {'type': 'function_call', 'id': 'fc_fixture', 'call_id': 'call_fixture', 'name': name, 'arguments': ''}
                emit('response.output_item.added', output_index=0, item=item)
                emit('response.function_call_arguments.delta', output_index=0, item_id=item['id'], delta=json.dumps(args))
                item['arguments'] = json.dumps(args)
                item['status'] = 'completed'
                emit('response.function_call_arguments.done', output_index=0, item_id=item['id'], arguments=item['arguments'])
                emit('response.output_item.done', output_index=0, item=item)
            else:
                item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}
                emit('response.output_item.added', output_index=0, item=item)
                emit('response.output_text.delta', output_index=0, content_index=0, item_id=item['id'], delta=('Fixture final after continuation' if self.server.mode == 'post_stop_compaction' and not first else 'Fixture done'))
                item.update(status='completed', content=[{'type': 'output_text', 'text': 'Fixture final after continuation' if self.server.mode == 'post_stop_compaction' and not first else 'Fixture done', 'annotations': []}])
                emit('response.output_item.done', output_index=0, item=item)
            input_tokens = 200 if self.server.mode in ('compaction', 'post_stop_compaction') and first else 100
            response.update(status='completed', output=[item], usage={'input_tokens': input_tokens, 'output_tokens': 20,
                'total_tokens': input_tokens + 20, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}})
            emit('response.completed', response=response)
        except (BrokenPipeError, ConnectionResetError):
            pass


class NativeServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), FakeProvider)
        self.provider.daemon_threads = True
        self.provider.requests, self.provider.mode, self.provider.release = [], 'text', threading.Event()
        self.provider.compaction_started = threading.Event()
        self.provider.request_started = threading.Event()
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        self.addCleanup(self.provider.server_close)
        self.addCleanup(self.provider.shutdown)
        key = self.root / 'dummy-key'
        key.write_text('fixture-only')
        key.chmod(0o600)
        settings = transport.live_settings(worker.config('openai/gpt-6-luna', 'high', False, key=key))
        settings['provider']['openai']['options']['baseURL'] = f'http://127.0.0.1:{self.provider.server_port}/v1'
        if self._testMethodName in ('test_native_auto_compaction_preserves_logical_request', 'test_native_final_text_is_provisional_while_compaction_continues', 'test_native_idle_rereads_final_after_compaction_between_reads'):
            settings['compaction']['reserved'] = 1050000 - 150
        self.settings = settings
        env = worker.environment(self.root, settings)
        env['OPENCODE_SERVER_PASSWORD'] = 'fixture-only-server-auth'
        env['OPENCODE_ENABLE_QUESTION_TOOL'] = 'true'
        self.log = (self.root / 'native.log').open('wb')
        self.addCleanup(self.log.close)
        self.process = subprocess.Popen([str(worker.BINARY), 'serve', '--hostname', '127.0.0.1', '--port', '0'],
            cwd=self.project, env=env, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            text = (self.root / 'native.log').read_text()
            for line in text.splitlines():
                if line.startswith('opencode server listening on '):
                    self.client = transport.NativeClient(line.split(' on ', 1)[1], 'fixture-only-server-auth', self.project)
                    self.session = self.client.create_session()
                    return
            if self.process.poll() is not None:
                self.fail('Native fixture server exited: ' + text[-1000:])
            time.sleep(0.05)
        self.fail('Native fixture server startup timed out')

    def stop(self):
        self.provider.release.set()
        if self.process.poll() is None:
            os_group = self.process.pid
            signal_number = signal.SIGTERM
            import os
            os.killpg(os_group, signal_number)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os_group, signal.SIGKILL)
                self.process.wait()

    def send(self, text='Fixture task'):
        message, payload = self.client.stage(self.session, text, 'openai/gpt-6-luna', 'high')
        self.client.dispatch(self.session, message, payload)
        return message

    def test_native_async_identity_late_wait_and_followup(self):
        self.assertTrue(self.client.request('GET', '/global/health')['healthy'])
        config = self.client.request('GET', '/config')
        self.assertEqual(config['compaction']['reserved'], 800000)
        first = self.send()
        result = self.client.wait(self.session, first, timeout=15)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['text'], 'Fixture done')
        self.assertEqual(self.client.wait(self.session, first, timeout=15)['status'], 'complete')
        messages = self.client.request('GET', f'/session/{self.session}/message')
        self.assertEqual(sum(m['info']['id'] == first for m in messages), 1)
        staged = next(m for m in messages if m['info']['id'] == first)
        self.assertEqual([p['text'] for p in staged['parts'] if p.get('type') == 'text'], ['Fixture task'])
        second = self.send('Explicit follow-up')
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.wait(self.session, second, timeout=15)['status'], 'complete')

    def test_native_delayed_provider_headers_and_output_survive_dispatch_grace(self):
        """Real pinned native execution remains live through >10s of provider silence, without clock mocks."""
        for mode in ('delayed_headers', 'delayed_output'):
            with self.subTest(mode=mode):
                self.provider.mode = mode
                self.provider.release.clear()
                self.provider.request_started.clear()
                message = self.send('Fixture deliberately delayed provider ' + mode)
                self.assertTrue(self.provider.request_started.wait(10))
                # The provider holds before response headers, or before its first output item.
                # This is actual elapsed time across the unmodified production grace period.
                self.assertFalse(self.provider.release.wait(transport.DISPATCH_GRACE_SECONDS + 1))
                result = self.client.snapshot(self.session, message)
                self.assertEqual(result['status'], 'running', result)
                self.assertEqual(result['native_status'], 'busy', result)
                self.assertTrue(result['execution_observed'], result)
                self.assertEqual(result['request_id'], message)
                self.provider.release.set()
                result = self.client.wait(self.session, message, timeout=15)
                self.assertEqual(result['status'], 'complete', result)
                self.assertEqual(result['text'], 'Fixture done')

    def test_native_prior_session_error_observed_after_followup_ack_keeps_busy_work_running(self):
        """Delay observer handling of a real old error; this is controlled delivery, not a native race claim."""
        stream = self.client.events()
        next(stream)
        captured = []

        def capture():
            try:
                for event in stream:
                    if event.get('type') == 'session.error':
                        captured.append(event)
                        break
            finally:
                stream.close()

        listener = threading.Thread(target=capture, daemon=True)
        listener.start()
        message, payload = self.client.stage(self.session, 'Fixture prior invalid model', 'openai/gpt-6-luna', 'high')
        payload['model'] = {'providerID': 'fixture-missing-provider', 'modelID': 'fixture-missing-model'}
        self.client.dispatch(self.session, message, payload)
        listener.join(10)
        self.assertFalse(listener.is_alive(), 'Native session.error was not captured')
        self.assertEqual(len(captured), 1)
        self.assertNotIn('messageID', captured[0]['properties'])
        self.provider.mode = 'delayed_headers'
        newer = self.send('Fixture newer request after native startup error')
        self.assertTrue(self.provider.request_started.wait(10))
        # This is an actual native event, deliberately processed after newer acknowledgement.
        self.client.observe_dispatch(self.session, newer, captured[0])
        result = self.client.snapshot(self.session, newer)
        self.assertEqual(result['status'], 'running', result)
        self.assertEqual(result['native_status'], 'busy', result)
        self.assertNotIn('error', self.client.dispatch_context[(self.session, newer)])
        self.provider.release.set()
        self.assertEqual(self.client.wait(self.session, newer, timeout=15)['status'], 'complete')

    def test_native_auto_compaction_preserves_logical_request(self):
        fixture = self.project / 'fixture.txt'
        fixture.write_text('Read-only compaction fixture')
        self.provider.mode, self.provider.read_path = 'compaction', str(fixture)
        message = self.send('Read the fixture, then finish')
        result = self.client.wait(self.session, message, timeout=15)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['request_id'], message)
        self.assertEqual(result['text'], 'Fixture done')
        messages = self.client.request('GET', f'/session/{self.session}/message')
        self.assertTrue(any(m['info'].get('summary') for m in messages))
        self.assertTrue(any(p.get('metadata', {}).get('compaction_continue') is True for m in messages for p in m['parts']))
        final = next(m for m in messages if m['info'].get('finish') == 'stop' and not m['info'].get('summary'))
        self.assertNotEqual(final['info']['parentID'], message)
        self.assertEqual(len(self.provider.requests), 3)  # Tool request, native summary, native continuation.
        self.assertEqual(self.client.wait(self.session, message, timeout=15)['status'], 'complete')

    def test_native_final_text_is_provisional_while_compaction_continues(self):
        self.provider.mode = 'post_stop_compaction'
        request = self.send('Read-only fixture with no commands')
        self.assertTrue(self.provider.compaction_started.wait(10))
        provisional = self.client.snapshot(self.session, request)
        self.assertEqual(provisional['finish_reason'], 'stop')
        self.assertEqual(provisional['text'], 'Fixture done')
        self.assertEqual(provisional['native_status'], 'busy')
        self.assertEqual(provisional['status'], 'running')
        self.provider.release.set()
        result = self.client.wait(self.session, request, timeout=15)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['native_status'], 'idle')
        self.assertEqual(result['text'], 'Fixture final after continuation')
        self.assertEqual(result['request_id'], request)
        self.assertEqual(len(self.provider.requests), 3)
        messages = self.client.request('GET', f'/session/{self.session}/message')
        explicit = [m for m in messages if m['info']['role'] == 'user'
                    and any(p.get('type') == 'text' and not p.get('synthetic') for p in m['parts'])]
        self.assertEqual([m['info']['id'] for m in explicit], [request])

    def test_native_idle_rereads_final_after_compaction_between_reads(self):
        self.provider.mode = 'post_stop_compaction'
        request = self.send('Read-only fixture with no commands')
        self.assertTrue(self.provider.compaction_started.wait(10))
        original = self.client.messages_since
        reads = []
        def advance_after_first_messages(session, message):
            messages = original(session, message)
            reads.append(messages)
            if len(reads) == 1:
                self.provider.release.set()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    native = self.client.request('GET', '/session/status').get(session, {'type': 'idle'})['type']
                    if len(self.provider.requests) == 3 and native == 'idle':
                        break
                    time.sleep(0.02)
                else:
                    self.fail('Fixture continuation did not finish between message/status reads')
            return messages
        with mock.patch.object(self.client, 'messages_since', side_effect=advance_after_first_messages):
            result = self.client.snapshot(self.session, request)
        self.assertEqual(len(reads), 2)
        first_text = [p.get('text') for m in reads[0] for p in m['parts'] if p.get('type') == 'text']
        self.assertIn('Fixture done', first_text)
        self.assertNotIn('Fixture final after continuation', first_text)
        self.assertEqual((result['status'], result['native_status'], result['text']),
                         ('complete', 'idle', 'Fixture final after continuation'))
        self.assertEqual(result['request_id'], request)
        self.assertEqual(len(self.provider.requests), 3)

    def managed_dispatch_failure(self, accepted):
        task = self.root / 'managed-task.txt'
        task.write_text('Fixture managed dispatch identity')
        args = argparse.Namespace(task_file=str(task), worktree=str(self.project), model='openai/gpt-6-luna', reasoning='high',
            allow_write=False, allow_commands=False, long_context=False, max_steps=None, max_output_tokens=None,
            context_window=None, model_output_limit=None, timeout=None, worker_id='a' * 32, submission_id='fixture-initial')
        directory = self.root / args.worker_id
        def ensure(directory):
            live.update(directory, session_id=self.session, server_lifecycle='running')
        original = self.client.dispatch
        def ambiguous(session, message, payload):
            if accepted:
                original(session, message, payload)
            raise OSError('Fixture lost dispatch acknowledgement' if accepted else 'Fixture failed before dispatch')
        with mock.patch.object(live.batch, 'STATE', self.root), mock.patch.object(live.batch, 'KEY', self.root / 'dummy-key'), mock.patch.object(live, 'ensure_server', side_effect=ensure), mock.patch.object(live, 'client', return_value=self.client):
            with live.batch.credit_lock(): live.batch.credit_save(False, None)
            with mock.patch.object(self.client, 'dispatch', side_effect=ambiguous) as dispatch:
                with self.assertRaisesRegex(ValueError, 'uncertain native dispatch'):
                    live.start(args)
                message = live.read(directory)['current_request_id']
                receipt_path = directory / live.read(directory)['latest_turn'] / 'receipt.json'
                self.assertEqual(json.loads(receipt_path.read_text())['dispatch_status'], 'pending')
                if accepted:
                    self.assertEqual(self.client.wait(self.session, message, timeout=15)['status'], 'complete')
                    self.assertEqual(live.status(directory)['status'], 'complete')
                    receipt = json.loads(receipt_path.read_text())
                    self.assertEqual(receipt['dispatch_status'], 'confirmed')
                    self.assertEqual(receipt['dispatch_confirmation'], 'native_execution')
                    self.assertTrue(live.start(args)['already_submitted'])
                    duplicate = live.send(directory, task.read_text(), 'fixture-initial')
                    self.assertTrue(duplicate['already_submitted'])
                    self.assertEqual(duplicate['request_id'], message)
                    self.assertEqual(live.wait(directory)['status'], 'complete')
                    self.assertEqual(len(self.provider.requests), 1)
                else:
                    for operation in (lambda: live.start(args),
                                      lambda: live.send(directory, task.read_text(), 'fixture-initial'),
                                      lambda: live.send(directory, 'Unrelated new task', 'new-submission')):
                        with self.assertRaisesRegex(ValueError, 'Uncertain native dispatch'):
                            operation()
                    for result in (live.status(directory), live.wait(directory)):
                        self.assertEqual(result['status'], 'incomplete')
                        self.assertEqual(result['failure_reason'], 'dispatch_unconfirmed')
                        self.assertEqual(result['request_id'], message)
                        self.assertNotIn('already_submitted', result)
                    self.assertEqual(len(self.provider.requests), 0)
                self.assertEqual(dispatch.call_count, 1)
                messages = self.client.request('GET', f'/session/{self.session}/message')
                self.assertEqual(sum(m['info']['id'] == message for m in messages), 1)
                user = next(m for m in messages if m['info']['id'] == message)
                self.assertEqual([p.get('text') for p in user['parts'] if p.get('type') == 'text'], [task.read_text()])
                self.assertEqual(len(list(directory.glob('turn-*'))), 1)

    def test_dispatch_failure_before_acceptance_is_inspectable_and_never_redispatched(self):
        self.managed_dispatch_failure(False)

    def test_lost_dispatch_ack_recovers_exact_native_execution_without_redispatch(self):
        self.managed_dispatch_failure(True)

    def test_native_questions_reply_and_completion(self):
        self.provider.mode = 'question'
        message = self.send()
        result = self.client.wait(self.session, message, timeout=15)
        self.assertEqual(result['status'], 'needs_input', {**result, 'fixture_tools': [t.get('name') for t in self.provider.requests[0].get('tools', [])], 'fixture_tool_statuses': [[p.get('state', {}).get('status'), p.get('state', {}).get('error')] for m in self.client.request('GET', f'/session/{self.session}/message') for p in m['parts'] if p.get('type') == 'tool']})
        question = result['questions'][0]
        self.client.reply(self.session, question['id'], [['Proceed']])
        self.assertEqual(self.client.wait(self.session, message, timeout=15)['status'], 'complete')

    def test_acknowledged_model_resolution_failure_does_not_wait_forever(self):
        """Exercise a real asynchronous startup failure before assistant creation without a provider request."""
        message, payload = self.client.stage(self.session, 'Fixture unresolved model', 'openai/gpt-6-luna', 'high')
        payload['model'] = {'providerID': 'fixture-missing-provider', 'modelID': 'fixture-missing-model'}
        self.client.dispatch(self.session, message, payload)
        with mock.patch.object(transport, 'DISPATCH_GRACE_SECONDS', 0.1):
            result = self.client.wait(self.session, message, timeout=15)
        self.assertEqual(result['status'], 'incomplete', result)
        self.assertEqual(result['incomplete_reason'], 'dispatched_without_execution')
        self.assertFalse(result['execution_observed'])
        self.assertEqual(result['request_id'], message)
        self.assertEqual(self.provider.requests, [])

    def test_managed_startup_failure_is_retained_and_idle_reaped_without_replay(self):
        """Verify the real owner caches an acknowledged pre-execution failure and reaps its native server."""
        self.stop()
        directory = self.root / ('d' * 32)
        directory.mkdir(mode=0o700)
        live.atomic(directory / 'settings.json', self.settings)
        (directory / 'server-auth').write_text('fixture-only-server-auth')
        live.atomic(directory / 'metadata.json', {'backend': 'native-server', 'run_id': directory.name,
            'worktree': str(self.project), 'model': 'openai/gpt-6-luna', 'reasoning': 'high',
            'server_lifecycle': 'new', 'idle_reap_seconds': 0.1})
        owner = multiprocessing.get_context('fork').Process(target=live.run_server, args=(directory,))
        owner.start()
        self.addCleanup(lambda: self.stop_owner(owner))
        deadline = time.monotonic() + 15
        while live.read(directory).get('server_lifecycle') != 'running':
            self.assertLess(time.monotonic(), deadline, 'Managed fixture did not start')
            time.sleep(0.05)
        original_dispatch = live.NativeClient.dispatch

        def unresolved(api, session, message, payload):
            """Make only this disposable dispatch fail local model resolution after staging succeeds."""
            payload['model'] = {'providerID': 'fixture-missing-provider', 'modelID': 'fixture-missing-model'}
            return original_dispatch(api, session, message, payload)

        with mock.patch.object(live.batch, 'STATE', self.root):
            with live.batch.credit_lock():
                live.batch.credit_save(False, None)
            with mock.patch.object(live.NativeClient, 'dispatch', autospec=True, side_effect=unresolved) as dispatch:
                receipt = live.send(directory, 'Fixture startup failure', 'startup-failure')
                result = live.wait(directory)
                self.assertEqual(result['status'], 'incomplete', result)
                self.assertEqual(result['incomplete_reason'], 'dispatched_without_execution')
                self.assertEqual(result['request_id'], receipt['request_id'])
                dispatch.assert_called_once()
            # Session-only errors now wait for grace expiry; owner heartbeat
            # reconciliation and a subsequent idle-reap observation can each lag.
            owner.join(30)
            self.assertFalse(owner.is_alive(), 'Failed idle native backend was not reaped')
            self.assertEqual(live.read(directory)['server_lifecycle'], 'stopped')
            self.assertEqual(live.status(directory)['incomplete_reason'], 'dispatched_without_execution')
            self.assertTrue(live.send(directory, 'Fixture startup failure', 'startup-failure')['already_submitted'])
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(len(list(directory.glob('turn-*'))), 1)

    def test_native_cancel_keeps_request_identity(self):
        self.provider.mode = 'wait'
        message = self.send()
        deadline = time.monotonic() + 10
        while not self.provider.requests and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.client.abort(self.session))
        result = self.client.wait(self.session, message, timeout=15)
        self.assertIn(result['status'], ('interrupted', 'incomplete'), result)
        self.assertEqual(result['request_id'], message)

    def test_managed_idle_reap_restart_commands_and_submission_idempotency(self):
        self.stop()  # Replace only this disposable fixture server with the managed owner.
        self.provider.mode = 'command'
        self.provider.requests.clear()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        directory = self.root / ('e' * 32)
        directory.mkdir(mode=0o700)
        settings = transport.live_settings(self.settings, allow_commands=True,
            shell_plugin=(ROOT / 'command_environment.js').as_uri())
        live.atomic(directory / 'settings.json', settings)
        (directory / 'server-auth').write_text('fixture-only-server-auth')
        (directory / 'server-auth').chmod(0o600)
        live.atomic(directory / 'metadata.json', {'backend': 'native-server', 'run_id': directory.name,
            'worktree': str(self.project), 'model': 'openai/gpt-6-luna', 'reasoning': 'high',
            'allow_commands': True, 'server_lifecycle': 'new', 'idle_reap_seconds': 0.1})
        context = multiprocessing.get_context('fork')
        def launch_owner():
            def target():
                original_events, original_snapshot = live.NativeClient.events, live.NativeClient.snapshot
                faults = {'injected': False, 'second': False}
                def flaky_events(api):
                    for event in original_events(api):
                        if event.get('type') == 'session.status' and event.get('properties', {}).get('status', {}).get('type') == 'busy' and not faults['injected']:
                            faults['injected'] = True
                            (directory / 'observer-fault-injected').write_text('fixture')
                            raise OSError('fixture transient SSE failure')
                        yield event
                def flaky_snapshot(api, *args):
                    if faults['injected'] and not faults['second']:
                        faults['second'] = True
                        raise OSError('fixture second transient read failure')
                    return original_snapshot(api, *args)
                live.NativeClient.events, live.NativeClient.snapshot = flaky_events, flaky_snapshot
                live.run_server(directory)
            process = context.Process(target=target)
            process.start()
            self.addCleanup(lambda: self.stop_owner(process))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if live.read(directory).get('server_lifecycle') == 'running': return process
                time.sleep(0.05)
            self.fail('Managed fixture did not start')
        owner = launch_owner()
        state = self.root / 'credit-guard.json'
        with mock.patch.object(live.batch, 'STATE', self.root):
            with live.batch.credit_lock(): live.batch.credit_save(False, None)
            receipt = live.send(directory, 'Run only the fixture Git/Python checks', 'fixture-submission')
            duplicate = live.send(directory, 'Run only the fixture Git/Python checks', 'fixture-submission')
            self.assertTrue(duplicate['already_submitted'])
            self.assertEqual(receipt['request_id'], duplicate['request_id'])
            taskfile = self.root / 'cli-task.txt'
            taskfile.write_text('Run only the fixture Git/Python checks')
            with contextlib.redirect_stdout(io.StringIO()) as receipt_output:
                self.assertEqual(live.main(['send', directory.name, '--task-file', str(taskfile), '--submission-id', 'fixture-submission']), 0)
            self.assertTrue(json.loads(receipt_output.getvalue())['already_submitted'])
            with contextlib.redirect_stdout(io.StringIO()) as wait_output:
                self.assertEqual(live.main(['wait', directory.name]), 0)
            result = json.loads(wait_output.getvalue())
            self.assertEqual(result['status'], 'complete', result)
        owner.join(15)
        self.assertFalse(owner.is_alive(), 'Completed native backend was not reaped')
        meta = live.read(directory)
        self.assertEqual(meta['server_lifecycle'], 'stopped')
        self.assertTrue(meta['stop_requested'])
        self.assertTrue((directory / 'observer-fault-injected').exists())
        self.assertGreater(meta['native_server_lifetime_peak_rss_kib'], 0)
        self.assertEqual(live.status(directory)['status'], 'complete')  # Late attach without resident server.
        self.assertEqual(len(self.provider.requests), 2)  # One tool call + one final response, no duplicate dispatch.
        api_session = meta['session_id']
        self.provider.mode = 'text'
        live.update(directory, idle_reap_seconds=30)
        restarted = launch_owner()
        self.assertEqual(live.read(directory)['session_id'], api_session)
        self.assertFalse(live.read(directory)['stop_requested'])
        with mock.patch.object(live.batch, 'STATE', self.root):
            second = live.send(directory, 'Explicit new task after reaping', 'next-submission')
            self.assertNotEqual(receipt['request_id'], second['request_id'])
            self.assertEqual(live.wait(directory)['status'], 'complete')
        api = live.client(directory)
        messages = api.request('GET', f'/session/{api_session}/message')
        first = next(m for m in messages if m['info']['id'] == receipt['request_id'])
        self.assertEqual(sum(p.get('text') == 'Run only the fixture Git/Python checks' for p in first['parts']), 1)
        tools = [p for m in messages for p in m['parts'] if p.get('type') == 'tool']
        self.assertTrue(any(p.get('state', {}).get('status') == 'completed' and '42' in p.get('state', {}).get('output', '') for p in tools))
        api.abort(api_session)
        restarted.terminate()
        restarted.join(8)
        self.assertFalse(restarted.is_alive())
        self.assertEqual(live.read(directory)['server_lifecycle'], 'stopped')

    @staticmethod
    def stop_owner(process):
        if process.is_alive():
            process.terminate()
            process.join(8)
        if process.is_alive():
            process.kill()
            process.join()


class DispatchOutcomeTests(unittest.TestCase):
    def client(self):
        """Create an idle retained-session fixture with a staged user message and no assistant."""
        api = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        api.messages_since = mock.Mock(return_value=[{'info': {'id': 'request', 'role': 'user'}, 'parts': []}])
        api.questions = mock.Mock(return_value=[])
        api.request = mock.Mock(return_value={})
        return api

    def test_initial_idle_requires_acknowledgement_and_post_dispatch_grace(self):
        """Do not confuse staging, young dispatches, active work, or questions with missing execution."""
        api = self.client()
        with mock.patch.object(transport.time, 'time', return_value=1000):
            self.assertEqual(api.snapshot('session', 'request')['status'], 'queued')
        api.note_dispatch('session', 'request', 100)
        with mock.patch.object(transport.time, 'time', return_value=100 + transport.DISPATCH_GRACE_SECONDS - 0.01):
            self.assertEqual(api.snapshot('session', 'request')['status'], 'queued')
        with mock.patch.object(transport.time, 'time', return_value=100 + transport.DISPATCH_GRACE_SECONDS):
            result = api.snapshot('session', 'request')
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['incomplete_reason'], 'dispatched_without_execution')
            self.assertFalse(result['execution_observed'])
            self.assertEqual(api.snapshot('other-session', 'request')['status'], 'queued')
            api.request.return_value = {'session': {'type': 'busy'}}
            self.assertEqual(api.snapshot('session', 'request')['status'], 'running')
            api.request.return_value = {}
            api.questions.return_value = [{'id': 'question'}]
            self.assertEqual(api.snapshot('session', 'request')['status'], 'needs_input')

    def test_matching_observed_error_shortens_grace_without_confirming_dispatch(self):
        """Ignore errors for other sessions/requests and never use an event as acknowledgement proof."""
        api = self.client()
        error = {'name': 'UnknownError', 'data': {'message': 'Fixture model resolution failure'}}
        event = {'type': 'session.error', 'properties': {'sessionID': 'session', 'messageID': 'request', 'error': error}}
        api.observe_dispatch('session', 'request', event)
        with mock.patch.object(transport.time, 'time', return_value=100):
            self.assertEqual(api.snapshot('session', 'request')['status'], 'queued')
            api.note_dispatch('session', 'request', 100)
            for properties in ({'sessionID': 'other', 'error': error},
                               {'sessionID': 'session', 'messageID': 'other', 'error': error}):
                api.observe_dispatch('session', 'request', {'type': 'session.error', 'properties': properties})
                self.assertEqual(api.snapshot('session', 'request')['status'], 'queued')
            api.observe_dispatch('session', 'request', event)
            result = api.snapshot('session', 'request')
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['error'], error)
        self.assertFalse(result['execution_observed'])

    def test_wait_returns_on_observed_error_or_expired_grace_without_redispatch(self):
        """Bound an acknowledged empty request through both the event and retained-state wait paths."""
        for observed_error in (False, True):
            with self.subTest(observed_error=observed_error):
                api = self.client()
                api.note_dispatch('session', 'request', 100)
                clock = mock.Mock(return_value=100)

                def events():
                    """Advance wall time only after wait has reconciled its initial idle snapshot."""
                    yield {'type': 'server.connected'}
                    if observed_error:
                        yield {'type': 'session.error', 'properties': {'sessionID': 'session', 'messageID': 'request',
                            'error': {'name': 'UnknownError'}}}
                    else:
                        clock.return_value = 100 + transport.DISPATCH_GRACE_SECONDS
                        yield {'type': 'session.idle'}

                with mock.patch.object(api, 'events', side_effect=events), \
                        mock.patch.object(transport.time, 'time', clock), mock.patch.object(api, 'dispatch') as dispatch:
                    result = api.wait('session', 'request')
                self.assertEqual(result['status'], 'incomplete')
                self.assertEqual(result['incomplete_reason'], 'dispatched_without_execution')
                dispatch.assert_not_called()


class EventReconciliationTests(unittest.TestCase):
    def test_token_events_are_throttled_but_outcome_events_are_immediate(self):
        """Keep questions and completion responsive while rate-limiting delta and heartbeat snapshots."""
        with mock.patch.object(transport.time, 'monotonic', return_value=10.5):
            for kind in ('message.part.updated', 'message.part.delta', 'server.heartbeat'):
                self.assertFalse(transport.snapshot_due({'type': kind}, 10))
                self.assertTrue(transport.snapshot_due({'type': kind}, 9.5))
            for kind in ('server.connected', 'session.status', 'session.idle', 'session.error',
                         'message.updated', 'question.asked', 'question.replied', 'question.rejected'):
                self.assertTrue(transport.snapshot_due({'type': kind}, 10.5))

    def test_wait_observes_token_burst_without_snapshotting_every_delta(self):
        """Observe every delta and close the stream while immediately returning questions or completion."""
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        running = {'status': 'running', 'request_id': 'fixture-request'}
        for kind, status in [('question.asked', 'needs_input'), ('session.idle', 'complete')]:
            closed = []

            def events():
                """Stream many deltas followed by a decisive event, recording generator cleanup."""
                try:
                    yield {'type': 'server.connected'}
                    for _ in range(1000):
                        yield {'type': 'message.part.delta'}
                    yield {'type': kind}
                finally:
                    closed.append(True)

            observer = mock.Mock()
            with self.subTest(kind=kind), mock.patch.object(client, 'events', side_effect=events), \
                    mock.patch.object(client, 'snapshot', side_effect=[running, {'status': status}]) as snapshot, \
                    mock.patch.object(transport.time, 'monotonic', return_value=100):
                self.assertEqual(client.wait('fixture-session', 'fixture-request', observer=observer)['status'], status)
            self.assertEqual(snapshot.call_count, 2)
            self.assertEqual(observer.call_count, 1003)  # Initial result, deltas, decisive event, terminal result.
            self.assertEqual(closed, [True])

    def test_owner_throttles_snapshots_and_only_writes_connected_state_once(self):
        """Preserve raw observation while a token burst avoids full snapshots and repeated state writes."""
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            project = directory / 'project'
            project.mkdir()
            live.atomic(directory / 'metadata.json', {'session_id': 'fixture-session',
                'worktree': str(project), 'current_request_id': 'fixture-request'})
            live.atomic(directory / 'settings.json', {})
            (directory / 'server-auth').write_text('fixture-only')
            process = mock.Mock(pid=12345)
            process.poll.return_value = None
            api = mock.Mock()
            api.snapshot.side_effect = [
                {'status': 'running', 'native_status': 'busy'},
                {'status': 'needs_input', 'native_status': 'idle'},
                {'status': 'complete', 'native_status': 'idle'},
            ]

            def launch(*args, **kwargs):
                """Expose a fixture server address without starting a process or network listener."""
                with (directory / 'native-server.log').open('a') as log:
                    log.write('opencode server listening on http://127.0.0.1:1\n')
                return process

            def events():
                """Stop the fake process after a burst and outcome-changing events are consumed."""
                yield {'type': 'server.connected'}
                for _ in range(1000):
                    yield {'type': 'message.part.updated'}
                yield {'type': 'question.asked'}
                yield {'type': 'session.idle'}
                process.poll.return_value = 0

            api.events.side_effect = events
            with mock.patch.object(live.batch, 'STATE', directory), \
                    mock.patch.object(live.batch, 'environment', return_value={}), \
                    mock.patch.object(live.subprocess, 'Popen', side_effect=launch), \
                    mock.patch.object(live, 'process_identity', return_value='fixture-ticks'), \
                    mock.patch.object(live.os, 'getpgid', return_value=12345), \
                    mock.patch.object(live.signal, 'signal'), \
                    mock.patch.object(live.time, 'monotonic', return_value=100), \
                    mock.patch.object(live, 'client', return_value=api), \
                    mock.patch.object(live, 'remember') as remember, \
                    mock.patch.object(live, 'update', wraps=live.update) as update:
                live.run_server(directory)
            self.assertEqual(api.snapshot.call_count, 3)
            self.assertEqual(remember.call_count, 3)
            self.assertEqual(sum(call.kwargs.get('observer_state') == 'connected'
                                 for call in update.call_args_list), 1)
            self.assertEqual(len((directory / 'native-events.jsonl').read_text().splitlines()), 1003)
            self.assertEqual(live.read(directory)['server_lifecycle'], 'stopped')


class LiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = mock.patch.object(live.batch, 'STATE', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        with live.batch.credit_lock(): live.batch.credit_save(False, None)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.directory = self.root / ('c' * 32)
        self.directory.mkdir(mode=0o700)
        live.atomic(self.directory / 'metadata.json', {'session_id': 'native-fixture-session',
            'model': 'openai/gpt-6-luna', 'reasoning': 'high', 'worktree': str(self.project)})

    def test_send_loses_idle_reaper_race_without_staging_or_dispatch(self):
        def reaper_wins(directory):
            with live.locked(directory / 'control.lock'):
                live.update(directory, stop_requested=True, server_lifecycle='running')
        with mock.patch.object(live, 'ensure_server', side_effect=reaper_wins), mock.patch.object(live, 'client') as client:
            with self.assertRaisesRegex(ValueError, 'stopping'):
                live.send(self.directory, 'Fixture task', 'reaper-race')
            client.assert_not_called()
        self.assertEqual(list(self.directory.glob('turn-*')), [])

    def test_status_and_late_wait_restore_acknowledgement_and_retained_session_error(self):
        """Persist a matching startup failure while keeping uncertain dispatches blocked and inspectable."""
        turn = self.directory / 'turn-fixture'
        turn.mkdir()
        (self.directory / 'server-auth').write_text('fixture-only')
        error = {'name': 'UnknownError', 'data': {'message': 'Fixture startup failure'}}
        for acknowledged, with_error in ((True, False), (True, True), (False, True)):
            with self.subTest(acknowledged=acknowledged, with_error=with_error):
                live.update(self.directory, server_url='http://127.0.0.1:1', server_lifecycle='running',
                    current_request_id='request', latest_turn=turn.name, submitted_at=100,
                    pending_dispatch=None if acknowledged else turn.name, current_result=None, session_error=None)
                live.atomic(turn / 'receipt.json', {'request_id': 'request',
                    'dispatch_status': 'confirmed' if acknowledged else 'pending', 'dispatch_confirmed_at': 100})
                if with_error:
                    live.observe(self.directory, {'type': 'session.error',
                        'properties': {'sessionID': 'native-fixture-session', 'messageID': 'request', 'error': error}})
                # Build each client through the real factory, as separate CLI invocations do.
                with mock.patch.object(live.NativeClient, 'messages_since', return_value=[]), \
                        mock.patch.object(live.NativeClient, 'questions', return_value=[]), \
                        mock.patch.object(live.NativeClient, 'request', return_value={}), \
                        mock.patch.object(live.time, 'time', return_value=110), \
                        mock.patch.object(live.NativeClient, 'dispatch') as dispatch:
                    for operation in (live.status, live.wait):
                        result = operation(self.directory)
                        self.assertEqual(result['status'], 'incomplete')
                        if acknowledged:
                            self.assertEqual(result['incomplete_reason'], 'dispatched_without_execution')
                            self.assertEqual(result.get('error'), error if with_error else None)
                        else:
                            self.assertEqual(result['failure_reason'], 'dispatch_unconfirmed')
                    dispatch.assert_not_called()

    def test_metadata_sync_failure_prevents_native_dispatch(self):
        """Fail the durable pending-dispatch metadata write before any native generation can start."""
        fsync = os.fsync
        api = mock.Mock()
        api.stage.return_value = ('fixture-message', {})

        def fail_pending_metadata_sync(fd):
            """Allow staging receipts to sync, then fail the pending metadata file sync."""
            if stat.S_ISREG(os.fstat(fd).st_mode):
                path = Path(os.readlink(f'/proc/self/fd/{fd}'))
                if path.parent == self.directory and json.loads(path.read_text()).get('pending_dispatch'):
                    raise OSError('fixture pending metadata sync failure')
            fsync(fd)

        with mock.patch.object(live, 'ensure_server'), mock.patch.object(live, 'client', return_value=api), \
                mock.patch.object(live.os, 'fsync', side_effect=fail_pending_metadata_sync):
            with self.assertRaisesRegex(OSError, 'pending metadata sync failure'):
                live.send(self.directory, 'Fixture task', 'durable-dispatch')
        api.stage.assert_called_once()
        api.dispatch.assert_not_called()
        self.assertIsNone(live.read(self.directory).get('pending_dispatch'))

    def test_live_owner_lock_blocks_replacement_after_health_failure(self):
        live.update(self.directory, server_lifecycle='running')
        api = mock.Mock()
        api.request.side_effect = OSError('fixture unresponsive health')
        before = live.read(self.directory)
        with live.locked(self.directory / 'server.lock'), mock.patch.object(live, 'client', return_value=api), mock.patch.object(live.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'still active but unresponsive'):
                live.ensure_server(self.directory)
            launch.assert_not_called()
        self.assertEqual(live.read(self.directory), before)

    def test_pending_staging_is_inspectable_without_snapshot_or_latest_turn(self):
        live.update(self.directory, server_lifecycle='running', pending_submission='turn-uncertain')
        with mock.patch.object(live, 'client') as client:
            live.remember(self.directory, {'status': 'complete'})
            result = live.status(self.directory)
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['pending_submission'], 'turn-uncertain')
            args = argparse.Namespace(max_steps=2)
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                live.configure_idle(self.directory, args)
            client.assert_not_called()
        live.update(self.directory, current_request_id='prior-request')
        with mock.patch.object(live, 'client') as client:
            self.assertEqual(live.status(self.directory)['failure_reason'], 'pending_submission')
            client.assert_not_called()
        live.update(self.directory, pending_submission=None, current_request_id=None)
        api = mock.Mock()
        api.request.return_value = {'native-fixture-session': {'type': 'busy'}}
        api.questions.return_value = []
        with mock.patch.object(live, 'client', return_value=api):
            with self.assertRaisesRegex(ValueError, 'idle worker'):
                live.configure_idle(self.directory, args)
            api.snapshot.assert_not_called()

    def test_retry_start_without_request_reaches_idempotent_send_and_uncertain_guard(self):
        task = self.root / 'initial.txt'
        task.write_text('Fixture initial task')
        key = self.root / 'dummy-key'
        key.write_text('fixture-only')
        key.chmod(0o600)
        args = argparse.Namespace(task_file=str(task), worktree=str(self.project), model='openai/gpt-6-luna', reasoning='high',
            allow_write=False, allow_commands=False, long_context=False, max_steps=None, max_output_tokens=None,
            context_window=None, model_output_limit=None, timeout=None, worker_id='d' * 32, submission_id='initial')
        with mock.patch.object(live.batch, 'KEY', key):
            with mock.patch.object(live, 'send', side_effect=OSError('fixture startup failure')):
                with self.assertRaises(OSError): live.start(args)
            with mock.patch.object(live, 'send', return_value={'request_id': 'native-accepted'}) as send:
                self.assertEqual(live.start(args)['request_id'], 'native-accepted')
                send.assert_called_once()
            directory = self.root / args.worker_id
            live.update(directory, session_id='native-fixture-session')
            api = mock.Mock()
            api.stage.side_effect = OSError('fixture lost stage')
            with mock.patch.object(live, 'ensure_server'), mock.patch.object(live, 'client', return_value=api):
                with self.assertRaisesRegex(ValueError, 'uncertain'): live.start(args)
                with self.assertRaisesRegex(ValueError, 'uncertain'): live.start(args)
                self.assertEqual(api.stage.call_count, 1)
                api.dispatch.assert_not_called()

    def test_existing_managed_worker_protected_scope_rejects_before_launch(self):
        live.update(self.directory, worktree=str(self.root))
        with mock.patch.object(live, 'ensure_server') as launch:
            with self.assertRaisesRegex(ValueError, 'must not contain'):
                live.send(self.directory, 'Fixture task')
            launch.assert_not_called()

    def test_stopped_dispatch_uses_only_cached_exact_request_execution_evidence(self):
        turn = self.directory / 'turn-fixture'
        turn.mkdir(mode=0o700)
        for request, observed, confirmed in (('native-current', False, False),
                                              ('native-other', True, False),
                                              ('native-current', True, True)):
            live.atomic(turn / 'receipt.json', {'request_id': 'native-current',
                'stage_status': 'staged', 'dispatch_status': 'pending'})
            live.update(self.directory, current_request_id='native-current', latest_turn=turn.name,
                pending_dispatch=turn.name, server_lifecycle='stopped',
                current_result={'request_id': request, 'status': 'complete', 'text': 'Fixture final',
                                'execution_observed': observed})
            with mock.patch.object(live, 'client') as client:
                result = live.status(self.directory)
                client.assert_not_called()
            self.assertEqual(result['status'], 'complete' if confirmed else 'incomplete')
            receipt = json.loads((turn / 'receipt.json').read_text())
            self.assertEqual(receipt['dispatch_status'], 'confirmed' if confirmed else 'pending')
        # Acknowledgement persisted before metadata clearing is sufficient too.
        live.update(self.directory, pending_dispatch=turn.name)
        with mock.patch.object(live, 'client') as client:
            self.assertEqual(live.status(self.directory)['status'], 'complete')
            self.assertIsNone(live.read(self.directory)['pending_dispatch'])
            client.assert_not_called()

    def test_question_answer_is_blocked_by_shared_credit_latch(self):
        with live.batch.credit_lock(): live.batch.credit_save(True, 'credit_balance_exhausted')
        with mock.patch.object(live, 'client') as client:
            with self.assertRaisesRegex(ValueError, 'paused'):
                live.reply(self.directory, 'native-fixture-question', [['Proceed']])
            client.assert_not_called()

    def test_uncertain_staging_has_durable_intent_and_never_replays(self):
        api = mock.Mock()
        api.stage.side_effect = OSError('fixture lost stage response')
        with mock.patch.object(live, 'ensure_server'), mock.patch.object(live, 'client', return_value=api):
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                live.send(self.directory, 'Fixture exact task', 'stable-submission')
            pending = list(self.directory.glob('turn-*/receipt.json'))
            self.assertEqual(len(pending), 1)
            self.assertEqual(json.loads(pending[0].read_text())['stage_status'], 'pending')
            with self.assertRaisesRegex(ValueError, 'uncertain'):
                live.send(self.directory, 'Fixture exact task', 'stable-submission')
            self.assertEqual(api.stage.call_count, 1)
            api.dispatch.assert_not_called()

    def test_start_identity_includes_resolved_worktree_and_permissions(self):
        first, second = self.root / 'first', self.root / 'second'
        first.mkdir()
        second.mkdir()
        task = self.root / 'task.txt'
        task.write_text('Fixture initial assignment')
        key = self.root / 'dummy-key'
        key.write_text('fixture-only')
        key.chmod(0o600)
        args = argparse.Namespace(task_file=str(task), worktree=str(first), model='openai/gpt-6-luna', reasoning='high',
            allow_write=False, allow_commands=False, long_context=False, max_steps=None, max_output_tokens=None,
            context_window=None, model_output_limit=None, timeout=None, worker_id='d' * 32, submission_id='initial')
        with mock.patch.object(live.batch, 'KEY', key), mock.patch.object(live, 'send', return_value={'run_id': args.worker_id}) as send:
            live.start(args)
            directory = self.root / args.worker_id
            live.update(directory, current_request_id='native-accepted')
            live.start(args)
            self.assertEqual(send.call_count, 1)
            args.worktree = str(second)
            with self.assertRaisesRegex(ValueError, 'different initial assignment'): live.start(args)
            args.worktree = str(first)
            args.allow_write = True
            with self.assertRaisesRegex(ValueError, 'different initial assignment'): live.start(args)

    def test_stale_process_identity_cannot_signal_and_long_context_clears_policy(self):
        meta = {'owner_pid': os.getpid(), 'owner_start_ticks': 'not-current', 'owner_pgid': os.getpgid(os.getpid())}
        with mock.patch.object(signal, 'pidfd_send_signal') as send:
            self.assertFalse(live.signal_owned(meta, 'owner', signal.SIGTERM))
            send.assert_not_called()
        settings = transport.live_settings(worker.config('openai/gpt-6-luna', 'high', False), long_context=True)
        self.assertNotIn('reserved', settings['compaction'])
        self.assertEqual(settings['provider']['openai']['models']['gpt-6-luna']['limit']['context'], 1050000)

    def test_cli_router_preserves_legacy_and_routes_native_records(self):
        live.atomic(self.directory / 'metadata.json', {'backend': 'native-server'})
        with mock.patch.object(router.batch, 'STATE', self.root), mock.patch.object(router.live, 'main', return_value=0) as native:
            self.assertEqual(router.main(['status', self.directory.name]), 0)
            native.assert_called_once_with(['status', self.directory.name])
        live.atomic(self.directory / 'metadata.json', {'model': 'openai/gpt-6-luna'})
        with mock.patch.object(router.batch, 'STATE', self.root), mock.patch.object(router.batch, 'main', return_value=0) as legacy, mock.patch.object(router.live, 'main') as native:
            self.assertEqual(router.main(['status', self.directory.name]), 0)
            legacy.assert_called_once()
            native.assert_not_called()
        with mock.patch.object(router.live, 'main', return_value=0) as native:
            self.assertEqual(router.main(['run', '--live', '--allow-commands', '--task-file', 'fixture']), 0)
            self.assertEqual(native.call_args.args[0], ['start', '--allow-commands', '--task-file', 'fixture', '--wait'])

    def test_live_run_accepts_variant_alias_forms_and_preserves_wait_routing(self):
        """Parse both batch-style variant aliases through each managed-run route without dispatching work."""
        for route in ('--live', '--allow-commands'):
            for reasoning in (['--variant', 'high'], ['--variant=high'], ['--reasoning', 'high']):
                with self.subTest(route=route, reasoning=reasoning), \
                        mock.patch.object(router.live.batch, 'STATE', self.root), \
                        mock.patch.object(router.live, 'start', return_value={'run_id': self.directory.name}) as start, \
                        mock.patch.object(router.live, 'wait', return_value={'status': 'complete'}) as wait, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(router.main(['run', route, '--task-file', 'fixture-task',
                        '--worktree', str(self.project), '--model', 'openai/gpt-6-luna', *reasoning]), 0)
                    start.assert_called_once()
                    self.assertEqual(start.call_args.args[0].reasoning, 'high')
                    self.assertTrue(start.call_args.args[0].wait)
                    self.assertEqual(start.call_args.args[0].allow_commands, route == '--allow-commands')
                    wait.assert_called_once_with(self.directory)
                    self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_wait_cursor_cannot_report_an_old_request_as_new_completion(self):
        live.atomic(self.directory / 'metadata.json', {'current_request_id': 'native-old', 'session_id': 'native-session'})
        api = mock.Mock()
        def advance(session, request, observer):
            live.update(self.directory, current_request_id='native-new')
            observer({'type': 'server.heartbeat'})
        api.wait.side_effect = advance
        with mock.patch.object(live, 'status', return_value={'status': 'running'}), mock.patch.object(live, 'client', return_value=api):
            result = live.wait(self.directory)
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['request_id'], 'native-old')
        self.assertEqual(result['next_request_id'], 'native-new')

    def test_wait_rechecks_cursor_during_terminal_reconciliation_and_early_status(self):
        for phase in ('terminal', 'early', 'transport-fallback'):
            live.atomic(self.directory / 'metadata.json', {'current_request_id': 'native-old', 'session_id': 'native-session'})
            api = mock.Mock()
            def advance(*args, **kwargs):
                live.update(self.directory, current_request_id='native-new')
            terminal = {'status': 'complete', 'request_id': 'native-old', 'text': 'Old answer'}
            def early_status(directory):
                advance()
                return terminal
            if phase == 'terminal':
                api.wait.return_value = terminal
                status = mock.patch.object(live, 'status', return_value={'status': 'running'})
                reconcile = mock.patch.object(live, 'remember', side_effect=advance)
            elif phase == 'early':
                status = mock.patch.object(live, 'status', side_effect=early_status)
                reconcile = mock.patch.object(live, 'remember')
            else:
                api.wait.side_effect = OSError('fixture transport loss')
                calls = iter(({'status': 'running'}, terminal))
                def recovered_status(directory):
                    result = next(calls)
                    if result['status'] == 'complete': advance()
                    return result
                status = mock.patch.object(live, 'status', side_effect=recovered_status)
                reconcile = mock.patch.object(live, 'remember')
            with status, reconcile, mock.patch.object(live, 'client', return_value=api):
                result = live.wait(self.directory)
            self.assertEqual(result['status'], 'superseded', phase)
            self.assertEqual(result['request_id'], 'native-old')
            self.assertEqual(result['next_request_id'], 'native-new')
            self.assertNotIn('text', result)

    def test_stopped_backend_nonterminal_receipt_is_failed_with_partial_text(self):
        for state in ('queued', 'running', 'needs_input'):
            live.atomic(self.directory / 'metadata.json', {'server_lifecycle': 'stopped',
                'current_request_id': 'native-partial', 'current_result': {'status': state,
                    'request_id': 'native-partial', 'text': 'Retained partial fixture'}})
            result = live.status(self.directory)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['native_status'], 'unknown')
            self.assertEqual(result['text'], 'Retained partial fixture')
            self.assertEqual(live.wait(self.directory)['status'], 'failed')
        live.atomic(self.directory / 'metadata.json', {'server_lifecycle': 'stopped', 'current_request_id': 'native-complete',
            'current_result': {'status': 'complete', 'request_id': 'native-complete', 'text': 'Final fixture'}})
        self.assertEqual(live.status(self.directory)['status'], 'complete')


class AtomicReceiptTests(unittest.TestCase):
    def test_atomic_syncs_contents_before_replace_and_directory_after(self):
        """Observe flushed JSON before file sync and the new directory entry before directory sync."""
        fsync, replace = os.fsync, os.replace
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / 'receipt.json'
            path.write_text('{"old": true}')
            expected = {'dispatch_status': 'pending'}
            events, descriptors = [], []

            def observe_sync(fd):
                """Check the appropriate pre-rename or post-rename durable-write boundary."""
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    self.assertEqual(os.fstat(fd).st_ino, directory.stat().st_ino)
                    self.assertEqual(json.loads(path.read_text()), expected)
                    self.assertEqual(list(directory.glob('.receipt-*')), [])
                    descriptors.append(fd)
                    events.append('directory')
                else:
                    temporary_path, = directory.glob('.receipt-*')
                    self.assertEqual(json.loads(temporary_path.read_text()), expected)
                    self.assertEqual(json.loads(path.read_text()), {'old': True})
                    events.append('file')
                fsync(fd)

            def observe_replace(source, target):
                replace(source, target)
                events.append('replace')

            with mock.patch.object(live.os, 'fsync', side_effect=observe_sync), \
                    mock.patch.object(live.os, 'replace', side_effect=observe_replace):
                live.atomic(path, expected)
            self.assertEqual(events, ['file', 'replace', 'directory'])
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])

    def test_atomic_sync_failures_propagate_without_leaking_temp_files_or_descriptors(self):
        """Preserve old contents before rename and report post-rename sync failures without hiding them."""
        fsync = os.fsync
        for failure in ('file', 'directory'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                path = directory / 'receipt.json'
                path.write_text('{"old": true}')
                descriptors = []

                def fail_sync(fd):
                    """Inject failure at the selected file or directory durability boundary."""
                    kind = 'directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file'
                    if kind == failure:
                        descriptors.append(fd)
                        raise OSError('fixture sync failure')
                    fsync(fd)

                with mock.patch.object(live.os, 'fsync', side_effect=fail_sync):
                    with self.assertRaisesRegex(OSError, 'fixture sync failure'):
                        live.atomic(path, {'new': True})
                self.assertEqual(json.loads(path.read_text()), {'old': True} if failure == 'file' else {'new': True})
                self.assertEqual(list(directory.glob('.receipt-*')), [])
                with self.assertRaises(OSError):
                    os.fstat(descriptors[0])


class CompactionLineageTests(unittest.TestCase):
    def messages(self):
        def user(identifier, parts):
            return {'info': {'id': identifier, 'role': 'user'}, 'parts': parts}
        def assistant(identifier, parent, finish='stop', summary=False, text=''):
            return {'info': {'id': identifier, 'role': 'assistant', 'parentID': parent,
                    'finish': finish, 'summary': summary, 'time': {'completed': 1}},
                    'parts': [{'type': 'text', 'text': text}] if text else []}
        self.user, self.assistant = user, assistant
        return [user('a', [{'type': 'text', 'text': 'Explicit fixture assignment'}]),
            assistant('b', 'a', 'tool-calls'),
            user('c', [{'type': 'compaction', 'auto': True}]),
            assistant('d', 'c', summary=True, text='Native summary, not the answer'),
            user('e', [{'type': 'text', 'synthetic': True, 'metadata': {'compaction_continue': True}}]),
            assistant('f', 'e', 'tool-calls'),
            user('g', [{'type': 'compaction', 'auto': True}]),
            assistant('h', 'g', summary=True, text='Second native summary'),
            user('i', [{'type': 'text', 'synthetic': True, 'metadata': {'compaction_continue': True}}]),
            assistant('j', 'i', text='Actual delegated answer')]

    def failed_compaction(self):
        messages = self.messages()[:4]
        messages[-1]['info'].update(finish=None, error={'name': 'APIError', 'data': {
            'message': 'Insufficient fixture credits', 'statusCode': 429, 'isRetryable': False,
            'responseHeaders': {}, 'responseBody': json.dumps({'error': {
                'type': 'credit_balance_exhausted', 'code': 'credit_balance_exhausted'}}), 'metadata': {}}})
        return messages

    def snapshot(self, messages):
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        with mock.patch.object(client, 'messages_since', return_value=messages), mock.patch.object(client, 'request', side_effect=[[], {}, [], {}]):
            return client.snapshot('native-fixture', 'a')

    def test_owned_compaction_quota_error_is_failure_without_summary_text(self):
        messages = self.failed_compaction()
        result = self.snapshot(messages)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], messages[-1]['info']['error'])
        self.assertEqual(result['request_id'], 'a')
        self.assertEqual(result['text'], '')
        self.assertTrue(result['execution_observed'])
        self.assertNotIn('incomplete_reason', result)
        self.assertEqual(worker.exhaustion_reason(json.dumps({'type': 'error', 'error': result['error']})), 'credit_balance_exhausted')

    def test_failed_compaction_alone_does_not_manufacture_execution_confirmation(self):
        messages = self.failed_compaction()
        result = self.snapshot([messages[0], *messages[2:]])
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['execution_observed'])
        self.assertEqual(result['text'], '')

    def test_unowned_compaction_error_cannot_replace_completed_handoff(self):
        messages = self.failed_compaction()
        messages[1] = self.assistant('b', 'a', text='Completed original handoff')
        messages[2] = self.user('c', [{'type': 'text', 'text': 'Unrelated follow-up'}])
        result = self.snapshot(messages)
        self.assertEqual((result['status'], result['text']), ('complete', 'Completed original handoff'))
        self.assertNotIn('error', result)
        messages = self.failed_compaction()
        messages[-1]['info']['parentID'] = 'foreign-compaction'
        result = self.snapshot(messages)
        self.assertEqual(result['status'], 'incomplete')
        self.assertNotIn('error', result)
        self.assertEqual(result['text'], '')

    def test_cached_compaction_failure_survives_status_wait_without_replay_or_credit_reset(self):
        result = self.snapshot(self.failed_compaction())
        with tempfile.TemporaryDirectory() as private:
            root = Path(private)
            directory = root / ('c' * 32)
            directory.mkdir(mode=0o700)
            (directory / 'turn-fixture').mkdir(mode=0o700)
            with mock.patch.object(live.batch, 'STATE', root), mock.patch.object(live.batch.time, 'time', return_value=123):
                with live.batch.credit_lock():
                    live.batch.credit_save(True, 'credit_balance_exhausted')
                guard_before = (root / 'credit-guard.json').read_bytes()
                live.atomic(directory / 'metadata.json', {'session_id': 'native-fixture', 'current_request_id': 'a',
                    'latest_turn': 'turn-fixture', 'status': 'failed', 'server_lifecycle': 'stopped', 'current_result': result})
                with mock.patch.object(live, 'client', side_effect=AssertionError('no native access or dispatch')), mock.patch.object(live.batch, 'credit_preflight', side_effect=AssertionError('no new submission')):
                    for read in (live.status, live.wait):
                        observed = read(directory)
                        self.assertEqual(observed['status'], 'failed')
                        self.assertEqual(observed['error'], result['error'])
                        self.assertEqual(observed['text'], '')
                self.assertEqual((root / 'credit-guard.json').read_bytes(), guard_before)

    def test_ordinary_compaction_rate_limit_does_not_become_credit_exhaustion(self):
        messages = self.failed_compaction()
        messages[-1]['info']['error']['data']['responseBody'] = json.dumps({'error': {'code': 'rate_limit_exceeded'}})
        result = self.snapshot(messages)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(worker.exhaustion_reason(json.dumps({'type': 'error', 'error': result['error']})))

    def test_repeated_native_compaction_keeps_request_and_excludes_summaries(self):
        messages = self.messages()
        candidates = transport.request_assistants(list(reversed(messages)), 'a')
        self.assertEqual([m['info']['id'] for m in candidates], ['b', 'f', 'j'])
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        with mock.patch.object(client, 'messages_since', return_value=messages), mock.patch.object(client, 'request', side_effect=[[], {}, [], {}]):
            result = client.snapshot('native-fixture', 'a')
        self.assertEqual((result['status'], result['request_id'], result['text']), ('complete', 'a', 'Actual delegated answer'))

    def test_summary_only_and_initial_idle_are_not_task_completion(self):
        messages = self.messages()
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        for state in (messages[:1], messages[:4]):
            with mock.patch.object(client, 'messages_since', return_value=state), mock.patch.object(client, 'request', side_effect=[[], {}, [], {}]):
                result = client.snapshot('native-fixture', 'a')
            self.assertNotEqual(result['status'], 'complete')
            self.assertEqual(result['text'], '')

    def test_unrelated_explicit_turn_is_a_strict_lineage_boundary(self):
        messages = self.messages()
        messages[4] = self.user('e', [{'type': 'text', 'text': 'A distinct explicit follow-up'}])
        self.assertEqual([m['info']['id'] for m in transport.request_assistants(messages, 'a')], ['b'])
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        with mock.patch.object(client, 'messages_since', return_value=messages), mock.patch.object(client, 'request', side_effect=[[], {}, [], {}]):
            result = client.snapshot('native-fixture', 'a')
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['text'], '')

    def test_synthetic_text_without_completed_owned_compaction_is_not_ancestry(self):
        messages = self.messages()
        for mutate in ('no-summary', 'not-synthetic', 'not-auto', 'no-marker'):
            candidate = json.loads(json.dumps(messages))
            if mutate == 'no-summary': candidate[3]['info']['summary'] = False
            elif mutate == 'not-synthetic': candidate[4]['parts'][0]['synthetic'] = False
            elif mutate == 'not-auto': candidate[2]['parts'][0]['auto'] = False
            else: candidate[4]['parts'][0]['metadata'] = None
            self.assertEqual([m['info']['id'] for m in transport.request_assistants(candidate, 'a')], ['b'])
        self.assertEqual(transport.request_assistants(messages[1:], 'a'), [])

    def test_native_pagination_retains_original_request_ancestry(self):
        messages = self.messages()
        class Response(io.BytesIO):
            def __init__(self, rows, headers):
                super().__init__(json.dumps(rows).encode())
                self.headers = headers
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        pages = [Response(messages[2:], {'X-Next-Cursor': 'native-cursor/=='}), Response(messages[:2], {})]
        with mock.patch.object(client, 'open', side_effect=pages) as opened:
            result = client.messages_since('native-fixture', 'a')
        self.assertEqual(len(result), len(messages))
        self.assertIn('before=native-cursor%2F%3D%3D', opened.call_args.args[1])
        self.assertEqual(transport.request_assistants(result, 'a')[-1]['info']['id'], 'j')

    def test_stop_text_is_not_complete_while_native_runner_is_busy(self):
        messages = self.messages()
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        with mock.patch.object(client, 'messages_since', return_value=messages), mock.patch.object(client, 'request', side_effect=[[], {'native-fixture': {'type': 'busy'}}]):
            result = client.snapshot('native-fixture', 'a')
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['finish_reason'], 'stop')
        self.assertEqual(result['text'], 'Actual delegated answer')

    def test_idle_reconciliation_rechecks_busy_state_and_pending_questions(self):
        messages = self.messages()
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        for questions, native, expected in (([], {'native-fixture': {'type': 'busy'}}, 'running'),
                                            ([{'id': 'native-question', 'sessionID': 'native-fixture'}], {}, 'needs_input')):
            with mock.patch.object(client, 'messages_since', return_value=messages), mock.patch.object(client, 'request', side_effect=[[], {}, questions, native]):
                result = client.snapshot('native-fixture', 'a')
            self.assertEqual(result['status'], expected)

    def test_execution_evidence_requires_owned_assistant_not_only_staged_user_or_summary(self):
        messages = self.messages()
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        states = [messages[:1], [messages[0], *messages[2:4]],
                  [messages[0], self.user('e', [{'type': 'text', 'text': 'Unrelated explicit task'}]),
                   self.assistant('f', 'e', text='Unrelated answer')]]
        for state in states:
            with mock.patch.object(client, 'messages_since', return_value=state), mock.patch.object(client, 'request', side_effect=[[], {}, [], {}]):
                self.assertFalse(client.snapshot('native-fixture', 'a')['execution_observed'])
