"""Small private launcher for pinned OpenCode; no model loop or credential storage."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import resource
import signal
import shutil
import stat
import subprocess
import sys
import time
import tempfile
import uuid

REASONING_CHOICES = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh')

ROOT = Path(__file__).resolve().parent


def user_directory(override, xdg, fallback):
    value = os.environ.get(override) or str(Path(os.environ.get(xdg) or (Path.home() / fallback)) / 'api-worker')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f'{override}/{xdg} must resolve to an absolute path')
    return path


STATE = user_directory('API_WORKER_STATE_DIR', 'XDG_STATE_HOME', '.local/state')
CONFIG = user_directory('API_WORKER_CONFIG_DIR', 'XDG_CONFIG_HOME', '.config')
KEY = CONFIG / 'openai-api-key'
BINARY = ROOT / 'runtime/node_modules/opencode-linux-x64/bin/opencode'
VERSION = '1.19.5'
NATIVE_OUTPUT_TOKENS = 32000
VERIFIED_MODEL_LIMITS = {
    'gpt-6-luna': {'context': 1050000, 'output': 128000},
    'gpt-6.1-sol': {'context': 1050000, 'output': 128000},
}


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(f'Expected owned private directory (0700): {path}')


def credential_check(path=KEY):
    # Only metadata; OpenCode resolves the file reference at live runtime.
    for item, kind, mode in ((path.parent, stat.S_ISDIR, 0o700), (path, stat.S_ISREG, 0o600)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            raise ValueError(f'Missing credential: create {path} privately (directory 0700, file 0600)') from None
        if not kind(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
            raise ValueError(f'Credential requires owned directory 0700 and regular file 0600: {path}')
    if path.stat().st_size == 0:
        raise ValueError('Credential file is empty')


def validate_worktree(value):
    directory = Path(value).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError('Worktree must be a directory')
    for protected in (KEY.parent, STATE):
        if protected.resolve().is_relative_to(directory):
            raise ValueError('Worktree must not contain the configured credential directory or worker state')
    return directory


def instruction_paths(worktree):
    directory = Path(worktree).resolve(strict=True)
    return [str(candidate) for ancestor in reversed((directory, *directory.parents))
            if (candidate := ancestor / 'AGENTS.md').is_file()]


def config(model, reasoning, writable, steps=None, key=KEY, worktree=None, limits=None, long_context=False):
    model_id = model.removeprefix('openai/')
    limits = limits or model_limits(model)
    native_limits = {**limits, 'input': limits['context']}
    compaction = {'auto': True}
    if not long_context:
        compaction['reserved'] = limits['context'] - min(250000, max(1, limits['context'] - 32000))
    permission = {'*': 'deny', 'read': 'allow', 'glob': 'allow', 'grep': 'allow',
                  'external_directory': 'deny', 'edit': 'allow' if writable else 'deny'}
    return {
        'model': model, 'small_model': model, 'default_agent': 'worker',
        'instructions': instruction_paths(worktree) if worktree is not None else [],
        'enabled_providers': ['openai'], 'share': 'disabled', 'autoupdate': False,
        'snapshot': False, 'plugin': [], 'mcp': {}, 'command': {}, 'lsp': False,
        'formatter': False, 'compaction': compaction, 'permission': permission,
        'provider': {'openai': {'npm': '@ai-sdk/openai', 'env': [],
            'whitelist': [model_id], 'options': {'baseURL': 'https://api.openai.com/v1',
                'apiKey': '{file:' + str(key) + '}'},
            'models': {model_id: {'id': model_id, 'name': model_id, 'reasoning': True,
                'tool_call': True, 'limit': native_limits, 'options': {'reasoningEffort': reasoning, 'store': False},
                'variants': {reasoning: {'reasoningEffort': reasoning}}}}}},
        'agent': {'worker': {'mode': 'primary', **({'steps': steps} if steps is not None else {}), 'model': model,
            'variant': reasoning, 'permission': permission,
            'prompt': 'You are a bounded delegated worker. Follow the explicit task and applicable AGENTS.md. '
                      'Before editing or creating any file, read all applicable nested AGENTS.md instructions along its directory path; '
                      'read existing file contents before editing. Do not assume root instructions replace nested instructions. '
                      'Only read, glob and grep are enabled; editing is available only when authorized by this launcher. '
                      'Shell commands, tests, runtime execution, networking, subagents and external files are unavailable. '
                      'Report completed work and unrun validation honestly. Do not attempt to access credentials or agent state.'},
            'title': {'disable': True}, 'summary': {'disable': True}, 'compaction': {'model': model, 'variant': reasoning}},
    }


def environment(directory, settings, max_output_tokens=None):
    # No inherited API keys, OAuth, proxy settings, telemetry, config overrides, or shell initialization.
    home = directory / 'home'
    private_dir(home)
    if settings.get('plugin'):
        # OpenCode installs its plugin SDK into each config directory before loading
        # even local hooks. Seed its locked dependencies offline: sanitized workers
        # deliberately cannot inherit package-registry credentials or proxy settings.
        runtime = ROOT / 'runtime'
        plugin = runtime / 'node_modules/@opencode-ai/plugin/package.json'
        if not plugin.is_file() or json.loads(plugin.read_text()).get('version') != VERSION:
            raise ValueError('Prepare pinned plugin dependencies: npm ci --prefix runtime')
        config_home = home / 'config/opencode'
        private_dir(config_home)
        for name in ('package.json', 'package-lock.json'):
            shutil.copyfile(runtime / name, config_home / name)
        modules = config_home / 'node_modules'
        expected = (runtime / 'node_modules').resolve()
        if modules.is_symlink():
            if modules.resolve() != expected:
                raise ValueError('Unexpected plugin dependency link in worker config')
        elif modules.exists():
            raise ValueError('Unexpected plugin dependency directory in worker config')
        else:
            modules.symlink_to(expected, target_is_directory=True)
    env = {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'SHELL': '/bin/sh',
           'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_DATA_HOME': str(home / 'data'),
           'XDG_STATE_HOME': str(home / 'state'), 'XDG_CACHE_HOME': str(home / 'cache'),
           'OPENCODE_CONFIG_CONTENT': json.dumps(settings),
           'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_COUNT': '2',
           'GIT_CONFIG_KEY_0': 'core.fsmonitor', 'GIT_CONFIG_VALUE_0': 'false',
           'GIT_CONFIG_KEY_1': 'core.hooksPath', 'GIT_CONFIG_VALUE_1': '/dev/null'}
    if max_output_tokens is not None:
        env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] = str(max_output_tokens)
    for flag in ('PURE', 'DISABLE_PROJECT_CONFIG', 'DISABLE_DEFAULT_PLUGINS', 'DISABLE_AUTOUPDATE',
                 'DISABLE_MODELS_FETCH', 'DISABLE_EXTERNAL_SKILLS', 'DISABLE_CLAUDE_CODE',
                 'DISABLE_LSP_DOWNLOAD', 'EXPERIMENTAL_DISABLE_FILEWATCHER'):
        env['OPENCODE_' + flag] = 'true'
    return env


def save(path, value):
    fd, filename = tempfile.mkstemp(prefix='.receipt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(filename, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)



@contextmanager
def credit_lock():
    fd = os.open(STATE / 'credit-guard.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'r+') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Credit guard lock must be an owned regular file with mode 0600')
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield handle


def credit_state():
    recovery = 'Run api-worker credit-reset --confirm-credit-available only after verifying available credits.'
    try:
        fd = os.open(STATE / 'credit-guard.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('Expected owned regular credit guard file with mode 0600')
            value = json.loads(handle.read(8193))
        if (not isinstance(value, dict) or set(value) != {'version', 'exhausted', 'reason', 'updated_at'}
                or type(value.get('updated_at')) not in (int, float) or not math.isfinite(value['updated_at'])
                or type(value.get('version')) is not int or value['version'] != 1
                or type(value.get('exhausted')) is not bool
                or value.get('reason') not in (None, 'credit_balance_exhausted', 'insufficient_quota')
                or value['exhausted'] != (value.get('reason') is not None)):
            raise ValueError('Invalid credit guard schema')
        return value
    except (OSError, ValueError) as error:
        raise ValueError(f'Credit guard unavailable or malformed; API dispatch refused. {recovery}') from error


def credit_save(exhausted, reason):
    value = {'version': 1, 'exhausted': exhausted, 'reason': reason, 'updated_at': time.time()}
    fd, filename = tempfile.mkstemp(prefix='.credit-guard-', dir=STATE)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(filename, STATE / 'credit-guard.json')
        directory_fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)
    return value


def credit_preflight():
    value = credit_state()
    if value['exhausted']:
        raise ValueError(f"API credits paused ({value['reason']}); use native helpers. "
                         'After verifying available credits, run api-worker credit-reset --confirm-credit-available.')


def exhaustion_reason(events):
    def code(value):
        if isinstance(value, dict):
            for field in ('code', 'type'):
                if value.get(field) in ('credit_balance_exhausted', 'insufficient_quota'):
                    return value[field]
            for field, child in value.items():
                if field == 'responseBody' and isinstance(child, str):
                    try:
                        child = json.loads(child)
                    except ValueError:
                        continue
                found = code(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = code(child)
                if found:
                    return found
        return None
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'error':
            found = code(event.get('error'))
            if found:
                return found
    return None


def run_id(value):
    if not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Expected a launcher run ID (32 lowercase hex characters)')
    return value


def collect(events):
    """Collect batch result text, session identity, and error presence from object events."""
    texts, sessions, errors = [], [], []
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('sessionID'):
            sessions.append(event['sessionID'])
        if event.get('type') == 'text':
            texts.append(event.get('part', {}).get('text', ''))
        if event.get('type') == 'error':
            errors.append(event.get('error'))
    return '\n\n'.join(texts), sessions[-1] if sessions else None, bool(errors)



def model_limits(model, args=None, meta=None):
    meta = meta or {}
    known = VERIFIED_MODEL_LIMITS.get(model.removeprefix('openai/'), {})
    saved = meta.get('model_limits', {}) if meta.get('model_limits_explicit') else {}
    limits = {}
    for field, flag in (('context', 'context_window'), ('output', 'model_output_limit')):
        value = getattr(args, flag, None)
        limits[field] = value if value is not None else saved.get(field, known.get(field))
    if any(type(value) is not int or value <= 0 for value in limits.values()):
        raise ValueError('Unknown model limits: supply positive --context-window and --model-output-limit from its official documentation')
    if limits['output'] > limits['context']:
        raise ValueError('Model output limit cannot exceed its context window')
    return limits


def output_budget(args, meta, limits):
    value = getattr(args, 'max_output_tokens', None)
    if value is None:
        # Previous implicit 4096/32768 defaults have no reliable explicit provenance.
        saved = meta.get('max_output_tokens')
        if meta.get('output_limit_explicit') or ('output_limit_explicit' not in meta and saved not in (None, 4096, 32768)):
            value = saved
    if value == 0 and type(value) is int:
        return None
    if value is not None and (type(value) is not int or not 1 <= value <= limits['output']):
        raise ValueError(f"Max output tokens must be 1-{limits['output']}, or 0 for native default")
    return value


def step_limit(args, meta):
    value = getattr(args, 'max_steps', None)
    if value is None:
        saved = meta.get('steps')
        if meta.get('steps_explicit') or ('steps_explicit' not in meta and saved not in (None, 8)):
            value = saved
    if value == 0 and type(value) is int:
        return None
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError('Max steps must be positive, or 0 for native unrestricted steps')
    return value


def incomplete_reason(events):
    finish, final_text, final_aborted, tools = None, False, False, {}
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get('part', {})
        kind = event.get('type')
        if kind == 'step_start':
            finish, final_text, final_aborted = None, False, False
        elif kind == 'step_finish':
            finish = part.get('reason')
        elif kind == 'text' and part.get('text', '').strip():
            final_text = True
        elif kind == 'tool_use':
            state = part.get('state', {})
            tools[part.get('callID') or part.get('id') or len(tools)] = state.get('status')
            if state.get('status') == 'error' and 'abort' in str(state.get('error', '')).lower():
                final_aborted = True
    if finish == 'length':
        return 'output_limit_reached'
    if any(status in ('pending', 'running') for status in tools.values()):
        return 'unfinished_tool_call'
    if final_aborted:
        return 'aborted_tool_call'
    if finish != 'stop':
        return 'missing_terminal_stop'
    if not final_text:
        return 'missing_terminal_text'
    return None


def execute(args):
    with credit_lock():
        credit_preflight()
    directory = STATE / (uuid.uuid4().hex if args.action == 'run' else run_id(args.run_id))
    private_dir(directory)
    with (directory / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('This run already has an active OpenCode process') from None
        if args.action == 'run':
            model = args.model if args.model.startswith('openai/') else 'openai/' + args.model
            if not re.fullmatch(r'openai/[A-Za-z0-9][A-Za-z0-9._-]*', model):
                raise ValueError('Model must be an explicit OpenAI model ID, optionally prefixed openai/')
            worktree = validate_worktree(args.worktree)
            meta = {'run_id': directory.name, 'worktree': str(worktree), 'model': model,
                    'reasoning': args.reasoning, 'allow_write': args.allow_write}
        else:
            meta = json.loads((directory / 'metadata.json').read_text())
            if not meta.get('session_id'):
                raise ValueError('No native session ID recorded; inspect artifacts before starting another run')
            worktree = validate_worktree(meta['worktree'])
        limits = model_limits(meta['model'], args, meta)
        budget = output_budget(args, meta, limits)
        steps = step_limit(args, meta)
        timeout = getattr(args, 'timeout', None)
        if timeout is not None and timeout < 0:
            raise ValueError('Timeout must be positive, or 0 for unlimited wall clock')
        timeout = timeout or None
        explicit_limits = bool(meta.get('model_limits_explicit') or getattr(args, 'context_window', None) or getattr(args, 'model_output_limit', None))
        meta.update(model_limits=limits, model_limits_explicit=explicit_limits,
                    max_output_tokens=budget if budget is not None else min(limits['output'], NATIVE_OUTPUT_TOKENS),
                    output_limit_explicit=budget is not None, steps=steps, steps_explicit=steps is not None, timeout_seconds=timeout)
        long_context = getattr(args, 'long_context', None)
        meta['long_context'] = meta.get('long_context', False) if long_context is None else long_context
        meta.pop('incomplete_reason', None)
        task = Path(args.task_file).read_text()
        if not task.strip():
            raise ValueError('Task file is empty')
        credential_check(KEY)
        turn = directory / ('turn-' + uuid.uuid4().hex)
        private_dir(turn)
        (turn / 'task.txt').write_text(task)
        settings = config(meta['model'], meta['reasoning'], meta['allow_write'], meta['steps'], key=KEY, worktree=worktree, limits=limits, long_context=meta['long_context'])
        save(turn / 'config.json', settings)  # File reference only; never key contents.
        command = [str(BINARY), 'run', '--pure', '--format', 'json', '--dir', str(worktree),
                   '--agent', 'worker', '--model', meta['model'], '--variant', meta['reasoning']]
        if args.action == 'resume':
            command += ['--session', meta['session_id']]
        code, status = 1, 'failed'
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        try:
            with (turn / 'events.jsonl').open('wb') as output, (turn / 'stderr.log').open('wb') as error:
                env = environment(directory, settings, max_output_tokens=budget)
                # Serialize the final check and launch against completed exhaustion writes.
                with credit_lock():
                    credit_preflight()
                    meta.update(status='running', latest_turn=turn.name, started_at=time.time())
                    save(directory / 'metadata.json', meta)
                    try:
                        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=error,
                                                cwd=worktree, env=env, start_new_session=True)
                    except OSError as launch_error:
                        (turn / 'result.txt').write_text('')
                        meta.update(status='failed', exit_code=1, finished_at=time.time(),
                                    failure_reason='native_launch_failed', launch_error=type(launch_error).__name__)
                        save(directory / 'metadata.json', meta)
                        raise
                def terminate(_signum, _frame):
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    raise KeyboardInterrupt()
                signal.signal(signal.SIGTERM, terminate)
                try:
                    proc.communicate(task.encode(), timeout=timeout)
                    code = proc.returncode
                    status = 'complete' if code == 0 else 'failed'
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    status, code = 'interrupted', 124
            meta['native_child_peak_rss_kib'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            events = (turn / 'events.jsonl').read_text(errors='replace')
            reason = exhaustion_reason(events)
            if reason:
                with credit_lock():
                    credit_state()  # Unexpected corruption stays closed until explicit owner reset.
                    credit_save(True, reason)
                meta['credit_exhaustion_reason'] = reason
                print(f'api-worker: {reason}; shared API dispatch paused. Use native helpers until credits are verified available.', file=sys.stderr)
            result, session_id, errors = collect(events)
            meta['native_exit_code'] = proc.returncode
            if status != 'interrupted' and errors:
                status, code = 'failed', 1
            elif status == 'complete':
                incomplete = incomplete_reason(events)
                if incomplete:
                    status, code = 'incomplete', 2
                    meta['incomplete_reason'] = incomplete
                    print(f'api-worker: incomplete turn ({incomplete}); inspect partial output and possible edits before explicit resume.', file=sys.stderr)
            if session_id:
                meta['session_id'] = session_id
            (turn / 'result.txt').write_text(result)
            meta.update(status=status, exit_code=code, finished_at=time.time())
            save(directory / 'metadata.json', meta)
            print(json.dumps({**meta, 'artifacts': str(turn)}))
            return code
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Pinned OpenCode delegated reading/editing; no shell tools or default model.')
    commands = parser.add_subparsers(dest='action', required=True)
    doctor = commands.add_parser('doctor', help='Offline installation inspection; never reads credentials')
    doctor.add_argument('--offline', action='store_true', required=True)
    run = commands.add_parser('run', help='Start a fresh native OpenCode session from an explicit task')
    run.add_argument('--worktree', required=True)
    run.add_argument('--model', required=True, help='Actual OpenAI API model ID; no fallback')
    run.add_argument('--reasoning', '--variant', required=True, dest='reasoning', choices=REASONING_CHOICES)
    run.add_argument('--allow-write', action='store_true', help='Enable native edit tools; default is read-only policy')
    resume = commands.add_parser('resume', help='Continue only the named native session with a new explicit task')
    resume.add_argument('run_id')
    for command in (run, resume):
        command.add_argument('--task-file', required=True)
        command.add_argument('--long-context', action=argparse.BooleanOptionalAction, default=None,
                             help='Opt in to full native context policy; default early compaction near 250k total tokens')
        command.add_argument('--max-output-tokens', type=int, default=None,
                             help='Opt-in reasoning/output cap up to model maximum; 0 clears saved cap; default native OpenCode behavior')
        command.add_argument('--max-steps', type=int, default=None, help='Opt-in agent step cap; 0 clears saved cap; default unrestricted')
        command.add_argument('--context-window', type=int, help='Documented model context tokens; required for unknown model IDs')
        command.add_argument('--model-output-limit', type=int, help='Documented model maximum output tokens; required for unknown model IDs')
        command.add_argument('--timeout', type=int, default=None, help='Opt-in wall-clock seconds; 0 or omitted means unlimited (native request timeouts still apply)')
    for name in ('status', 'result'):
        command = commands.add_parser(name, help='Inspect private local artifacts without API calls')
        command.add_argument('run_id', type=run_id)
    commands.add_parser('credit-status', help='Inspect the local shared credit guard; no API request')
    reset = commands.add_parser('credit-reset', help='Initialize/reset guard only after verifying available credits')
    reset.add_argument('--confirm-credit-available', action='store_true', required=True)
    args = parser.parse_args()
    if args.action == 'doctor':
        print(json.dumps({'opencode_version': VERSION, 'binary_exists': BINARY.is_file(),
                          'binary': str(BINARY), 'state': str(STATE), 'credential': 'not inspected',
                          'tools': 'read/glob/grep; edit only with --allow-write; shell disabled',
                          'isolation': 'OpenCode permissions only; not an OS sandbox'}))
        return 0 if BINARY.is_file() else 1
    private_dir(STATE)
    if args.action in ('credit-status', 'credit-reset'):
        with credit_lock():
            value = credit_save(False, None) if args.action == 'credit-reset' else credit_state()
        print(json.dumps(value, indent=2))
        return 0
    if args.action in ('run', 'resume'):
        return execute(args)
    directory = STATE / args.run_id
    meta = json.loads((directory / 'metadata.json').read_text())
    if args.action == 'status':
        print(json.dumps(meta, indent=2))
    else:
        print((directory / meta['latest_turn'] / 'result.txt').read_text())
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(f'api-worker: {error}', file=sys.stderr)
        sys.exit(1)
