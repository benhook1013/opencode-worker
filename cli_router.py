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
    if command in ('resume', 'status', 'result'):
        routing = argparse.ArgumentParser(add_help=False)
        routing.add_argument('run_id', nargs='?')
        for option in ('task-file', 'submission-id', 'max-output-tokens', 'max-steps',
                       'context-window', 'model-output-limit', 'timeout'):
            routing.add_argument('--' + option)
        routing.add_argument('--long-context', action=argparse.BooleanOptionalAction)
        parsed, _ = routing.parse_known_args(argv[1:])
        if parsed.run_id:
            identifier = batch.run_id(parsed.run_id)
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
