"""Keep step-target exposure truthful without claiming native runtime enforcement."""
import argparse
import subprocess
import sys
import unittest
from pathlib import Path

import worker
from native_transport import live_settings


class AdvisoryStepsTests(unittest.TestCase):
    def test_target_is_native_configuration_not_a_tool_permission_cap(self):
        settings = worker.config('openai/gpt-6-luna', 'high', True, steps=1)
        self.assertEqual(settings['agent']['worker']['steps'], 1)
        self.assertEqual(settings['agent']['worker']['permission']['edit'], 'allow')
        live = live_settings(settings, allow_commands=True, shell_plugin='file:///fixture.js')
        self.assertEqual(live['agent']['worker']['steps'], 1)
        self.assertEqual(live['agent']['worker']['permission']['bash'], 'allow')
        self.assertIsNone(worker.step_limit(argparse.Namespace(max_steps=0),
                                          {'steps': 1, 'steps_explicit': True}))

    def test_batch_help_does_not_promise_an_enforced_cap(self):
        for action in ('run', 'resume'):
            result = subprocess.run([sys.executable, str(Path(worker.__file__)), action, '--help'],
                                    check=True, capture_output=True, text=True)
            text = ' '.join(result.stdout.split())
            self.assertIn('Advisory native step target, not an enforced tool-call cap', text)
            self.assertIn('0 clears saved target', text)

    def test_live_help_describes_advisory_target(self):
        for action in ('start', 'send', 'resume'):
            result = subprocess.run([sys.executable, str(Path(worker.__file__).with_name('live_worker.py')), action, '--help'],
                                    check=True, capture_output=True, text=True)
            self.assertIn('Advisory native step target, not an enforced tool-call cap',
                          ' '.join(result.stdout.split()))


if __name__ == '__main__':
    unittest.main()
