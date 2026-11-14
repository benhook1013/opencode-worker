"""Stable-idle evidence survives observer attachment but never authorizes busy work."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_transport as native
import live_worker as live


class DurableIdleTests(unittest.TestCase):
    def client(self):
        return native.NativeClient('http://127.0.0.1:1', 'fixture', '/fixture')

    def messages(self):
        return [{'info': {'id': 'm', 'role': 'user'}, 'parts': []},
                {'info': {'id': 'n', 'role': 'assistant', 'parentID': 'm', 'time': {}},
                 'parts': [{'type': 'text', 'text': 'partial'}]}]

    def snapshot(self, api, now, messages=None, busy=False):
        with patch.object(native, 'boot_id', return_value='boot-one'), \
                patch.object(native.time, 'monotonic', return_value=now), \
                patch.object(api, 'messages_since', return_value=messages or self.messages()), \
                patch.object(api, 'questions', return_value=[]), \
                patch.object(api, 'request', return_value={'s': {'type': 'busy'}} if busy else {}):
            return api.snapshot('s', 'm')

    def terminal(self):
        api = self.client()
        self.assertEqual(self.snapshot(api, 100)['status'], 'queued')
        result = self.snapshot(api, 111)
        self.assertEqual(result['status'], 'incomplete')
        return result

    def restore(self, api, result, boot='boot-one'):
        with patch.object(native, 'boot_id', return_value=boot), \
                patch.object(native.time, 'monotonic', return_value=112):
            api.restore_idle_evidence('s', 'm', result)

    def test_fresh_client_reuses_durable_terminal_stability(self):
        result = json.loads(json.dumps(self.terminal()))
        api = self.client()
        self.restore(api, result)
        self.assertEqual(self.snapshot(api, 112)['incomplete_reason'], 'idle_unfinished_assistant')

    def test_changed_message_restarts_stability(self):
        api = self.client()
        self.restore(api, self.terminal())
        messages = self.messages()
        messages[-1]['parts'][0]['text'] = 'new output'
        self.assertEqual(self.snapshot(api, 112, messages)['status'], 'queued')

    def test_busy_native_does_not_admit_and_resets_local_stability(self):
        api = self.client()
        self.restore(api, self.terminal())
        result = self.snapshot(api, 112, busy=True)
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['native_status'], 'busy')
        self.assertEqual(self.snapshot(api, 113)['status'], 'queued')

    def test_other_request_session_boot_or_unknown_boot_cannot_restore(self):
        result = self.terminal()
        for field, value, boot in [('request_id', 'other', 'boot-one'),
                                  ('session_id', 'other', 'boot-one'),
                                  ('request_id', 'm', 'boot-two'),
                                  ('request_id', 'm', None)]:
            with self.subTest(field=field, value=value, boot=boot):
                changed = copy.deepcopy(result)
                changed[field] = value
                api = self.client()
                self.restore(api, changed, boot)
                self.assertEqual(self.snapshot(api, 112)['status'], 'queued')

    def test_live_client_restores_saved_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'server-auth').write_text('fixture')
            (directory / 'turn-one').mkdir()
            (directory / 'turn-one/receipt.json').write_text(json.dumps({
                'request_id': 'm', 'dispatch_status': 'confirmed', 'dispatch_confirmed_at': 1}))
            live.atomic(directory / 'metadata.json', {'server_url': 'http://127.0.0.1:1',
                'worktree': '/fixture', 'session_id': 's', 'current_request_id': 'm',
                'latest_turn': 'turn-one', 'current_result': self.terminal()})
            with patch.object(native, 'boot_id', return_value='boot-one'), \
                    patch.object(native.time, 'monotonic', return_value=112):
                api = live.client(directory)
            self.assertEqual(self.snapshot(api, 112)['status'], 'incomplete')

    def test_repeated_restore_does_not_reset_new_local_stability(self):
        old = self.terminal()
        api = self.client()
        self.restore(api, old)
        changed = self.messages()
        changed[-1]['parts'][0]['text'] = 'changed'
        self.assertEqual(self.snapshot(api, 112, changed)['status'], 'queued')
        self.restore(api, old)
        self.assertEqual(self.snapshot(api, 123, changed)['status'], 'incomplete')
        self.snapshot(api, 124, busy=True)
        self.restore(api, old)
        self.assertEqual(self.snapshot(api, 125)['status'], 'queued')

    def test_new_evidence_survives_terminal_receipt_guard(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'turn-one').mkdir()
            original = self.terminal()
            live.atomic(directory / 'metadata.json', {'session_id': 's', 'current_request_id': 'm',
                'latest_turn': 'turn-one', 'current_result': original})
            api = self.client()
            self.restore(api, original)
            changed = self.messages()
            changed[-1]['parts'][0]['text'] = 'changed'
            result = self.snapshot(api, 112, changed)
            live.remember(directory, result)
            retained = live.read(directory)['current_result']
            self.assertEqual(retained['status'], 'incomplete')
            self.assertEqual(retained['idle_unfinished_evidence'], result['idle_unfinished_evidence'])
            fresh = self.client()
            self.restore(fresh, retained)
            self.assertEqual(self.snapshot(fresh, 123, changed)['status'], 'incomplete')
            live.remember(directory, self.snapshot(fresh, 124, busy=True))
            self.assertNotIn('idle_unfinished_evidence', live.read(directory)['current_result'])
