import importlib.util
import os
import argparse
import contextlib
import io
import json
import multiprocessing
import stat
import sys
from unittest import mock
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('worker', Path(__file__).resolve().parents[1] / 'worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class LauncherTests(unittest.TestCase):
    def test_no_inherited_secrets_and_native_tools_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            os.environ['OPENAI_API_KEY'] = 'fixture-only'
            settings = worker.config('openai/gpt-6-luna', 'high', False, 8, Path(temp) / 'dummy')
            env = worker.environment(Path(temp), settings)
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertEqual(settings['permission']['*'], 'deny')
            self.assertEqual(settings['permission']['external_directory'], 'deny')
            self.assertEqual(settings['permission']['edit'], 'deny')
            self.assertFalse(settings['lsp'])
            self.assertFalse(settings['formatter'])
            self.assertEqual(settings['provider']['openai']['whitelist'], ['gpt-6-luna'])
            self.assertEqual(settings['provider']['openai']['options']['baseURL'], 'https://api.openai.com/v1')
            self.assertEqual(env['OPENCODE_DISABLE_PROJECT_CONFIG'], 'true')
            self.assertEqual(env['OPENCODE_DISABLE_DEFAULT_PLUGINS'], 'true')
            self.assertEqual(env['OPENCODE_PURE'], 'true')
            self.assertEqual(worker.config('openai/gpt-6-luna', 'high', True, 8)['permission']['edit'], 'allow')
            del os.environ['OPENAI_API_KEY']

    def test_credential_metadata_rejects_insecure_file_and_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / 'dummy'
            key.write_text('fixture-only')
            key.chmod(0o600)
            worker.credential_check(key)
            key.chmod(0o644)
            with self.assertRaises(ValueError): worker.credential_check(key)
            key.chmod(0o600)
            alias = Path(temp) / 'alias'
            alias.symlink_to(key)
            with self.assertRaises(ValueError): worker.credential_check(alias)

    def test_worktree_rejects_protected_ancestors_and_symlink_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            project = home / 'project'
            project.mkdir()
            config = home / 'config'
            config.mkdir(mode=0o700)
            state = home / 'state'
            state.mkdir(mode=0o700)
            alias = home / 'alias'
            alias.symlink_to(home, target_is_directory=True)
            with mock.patch.object(worker, 'KEY', config / 'dummy-key'), mock.patch.object(worker, 'STATE', state):
                self.assertEqual(worker.validate_worktree(project), project)
                for candidate in (home, config, state, alias):
                    with self.assertRaisesRegex(ValueError, 'must not contain'):
                        worker.validate_worktree(candidate)
                with worker.credit_lock(): worker.credit_save(False, None)
                args = argparse.Namespace(action='run', model='openai/gpt-6-luna', worktree=str(alias))
                with mock.patch.object(worker, 'credential_check') as credential, mock.patch.object(worker.subprocess, 'Popen') as launch:
                    with self.assertRaisesRegex(ValueError, 'must not contain'): worker.execute(args)
                    credential.assert_not_called()
                    launch.assert_not_called()

    def test_native_event_result_collection(self):
        events = '\n'.join(['42', 'null', '[]', '{"type":"text","sessionID":"ses_fixture","part":{"text":"done"}}',
                            '{"type":"error","sessionID":"ses_fixture","error":{"name":"fixture"}}'])
        self.assertEqual(worker.collect(events), ('done', 'ses_fixture', True))
        with self.assertRaises(ValueError): worker.run_id('../escape')

    def test_explicit_ancestor_instructions_with_project_configuration_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'project'
            project.mkdir()
            (root / 'AGENTS.md').write_text('Ancestor fixture instructions')
            (project / 'AGENTS.md').write_text('Project fixture instructions')
            (project / 'nested').mkdir()
            (project / 'nested/AGENTS.md').write_text('Nested instructions: inspect before edits')
            settings = worker.config('openai/gpt-6-luna', 'high', False, 8, root / 'dummy', worktree=project)
            self.assertEqual(settings['instructions'][-2:], [str(root / 'AGENTS.md'), str(project / 'AGENTS.md')])
            self.assertNotIn(str(project / 'nested/AGENTS.md'), settings['instructions'])
            self.assertTrue(all(Path(path).is_absolute() for path in settings['instructions']))
            self.assertIn('read all applicable nested AGENTS.md', settings['agent']['worker']['prompt'])
            self.assertEqual(worker.environment(root, settings)['OPENCODE_DISABLE_PROJECT_CONFIG'], 'true')


class CreditGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.patch = mock.patch.object(worker, 'STATE', self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.temporary.cleanup)

    def reset(self):
        with worker.credit_lock():
            return worker.credit_save(False, None)

    def error(self, code, status=429):
        return json.dumps({'type': 'error', 'sessionID': 'ses_fixture', 'error': {
            'name': 'APIError', 'data': {'statusCode': status,
                'responseBody': json.dumps({'error': {'code': code}})}}})

    def test_exact_quota_codes_only_in_native_error_payload(self):
        for code in ('credit_balance_exhausted', 'insufficient_quota'):
            self.assertEqual(worker.exhaustion_reason(self.error(code)), code)
        for code in ('rate_limit_exceeded', 'invalid_api_key'):
            self.assertIsNone(worker.exhaustion_reason(self.error(code)))
        self.assertIsNone(worker.exhaustion_reason(json.dumps({'type': 'text', 'part': {'text': 'credit_balance_exhausted'}})))
        self.assertIsNone(worker.exhaustion_reason(json.dumps({'type': 'error', 'error': {'message': 'credit_balance_exhausted'}})))

    def test_missing_malformed_and_latched_states_fail_closed_until_explicit_reset(self):
        with worker.credit_lock():
            with self.assertRaisesRegex(ValueError, 'credit-reset'): worker.credit_preflight()
        self.reset()
        with worker.credit_lock(): worker.credit_preflight()
        path = self.state / 'credit-guard.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for content in ('bad json', '{}', '{"version":1,"exhausted":false}'):
            path.write_text(content)
            with worker.credit_lock():
                with self.assertRaisesRegex(ValueError, 'malformed'): worker.credit_preflight()
            self.assertEqual(path.read_text(), content)
        self.reset()
        with worker.credit_lock(): worker.credit_save(True, 'credit_balance_exhausted')
        with worker.credit_lock():
            with self.assertRaisesRegex(ValueError, 'paused'): worker.credit_preflight()
        self.reset()
        with worker.credit_lock(): worker.credit_preflight()

    def test_latched_run_and_resume_reject_before_credentials_or_native_launch(self):
        self.reset()
        with worker.credit_lock(): worker.credit_save(True, 'insufficient_quota')
        with mock.patch.object(worker, 'credential_check') as credential, mock.patch.object(worker.subprocess, 'Popen') as launch:
            for action in ('run', 'resume'):
                with self.assertRaisesRegex(ValueError, 'paused'):
                    worker.execute(argparse.Namespace(action=action))
            credential.assert_not_called()
            launch.assert_not_called()

    def test_fake_native_run_persists_global_latch_ram_and_stops_next_model(self):
        self.reset()
        project = self.root / 'project'
        project.mkdir()
        task = self.root / 'task.txt'
        task.write_text('Fixture only')
        key = self.root / 'dummy-key'
        key.write_text('fixture-only')
        key.chmod(0o600)
        binary = self.root / 'fake-opencode'
        binary.write_text(f'#!{sys.executable}\nimport sys\nsys.stdin.read()\nprint(' + repr(self.error('credit_balance_exhausted')) + ')\n')
        binary.chmod(0o700)
        args = argparse.Namespace(action='run', model='openai/gpt-6-luna', worktree=str(project),
            task_file=str(task), reasoning='high', allow_write=False, max_steps=None, timeout=None)
        with mock.patch.object(worker, 'KEY', key), mock.patch.object(worker, 'BINARY', binary), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(worker.execute(args), 1)
            meta = json.loads(output.getvalue())
            self.assertGreater(meta['native_child_peak_rss_kib'], 0)
            self.assertEqual(meta['credit_exhaustion_reason'], 'credit_balance_exhausted')
            self.assertEqual(meta['status'], 'failed')
            args.model = 'openai/another-fixture'
            with self.assertRaisesRegex(ValueError, 'paused'): worker.execute(args)
        with worker.credit_lock():
            self.assertTrue(worker.credit_state()['exhausted'])

    def test_cli_reset_and_status_are_local_explicit_operations(self):
        with mock.patch('sys.argv', ['api-worker', 'credit-reset', '--confirm-credit-available']), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(worker.main(), 0)
            self.assertFalse(json.loads(output.getvalue())['exhausted'])
        with mock.patch('sys.argv', ['api-worker', 'credit-status']), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(worker.main(), 0)
            self.assertEqual(json.loads(output.getvalue())['version'], 1)

    def test_shared_lock_serializes_dispatch_against_known_exhaustion(self):
        self.reset()
        context = multiprocessing.get_context('fork')
        parent, child = context.Pipe()
        def competing_dispatch():
            held.close()  # Drop the forked copy of the parent's lock descriptor.
            child.send('waiting')
            with worker.credit_lock():
                try:
                    worker.credit_preflight()
                except ValueError:
                    child.send('blocked')
                else:
                    child.send('launched')
        with worker.credit_lock() as held:
            process = context.Process(target=competing_dispatch)
            process.start()
            self.assertTrue(parent.poll(3))
            self.assertEqual(parent.recv(), 'waiting')
            worker.credit_save(True, 'credit_balance_exhausted')
        process.join(3)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail('Competing guard check did not finish')
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(parent.recv(), 'blocked')
        parent.close()
        child.close()

    def test_fake_rate_limit_failure_does_not_pause_future_dispatch(self):
        self.reset()
        with worker.credit_lock():
            reason = worker.exhaustion_reason(self.error('rate_limit_exceeded'))
            if reason:
                worker.credit_save(True, reason)
            worker.credit_preflight()
            self.assertFalse(worker.credit_state()['exhausted'])

    def native_fixture(self, events, *, budget=None, resume=False, native_code=0, timeout=False, wall_timeout=None):
        self.reset()
        project = self.root / 'project'
        project.mkdir(exist_ok=True)
        task = self.root / 'task.txt'
        task.write_text('Fixture only')
        key = self.root / 'dummy-key'
        key.write_text('fixture-only')
        key.chmod(0o600)
        binary = self.root / 'fake-opencode'
        binary.write_text(f'#!{sys.executable}\nimport sys,os,json\nsys.stdin.read()\n'
            'settings=json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])\n'
            'model=settings["model"].split("/",1)[1]\n'
            'assert settings["provider"]["openai"]["models"][model]["limit"] == {"context":1050000,"output":128000,"input":1050000}\n'
            + 'assert os.environ.get("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX") == ' + repr(str(budget) if budget else None) + '\n'
            + 'print(' + repr(events) + ',flush=True)\n' + ('import time; time.sleep(2)\n' if timeout else '') + 'sys.exit(' + str(native_code) + ')\n')
        binary.chmod(0o700)
        args = argparse.Namespace(action='resume' if resume else 'run', model='openai/gpt-6-luna', worktree=str(project),
            task_file=str(task), reasoning='high', allow_write=False, max_steps=None, timeout=0.1 if timeout else wall_timeout, max_output_tokens=budget)
        if resume:
            args.run_id = 'b' * 32
            directory = self.state / args.run_id
            directory.mkdir(exist_ok=True, mode=0o700)
            worker.save(directory / 'metadata.json', {'run_id': args.run_id, 'worktree': str(project),
                'model': args.model, 'reasoning': args.reasoning, 'allow_write': False, 'steps': 8, 'session_id': 'ses_fixture'})
        with mock.patch.object(worker, 'KEY', key), mock.patch.object(worker, 'BINARY', binary), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            code = worker.execute(args)
        return code, json.loads(output.getvalue())

    def terminal_events(self):
        return [{'type': 'step_start'}, {'type': 'text', 'sessionID': 'ses_fixture', 'part': {'text': 'Fixture done'}},
                {'type': 'step_finish', 'part': {'reason': 'stop'}}]

    def test_output_budget_config_bounds_and_legacy_default(self):
        args = argparse.Namespace(max_output_tokens=None)
        limits = worker.model_limits('openai/gpt-6-luna')
        self.assertIsNone(worker.output_budget(args, {}, limits))
        self.assertIsNone(worker.output_budget(args, {'max_output_tokens': 32768}, limits))
        self.assertEqual(worker.output_budget(args, {'max_output_tokens': 64000}, limits), 64000)
        args.max_output_tokens = 128000
        self.assertEqual(worker.output_budget(args, {}, limits), 128000)
        args.max_output_tokens = 0
        self.assertIsNone(worker.output_budget(args, {}, limits))
        for value in (-1, 128001, True):
            args.max_output_tokens = value
            with self.assertRaises(ValueError): worker.output_budget(args, {}, limits)
        settings = worker.config('openai/gpt-6-luna', 'high', False)
        self.assertEqual(settings['provider']['openai']['models']['gpt-6-luna']['limit'], {**limits, 'input': limits['context']})
        self.assertEqual(limits, {'context': 1050000, 'output': 128000})

    def test_actual_length_then_aborted_shape_is_incomplete_not_success(self):
        # Native failure shape: final step finishes length, then tool_use reports aborted.
        events = [{'type': 'step_start'}, {'type': 'step_finish', 'part': {'reason': 'length'}},
                  {'type': 'tool_use', 'part': {'callID': 'call_fixture', 'state': {'status': 'error', 'error': 'Tool execution aborted'}}}]
        code, meta = self.native_fixture('\n'.join(map(json.dumps, events)))
        self.assertEqual((code, meta['status'], meta['native_exit_code']), (2, 'incomplete', 0))
        self.assertEqual(meta['incomplete_reason'], 'output_limit_reached')
        self.assertEqual(meta['max_output_tokens'], 32000)
        self.assertTrue((Path(meta['artifacts']) / 'events.jsonl').exists())
        self.assertEqual((Path(meta['artifacts']) / 'result.txt').read_text(), '')

    def test_missing_terminal_text_pending_and_aborted_calls(self):
        for events, reason in (([], 'missing_terminal_stop'),
            ([{'type': 'step_finish', 'part': {'reason': 'stop'}}], 'missing_terminal_text'),
            (self.terminal_events() + [{'type': 'tool_use', 'part': {'state': {'status': 'pending'}}}], 'unfinished_tool_call'),
            (self.terminal_events() + [{'type': 'tool_use', 'part': {'state': {'status': 'running'}}}], 'unfinished_tool_call'),
            (self.terminal_events() + [{'type': 'tool_use', 'part': {'state': {'status': 'error', 'error': 'Tool execution aborted'}}}], 'aborted_tool_call')):
            self.assertEqual(worker.incomplete_reason('\n'.join(map(json.dumps, events))), reason)

    def test_recovered_earlier_tool_error_does_not_fail_terminal_success(self):
        events = [{'type': 'step_start'}, {'type': 'tool_use', 'part': {'callID': 'recovered', 'state': {'status': 'error', 'error': 'verification failed'}}},
                  {'type': 'step_finish', 'part': {'reason': 'tool-calls'}}] + self.terminal_events()
        code, meta = self.native_fixture('\n'.join(map(json.dumps, events)))
        self.assertEqual((code, meta['status']), (0, 'complete'))
        events[1]['part']['state']['error'] = 'Tool execution aborted'
        self.assertIsNone(worker.incomplete_reason('\n'.join(map(json.dumps, events))))

    def test_legacy_resume_default_explicit_budget_override_and_native_failure(self):
        events = '\n'.join(map(json.dumps, self.terminal_events()))
        code, meta = self.native_fixture(events, resume=True)
        self.assertEqual((code, meta['max_output_tokens']), (0, 32000))
        code, meta = self.native_fixture(events, budget=64000, resume=True)
        self.assertEqual((code, meta['max_output_tokens']), (0, 64000))
        saved = json.loads((Path(meta['artifacts']) / 'config.json').read_text())
        self.assertEqual(saved['provider']['openai']['models']['gpt-6-luna']['limit']['output'], 128000)
        code, meta = self.native_fixture('', native_code=7)
        self.assertEqual((code, meta['status'], meta['native_exit_code']), (7, 'failed', 7))

    def test_interruption_remains_interrupted_even_with_native_error_events(self):
        code, meta = self.native_fixture(self.error('rate_limit_exceeded'), timeout=True)
        self.assertEqual((code, meta['status']), (124, 'interrupted'))
        self.assertNotIn('incomplete_reason', meta)

    def test_repeated_sigterm_during_cleanup_preserves_escalation_and_metadata(self):
        previous = worker.signal.getsignal(worker.signal.SIGTERM)
        for first in (KeyboardInterrupt(), worker.subprocess.TimeoutExpired('fixture', 1)):
            with self.subTest(first=type(first).__name__):
                process = mock.Mock(pid=12345, returncode=-9)
                process.communicate.side_effect = first
                waits = []
                def wait(timeout=None):
                    self.assertEqual(worker.signal.getsignal(worker.signal.SIGTERM), worker.signal.SIG_IGN)
                    # A real repeated signal must be harmless during both waits.
                    worker.signal.raise_signal(worker.signal.SIGTERM)
                    waits.append(timeout)
                    if timeout is not None:
                        raise worker.subprocess.TimeoutExpired('fixture', timeout)
                    return -9
                process.wait.side_effect = wait
                original_save = worker.save
                def save(path, value):
                    if value.get('status') == 'interrupted':
                        self.assertEqual(worker.signal.getsignal(worker.signal.SIGTERM), worker.signal.SIG_IGN)
                        worker.signal.raise_signal(worker.signal.SIGTERM)
                    return original_save(path, value)
                with mock.patch.object(worker.subprocess, 'Popen', return_value=process), \
                        mock.patch.object(worker.os, 'killpg') as kill, \
                        mock.patch.object(worker, 'save', side_effect=save):
                    code, meta = self.native_fixture('')
                self.assertEqual((code, meta['status']), (124, 'interrupted'))
                self.assertEqual(waits, [3, None])
                self.assertEqual(kill.call_args_list, [mock.call(12345, worker.signal.SIGTERM),
                                                       mock.call(12345, worker.signal.SIGKILL)])
                saved = json.loads((Path(meta['artifacts']).parent / 'metadata.json').read_text())
                self.assertEqual(saved['status'], 'interrupted')
                self.assertEqual(worker.signal.getsignal(worker.signal.SIGTERM), previous)

    def test_missing_child_group_during_cleanup_still_finalizes(self):
        previous = worker.signal.getsignal(worker.signal.SIGTERM)
        for missing in (worker.signal.SIGTERM, worker.signal.SIGKILL):
            with self.subTest(missing=missing):
                process = mock.Mock(pid=12345, returncode=0)
                process.communicate.side_effect = worker.subprocess.TimeoutExpired('fixture', 1)
                process.wait.side_effect = ([0] if missing == worker.signal.SIGTERM else
                    [worker.subprocess.TimeoutExpired('fixture', 3), 0])
                def killpg(pid, signum):
                    if signum == missing:
                        raise ProcessLookupError('child group already exited')
                with mock.patch.object(worker.subprocess, 'Popen', return_value=process), \
                        mock.patch.object(worker.os, 'killpg', side_effect=killpg):
                    code, meta = self.native_fixture('')
                self.assertEqual((code, meta['status']), (124, 'interrupted'))
                self.assertEqual(process.wait.call_count, 1 if missing == worker.signal.SIGTERM else 2)
                artifact = Path(meta['artifacts'])
                saved = json.loads((artifact.parent / 'metadata.json').read_text())
                self.assertEqual(saved['status'], 'interrupted')
                self.assertTrue(artifact.joinpath('result.txt').exists())
                self.assertEqual(worker.signal.getsignal(worker.signal.SIGTERM), previous)

    def test_native_defaults_compaction_and_request_timeouts(self):
        settings = worker.config('openai/gpt-6-luna', 'high', False)
        self.assertNotIn('steps', settings['agent']['worker'])
        self.assertTrue(settings['compaction']['auto'])
        self.assertEqual(settings['agent']['compaction']['model'], settings['model'])
        self.assertNotIn('disable', settings['agent']['compaction'])
        self.assertNotIn('timeout', settings['provider']['openai']['options'])
        self.assertEqual(settings['small_model'], settings['model'])
        env = worker.environment(self.root, settings)
        self.assertNotIn('OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX', env)
        self.assertEqual(worker.environment(self.root, settings, 64000)['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'], '64000')

    def test_legacy_step_and_output_provenance(self):
        args = argparse.Namespace(max_steps=None, max_output_tokens=None)
        self.assertIsNone(worker.step_limit(args, {'steps': 8}))
        self.assertEqual(worker.step_limit(args, {'steps': 12}), 12)
        self.assertEqual(worker.step_limit(args, {'steps': 8, 'steps_explicit': True}), 8)
        args.max_steps = 0
        self.assertIsNone(worker.step_limit(args, {'steps': 12, 'steps_explicit': True}))
        args.max_steps = 500
        self.assertEqual(worker.step_limit(args, {}), 500)
        limits = worker.model_limits('openai/gpt-6-luna')
        self.assertIsNone(worker.output_budget(args, {'max_output_tokens': 4096}, limits))
        self.assertIsNone(worker.output_budget(args, {'max_output_tokens': 32768}, limits))
        self.assertEqual(worker.output_budget(args, {'max_output_tokens': 32768, 'output_limit_explicit': True}, limits), 32768)

    def test_unknown_model_requires_truthful_explicit_metadata(self):
        with self.assertRaisesRegex(ValueError, 'Unknown model limits'):
            worker.model_limits('openai/unknown-fixture')
        args = argparse.Namespace(context_window=200000, model_output_limit=50000)
        limits = worker.model_limits('openai/unknown-fixture', args)
        self.assertEqual(limits, {'context': 200000, 'output': 50000})
        settings = worker.config('openai/unknown-fixture', 'high', False, limits=limits)
        self.assertEqual(settings['provider']['openai']['models']['unknown-fixture']['limit'], {**limits, 'input': limits['context']})
        self.assertEqual(worker.model_limits('openai/unknown-fixture', meta={'model_limits_explicit': True, 'model_limits': limits}), limits)
        args.context_window = 0
        with self.assertRaises(ValueError): worker.model_limits('openai/gpt-6-luna', args)

    def test_default_unlimited_and_explicit_two_hour_task_use_correct_communicate_timeout(self):
        events = '\n'.join(map(json.dumps, self.terminal_events()))
        for selected in (None, 7200):
            process = mock.Mock(returncode=0)
            def communicate(task, *, timeout):
                # Simulates a healthy long task without sleeping or API work.
                self.assertEqual(timeout, selected)
            process.communicate.side_effect = communicate
            def launch(command, **kwargs):
                kwargs['stdout'].write(events.encode())
                kwargs['stdout'].flush()
                return process
            with mock.patch.object(worker.subprocess, 'Popen', side_effect=launch), mock.patch.object(worker.os, 'killpg') as kill:
                code, meta = self.native_fixture(events, wall_timeout=selected)
            self.assertEqual((code, meta['status'], meta['timeout_seconds']), (0, 'complete', selected))
            self.assertIsNone(meta['steps'])
            self.assertFalse(meta['steps_explicit'])
            self.assertFalse(meta['output_limit_explicit'])
            kill.assert_not_called()

    def test_legacy_resume_drops_implicit_steps_and_uses_verified_model_context(self):
        code, meta = self.native_fixture('\n'.join(map(json.dumps, self.terminal_events())), resume=True)
        settings = json.loads((Path(meta['artifacts']) / 'config.json').read_text())
        self.assertEqual(code, 0)
        self.assertNotIn('steps', settings['agent']['worker'])
        self.assertEqual(settings['provider']['openai']['models']['gpt-6-luna']['limit']['context'], 1050000)
        self.assertFalse(meta['steps_explicit'])
