"""Offline classifier regressions; synthetic schedules are not native timing reproductions."""
import unittest
import tempfile
from pathlib import Path
from unittest import mock

import native_transport as transport
import live_worker as live


class LateSessionErrorTests(unittest.TestCase):
    def test_synthetic_prior_request_error_cannot_attach_to_new_idle_request(self):
        """Guard a synthetic delayed-event schedule, without claiming native emits that schedule."""
        api = transport.NativeClient('http://127.0.0.1:1', 'fixture-only', '/fixture')
        api.messages_since = mock.Mock(return_value=[
            {'info': {'id': 'old', 'role': 'user'}, 'parts': []},
            {'info': {'id': 'old-answer', 'role': 'assistant', 'parentID': 'old',
                      'finish': 'stop', 'time': {'completed': 99}},
             'parts': [{'type': 'text', 'text': 'Earlier result'}]},
            {'info': {'id': 'request', 'role': 'user'}, 'parts': []},
        ])
        api.questions = mock.Mock(return_value=[])
        api.request = mock.Mock(return_value={})
        api.note_dispatch('session', 'old', 90)
        api.note_dispatch('session', 'request', 100)
        with mock.patch.object(transport.time, 'time', return_value=100):
            self.assertEqual(api.snapshot('session', 'request')['status'], 'queued')
            # Explicit old identity is filtered out correctly.
            error = {'name': 'UnknownError', 'data': {'message': 'Prior request error'}}
            api.observe_dispatch('session', 'request', {'type': 'session.error', 'properties': {
                'sessionID': 'session', 'messageID': 'old', 'error': error}})
            self.assertEqual(api.snapshot('session', 'request')['status'], 'queued')
            # Without identity the diagnostic cannot establish ownership of the new request.
            api.observe_dispatch('session', 'request', {'type': 'session.error', 'properties': {
                'sessionID': 'session', 'error': error}})
            result = api.snapshot('session', 'request')
            self.assertEqual(result['status'], 'queued', result)
            self.assertNotIn('error', api.dispatch_context[('session', 'request')])
        with mock.patch.object(transport.time, 'time', return_value=110):
            result = api.snapshot('session', 'request')
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['incomplete_reason'], 'dispatched_without_execution')
        self.assertNotIn('error', result)
        self.assertFalse(result['execution_observed'])
        self.assertEqual(result['request_id'], 'request')


class RetainedSessionErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / 'turn-fixture').mkdir()
        (self.directory / 'server-auth').write_text('fixture-only')
        live.atomic(self.directory / 'metadata.json', {
            'session_id': 'session', 'current_request_id': 'request', 'latest_turn': 'turn-fixture',
            'submitted_at': 100, 'server_url': 'http://127.0.0.1:1', 'worktree': '/fixture',
            'server_lifecycle': 'running', 'current_result': None, 'pending_dispatch': None})
        live.atomic(self.directory / 'turn-fixture/receipt.json', {
            'request_id': 'request', 'dispatch_status': 'confirmed', 'dispatch_confirmed_at': 100})
        self.error = {'name': 'UnknownError', 'data': {'message': 'Fixture prior error'}}

    def snapshot(self, now=100):
        with mock.patch.object(live.NativeClient, 'messages_since', return_value=[]), \
                mock.patch.object(live.NativeClient, 'questions', return_value=[]), \
                mock.patch.object(live.NativeClient, 'request', return_value={}), \
                mock.patch.object(live.time, 'time', return_value=now):
            return live.status(self.directory)

    def test_unattributed_and_stale_events_remain_raw_without_request_attachment(self):
        for identity in ({}, {'messageID': 'old'}):
            with self.subTest(identity=identity):
                event = {'type': 'session.error', 'properties': {
                    'sessionID': 'session', 'error': self.error, **identity}}
                live.observe(self.directory, event, record=True)
                self.assertNotIn('session_error', live.read(self.directory))
                self.assertEqual(self.snapshot()['status'], 'queued')
        retained = (self.directory / 'native-events.jsonl').read_text()
        self.assertIn('Fixture prior error', retained)
        expired = self.snapshot(110)
        self.assertEqual(expired['status'], 'incomplete')
        self.assertNotIn('error', expired)

    def test_explicit_error_restores_for_late_status_and_wait(self):
        live.observe(self.directory, {'type': 'session.error', 'properties': {
            'sessionID': 'session', 'messageID': 'request', 'error': self.error}})
        self.assertEqual(live.read(self.directory)['session_error']['attribution'], 'explicit_message_id')
        result = self.snapshot()
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['error'], self.error)
        self.assertEqual(result['error_attribution'], 'explicit_message_id')
        live.update(self.directory, server_lifecycle='stopped')
        self.assertEqual(live.wait(self.directory)['error'], self.error)

    def test_legacy_inferred_error_does_not_restore_ownership_or_shorten_grace(self):
        live.update(self.directory, session_error={'request_id': 'request', 'error': self.error})
        self.assertEqual(self.snapshot()['status'], 'queued')
        expired = self.snapshot(110)
        self.assertEqual(expired['status'], 'incomplete')
        self.assertNotIn('error', expired)

    def test_legacy_cached_startup_error_is_a_diagnostic_on_late_status_and_wait(self):
        live.update(self.directory, server_lifecycle='stopped', current_result={
            'request_id': 'request', 'status': 'incomplete', 'native_status': 'idle',
            'incomplete_reason': 'dispatched_without_execution', 'error': self.error})
        for operation in (live.status, live.wait):
            result = operation(self.directory)
            self.assertNotIn('error', result)
            self.assertEqual(result['unattributed_session_error'], self.error)

    def test_session_only_quota_error_still_latches_global_credit_guard(self):
        from contextlib import nullcontext
        with mock.patch.object(live.batch, 'credit_lock', return_value=nullcontext()), \
                mock.patch.object(live.batch, 'credit_state'), mock.patch.object(live.batch, 'credit_save') as save:
            live.observe(self.directory, {'type': 'session.error', 'properties': {
                'sessionID': 'session', 'error': {'name': 'APIError', 'data': {'code': 'insufficient_quota'}}}})
            save.assert_called_once_with(True, 'insufficient_quota')
        self.assertNotIn('session_error', live.read(self.directory))


if __name__ == '__main__':
    unittest.main()
