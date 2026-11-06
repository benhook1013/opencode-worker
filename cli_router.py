"""Agent-facing CLI routing; legacy sessions keep their batch implementation."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worker as batch
import live_worker as live


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help'):
        parser = argparse.ArgumentParser(description='Private OpenCode delegated workers: live native sessions and preserved batch sessions.',
            epilog='Commands: start, send, wait, reply, cancel, shutdown, resume, status, result; legacy run, doctor --offline, credit-status, credit-reset. Use COMMAND --help. run --live is start --wait.')
        parser.print_help()
        return 0
    command = argv[0]
    if command in ('start', 'send', 'wait', 'reply', 'cancel', 'shutdown'):
        return live.main(argv)
    if command == 'run' and ('--live' in argv or '--allow-commands' in argv):
        arguments = [value for value in argv[1:] if value != '--live']
        if '--wait' not in arguments:
            arguments.append('--wait')
        return live.main(['start', *arguments])
    if command in ('resume', 'status', 'result') and len(argv) > 1 and not argv[1].startswith('-'):
        identifier = batch.run_id(argv[1])
        path = batch.STATE / identifier / 'metadata.json'
        if path.is_file() and json.loads(path.read_text()).get('backend') == 'native-server':
            return live.main(argv)
    previous = sys.argv
    try:
        sys.argv = ['api-worker', *argv]
        return batch.main()
    finally:
        sys.argv = previous


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print('api-worker: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
