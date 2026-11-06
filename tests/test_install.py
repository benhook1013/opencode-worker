"""Packaging checks use disposable homes/prefixes; never inspect host credentials/state."""
import json
import importlib.util
from unittest import mock
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def environment(self, root):
        home = root / 'home'
        home.mkdir()
        return {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home),
                'XDG_CONFIG_HOME': str(root / 'xdg-config'), 'XDG_STATE_HOME': str(root / 'xdg-state')}

    def invoke(self, argv, env, cwd):
        return subprocess.run(argv, env=env, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_relocated_install_help_doctor_paths_and_clobber_refusal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = self.environment(root)
            prefix = root / 'prefix with spaces'
            command = [sys.executable, str(ROOT / 'install.py'), '--prefix', str(prefix)]
            installed = self.invoke(command, env, root)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            launcher = prefix / 'bin/api-worker'
            self.assertEqual(self.invoke([str(launcher), '--help'], env, root).returncode, 0)
            for subcommand in ('start', 'run', 'send', 'resume', 'wait', 'reply', 'shutdown'):
                result = self.invoke([str(launcher), subcommand, '--help'], env, root)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = self.invoke([str(launcher), 'doctor', '--offline'], env, root)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertTrue(receipt['binary_exists'])
            self.assertEqual(receipt['state'], str(root / 'xdg-state/api-worker'))
            self.assertTrue(receipt['binary'].startswith(str(prefix)))
            self.assertFalse((root / 'xdg-state').exists())  # Offline doctor creates nothing.
            self.assertFalse((root / 'xdg-config').exists())
            before = launcher.read_bytes()
            refused = self.invoke(command, env, root)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('Refusing existing', refused.stderr)
            self.assertEqual(launcher.read_bytes(), before)
            # Existing unrelated prefix contents are never overwritten either.
            second = root / 'existing'
            (second / 'bin').mkdir(parents=True)
            (second / 'bin/api-worker').write_text('unrelated sentinel')
            refused = self.invoke([sys.executable, str(ROOT / 'install.py'), '--prefix', str(second)], env, root)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual((second / 'bin/api-worker').read_text(), 'unrelated sentinel')
            self.assertFalse((second / 'share/api-worker').exists())

    def test_xdg_home_defaults_explicit_overrides_and_relative_path_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = self.environment(root)
            script = ('import sys;sys.path.insert(0,sys.argv[1]);import worker,json;'
                      'print(json.dumps([str(worker.ROOT),str(worker.STATE),str(worker.KEY)]))')
            command = [sys.executable, '-I', '-c', script, str(ROOT)]
            result = self.invoke(command, env, root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [str(ROOT), str(root / 'xdg-state/api-worker'), str(root / 'xdg-config/api-worker/openai-api-key')])
            env.pop('XDG_CONFIG_HOME')
            env.pop('XDG_STATE_HOME')
            result = self.invoke(command, env, root)
            self.assertEqual(json.loads(result.stdout)[1:], [str(root / 'home/.local/state/api-worker'), str(root / 'home/.config/api-worker/openai-api-key')])
            env.update(API_WORKER_STATE_DIR=str(root / 'private-state'), API_WORKER_CONFIG_DIR=str(root / 'private-config'))
            result = self.invoke(command, env, root)
            self.assertEqual(json.loads(result.stdout)[1:], [str(root / 'private-state'), str(root / 'private-config/openai-api-key')])
            env['API_WORKER_STATE_DIR'] = 'relative-state'
            result = self.invoke(command, env, root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('absolute path', result.stderr)

    def test_source_runtime_prefixes_are_rejected_before_creating_or_copying_output(self):
        """Reject the runtime, descendants, and symlink aliases before recursive staging can begin."""
        spec = importlib.util.spec_from_file_location('fixture_installer', ROOT / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / 'source/runtime'
            binary = runtime / 'node_modules/opencode-linux-x64/bin/opencode'
            binary.parent.mkdir(parents=True)
            binary.write_text('fixture')
            alias = root / 'runtime-alias'
            alias.symlink_to(runtime, target_is_directory=True)
            external = root / 'external-prefix'
            external.mkdir()
            (external / 'share').symlink_to(runtime / 'node_modules', target_is_directory=True)
            for prefix in (runtime, runtime / 'nested/prefix', alias, alias / 'nested/prefix', external):
                with self.subTest(prefix=prefix), mock.patch.object(installer, 'SOURCE', runtime.parent), \
                        mock.patch.object(Path, 'mkdir') as mkdir, \
                        mock.patch.object(installer.shutil, 'copytree') as copytree:
                    with self.assertRaisesRegex(ValueError, 'outside the source runtime'):
                        installer.install(prefix)
                    mkdir.assert_not_called()
                    copytree.assert_not_called()

    def test_failed_launcher_creation_rolls_back_only_its_own_file(self):
        spec = importlib.util.spec_from_file_location('fixture_installer', ROOT / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for replaced in (False, True):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / 'source'
                source.mkdir()
                for name in (*installer.MODULES, 'README.md', 'LICENSE', 'bin/api-worker',
                             'runtime/node_modules/opencode-linux-x64/bin/opencode'):
                    path = source / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('fixture')
                prefix = root / 'prefix'
                launcher = prefix / 'bin/api-worker'
                def fail_mode(fd, mode):
                    if replaced:
                        launcher.unlink()
                        launcher.write_text('Concurrent replacement must survive')
                    raise OSError('fixture failure after exclusive launcher creation')
                with mock.patch.object(installer, 'SOURCE', source), mock.patch.object(installer.os, 'fchmod', side_effect=fail_mode):
                    with self.assertRaisesRegex(OSError, 'fixture failure'):
                        installer.install(prefix)
                self.assertFalse((prefix / 'share/api-worker').exists())
                self.assertEqual(list((prefix / 'share').iterdir()), [])
                if replaced:
                    self.assertEqual(launcher.read_text(), 'Concurrent replacement must survive')
                else:
                    self.assertFalse(launcher.exists())
                    with mock.patch.object(installer, 'SOURCE', source):
                        installer.install(prefix)  # A failed install must not poison a retry.
                    self.assertTrue(launcher.exists())
