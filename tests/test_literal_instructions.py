"""Exercise literal instruction paths through the pinned native loader, no real API."""
import json
import unittest
from unittest import mock
import test_native_transport as fixture


class GitFixtureEnvironmentTests(unittest.TestCase):
    def test_unsupported_instruction_glob_characters_fail_explicitly(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for char in '{}()!+@\\':
                target = root / ('guidelines' + char + '.md')
                target.write_text('dummy')
                link = root / 'AGENTS.md'
                link.symlink_to(target)
                with self.assertRaisesRegex(ValueError, 'Unsupported glob characters'):
                    fixture.worker.instruction_paths(root, root / 'private/key')
                link.unlink()

    def test_repository_location_is_not_inherited(self):
        keys = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM')
        env = {key: '/dummy-not-used' for key in keys}
        env.update(PATH='/usr/bin', GIT_CONFIG_GLOBAL='/dummy-config')
        with mock.patch.dict(fixture.os.environ, env, clear=True):
            self.assertEqual(fixture.git_fixture_environment(),
                             {'PATH': '/usr/bin', 'GIT_CONFIG_GLOBAL': '/dummy-config'})


class LiteralInstructionTests(unittest.TestCase):
    stop = fixture.NativeServerTests.stop
    send = fixture.NativeServerTests.send

    def setUp(self):
        original = fixture.worker.config
        def config(*args, **kwargs):
            settings = original(*args, **kwargs)
            settings['instructions'] = []
            self.tokens = []
            for index, name in enumerate(['guidelines[prod].md', 'star*.md', 'question?.md', 'guidelines[prod]*?.md']):
                folder = self.project / str(index)
                folder.mkdir()
                target = folder / name
                token = 'LITERAL_SENTINEL_' + str(index) + '_END'
                target.write_text(token)
                (folder / 'guidelinesp.md').write_text('WRONG_GLOB_SENTINEL_783')
                self.tokens.append(token)
                (folder / 'AGENTS.md').symlink_to(target)
                settings['instructions'] += fixture.worker.instruction_paths(folder, self.root / 'private/dummy')
            return settings
        with mock.patch.object(fixture.worker, 'config', side_effect=config):
            fixture.NativeServerTests.setUp(self)

    def test_native_loader_reads_only_checked_literal_target(self):
        message = self.send()
        result = self.client.wait(self.session, message, timeout=15)
        self.assertEqual(result['status'], 'complete')
        request = json.dumps(self.provider.requests[0])
        self.assertEqual([token for token in self.tokens if token not in request], [])
        self.assertNotIn('WRONG_GLOB_SENTINEL_783', request)


if __name__ == '__main__':
    unittest.main()
