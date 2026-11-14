"""Regression coverage for the latest hosted review, without live provider access."""
import contextlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import cli_router
import install
import live_worker as live
import native_transport as transport
import worker


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / ('a' * 32)
        self.directory.mkdir(mode=0o700)
        self.turn = self.directory / 'turn-one'
        self.turn.mkdir()
        live.atomic(self.directory / 'metadata.json', {'backend': 'native-server', 'session_id': 's',
            'current_request_id': 'm', 'latest_turn': self.turn.name, 'worktree': str(self.root / 'project'),
            'model': 'openai/gpt-6-luna', 'reasoning': 'high', 'server_lifecycle': 'running'})
        (self.root / 'project').mkdir()

    def test_wait_reobserves_newer_nonterminal_result_on_same_cursor(self):
        with mock.patch.object(live, 'wait_once', side_effect=[{'status': 'running'}, {'status': 'complete'}]) as observe:
            self.assertEqual(live.wait(self.directory)['status'], 'complete')
            self.assertEqual(observe.call_args_list, [mock.call(self.directory, 'm')] * 2)

    def test_reconnect_snapshot_failure_retries_until_terminal(self):
        api = transport.NativeClient('http://127.0.0.1:1', 'fixture', '/tmp')
        with mock.patch.object(api, 'events', side_effect=lambda: (x for x in ())), mock.patch.object(api, 'snapshot', side_effect=[ConnectionError('offline'), {'status': 'complete'}]), mock.patch.object(transport.time, 'sleep'):
            self.assertEqual(api.wait('s', 'm', timeout=1)['status'], 'complete')

    def test_reconnect_snapshot_failure_preserves_deadline(self):
        api = transport.NativeClient('http://127.0.0.1:1', 'fixture', '/tmp')
        with mock.patch.object(api, 'events', side_effect=lambda: (x for x in ())), mock.patch.object(api, 'snapshot', side_effect=ConnectionError('offline')):
            self.assertTrue(api.wait('s', 'm', timeout=0.01)['wait_timed_out'])

    def test_durable_text_syncs_file_before_parent(self):
        events = []
        with mock.patch.object(worker.os, 'fsync', side_effect=lambda fd: events.append('file')), mock.patch.object(worker, 'sync_directory', side_effect=lambda path: events.append(path)):
            worker.durable_text(self.turn / 'result.txt', 'done')
        self.assertEqual(events, ['file', self.turn])
        self.assertEqual((self.turn / 'result.txt').read_text(), 'done')

    def test_resume_clears_old_failure_diagnostics_before_launch(self):
        args, key = self.batch_args()
        args.action, args.run_id = 'resume', self.directory.name
        live.update(self.directory, allow_write=False, failure_reason='old', launch_error='old', credit_exhaustion_reason='old')
        def launch(*a, **k):
            record = live.read(self.directory)
            for field in ('failure_reason', 'launch_error', 'credit_exhaustion_reason'):
                self.assertNotIn(field, record)
            raise OSError('fixture')
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(worker, 'KEY', key), mock.patch.object(worker.subprocess, 'Popen', side_effect=launch):
            with worker.credit_lock(): worker.credit_save(False, None)
            with self.assertRaises(OSError): worker.execute(args)

    def test_managed_result_is_durable_before_terminal_metadata(self):
        events = []
        atomic = live.atomic
        def tracked(path, value):
            if path.name == 'metadata.json':
                events.append('metadata')
            return atomic(path, value)
        with mock.patch.object(live.batch, 'durable_text', side_effect=lambda path, text: events.append('durable-result')), mock.patch.object(live, 'atomic', side_effect=tracked):
            live.remember(self.directory, {'request_id': 'm', 'status': 'complete', 'text': 'done'})
        self.assertEqual(events, ['durable-result', 'metadata'])

    def test_option_first_resume_routes_to_managed(self):
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(cli_router.live, 'main', return_value=0) as route:
            args = ['resume', '--task-file', 'task.txt', self.directory.name]
            self.assertEqual(cli_router.main(args), 0)
            route.assert_called_once_with(args)

    def test_stop_commands_reject_legacy_batch_without_success(self):
        live.update(self.directory, backend='batch', status='running')
        for command in ('cancel', 'shutdown'):
            with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(cli_router.live, 'main') as route:
                with self.assertRaisesRegex(ValueError, 'unsupported for legacy batch'):
                    cli_router.main([command, self.directory.name])
                route.assert_not_called()
            self.assertEqual(live.read(self.directory)['status'], 'running')

    def test_stop_commands_route_managed_backends(self):
        for command in ('cancel', 'shutdown'):
            with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(cli_router.live, 'main', return_value=0) as route:
                self.assertEqual(cli_router.main([command, self.directory.name]), 0)
                route.assert_called_once_with([command, self.directory.name])

    def test_old_snapshot_cannot_replace_new_result(self):
        done = {'request_id': 'm', 'status': 'complete', 'text': 'done', 'observed_at_ns': 20, 'observed_boot_id': 'boot-one'}
        live.remember(self.directory, done)
        live.remember(self.directory, {'request_id': 'm', 'status': 'running', 'text': '', 'observed_at_ns': 10, 'observed_boot_id': 'boot-one'})
        self.assertEqual(live.read(self.directory)['current_result'], done)
        self.assertEqual((self.turn / 'result.txt').read_text(), 'done')

    def test_wait_returns_newer_retained_result_for_same_cursor(self):
        live.update(self.directory, pricing_warning='fixture warning')
        stale = {'request_id': 'm', 'status': 'needs_input', 'questions': ['answered'],
                 'observed_at_ns': 10, 'observed_boot_id': 'boot-one'}
        done = {'request_id': 'm', 'status': 'complete', 'text': 'done',
                'observed_at_ns': 20, 'observed_boot_id': 'boot-one'}
        live.remember(self.directory, done)
        live.remember(self.directory, stale)
        result = live.wait_result(self.directory, stale, 'm')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['text'], 'done')
        self.assertNotIn('questions', result)
        self.assertEqual(result['run_id'], self.directory.name)
        self.assertEqual(result['pricing_warning'], 'fixture warning')
        live.update(self.directory, current_request_id='next')
        self.assertEqual(live.wait_result(self.directory, stale, 'm')['status'], 'superseded')

    def test_controls_do_not_stop_queued_idle_request(self):
        from argparse import Namespace
        api = mock.Mock()
        api.snapshot.return_value = {'request_id': 'm', 'status': 'queued',
                                     'native_status': 'idle', 'questions': []}
        with mock.patch.object(live, 'client', return_value=api), mock.patch.object(live, 'signal_owned') as stop:
            with self.assertRaisesRegex(ValueError, 'idle worker'):
                live.configure_idle(self.directory, Namespace(max_steps=3))
        stop.assert_not_called()
        self.assertEqual(live.read(self.directory)['server_lifecycle'], 'running')

    def test_new_boot_observation_replaces_old_monotonic_time(self):
        live.remember(self.directory, {'request_id': 'm', 'status': 'running', 'text': 'old',
            'observed_at_ns': 9999, 'observed_boot_id': 'previous-boot'})
        live.remember(self.directory, {'request_id': 'm', 'status': 'complete', 'text': 'new',
            'observed_at_ns': 1, 'observed_boot_id': 'current-boot'})
        self.assertEqual((self.turn / 'result.txt').read_text(), 'new')

    def test_status_does_not_relabel_stale_snapshot_as_pending_request(self):
        api = mock.Mock()
        def snapshot(*args):
            live.update(self.directory, current_request_id='new', current_result=None, pending_dispatch='turn-new')
            return {'request_id': 'm', 'status': 'running', 'text': 'old output', 'execution_observed': True}
        api.snapshot.side_effect = snapshot
        with mock.patch.object(live, 'client', return_value=api):
            result = live.status(self.directory)
        self.assertEqual(result['request_id'], 'new')
        self.assertEqual(result['text'], '')
        self.assertNotIn('execution_observed', result)
        self.assertEqual(result['failure_reason'], 'dispatch_unconfirmed')

    def test_submission_lock_covers_controls_and_admission(self):
        acquired = []
        @contextlib.contextmanager
        def lock(path, **kwargs):
            acquired.append(path.name)
            try: yield
            finally: acquired.pop()
        def controls(*args):
            self.assertEqual(acquired, ['submission.lock'])
        def admission(*args):
            self.assertEqual(acquired, ['submission.lock'])
            return 'accepted'
        with mock.patch.object(live, 'locked', side_effect=lock), mock.patch.object(live, 'configure_idle', side_effect=controls), mock.patch.object(live, '_send', side_effect=admission):
            self.assertEqual(live.send(self.directory, 'task', 'key', controls=object()), 'accepted')
            self.assertEqual(live.send(self.directory, 'task', 'key'), 'accepted')

    def test_duplicate_with_controls_returns_receipt_without_changing_backend(self):
        import hashlib
        live.atomic(self.turn / 'receipt.json', {'submission_id': 'same',
            'task_sha256': hashlib.sha256(b'task').hexdigest(), 'request_id': 'm',
            'stage_status': 'staged', 'dispatch_status': 'confirmed'})
        with mock.patch.object(live, 'configure_idle') as configure, mock.patch.object(live, '_send') as send:
            result = live.send(self.directory, 'task', 'same', controls=object())
        self.assertTrue(result['already_submitted'])
        configure.assert_not_called()
        send.assert_not_called()

    def test_uncertain_duplicate_with_controls_does_not_change_backend(self):
        import hashlib
        live.atomic(self.turn / 'receipt.json', {'submission_id': 'same',
            'task_sha256': hashlib.sha256(b'task').hexdigest(), 'stage_status': 'pending'})
        with mock.patch.object(live, 'configure_idle') as configure:
            with self.assertRaisesRegex(ValueError, 'uncertain native staging'):
                live.send(self.directory, 'task', 'same', controls=object())
        configure.assert_not_called()

    def test_stopped_without_cached_result_keeps_failure_identity(self):
        live.update(self.directory, server_lifecycle='stopped', current_result=None)
        result = live.status(self.directory)
        self.assertEqual(result['request_id'], 'm')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure_reason'], 'backend_stopped_without_terminal_receipt')

    def test_recovery_snapshot_reaps_only_confirmed_terminal_idle(self):
        live.update(self.directory, idle_reap_seconds=1)
        api, stopping = mock.Mock(), threading.Event()
        result = {'request_id': 'm', 'status': 'complete', 'native_status': 'idle', 'text': 'done'}
        api.snapshot.return_value = result
        with mock.patch.object(live.time, 'monotonic', return_value=10):
            since = live.reconcile_owner_snapshot(self.directory, api, 's', live.read(self.directory), result, None, stopping)
        self.assertEqual(since, ('m', 10))
        self.assertFalse(stopping.is_set())
        with mock.patch.object(live.time, 'monotonic', return_value=12):
            live.reconcile_owner_snapshot(self.directory, api, 's', live.read(self.directory), result, since, stopping)
        self.assertTrue(stopping.is_set())
        self.assertTrue(live.read(self.directory)['stop_requested'])

    def test_reaper_retains_final_protected_snapshot_before_stop(self):
        live.update(self.directory, idle_reap_seconds=1)
        provisional = {'request_id': 'm', 'status': 'incomplete', 'native_status': 'idle',
                       'incomplete_reason': 'idle_unfinished_assistant', 'text': 'partial'}
        final = {'request_id': 'm', 'status': 'complete', 'native_status': 'idle', 'text': 'final answer'}
        api, stopping = mock.Mock(), threading.Event()
        api.snapshot.return_value = final
        with mock.patch.object(live.time, 'monotonic', return_value=12):
            live.reconcile_owner_snapshot(self.directory, api, 's', live.read(self.directory), provisional, ('m', 10), stopping)
        self.assertTrue(stopping.is_set())
        self.assertEqual(live.read(self.directory)['current_result'], final)
        self.assertEqual((self.turn / 'result.txt').read_text(), 'final answer')
        live.update(self.directory, server_lifecycle='stopped')
        self.assertEqual(live.status(self.directory)['text'], 'final answer')
        self.assertEqual(live.wait(self.directory)['text'], 'final answer')

    def test_controls_validate_before_stopping_idle_backend(self):
        from argparse import Namespace
        args = Namespace(timeout=-1, max_steps=None, max_output_tokens=None,
                         context_window=None, model_output_limit=None, long_context=None)
        with mock.patch.object(live, 'client') as client, mock.patch.object(live, 'signal_owned') as stop:
            with self.assertRaisesRegex(ValueError, 'Timeout must be positive'):
                live.configure_idle(self.directory, args)
        client.assert_not_called()
        stop.assert_not_called()

    def test_controls_retain_terminal_snapshot_before_stopping_backend(self):
        from argparse import Namespace
        args = Namespace(timeout=0, max_steps=None, max_output_tokens=None,
                         context_window=None, model_output_limit=None, long_context=None)
        final = {'request_id': 'm', 'status': 'complete', 'native_status': 'idle',
                 'questions': [], 'text': 'final control answer'}
        api = mock.Mock()
        api.snapshot.return_value = final
        def stop(*args):
            self.assertEqual(live.read(self.directory)['current_result'], final)
            self.assertEqual((self.turn / 'result.txt').read_text(), 'final control answer')
            raise OSError('fixture stops after retained proof')
        with mock.patch.object(live, 'client', return_value=api), mock.patch.object(live, 'signal_owned', side_effect=stop):
            with self.assertRaisesRegex(OSError, 'retained proof'):
                live.configure_idle(self.directory, args)

    def test_idle_timer_restarts_for_new_request_without_observed_activity(self):
        live.update(self.directory, idle_reap_seconds=30)
        api, stopping = mock.Mock(), threading.Event()
        result = {'request_id': 'm', 'status': 'complete', 'native_status': 'idle'}
        api.snapshot.return_value = result
        with mock.patch.object(live.time, 'monotonic', return_value=39):
            since = live.reconcile_owner_snapshot(self.directory, api, 's',
                live.read(self.directory), result, ('previous', 10), stopping)
        self.assertEqual(since, ('m', 39))
        self.assertFalse(stopping.is_set())
        with mock.patch.object(live.time, 'monotonic', return_value=40):
            since = live.reconcile_owner_snapshot(self.directory, api, 's',
                live.read(self.directory), result, since, stopping)
        self.assertEqual(since, ('m', 39))
        self.assertFalse(stopping.is_set())
        api.snapshot.assert_not_called()
        with mock.patch.object(live.time, 'monotonic', return_value=69):
            live.reconcile_owner_snapshot(self.directory, api, 's',
                live.read(self.directory), result, since, stopping)
        self.assertTrue(stopping.is_set())

    def test_idle_unanswered_continuation_can_resume_or_ask_question(self):
        for result in [
                {'status': 'running', 'native_status': 'busy'},
                {'status': 'needs_input', 'questions': [{'id': 'q'}]}]:
            with self.subTest(result=result):
                prior = {'request_id': 'm', 'status': 'incomplete',
                         'incomplete_reason': 'idle_unanswered_continuation'}
                live.update(self.directory, current_result=prior)
                expected = {'request_id': 'm', **result}
                live.remember(self.directory, expected)
                self.assertEqual(live.read(self.directory)['current_result'], expected)

    def test_recovery_snapshot_does_not_reap_active_work(self):
        live.update(self.directory, idle_reap_seconds=1)
        api, stopping = mock.Mock(), threading.Event()
        result = {'request_id': 'm', 'status': 'running', 'native_status': 'busy'}
        since = live.reconcile_owner_snapshot(self.directory, api, 's', live.read(self.directory), result, ('m', 1), stopping)
        self.assertIsNone(since)
        self.assertFalse(stopping.is_set())
        api.snapshot.assert_not_called()

    def test_old_wait_cursor_is_superseded_before_observation(self):
        with mock.patch.object(live, 'status') as status:
            result = live.wait(self.directory, 'older')
        self.assertEqual(result['status'], 'superseded')
        status.assert_not_called()

    def test_timeout_is_enforced_without_events(self):
        live.update(self.directory, timeout_seconds=2, submitted_at=time.time() - 5)
        api = mock.Mock()
        live.enforce_timeout(self.directory, api, 's')
        api.abort.assert_called_once_with('s')

    def test_timeout_rereads_request_after_control_lock(self):
        live.update(self.directory, timeout_seconds=2, submitted_at=time.time() - 5)
        original_lock = live.locked
        @contextlib.contextmanager
        def replacing_lock(path, **kwargs):
            with original_lock(path, **kwargs):
                if path.name == 'control.lock':
                    live.update(self.directory, current_request_id='replacement', submitted_at=time.time())
                yield
        api = mock.Mock()
        with mock.patch.object(live, 'locked', side_effect=replacing_lock):
            live.enforce_timeout(self.directory, api, 's')
        api.abort.assert_not_called()

    def test_distinct_send_retains_previous_terminal_result(self):
        api = mock.Mock()
        api.snapshot.return_value = {'request_id': 'm', 'status': 'complete',
            'native_status': 'idle', 'text': 'final prior result'}
        api.stage.side_effect = ValueError('fixture stop after retention')
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(live, 'ensure_server'), mock.patch.object(live, 'client', return_value=api):
            with worker.credit_lock(): worker.credit_save(False, None)
            with self.assertRaisesRegex(ValueError, 'fixture stop'):
                live.send(self.directory, 'new task', 'new')
        self.assertEqual((self.turn / 'result.txt').read_text(), 'final prior result')
        self.assertEqual(json.loads((self.turn / 'status.json').read_text())['status'], 'complete')

    def test_controls_reject_starting_backend(self):
        from argparse import Namespace
        live.update(self.directory, server_lifecycle='starting')
        args = Namespace(max_steps=3)
        original_lock = live.locked
        with mock.patch.object(live, 'locked', wraps=original_lock) as lock:
            with self.assertRaisesRegex(ValueError, 'backend is starting'):
                live.configure_idle(self.directory, args)
        self.assertIn(mock.call(self.directory / 'launch.lock'), lock.call_args_list)
        self.assertEqual(live.read(self.directory)['server_lifecycle'], 'starting')

    def test_orphan_backend_is_rejected(self):
        api = mock.Mock()
        api.request.return_value = {'healthy': True}
        with mock.patch.object(live, 'client', return_value=api), self.assertRaisesRegex(ValueError, 'no live owner'):
            live.ensure_server(self.directory)

    def test_pending_receipt_without_metadata_blocks_distinct_task(self):
        live.atomic(self.turn / 'receipt.json', {'stage_status': 'pending', 'submission_id': 'old'})
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(live, 'ensure_server'), mock.patch.object(live, 'client') as api:
            with worker.credit_lock(): worker.credit_save(False, None)
            with self.assertRaisesRegex(ValueError, 'staging receipt'):
                live.send(self.directory, 'new task', 'new')
            api.assert_not_called()

    def test_active_request_blocks_distinct_task(self):
        api = mock.Mock()
        api.snapshot.return_value = {'status': 'running', 'native_status': 'busy'}
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(live, 'ensure_server'), mock.patch.object(live, 'client', return_value=api):
            with worker.credit_lock(): worker.credit_save(False, None)
            with self.assertRaisesRegex(ValueError, 'still active'):
                live.send(self.directory, 'new task', 'new')
            api.stage.assert_not_called()

    def test_invalid_server_auth_is_rejected(self):
        for value in ('', 'short', 'a' * 42, 'a' * 43 + '\n'):
            (self.directory / 'server-auth').write_text(value)
            with self.assertRaises(ValueError): live.server_password(self.directory)
        (self.directory / 'server-auth').write_text('a' * 43)
        self.assertEqual(live.server_password(self.directory), 'a' * 43)

    def test_lock_wait_is_interruptible(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            with live.locked(self.directory / 'control.lock', stopping=stop):
                self.fail('entered after stop')

    def test_incomplete_json_protocol_becomes_connection_error(self):
        api = transport.NativeClient('http://127.0.0.1:1', 'fixture', '/tmp')
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = http.client.IncompleteRead(b'')
        with mock.patch.object(api, 'open', return_value=response):
            with self.assertRaises(ConnectionError): api.request('GET', '/test')
            with self.assertRaises(ConnectionError): api.messages_since('s', 'm')

    def test_empty_stream_uses_snapshot_and_reconnects_until_deadline(self):
        api = transport.NativeClient('http://127.0.0.1:1', 'fixture', '/tmp')
        with mock.patch.object(api, 'events', side_effect=lambda: (x for x in ())), mock.patch.object(api, 'snapshot', return_value={'status': 'running'}):
            start = time.monotonic()
            result = api.wait('s', 'm', timeout=0.05)
            self.assertTrue(result['wait_timed_out'])
            self.assertLess(time.monotonic() - start, 0.3)

    def test_length_finish_is_provisional_while_busy(self):
        api = transport.NativeClient('http://127.0.0.1:1', 'fixture', '/tmp')
        messages = [{'info': {'id': 'm', 'role': 'user'}, 'parts': []},
                    {'info': {'id': 'n', 'role': 'assistant', 'parentID': 'm', 'finish': 'length', 'time': {'completed': 1}}, 'parts': []}]
        with mock.patch.object(api, 'messages_since', return_value=messages), mock.patch.object(api, 'questions', return_value=[]), mock.patch.object(api, 'request', return_value={'s': {'type': 'busy'}}):
            self.assertEqual(api.snapshot('s', 'm')['status'], 'running')

    def test_persistently_idle_unfinished_assistant_needs_stable_grace(self):
        api = transport.NativeClient('http://127.0.0.1:1', 'fixture', '/tmp')
        messages = [{'info': {'id': 'm', 'role': 'user'}, 'parts': []},
                    {'info': {'id': 'n', 'role': 'assistant', 'parentID': 'm'}, 'parts': []}]
        with mock.patch.object(api, 'messages_since', return_value=messages), mock.patch.object(api, 'questions', return_value=[]), mock.patch.object(api, 'request', return_value={}), mock.patch.object(transport.time, 'monotonic', return_value=0):
            self.assertEqual(api.snapshot('s', 'm')['status'], 'queued')
        with mock.patch.object(api, 'messages_since', return_value=messages), mock.patch.object(api, 'questions', return_value=[]), mock.patch.object(api, 'request', return_value={}), mock.patch.object(transport.time, 'monotonic', return_value=11):
            self.assertEqual(api.snapshot('s', 'm')['incomplete_reason'], 'idle_unfinished_assistant')

    def test_bin_symlink_to_runtime_is_rejected(self):
        source = self.root / 'source'
        (source / 'runtime').mkdir(parents=True)
        prefix = self.root / 'prefix'
        prefix.mkdir()
        (prefix / 'bin').symlink_to(source / 'runtime')
        with mock.patch.object(install, 'SOURCE', source), self.assertRaisesRegex(ValueError, 'outside the source runtime'):
            install.install(prefix)
        self.assertEqual(list((source / 'runtime').iterdir()), [])

    def test_atomic_save_preserves_existing_file_on_write_failure(self):
        path = self.root / 'state.json'
        worker.save(path, {'old': True})
        with mock.patch.object(worker.os, 'fsync', side_effect=OSError('fixture')), self.assertRaises(OSError):
            worker.save(path, {'new': True})
        self.assertEqual(json.loads(path.read_text()), {'old': True})
        self.assertEqual(list(self.root.glob('.receipt-*')), [])

    def batch_args(self):
        import argparse
        task = self.root / 'task.txt'
        task.write_text('fixture')
        key = self.root / 'key'
        key.write_text('fixture')
        key.chmod(0o600)
        args = argparse.Namespace(action='run', model='openai/gpt-6-luna', reasoning='high',
            worktree=str(self.root / 'project'), task_file=str(task), allow_write=False,
            max_steps=None, max_output_tokens=None, timeout=None)
        return args, key

    def test_batch_launch_failure_finishes_receipt(self):
        args, key = self.batch_args()
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(worker, 'KEY', key), mock.patch.object(worker.subprocess, 'Popen', side_effect=OSError('fixture')):
            with worker.credit_lock(): worker.credit_save(False, None)
            with self.assertRaises(OSError): worker.execute(args)
        records = [json.loads(p.read_text()) for p in self.root.glob('*/metadata.json')]
        failed = next(record for record in records if record.get('failure_reason') == 'native_launch_failed')
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue((self.root / failed['run_id'] / failed['latest_turn'] / 'result.txt').exists())

    def test_batch_sigterm_stops_child_and_records_interruption(self):
        import signal
        args, key = self.batch_args()
        process = mock.Mock(pid=999999, returncode=None)
        process.wait.side_effect = lambda **kwargs: setattr(process, 'returncode', -15)
        process.communicate.side_effect = lambda *a, **k: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(worker, 'KEY', key), mock.patch.object(worker.subprocess, 'Popen', return_value=process), mock.patch.object(worker.os, 'killpg') as kill, contextlib.redirect_stdout(__import__('io').StringIO()):
            with worker.credit_lock(): worker.credit_save(False, None)
            self.assertEqual(worker.execute(args), 124)
            kill.assert_called_once_with(process.pid, signal.SIGTERM)
            process.wait.assert_called_once()

    def test_local_hook_dependencies_are_prepared_offline_and_reusable(self):
        settings = {'plugin': [(worker.ROOT / 'command_environment.js').as_uri()]}
        with mock.patch.object(worker.subprocess, 'Popen', side_effect=AssertionError('No package install at startup')):
            first = worker.environment(self.directory, settings)
            second = worker.environment(self.directory, settings)
        config = Path(first['XDG_CONFIG_HOME']) / 'opencode'
        self.assertEqual(first, second)
        self.assertEqual((config / 'node_modules').resolve(), (worker.ROOT / 'runtime/node_modules').resolve())
        manifest = json.loads((config / 'package.json').read_text())
        lock = json.loads((config / 'package-lock.json').read_text())
        self.assertEqual(manifest['dependencies']['@opencode-ai/plugin'], worker.VERSION)
        self.assertEqual(lock['packages']['']['dependencies'], manifest['dependencies'])
        self.assertNotIn('HTTPS_PROXY', first)

    def test_local_hook_missing_dependency_fails_before_native_launch(self):
        with mock.patch.object(worker, 'ROOT', self.root):
            with self.assertRaisesRegex(ValueError, 'npm ci --prefix runtime'):
                worker.environment(self.directory, {'plugin': ['file:///fixture.js']})

    def test_local_hook_rejects_unexpected_dependency_link(self):
        config = self.directory / 'home/config/opencode'
        (self.directory / 'home').mkdir(mode=0o700)
        config.mkdir(parents=True, mode=0o700)
        (config / 'node_modules').symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'Unexpected plugin dependency link'):
            worker.environment(self.directory, {'plugin': ['file:///fixture.js']})

    def test_shutdown_starting_is_not_reported_stopped(self):
        live.update(self.directory, server_lifecycle='starting')
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(live, 'client') as api:
            with self.assertRaisesRegex(ValueError, 'Backend is starting'):
                live.main(['shutdown', self.directory.name])
        api.assert_not_called()
        self.assertEqual(live.read(self.directory)['server_lifecycle'], 'starting')

    def test_cancel_starting_is_not_reported_requested(self):
        live.update(self.directory, server_lifecycle='starting')
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(live, 'client') as api:
            with self.assertRaisesRegex(ValueError, 'Backend is starting; retry cancel'):
                live.main(['cancel', self.directory.name])
        api.assert_not_called()
        self.assertEqual(live.read(self.directory)['server_lifecycle'], 'starting')

    def test_quiet_owner_ticks_keep_one_subscription(self):
        import threading
        release = threading.Event()
        stopping = threading.Event()
        api = mock.Mock()
        def events(**kwargs):
            yield {'type': 'server.connected'}
            release.wait(2)
            yield {'type': 'session.idle'}
        api.events.side_effect = events
        stream = live.owner_events(api, stopping, interval=0.01)
        try:
            self.assertEqual(next(stream)['type'], 'server.connected')
            for _ in range(3):
                self.assertEqual(next(stream)['type'], 'observer.tick')
            api.events.assert_called_once_with(timeout=15)
            release.set()
            self.assertEqual(next(stream)['type'], 'session.idle')
        finally:
            release.set()
            stream.close()

    def test_shutdown_publishes_intent_before_abort(self):
        api = mock.Mock()
        api.abort.side_effect = lambda session: self.assertTrue(live.read(self.directory)['stop_requested'])
        api.snapshot.return_value = {'request_id': 'm', 'status': 'interrupted', 'text': 'retained'}
        api.request.return_value = {}
        def stopped(*args):
            live.update(self.directory, server_lifecycle='stopped')
            return True
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(live, 'client', return_value=api), mock.patch.object(live, 'signal_owned', side_effect=stopped), contextlib.redirect_stdout(__import__('io').StringIO()):
            self.assertEqual(live.main(['shutdown', self.directory.name]), 0)
            api.abort.assert_called_once_with('s')

    def test_rollback_preserves_both_original_and_cleanup_errors(self):
        # The install's explicit cleanup diagnostic must retain the initiating failure.
        source = self.root / 'source'
        runtime = source / 'runtime'
        binary = runtime / 'node_modules/opencode-linux-x64/bin/opencode'
        binary.parent.mkdir(parents=True)
        binary.write_text('fixture')
        for name in (*install.MODULES, 'README.md', 'LICENSE'):
            (source / name).write_text('fixture')
        with mock.patch.object(install, 'SOURCE', source), mock.patch.object(install.shutil, 'copyfile', side_effect=OSError('original')), mock.patch.object(install, 'remove_tree', side_effect=OSError('cleanup')):
            with self.assertRaisesRegex(OSError, 'original; rollback also failed: cleanup'):
                install.install(self.root / 'prefix')


    def test_installer_rejects_external_bin_symlink_without_writes(self):
        prefix = self.root / 'prefix'
        prefix.mkdir()
        external = self.root / 'external'
        external.mkdir()
        (prefix / 'bin').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'inside the installation prefix'):
            install.install(prefix)
        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse((prefix / 'share').exists())

    def test_managed_start_accepts_both_long_context_forms(self):
        import io
        base = ['--task-file', 'fixture', '--worktree', '/tmp', '--model', 'gpt-6-luna', '--reasoning', 'high']
        for command in (['start'], ['run', '--live']):
            for flag, expected in (([], False), (['--long-context'], True), (['--no-long-context'], False)):
                with self.subTest(command=command, flag=flag), mock.patch.object(worker, 'STATE', self.root), mock.patch.object(live, 'start', return_value={'run_id': 'fixture'}) as start, mock.patch.object(live, 'wait', return_value={'status': 'complete'}), contextlib.redirect_stdout(io.StringIO()):
                    cli_router.main(command + base + flag)
                    self.assertIs(start.call_args.args[0].long_context, expected)
                    self.assertTrue((self.root / 'start.lock').is_file())

    def test_staged_unadopted_receipt_blocks_distinct_submission(self):
        orphan = self.directory / 'turn-orphan'
        orphan.mkdir()
        live.atomic(orphan / 'receipt.json', {'request_id': 'orphan', 'submission_id': 'old',
            'task_sha256': 'old', 'stage_status': 'staged', 'dispatch_status': 'pending'})
        with mock.patch.object(live, 'require_known_dispatch'), mock.patch.object(live.batch, 'credit_lock', side_effect=contextlib.nullcontext), mock.patch.object(live.batch, 'credit_preflight'), mock.patch.object(live.batch, 'validate_worktree'), mock.patch.object(live, 'ensure_server'), mock.patch.object(live, 'client') as client:
            with self.assertRaisesRegex(ValueError, 'Uncertain native staging receipt'):
                live.send(self.directory, 'distinct task', 'distinct')
            client.assert_not_called()
        self.assertEqual(live.read(self.directory)['current_request_id'], 'm')
        self.assertEqual(json.loads((orphan / 'receipt.json').read_text())['dispatch_status'], 'pending')

    def test_question_clears_idle_grace_even_with_unchanged_assistant(self):
        api = transport.NativeClient('http://127.0.0.1:1', 'fixture', '/tmp')
        messages = [{'info': {'id': 'a', 'role': 'user'}, 'parts': [{'type': 'text', 'text': 'task'}]},
            {'info': {'id': 'b', 'role': 'assistant', 'parentID': 'a', 'time': {}}, 'parts': []}]
        clock = [0.0]
        native = ['idle']
        questions = [[]]
        with mock.patch.object(api, 'messages_since', return_value=messages), mock.patch.object(api, 'questions', side_effect=lambda _: questions[0]), mock.patch.object(api, 'request', side_effect=lambda *a: {'s': {'type': native[0]}}), mock.patch.object(transport.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assertNotEqual(api.snapshot('s', 'a')['status'], 'incomplete')
            clock[0] = transport.DISPATCH_GRACE_SECONDS + 1
            native[0], questions[0] = 'busy', [{'id': 'q'}]
            self.assertEqual(api.snapshot('s', 'a')['status'], 'needs_input')
            self.assertNotIn(('s', 'a'), api.idle_unfinished)
            native[0], questions[0] = 'idle', []
            self.assertNotEqual(api.snapshot('s', 'a')['status'], 'incomplete')
            clock[0] += transport.DISPATCH_GRACE_SECONDS + 1
            self.assertEqual(api.snapshot('s', 'a')['status'], 'incomplete')


    def test_metadata_adoption_failure_leaves_staged_receipt_blocking_next_task(self):
        api = mock.Mock()
        api.stage.return_value = ('orphan', {})
        api.snapshot.return_value = {'status': 'complete', 'native_status': 'idle', 'request_id': 'm'}
        with mock.patch.object(live, 'require_known_dispatch'), mock.patch.object(live.batch, 'credit_lock', side_effect=contextlib.nullcontext), mock.patch.object(live.batch, 'credit_preflight'), mock.patch.object(live.batch, 'validate_worktree'), mock.patch.object(live, 'ensure_server'), mock.patch.object(live, 'remember'), mock.patch.object(live, 'client', return_value=api):
            with mock.patch.object(live, 'update', side_effect=OSError('metadata adoption failed')):
                with self.assertRaisesRegex(OSError, 'metadata adoption failed'):
                    live.send(self.directory, 'first task', 'first')
            api.dispatch.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'Uncertain native staging receipt'):
                live.send(self.directory, 'second task', 'second')
            api.stage.assert_called_once()
            api.dispatch.assert_not_called()

    def test_batch_task_durability_failure_prevents_launch(self):
        args, key = self.batch_args()
        with mock.patch.object(worker, 'STATE', self.root), mock.patch.object(worker, 'KEY', key), mock.patch.object(worker, 'durable_text', side_effect=OSError('task fsync failed')) as durable, mock.patch.object(worker.subprocess, 'Popen') as launch:
            with worker.credit_lock(): worker.credit_save(False, None)
            with self.assertRaisesRegex(OSError, 'task fsync failed'):
                worker.execute(args)
            self.assertEqual(durable.call_args.args[0].name, 'task.txt')
            launch.assert_not_called()

    def test_managed_task_durability_failure_prevents_staging(self):
        api = mock.Mock()
        api.snapshot.return_value = {'status': 'complete', 'native_status': 'idle', 'request_id': 'm'}
        with mock.patch.object(live, 'require_known_dispatch'), mock.patch.object(live.batch, 'credit_lock', side_effect=contextlib.nullcontext), mock.patch.object(live.batch, 'credit_preflight'), mock.patch.object(live.batch, 'validate_worktree'), mock.patch.object(live, 'ensure_server'), mock.patch.object(live, 'remember'), mock.patch.object(live, 'client', return_value=api), mock.patch.object(worker, 'durable_text', side_effect=OSError('task fsync failed')) as durable:
            with self.assertRaisesRegex(OSError, 'task fsync failed'):
                live.send(self.directory, 'durable task', 'durable')
            self.assertEqual(durable.call_args.args[0].name, 'task.txt')
            api.stage.assert_not_called()
            api.dispatch.assert_not_called()
