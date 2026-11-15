"""Retained native failures and shutdown must survive later control operations."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import live_worker as live
import worker


class LiveLatchShutdownTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / ('a' * 32)
        self.directory.mkdir(mode=0o700)
        self.turn = self.directory / 'turn-one'
        self.turn.mkdir(mode=0o700)
        live.atomic(self.directory / 'metadata.json', {
            'session_id': 's', 'current_request_id': 'm', 'latest_turn': self.turn.name,
            'server_lifecycle': 'running', 'pending_dispatch': None})
        self.state = mock.patch.object(worker, 'STATE', self.root)
        self.state.start()
        self.addCleanup(self.state.stop)
        worker.credit_save(False, None)

    def error_result(self):
        return {'request_id': 'm', 'status': 'failed', 'native_status': 'idle',
                'text': 'retained output', 'execution_observed': True,
                'error': {'name': 'APIError', 'data': {'code': 'insufficient_quota'}}}

    def test_reset_retained_and_stopped_reads_do_not_relatch_old_failure(self):
        result = self.error_result()
        live.remember(self.directory, result)
        self.assertTrue(worker.credit_state()['exhausted'])
        with worker.credit_lock():
            worker.credit_save(False, None)
        live.remember(self.directory, result)
        live.update(self.directory, server_lifecycle='stopped')
        self.assertEqual(live.status(self.directory)['text'], 'retained output')
        worker.credit_preflight()
        # A different request with the same provider failure still closes dispatch.
        live.update(self.directory, current_request_id='next', current_result=None)
        live.remember(self.directory, {**result, 'request_id': 'next'})
        self.assertTrue(worker.credit_state()['exhausted'])

    def test_reset_then_followup_can_dispatch_after_old_error_snapshot(self):
        result = self.error_result()
        live.remember(self.directory, result)
        with worker.credit_lock():
            worker.credit_save(False, None)
        live.update(self.directory, worktree=str(self.root), model='openai/gpt-6-luna', reasoning='high')
        api = mock.Mock()
        api.snapshot.return_value = result
        api.stage.return_value = ('next', {'parts': []})
        with mock.patch.object(live, 'ensure_server'), \
                mock.patch.object(worker, 'validate_worktree'), \
                mock.patch.object(live, 'client', return_value=api):
            sent = live.send(self.directory, 'next authorized task', 'new-submission')
        self.assertEqual(sent['request_id'], 'next')
        api.dispatch.assert_called_once()
        worker.credit_preflight()

    def test_observer_and_snapshot_share_request_latch_identity(self):
        result = self.error_result()
        event = {'type': 'session.error', 'properties': {
            'sessionID': 's', 'messageID': 'm', 'error': result['error']}}
        live.observe(self.directory, event)
        with worker.credit_lock():
            worker.credit_save(False, None)
        live.remember(self.directory, result)
        live.observe(self.directory, event)
        worker.credit_preflight()
        # A distinct supported exhaustion reason is new evidence.
        other = {'code': 'credit_balance_exhausted'}
        live.observe(self.directory, {'type': 'session.error', 'properties': {
            'sessionID': 's', 'messageID': 'm', 'error': other}})
        self.assertEqual(worker.credit_state()['reason'], 'credit_balance_exhausted')

    def test_shutdown_retains_result_before_signalling_owner(self):
        result = {'request_id': 'm', 'status': 'interrupted', 'native_status': 'idle',
                  'text': 'final output', 'execution_observed': True}
        api = mock.Mock()
        api.request.return_value = {}
        api.snapshot.return_value = result
        def signal(meta, kind, signum):
            self.assertEqual(live.read(self.directory)['current_result'], result)
            self.assertEqual((self.turn / 'result.txt').read_text(), 'final output')
            live.update(self.directory, server_lifecycle='stopped')
            return True
        with mock.patch.object(live, 'client', return_value=api), \
                mock.patch.object(live, 'signal_owned', side_effect=signal) as stopped, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(['shutdown', self.directory.name]), 0)
        api.snapshot.assert_called_once_with('s', 'm')
        self.assertEqual(stopped.call_count, 1)

    def test_shutdown_snapshot_failure_does_not_signal_processes(self):
        api = mock.Mock()
        api.request.return_value = {}
        api.snapshot.side_effect = OSError('lost terminal observation')
        with mock.patch.object(live, 'client', return_value=api), \
                mock.patch.object(live, 'signal_owned') as stopped:
            with self.assertRaises(OSError):
                live.main(['shutdown', self.directory.name])
        stopped.assert_not_called()


if __name__ == '__main__':
    unittest.main()
