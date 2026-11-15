"""Compaction limits follow pinned OpenCode 1.19.5 session/overflow.ts semantics."""
import unittest
import worker
from native_transport import live_settings


class CompactionLimitTests(unittest.TestCase):
    def test_batch_and_live_keep_usable_small_windows(self):
        for context, output, usable in ((16000, 4000, 12000), (8000, 1000, 7000),
                                        (32000, 32000, 24000), (1, 1, 1),
                                        (128000, 32000, 96000), (1050000, 128000, 250000)):
            with self.subTest(context=context, output=output):
                batch = worker.config('openai/custom', 'high', False,
                                      limits={'context': context, 'output': output})
                for settings in (batch, live_settings(batch)):
                    limits = settings['provider']['openai']['models']['custom']['limit']
                    # Pinned native: usable=max(0, input-reserved); overflow=total>=usable.
                    threshold = max(0, limits['input'] - settings['compaction']['reserved'])
                    self.assertEqual(threshold, usable)
                    self.assertGreater(threshold, context // 2 if context < 250000 else 0)
                    self.assertEqual(limits, {'context': context, 'input': context, 'output': output})

    def test_long_context_leaves_native_reservation_policy_unchanged(self):
        batch = worker.config('openai/custom', 'high', False,
                              limits={'context': 16000, 'output': 4000}, long_context=True)
        self.assertNotIn('reserved', batch['compaction'])
        self.assertNotIn('reserved', live_settings(batch, long_context=True)['compaction'])


if __name__ == '__main__':
    unittest.main()
