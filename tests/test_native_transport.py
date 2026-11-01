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

    def test_native_questions_reply_and_completion(self):
        self.provider.mode = 'question'
        message = self.send()
        result = self.client.wait(self.session, message, timeout=15)
        self.assertEqual(result['status'], 'needs_input', {**result, 'fixture_tools': [t.get('name') for t in self.provider.requests[0].get('tools', [])], 'fixture_tool_statuses': [[p.get('state', {}).get('status'), p.get('state', {}).get('error')] for m in self.client.request('GET', f'/session/{self.session}/message') for p in m['parts'] if p.get('type') == 'tool']})
        question = result['questions'][0]
        self.client.reply(self.session, question['id'], [['Proceed']])
        self.assertEqual(self.client.wait(self.session, message, timeout=15)['status'], 'complete')

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
        self.assertTrue((directory / 'observer-fault-injected').exists())
        self.assertGreater(meta['native_server_lifetime_peak_rss_kib'], 0)
        self.assertEqual(live.status(directory)['status'], 'complete')  # Late attach without resident server.
        self.assertEqual(len(self.provider.requests), 2)  # One tool call + one final response, no duplicate dispatch.
        api_session = meta['session_id']
        self.provider.mode = 'text'
        live.update(directory, idle_reap_seconds=30)
        restarted = launch_owner()
        self.assertEqual(live.read(directory)['session_id'], api_session)
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


class LiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = mock.patch.object(live.batch, 'STATE', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        with live.batch.credit_lock(): live.batch.credit_save(False, None)
        self.directory = self.root / ('c' * 32)
        self.directory.mkdir(mode=0o700)
        live.atomic(self.directory / 'metadata.json', {'session_id': 'native-fixture-session',
            'model': 'openai/gpt-6-luna', 'reasoning': 'high'})

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
        live.atomic(self.directory / 'metadata.json', {'server_lifecycle': 'stopped',
            'current_result': {'status': 'complete', 'request_id': 'native-complete', 'text': 'Final fixture'}})
        self.assertEqual(live.status(self.directory)['status'], 'complete')


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
