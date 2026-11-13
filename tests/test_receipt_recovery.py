"""Regression coverage for questions, control commits and compaction dispatch proof."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import live_worker as live
import native_transport as native

class ReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.directory = Path(t.name)
        (self.directory / 'turn').mkdir()
        live.atomic(self.directory / 'metadata.json', {'session_id':'s', 'current_request_id':'m',
            'latest_turn':'turn', 'server_lifecycle':'running'})

    def test_question_supersedes_provisional_incomplete_but_not_completed(self):
        incomplete = {'request_id':'m', 'status':'incomplete', 'incomplete_reason':'idle_unfinished_assistant',
                      'observed_at_ns':1, 'observed_boot_id':'boot'}
        question = {'request_id':'m', 'status':'needs_input', 'questions':[{'id':'q'}],
                    'observed_at_ns':2, 'observed_boot_id':'boot'}
        live.remember(self.directory, incomplete)
        live.remember(self.directory, question)
        with patch.object(live, 'client') as client:
            client.return_value.snapshot.return_value = question
            self.assertEqual(live.status(self.directory)['questions'], [{'id':'q'}])
            self.assertEqual(live.wait(self.directory)['status'], 'needs_input')
        live.remember(self.directory, {'request_id':'m','status':'complete','text':'done'})
        live.remember(self.directory, question)
        self.assertEqual(live.read(self.directory)['current_result']['status'], 'complete')

    def test_compaction_failure_confirms_lost_dispatch_without_summary_answer(self):
        messages = [ {'info':{'id':'m','role':'user'},'parts':[]},
            {'info':{'id':'n','role':'user'},'parts':[{'type':'compaction','auto':True}]},
            {'info':{'id':'o','role':'assistant','parentID':'n','summary':True,'error':{'name':'APIError'}},
             'parts':[{'type':'text','text':'not the answer'}]} ]
        api = native.NativeClient('http://127.0.0.1:1','fixture','/fixture')
        with patch.object(api,'messages_since',return_value=messages), patch.object(api,'questions',return_value=[]), patch.object(api,'request',return_value={}):
            result = api.snapshot('s','m')
        self.assertTrue(result['execution_observed'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['text'],'')
        live.atomic(self.directory/'turn/receipt.json',{'request_id':'m','dispatch_status':'pending'})
        live.update(self.directory,pending_dispatch='turn')
        live.remember(self.directory,result)
        self.assertIsNone(live.read(self.directory)['pending_dispatch'])
        self.assertEqual(json.loads((self.directory/'turn/receipt.json').read_text())['dispatch_status'],'confirmed')
        with patch.object(live,'client',return_value=api), patch.object(api,'snapshot',return_value=result):
            self.assertEqual(live.status(self.directory)['status'],'failed')

    def test_interrupted_controls_block_both_launch_paths_and_can_be_reapplied(self):
        old = {'agent':{'worker':{'prompt':'fixture'}}}
        live.atomic(self.directory/'settings.json',old)
        live.update(self.directory,server_lifecycle='stopped',model='openai/gpt-6-luna',reasoning='high',
                    allow_write=False,worktree=str(self.directory),settings_sha256=live.settings_digest(old))
        args=argparse.Namespace(max_steps=3,max_output_tokens=None,context_window=None,model_output_limit=None,
                                timeout=None,long_context=None)
        real_update=live.update
        def fail_commit(directory,**fields):
            if fields.get('controls_pending') is False:
                raise OSError('simulated interrupted metadata commit')
            return real_update(directory,**fields)
        with patch.object(live,'update',side_effect=fail_commit):
            with self.assertRaisesRegex(OSError,'simulated'):
                live.configure_idle(self.directory,args)
        self.assertTrue(live.read(self.directory)['controls_pending'])
        with patch.object(live.subprocess,'Popen') as spawn, patch.object(live.batch,'STATE',live.batch.STATE):
            for launch in (live.ensure_server,live.run_server):
                with self.assertRaisesRegex(ValueError,'Interrupted control update'):
                    launch(self.directory)
            spawn.assert_not_called()
        live.configure_idle(self.directory,args)
        self.assertFalse(live.read(self.directory)['controls_pending'])
        self.assertEqual(live.checked_settings(self.directory,live.read(self.directory))['agent']['worker']['steps'],3)
        changed=live.checked_settings(self.directory,live.read(self.directory))
        changed['agent']['worker']['steps']=4
        live.atomic(self.directory/'settings.json',changed)
        with self.assertRaisesRegex(ValueError,'Interrupted control update'):
            live.checked_settings(self.directory,live.read(self.directory))
