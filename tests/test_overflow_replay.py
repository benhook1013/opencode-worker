"""Pinned native overflow replay copies part metadata, unlike manual follow-ups."""
import unittest
from unittest.mock import patch

import native_transport as transport


def user(identifier, parts):
    return {'info': {'id': identifier, 'role': 'user'}, 'parts': parts}


def assistant(identifier, parent, summary=False):
    return {'info': {'id': identifier, 'role': 'assistant', 'parentID': parent,
                     'summary': summary, 'finish': 'stop', 'time': {'completed': 1}},
            'parts': [{'type': 'text', 'text': 'Recovered answer'}]}


class OverflowReplayTests(unittest.TestCase):
    def messages(self, marker='a', overflow=True):
        return [user('a', [{'type': 'text', 'text': 'Original task',
                           'metadata': {transport.REQUEST_MARKER: 'a'}}]),
                user('b', [{'type': 'compaction', 'auto': True, 'overflow': overflow}]),
                assistant('c', 'b', summary=True),
                user('d', [{'type': 'text', 'text': 'Original task',
                           'metadata': {transport.REQUEST_MARKER: marker}}]),
                assistant('e', 'd')]

    def test_owned_overflow_replay_is_included_without_synthetic_markers(self):
        self.assertEqual([m['info']['id'] for m in transport.request_assistants(self.messages(), 'a')], ['e'])

    def test_manual_same_text_and_unrelated_markers_remain_distinct(self):
        for marker in [None, 'other-request']:
            with self.subTest(marker=marker):
                self.assertEqual(transport.request_assistants(self.messages(marker), 'a'), [])

    def test_matching_marker_requires_completed_owned_overflow(self):
        self.assertEqual(transport.request_assistants(self.messages(overflow=False), 'a'), [])
        messages = self.messages()
        messages[2]['info']['finish'] = 'error'
        self.assertEqual(transport.request_assistants(messages, 'a'), [])
        messages = self.messages()
        messages[2]['info']['parentID'] = 'unrelated'
        self.assertEqual(transport.request_assistants(messages, 'a'), [])

    def test_manual_boundary_after_replay_is_not_crossed(self):
        messages = self.messages() + [user('f', [{'type': 'text', 'text': 'Original task'}]), assistant('g', 'f')]
        self.assertEqual([m['info']['id'] for m in transport.request_assistants(messages, 'a')], ['e'])

    def test_staged_payload_preserves_native_ids_and_marks_request(self):
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture', '/fixture')
        receipt = user('a', [{'id': 'part-one', 'type': 'text', 'text': 'Task'}])
        with patch.object(client, 'request', return_value=receipt):
            request_id, payload = client.stage('session', 'Task', 'openai/model', 'high')
        self.assertEqual(request_id, 'a')
        self.assertEqual(payload['parts'][0]['id'], 'part-one')
        self.assertEqual(payload['parts'][0]['metadata'][transport.REQUEST_MARKER], 'a')

    def test_idle_snapshot_returns_recovered_answer_and_clock_epoch(self):
        client = transport.NativeClient('http://127.0.0.1:1', 'fixture', '/fixture')
        with patch.object(client, 'messages_since', return_value=self.messages()), \
                patch.object(client, 'request', side_effect=[[], {}, [], {}]), \
                patch.object(transport, 'boot_id', return_value='fixture-boot'):
            snapshot = client.snapshot('session', 'a')
        self.assertEqual(snapshot['status'], 'complete')
        self.assertEqual(snapshot['text'], 'Recovered answer')
        self.assertEqual(snapshot['observed_boot_id'], 'fixture-boot')

    def test_unavailable_boot_identity_is_unknown(self):
        with patch.object(transport.Path, 'read_text', side_effect=OSError):
            self.assertIsNone(transport.boot_id())
