"""CLI and lifecycle around native OpenCode sessions; no agent/provider execution loop."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import hashlib
import re
import os
import queue
from pathlib import Path
import resource
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent))
import worker as batch
from native_transport import NativeClient, live_settings, snapshot_due

ROOT = Path(__file__).resolve().parent
TERMINAL = {'complete', 'incomplete', 'failed', 'interrupted'}


class RequestAdvanced(Exception):
    pass


atomic = batch.save


@contextmanager
def locked(path, blocking=True, stopping=None):
    with path.open('a') as handle:
        if stopping is None:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        else:
            while not stopping.is_set():
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    stopping.wait(0.05)
            else:
                raise InterruptedError('Shutdown interrupted lock wait')
        yield


def read(directory):
    return json.loads((directory / 'metadata.json').read_text())


def update(directory, **fields):
    with locked(directory / 'metadata.lock'):
        meta = read(directory)
        meta.update(fields)
        atomic(directory / 'metadata.json', meta)
        return meta



def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def signal_owned(meta, kind, signum):
    pid = meta.get(kind + '_pid')
    token = meta.get(kind + '_start_ticks')
    if not pid or not token:
        return False
    descriptor = None
    try:
        # pidfd pins the process across checks and prevents PID-reuse signalling races.
        descriptor = os.pidfd_open(pid)
        if process_identity(pid) != token or os.getpgid(pid) != meta.get(kind + '_pgid'):
            return False
        expected = str(ROOT / 'live_worker.py') if kind == 'owner' else str(batch.BINARY)
        if expected.encode() not in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0'):
            return False
        signal.pidfd_send_signal(descriptor, signum)
        return True
    except (OSError, AttributeError):
        return False  # Never fall back to an unchecked PID signal.
    finally:
        if descriptor is not None:
            os.close(descriptor)


def server_password(directory):
    password = (directory / 'server-auth').read_text()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', password):
        raise ValueError('Invalid server authentication file; backend was not launched')
    return password


def owner_alive(meta):
    pid, ticks = meta.get('owner_pid'), meta.get('owner_start_ticks')
    return bool(pid and ticks and process_identity(pid) == ticks)


def enforce_timeout(directory, api, session, stopping=None):
    with locked(directory / 'control.lock', stopping=stopping):
        meta = read(directory)
        if (meta.get('timeout_seconds') and meta.get('current_request_id')
                and (meta.get('current_result') or {}).get('status') not in TERMINAL
                and time.time() - meta.get('submitted_at', time.time()) >= meta['timeout_seconds']):
            api.abort(session)


def client(directory):
    meta = read(directory)
    password = (directory / 'server-auth').read_text()
    api = NativeClient(meta['server_url'], password, meta['worktree'])
    configure_dispatch(directory, api, meta)
    return api


def configure_dispatch(directory, api, meta):
    """Restore request-specific acknowledgement context for owners and late-attaching observers."""
    message, turn = meta.get('current_request_id'), meta.get('latest_turn')
    if message:
        api.restore_idle_evidence(meta['session_id'], message, meta.get('current_result') or {})
    if not message or not turn:
        return
    receipt = json.loads((directory / turn / 'receipt.json').read_text())
    if receipt.get('request_id') != message or receipt.get('dispatch_status') != 'confirmed':
        return
    acknowledged_at = receipt.get('dispatch_confirmed_at', meta.get('submitted_at'))
    if acknowledged_at is None:
        return
    observed = meta.get('session_error') or {}
    error = observed.get('error') if (observed.get('request_id') == message
        and observed.get('attribution') == 'explicit_message_id') else None
    api.note_dispatch(meta['session_id'], message, acknowledged_at, error)


def confirm_dispatch(directory, meta, message, proof):
    # Caller holds metadata.lock; only request-tied native evidence or HTTP ack qualifies.
    turn = meta.get('pending_dispatch')
    if not turn or meta.get('current_request_id') != message:
        return
    path = directory / turn / 'receipt.json'
    receipt = json.loads(path.read_text())
    if receipt.get('request_id') != message:
        raise ValueError('Dispatch receipt identity mismatch; inspect retained state')
    atomic(path, {**receipt, 'dispatch_status': 'confirmed', 'dispatch_confirmation': proof,
                  'dispatch_confirmed_at': time.time()})
    meta['pending_dispatch'] = None


def remember(directory, result):
    meta = read(directory)
    with locked(directory / 'metadata.lock'):
        meta = read(directory)
        if not result.get('request_id') or result['request_id'] != meta.get('current_request_id'):
            return
        prior = meta.get('current_result') or {}
        if (prior.get('observed_boot_id') and prior.get('observed_boot_id') == result.get('observed_boot_id')
                and prior.get('observed_at_ns', 0) > result.get('observed_at_ns', 0)):
            return
        pending_question = (prior.get('incomplete_reason') == 'idle_unfinished_assistant'
                            and result.get('status') == 'needs_input' and bool(result.get('questions')))
        if prior.get('status') in TERMINAL and result.get('status') not in TERMINAL and not pending_question:
            if prior.get('incomplete_reason') == 'idle_unfinished_assistant':
                # Keep the terminal receipt, but replace/invalidate stability evidence
                # when a newer native observation shows changed output or activity.
                prior = dict(prior)
                prior.pop('idle_unfinished_evidence', None)
                if result.get('idle_unfinished_evidence'):
                    prior['idle_unfinished_evidence'] = result['idle_unfinished_evidence']
                for field in ('observed_at_ns', 'observed_boot_id'):
                    if field in result:
                        prior[field] = result[field]
                meta['current_result'] = prior
                atomic(directory / meta['latest_turn'] / 'status.json', prior)
                atomic(directory / 'metadata.json', meta)
            return
        if result.get('execution_observed'):
            confirm_dispatch(directory, meta, result['request_id'], 'native_execution')
        turn = directory / meta['latest_turn']
        atomic(turn / 'status.json', result)
        batch.durable_text(turn / 'result.txt', result.get('text', ''))
        meta.update(status=result['status'], current_result=result)
        atomic(directory / 'metadata.json', meta)
    if result.get('error'):
        latch_request_error(directory, result['error'], result['request_id'])


def latch_request_error(directory, error, request_id=None):
    """Latch each request/reason once, so retained failures cannot undo an owner reset."""
    reason = batch.exhaustion_reason(json.dumps({'type': 'error', 'error': error}))
    if not reason:
        return
    # Same order as dispatch: credit then metadata. Keep reset serialized until
    # the successful latch is durably attributed to its original request.
    with batch.credit_lock():
        with locked(directory / 'metadata.lock'):
            meta = read(directory)
            message = request_id or meta.get('current_request_id')
            if request_id and request_id != meta.get('current_request_id'):
                return
            identity = [meta.get('session_id'), message, reason]
            latched = meta.get('credit_latched_errors', [])
            if identity in latched:
                return
            batch.credit_latch(reason)
            meta['credit_latched_errors'] = [*latched, identity]
            atomic(directory / 'metadata.json', meta)


def observe(directory, event, record=False):
    if record:
        with (directory / 'native-events.jsonl').open('a') as output:
            output.write(json.dumps(event) + '\n')
    props = event.get('properties', {})
    error = props.get('error') or props.get('info', {}).get('error')
    if event.get('type') == 'session.error' and error:
        with locked(directory / 'metadata.lock'):
            meta = read(directory)
            message = meta.get('current_request_id')
            if message and props.get('sessionID') == meta.get('session_id') and props.get('messageID') == message:
                meta['session_error'] = {'request_id': message, 'error': error, 'attribution': 'explicit_message_id'}
                atomic(directory / 'metadata.json', meta)
    if error:
        latch_request_error(directory, error, props.get('messageID'))
    part = props.get('part', {})
    tokens = part.get('tokens')
    if part.get('type') == 'step-finish' and isinstance(tokens, dict):
        cache = tokens.get('cache') or {}
        count = tokens.get('input', 0) + cache.get('read', 0) + cache.get('write', 0)
        if count > 272000:
            update(directory, pricing_warning={'input_tokens': count, 'boundary': 272000,
                'message': 'Recorded input crossed the long-context pricing boundary; early compaction is best effort.'})


def checked_settings(directory, meta):
    """Reject interrupted control commits before either owner launch path."""
    settings = json.loads((directory / 'settings.json').read_text())
    if (meta.get('controls_pending') or (meta.get('settings_sha256')
            and meta['settings_sha256'] != settings_digest(settings))):
        raise ValueError('Interrupted control update; reapply explicit controls before restarting')
    return settings


def settings_digest(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def run_server(directory):
    """One tiny native-process owner, with SSE observation and terminal-only idle reaping."""
    os.umask(0o077)
    batch.private_dir(directory)
    batch.STATE = directory.parent
    with locked(directory / 'server.lock', blocking=False):
        meta = read(directory)
        settings = checked_settings(directory, meta)
        env = batch.environment(directory, settings, meta.get('output_override'))
        env.update(meta.get('command_environment', {}))
        env['OPENCODE_SERVER_PASSWORD'] = server_password(directory)
        env['OPENCODE_ENABLE_QUESTION_TOOL'] = 'true'
        if meta.get('allow_commands'):
            env['OPENCODE_PURE'] = 'false'  # Only the controlled shell.env hook in isolated config.
        stopping = threading.Event()
        process = None
        def stop(_signum, _frame):
            stopping.set()
            if process and process.poll() is None:
                process.terminate()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            with (directory / 'native-server.log').open('ab') as log:
                offset = log.tell()
                process = subprocess.Popen([str(batch.BINARY), 'serve', '--hostname', '127.0.0.1', '--port', '0'],
                    cwd=meta['worktree'], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                update(directory, owner_pid=os.getpid(), owner_start_ticks=process_identity(os.getpid()), owner_pgid=os.getpgid(os.getpid()),
                    native_server_pid=process.pid, native_server_start_ticks=process_identity(process.pid), native_server_pgid=os.getpgid(process.pid),
                    native_server_lifetime_peak_rss_kib=None, server_lifecycle='starting')
                deadline = time.monotonic() + 20
                url = None
                while process.poll() is None and time.monotonic() < deadline and not stopping.is_set():
                    with (directory / 'native-server.log').open() as source:
                        source.seek(offset)
                        for line in source:
                            if line.startswith('opencode server listening on '):
                                url = line.strip().split(' on ', 1)[1]
                    if url:
                        break
                    time.sleep(0.05)
                if not url:
                    raise ValueError('Native server startup failed; inspect private native-server.log')
                update(directory, server_url=url)
                api = client(directory)
                session = meta.get('session_id') or api.create_session()
                current = update(directory, session_id=session, server_lifecycle='running')
                if current.get('stop_requested'):
                    stopping.set()
                idle_since = None
                last_snapshot = float('-inf')
                while process.poll() is None and not stopping.is_set():
                    try:
                        for event in owner_events(api, stopping):
                            if event.get('type') != 'observer.tick':
                                observe(directory, event, record=True)
                            meta = read(directory)
                            if meta.get('observer_state') != 'connected':
                                meta = update(directory, observer_state='connected')
                            if stopping.is_set():
                                break
                            if not snapshot_due(event, last_snapshot):
                                continue
                            message = meta.get('current_request_id')
                            configure_dispatch(directory, api, meta)
                            result = api.snapshot(session, message) if message else None
                            last_snapshot = time.monotonic()
                            idle_since = reconcile_owner_snapshot(directory, api, session, meta, result, idle_since, stopping)
                            if stopping.is_set():
                                break
                    except (OSError, ValueError):
                        if process.poll() is not None or stopping.is_set():
                            break
                        update(directory, observer_state='degraded')
                        # Reconcile retained native state after transport loss; never replay a prompt.
                        try:
                            enforce_timeout(directory, api, session, stopping=stopping)
                            latest = read(directory)
                            message = latest.get('current_request_id')
                            configure_dispatch(directory, api, latest)
                            result = api.snapshot(session, message) if message else None
                            idle_since = reconcile_owner_snapshot(directory, api, session, latest, result, idle_since, stopping)
                        except (OSError, ValueError):
                            pass  # Observation failure must not terminate healthy native work.
                        time.sleep(0.2)
        except Exception as error:
            update(directory, server_error=type(error).__name__)
        finally:
            if process:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                update(directory, native_server_lifetime_peak_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
            update(directory, server_lifecycle='stopped', owner_pid=None, native_server_pid=None)


def reconcile_owner_snapshot(directory, api, session, meta, result, idle_since, stopping):
    """Apply successful observations identically for event and recovery paths."""
    message = meta.get('current_request_id')
    if result:
        remember(directory, result)
    idle = (api.request('GET', '/session/status').get(session, {'type': 'idle'})['type'] == 'idle'
            if result is None else result['status'] in TERMINAL and result.get('native_status') == 'idle')
    now = time.monotonic()
    idle_since = (now if idle_since is None else idle_since) if idle else None
    enforce_timeout(directory, api, session, stopping=stopping)
    if idle_since is not None and now - idle_since >= (meta.get('idle_reap_seconds', 30) if message else 30):
        with locked(directory / 'control.lock', stopping=stopping):
            latest = read(directory)
            if latest.get('current_request_id') == message:
                confirmed = api.snapshot(session, message) if message else None
                confirmed_idle = (api.request('GET', '/session/status').get(session, {'type': 'idle'})['type'] == 'idle'
                                  if confirmed is None else confirmed['status'] in TERMINAL and confirmed.get('native_status') == 'idle')
                if confirmed_idle:
                    update(directory, stop_requested=True)
                    stopping.set()
    return idle_since


def owner_events(api, stopping, interval=1):
    """Keep the SSE subscription open while supplying independent reconciliation ticks."""
    pending = queue.Queue(maxsize=128)
    closed = threading.Event()

    def deliver(item):
        while not closed.is_set() and not stopping.is_set():
            try:
                pending.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def receive():
        try:
            # Longer than the native ten-second heartbeat, not the reconciliation interval.
            for event in api.events(timeout=15):
                if closed.is_set() or stopping.is_set():
                    break
                deliver((event, None))
        except Exception as error:
            deliver((None, error))
        finally:
            deliver((None, ConnectionError('Native event stream closed')))

    threading.Thread(target=receive, daemon=True).start()
    try:
        while not stopping.is_set():
            try:
                event, error = pending.get(timeout=interval)
            except queue.Empty:
                yield {'type': 'observer.tick'}
                continue
            if error:
                raise error
            yield event
    finally:
        closed.set()


def ensure_server(directory):
    meta = read(directory)
    if meta.get('server_lifecycle') == 'running':
        try:
            if client(directory).request('GET', '/global/health').get('healthy'):
                if not owner_alive(meta):
                    raise ValueError('Native backend has no live owner; inspect or shut it down before submitting')
                return
        except OSError:
            pass
    with locked(directory / 'launch.lock'):
        meta = read(directory)
        if meta.get('server_lifecycle') == 'running':
            try:
                if client(directory).request('GET', '/global/health').get('healthy'):
                    if not owner_alive(meta):
                        raise ValueError('Native backend has no live owner; inspect or shut it down before submitting')
                    return
            except OSError:
                pass
        try:
            with locked(directory / 'server.lock', blocking=False):
                pass
        except BlockingIOError:
            raise ValueError('Native owner is still active but unresponsive; no backend was replaced or task replayed') from None
        checked_settings(directory, meta)
        server_password(directory)
        update(directory, server_lifecycle='starting', server_error=None, stop_requested=False)
        with (directory / 'owner.log').open('ab') as log:
            subprocess.Popen([sys.executable, '-I', str(ROOT / 'live_worker.py'), '_serve', str(directory)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                             env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            meta = read(directory)
            if meta.get('server_lifecycle') == 'running':
                return
            if meta.get('server_error'):
                raise ValueError('Native backend failed; inspect private owner/server logs')
            time.sleep(0.05)
        raise ValueError('Native backend not ready; no task was replayed')


def prior_submission(directory, submission_id, fingerprint):
    if submission_id:
        for path in directory.glob('turn-*/receipt.json'):
            saved = json.loads(path.read_text())
            if saved.get('submission_id') == submission_id:
                if saved.get('task_sha256') != fingerprint:
                    raise ValueError('Submission ID already names a different task; nothing was replayed')
                if saved.get('stage_status') == 'pending':
                    raise ValueError('Prior submission has uncertain native staging; inspect the retained session before any explicit new task. Nothing was replayed.')
                if saved.get('dispatch_status') == 'pending':
                    raise ValueError('Prior submission has uncertain native dispatch; inspect status. Nothing was redispatched.')
                return {**saved, 'run_id': directory.name, 'already_submitted': True}
    return None


def require_known_dispatch(directory):
    if read(directory).get('pending_dispatch'):
        status(directory)  # Read-only reconciliation against this exact native request.
        if read(directory).get('pending_dispatch'):
            raise ValueError('Uncertain native dispatch; inspect retained status before any new submission. Nothing was redispatched.')


def send(directory, task, submission_id=None, controls=None):
    # All entry points serialize controls and admission before acquiring existing locks.
    with locked(directory / 'submission.lock'):
        if not task.strip():
            raise ValueError('Task file is empty')
        require_known_dispatch(directory)
        fingerprint = hashlib.sha256(task.encode()).hexdigest()
        prior = prior_submission(directory, submission_id or fingerprint, fingerprint)
        if prior:
            return prior
        if controls is not None:
            configure_idle(directory, controls)
        return _send(directory, task, submission_id)


def _send(directory, task, submission_id=None):
    if not task.strip():
        raise ValueError('Task file is empty')
    require_known_dispatch(directory)
    fingerprint = hashlib.sha256(task.encode()).hexdigest()
    submission_id = submission_id or fingerprint
    prior = prior_submission(directory, submission_id, fingerprint)
    if prior:
        return prior
    if read(directory).get('pending_submission'):
        raise ValueError('Uncertain native staging blocks new submissions; inspect retained state')
    with batch.credit_lock():
        batch.credit_preflight()
    batch.validate_worktree(read(directory)['worktree'])
    ensure_server(directory)
    with locked(directory / 'control.lock'):
        prior = prior_submission(directory, submission_id, fingerprint)
        if prior:
            return prior
        meta = read(directory)
        if meta.get('pending_dispatch') or meta.get('pending_submission'):
            raise ValueError('Uncertain native submission blocks new tasks; inspect retained state. Nothing was redispatched.')
        if meta.get('stop_requested'):
            raise ValueError('Native backend is stopping; no task was staged. Retry explicitly after shutdown completes')
        if any(json.loads(path.read_text()).get('stage_status') == 'pending'
               for path in directory.glob('turn-*/receipt.json')):
            raise ValueError('Uncertain native staging receipt blocks new submissions; inspect retained state')
        batch.validate_worktree(meta['worktree'])
        api = client(directory)
        if meta.get('current_request_id'):
            current = api.snapshot(meta['session_id'], meta['current_request_id'])
            remember(directory, current)
            if current['status'] not in TERMINAL or current.get('native_status') != 'idle':
                raise ValueError('Prior request is still active; wait or cancel before a distinct submission')
        turn = directory / ('turn-' + uuid.uuid4().hex)
        batch.private_dir(turn)
        (turn / 'task.txt').write_text(task)
        with batch.credit_lock():
            batch.credit_preflight()
            atomic(turn / 'receipt.json', {'submission_id': submission_id, 'task_sha256': fingerprint,
                'stage_status': 'pending', 'session_id': meta['session_id']})
            try:
                message, payload = api.stage(meta['session_id'], task, meta['model'], meta['reasoning'])
            except OSError:
                update(directory, pending_submission=turn.name)
                raise ValueError(f'Worker {directory.name} retained an uncertain staging receipt; inspect native state before any new submission') from None
            # Persist identity before starting execution, including ambiguous transport failures.
            atomic(turn / 'receipt.json', {'request_id': message, 'session_id': meta['session_id'],
                'submission_id': submission_id, 'task_sha256': fingerprint, 'stage_status': 'staged', 'dispatch_status': 'pending'})
            update(directory, current_request_id=message, latest_turn=turn.name, submitted_at=time.time(),
                   current_result=None, session_error=None, pending_submission=None, pending_dispatch=turn.name, status='queued')
            try:
                api.dispatch(meta['session_id'], message, payload)
            except OSError:
                raise ValueError(f'Worker {directory.name} retained an uncertain native dispatch; inspect status. Nothing was redispatched.') from None
            with locked(directory / 'metadata.lock'):
                latest = read(directory)
                confirm_dispatch(directory, latest, message, 'native_acknowledgement')
                atomic(directory / 'metadata.json', latest)
        return {'run_id': directory.name, 'session_id': meta['session_id'], 'request_id': message, 'status': 'queued'}


def status(directory):
    meta = read(directory)
    if meta.get('pending_dispatch'):
        # Receipt acknowledgement may have committed just before an owner crash.
        with locked(directory / 'metadata.lock'):
            meta = read(directory)
            if meta.get('pending_dispatch'):
                receipt = json.loads((directory / meta['pending_dispatch'] / 'receipt.json').read_text())
                if receipt.get('dispatch_status') == 'confirmed' and receipt.get('request_id') == meta.get('current_request_id'):
                    meta['pending_dispatch'] = None
                    atomic(directory / 'metadata.json', meta)
    if meta.get('pending_submission') or not meta.get('current_request_id'):
        return {'run_id': directory.name, 'status': 'incomplete', 'native_status': 'unknown',
                'server_lifecycle': meta.get('server_lifecycle'), 'pending_submission': meta.get('pending_submission'),
                'failure_reason': 'pending_submission' if meta.get('pending_submission') else 'no_accepted_request',
                'message': 'Native staging is uncertain or no accepted request is recorded; inspect retained staging before any explicit new submission'}
    if meta.get('server_lifecycle') == 'running':
        try:
            result = client(directory).snapshot(meta['session_id'], meta.get('current_request_id'))
            remember(directory, result)
            result = read(directory).get('current_result') or result
        except OSError:
            result = read(directory).get('current_result')
            if not result or result['status'] not in TERMINAL:
                if not meta.get('pending_dispatch'):
                    raise ValueError('Native transport unavailable; inspect partial state before an explicit follow-up')
                result = result or {}
    else:
        result = meta.get('current_result') or {'request_id': meta['current_request_id']}
        if result.get('execution_observed') and result.get('request_id') == meta.get('current_request_id'):
            remember(directory, result)
        if result.get('status') not in TERMINAL:
            result = {**result, 'status': 'failed', 'native_status': 'unknown',
                'failure_reason': 'backend_stopped_without_terminal_receipt',
                'message': 'Native termination outcome is unknown; partial text/side effects are retained. Inspect before an explicit follow-up; nothing was replayed.'}
    latest = read(directory)
    if result.get('request_id') != latest.get('current_request_id'):
        # Never relabel an older observation as a newly admitted request.
        result = latest.get('current_result') or {}
        if result.get('request_id') != latest.get('current_request_id'):
            result = {'request_id': latest.get('current_request_id'), 'status': 'incomplete',
                      'failure_reason': 'request_advanced', 'text': ''}
    if latest.get('pending_dispatch'):
        result = {**result, 'status': 'incomplete', 'request_id': latest.get('current_request_id'),
                  'failure_reason': 'dispatch_unconfirmed', 'dispatch_status': 'pending',
                  'pending_dispatch': latest['pending_dispatch'],
                  'message': 'Native dispatch acceptance is unknown; inspect retained state. No automatic redispatch occurs.'}
    # Older receipts inferred ownership from session identity alone. Keep their raw
    # diagnostic, but never expose it as a qualified request error on late attachment.
    if (result.get('incomplete_reason') == 'dispatched_without_execution' and result.get('error')
            and result.get('error_attribution') != 'explicit_message_id'):
        result = {**result, 'unattributed_session_error': result['error']}
        result.pop('error')
    return {**result, 'run_id': directory.name, 'server_lifecycle': read(directory).get('server_lifecycle'),
            'native_server_lifetime_peak_rss_kib': read(directory).get('native_server_lifetime_peak_rss_kib'),
            'pricing_warning': read(directory).get('pricing_warning')}


def wait_result(directory, result, request_id):
    # Linearize the returned cursor against send's existing control lock.
    # Never hold it while observing native network/event activity.
    with locked(directory / 'control.lock'):
        meta = read(directory)
        if meta.get('current_request_id') != request_id:
            result = {'status': 'superseded', 'request_id': request_id,
                      'next_request_id': meta.get('current_request_id')}
        else:
            retained = meta.get('current_result') or {}
            if (retained.get('request_id') == request_id
                    and ((retained.get('observed_boot_id')
                          and retained.get('observed_boot_id') == result.get('observed_boot_id')
                          and retained.get('observed_at_ns', 0) > result.get('observed_at_ns', 0))
                         or (retained.get('status') in TERMINAL
                             and result.get('status') not in TERMINAL))):
                result = retained
        return {**result, 'run_id': directory.name, 'pricing_warning': meta.get('pricing_warning')}


def wait(directory, request_id=None):
    # A newer retained observation can invalidate a provisional terminal result.
    # Keep the original cursor while observing again rather than returning running.
    if request_id is None:
        with locked(directory / 'control.lock'):
            request_id = read(directory).get('current_request_id')
    while True:
        result = wait_once(directory, request_id)
        if result['status'] in TERMINAL or result['status'] in {'needs_input', 'superseded'}:
            return result


def wait_once(directory, request_id=None):
    with locked(directory / 'control.lock'):
        meta = read(directory)
    if request_id is not None and meta.get('current_request_id') != request_id:
        return wait_result(directory, {}, request_id)
    current = status(directory)
    if current['status'] in TERMINAL or current['status'] == 'needs_input':
        return wait_result(directory, current, meta.get('current_request_id'))
    def observer(event):
        if read(directory).get('current_request_id') != meta['current_request_id']:
            raise RequestAdvanced()
        observe(directory, event)
    try:
        result = client(directory).wait(meta['session_id'], meta['current_request_id'], observer=observer)
        remember(directory, result)
    except RequestAdvanced:
        result = {'status': 'superseded', 'request_id': meta['current_request_id']}
    except OSError:
        result = status(directory)  # Terminal receipt survives idle server reaping.
    return wait_result(directory, result, meta.get('current_request_id'))


def start(args):
    with batch.credit_lock():
        batch.credit_preflight()
    batch.credential_check(batch.KEY)
    task = Path(args.task_file).read_text()
    if not task.strip():
        raise ValueError('Task file is empty')
    if args.timeout is not None and args.timeout < 0:
        raise ValueError('Timeout must be positive or 0 for unlimited')
    model = args.model if args.model.startswith('openai/') else 'openai/' + args.model
    if not re.fullmatch(r'openai/[A-Za-z0-9][A-Za-z0-9._-]*', model):
        raise ValueError('Expected an explicit direct OpenAI model ID')
    limits = batch.model_limits(model, args)
    budget = batch.output_budget(args, {}, limits)
    steps = batch.step_limit(args, {})
    worktree = str(batch.validate_worktree(args.worktree))
    spec = hashlib.sha256(json.dumps({'task': task, 'worktree': worktree, 'model': model, 'reasoning': args.reasoning,
        'write': args.allow_write, 'commands': args.allow_commands, 'long_context': args.long_context,
        'steps': steps, 'output': budget, 'limits': limits, 'timeout': args.timeout}, sort_keys=True).encode()).hexdigest()
    directory = batch.STATE / (args.worker_id or spec[:32])
    metadata = directory / 'metadata.json'
    if metadata.exists() or metadata.is_symlink():
        previous = read(directory)
        if previous.get('backend') != 'native-server' or previous.get('start_spec_sha256') != spec:
            raise ValueError('Worker ID already exists with a different initial assignment; nothing was replayed')
        initial_id = previous.get('initial_submission_id')
        if not initial_id:
            raise ValueError('Initial submission identity is unavailable; inspect retained receipts. Nothing was replayed.')
        if args.submission_id is not None and args.submission_id != initial_id:
            raise ValueError('Submission ID differs from the initial assignment; nothing was replayed')
        require_known_dispatch(directory)
        return send(directory, task, initial_id)
    batch.private_dir(directory)
    # Older wait commands could leave a control lock before initialization.
    for entry in directory.iterdir():
        initialization_file = entry.name in ('settings.json', 'server-auth', 'control.lock') or entry.name.startswith('.receipt-')
        if not initialization_file or entry.is_symlink() or not entry.is_file():
            raise ValueError('Worker has retained state without metadata. Inspect it before starting work.')
    settings = batch.config(model, args.reasoning, args.allow_write, steps, key=batch.KEY, worktree=worktree, limits=limits)
    settings = live_settings(settings, allow_commands=args.allow_commands, long_context=args.long_context,
                             shell_plugin=(ROOT / 'command_environment.js').as_uri())
    settings['agent']['worker']['prompt'] = ('Follow the explicit delegated task, applicable AGENTS.md and its closed command/validation allowlist. '
        'Read applicable nested instructions before edits. No review, CI, PR, controller, commit, push or publication authority is implied. '
        'Use questions for required clarification. Report partial effects and proof limits honestly. Tool permissions are not an OS sandbox.')
    command_env = {}
    if args.allow_commands:
        command_env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
        for name in ('JAVA_HOME', 'GRADLE_USER_HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT'):
            if os.environ.get(name):
                command_env[name] = os.environ[name]
        command_env.setdefault('GRADLE_USER_HOME', str(Path.home() / '.gradle'))
    atomic(directory / 'settings.json', settings)
    with (directory / 'server-auth').open('w') as password_file:
        os.fchmod(password_file.fileno(), 0o600)
        password_file.write(secrets.token_urlsafe(32))
        password_file.flush()
        os.fsync(password_file.fileno())
    initial_id = args.submission_id or hashlib.sha256(task.encode()).hexdigest()
    atomic(directory / 'metadata.json', {'backend': 'native-server', 'initial_submission_id': initial_id, 'start_spec_sha256': spec, 'settings_sha256': settings_digest(settings), 'status': 'starting', 'run_id': directory.name, 'worktree': worktree,
        'model': model, 'reasoning': args.reasoning, 'model_limits': limits, 'model_limits_explicit': bool(args.context_window or args.model_output_limit),
        'allow_write': args.allow_write, 'allow_commands': args.allow_commands, 'long_context': args.long_context,
        'steps': steps, 'steps_explicit': steps is not None, 'output_override': budget, 'output_limit_explicit': budget is not None,
        'max_output_tokens': budget or min(32000, limits['output']), 'timeout_seconds': args.timeout or None,
        'command_environment': command_env, 'idle_reap_seconds': 30, 'server_lifecycle': 'new'})
    return send(directory, task, initial_id)




def reply(directory, question_id, answers):
    require_known_dispatch(directory)
    if not isinstance(answers, list) or any(not isinstance(answer, list) or any(not isinstance(label, str) for label in answer) for answer in answers):
        raise ValueError('Answers must be an array of arrays of strings')
    with batch.credit_lock():
        batch.credit_preflight()
        meta = read(directory)
        return client(directory).reply(meta['session_id'], question_id, answers)


def configure_idle(directory, args):
    fields = ('max_steps', 'max_output_tokens', 'context_window', 'model_output_limit', 'timeout', 'long_context')
    if not any(getattr(args, field, None) is not None for field in fields):
        return
    with locked(directory / 'control.lock'), locked(directory / 'launch.lock'):
        meta = read(directory)
        if meta.get('pending_submission') or meta.get('pending_dispatch'):
            raise ValueError('Pending native submission is uncertain; inspect retained state before changing controls')
        if meta.get('server_lifecycle') == 'starting':
            raise ValueError('Native backend is starting; retry controls after it is running or stopped')
        if meta.get('server_lifecycle') == 'running':
            api = client(directory)
            if meta.get('current_request_id'):
                state = api.snapshot(meta['session_id'], meta['current_request_id'])
            else:
                state = {'native_status': api.request('GET', '/session/status').get(meta['session_id'], {'type': 'idle'})['type'],
                         'questions': api.questions(meta['session_id'])}
            if (state['native_status'] != 'idle' or state['questions']
                    or (meta.get('current_request_id') and state.get('status') not in TERMINAL)):
                raise ValueError('Control overrides require an idle worker; send without overrides to follow up live')
            # Stop only this idle managed backend; the native DB/session remains.
            signal_owned(meta, 'owner', signal.SIGTERM)
            deadline = time.monotonic() + 10
            while read(directory).get('server_lifecycle') != 'stopped' and time.monotonic() < deadline:
                time.sleep(0.05)
            if read(directory).get('server_lifecycle') != 'stopped':
                raise ValueError('Idle backend shutdown not confirmed; controls were not changed')
        limits = batch.model_limits(meta['model'], args, meta)
        budget = batch.output_budget(args, meta, limits)
        steps = batch.step_limit(args, meta)
        timeout = getattr(args, 'timeout', None)
        if timeout is not None and timeout < 0:
            raise ValueError('Timeout must be positive or 0 for unlimited')
        long_context = meta.get('long_context', False) if args.long_context is None else args.long_context
        settings = batch.config(meta['model'], meta['reasoning'], meta['allow_write'], steps,
            key=batch.KEY, worktree=meta['worktree'], limits=limits, long_context=long_context)
        settings = live_settings(settings, allow_commands=meta.get('allow_commands', False), long_context=long_context,
            shell_plugin=(ROOT / 'command_environment.js').as_uri())
        settings['agent']['worker']['prompt'] = json.loads((directory / 'settings.json').read_text())['agent']['worker']['prompt']
        # Mark the transaction first: failure at either following write must block launch.
        update(directory, controls_pending=True)
        atomic(directory / 'settings.json', settings)
        update(directory, controls_pending=False, settings_sha256=settings_digest(settings), steps=steps, steps_explicit=steps is not None, output_override=budget,
            output_limit_explicit=budget is not None, max_output_tokens=budget or min(32000, limits['output']),
            model_limits=limits, model_limits_explicit=bool(meta.get('model_limits_explicit') or args.context_window or args.model_output_limit),
            long_context=long_context, timeout_seconds=meta.get('timeout_seconds') if timeout is None else timeout or None)


def main(argv=None):
    os.umask(0o077)
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == '_serve':
        run_server(Path(argv[1]))
        return 0
    parser = argparse.ArgumentParser(description='Managed native OpenCode live sessions; no model loop in this client')
    sub = parser.add_subparsers(dest='action', required=True)
    new = sub.add_parser('start')
    for flag in ('task-file', 'worktree', 'model'):
        new.add_argument('--' + flag, required=True)
    new.add_argument('--reasoning', '--variant', required=True, choices=batch.REASONING_CHOICES)
    new.add_argument('--wait', action='store_true', help='Print the start receipt, then block on native events')
    new.add_argument('--worker-id', type=batch.run_id, help='Optional stable ID: exactly 32 lowercase hexadecimal characters (for example, 0123456789abcdef0123456789abcdef); omit to generate one. The same initial assignment is not replayed')
    new.add_argument('--submission-id', help='Owner-supplied idempotency key for this task')
    for flag in ('allow-write', 'allow-commands', 'long-context'):
        help_text = {'allow-write': 'Enable native edit tools; does not restrict Bash filesystem writes',
            'allow-commands': 'Grant broad native Bash execution, including writes/tests; omit for read-only or Validation NONE tasks',
            'long-context': 'Opt in to full-window native compaction policy'}[flag]
        new.add_argument('--' + flag, action='store_true', help=help_text)
    for flag in ('max-output-tokens', 'max-steps', 'context-window', 'model-output-limit', 'timeout'):
        new.add_argument('--' + flag, type=int, help=('Advisory native step target, not an enforced tool-call cap; 0 clears saved target' if flag == 'max-steps' else None))
    for name in ('send', 'resume', 'wait', 'status', 'result', 'cancel', 'shutdown', 'reply'):
        item = sub.add_parser(name)
        item.add_argument('run_id', type=batch.run_id)
        if name in ('send', 'resume'):
            item.add_argument('--task-file', required=True)
            item.add_argument('--submission-id', help='Same key/task returns the prior receipt without replay')
            for flag in ('max-output-tokens', 'max-steps', 'context-window', 'model-output-limit', 'timeout'):
                item.add_argument('--' + flag, type=int, help=('Advisory native step target, not an enforced tool-call cap; 0 clears saved target' if flag == 'max-steps' else None))
            item.add_argument('--long-context', action=argparse.BooleanOptionalAction, default=None)
        if name == 'reply':
            item.add_argument('--question-id', required=True)
            item.add_argument('--answers-file', required=True, help='JSON file containing one array of selected answer strings per question, for example [["Proceed"]]')
    args = parser.parse_args(argv)
    if args.action == 'start':
        batch.private_dir(batch.STATE)
        with locked(batch.STATE / 'start.lock'):
            result = start(args)
    else:
        directory = batch.STATE / args.run_id
        if not (directory / 'metadata.json').is_file():
            raise ValueError(f'Unknown worker ID: {args.run_id}')
        batch.private_dir(batch.STATE)
        batch.private_dir(directory)
        if args.action in ('send', 'resume'):
            result = send(directory, Path(args.task_file).read_text(), args.submission_id, controls=args)
        elif args.action == 'wait':
            result = wait(directory)
        elif args.action in ('status', 'result'):
            result = status(directory)
            if args.action == 'result':
                print(result.get('text', ''))
                return 0
        elif args.action == 'reply':
            meta = read(directory)
            reply(directory, args.question_id, json.loads(Path(args.answers_file).read_text()))
            result = {'run_id': directory.name, 'status': 'answer_submitted'}
        else:
            # Serialize cancellation and shutdown, including native cleanup, against send.
            with locked(directory / 'control.lock'):
                meta = read(directory)
                if args.action == 'shutdown':
                    if meta.get('server_lifecycle') == 'starting':
                        raise ValueError('Backend is starting; retry shutdown after it reports running or stopped')
                    update(directory, stop_requested=True)
                if meta.get('server_lifecycle') == 'running':
                    api = client(directory)
                    api.abort(meta['session_id'])
                    if args.action == 'shutdown':
                        deadline = time.monotonic() + 10
                        while api.request('GET', '/session/status').get(meta['session_id'], {'type': 'idle'})['type'] != 'idle':
                            if time.monotonic() >= deadline:
                                raise ValueError('Native cancellation cleanup not confirmed; backend retained for inspection')
                            time.sleep(0.05)
                        # Retain the terminal receipt before the owner's signal
                        # handler can stop observing the native session.
                        if meta.get('current_request_id'):
                            remember(directory, api.snapshot(meta['session_id'], meta['current_request_id']))
                        if not signal_owned(meta, 'owner', signal.SIGTERM):
                            signal_owned(meta, 'native_server', signal.SIGTERM)
                        while read(directory).get('server_lifecycle') != 'stopped' and time.monotonic() < deadline:
                            if process_identity(meta.get('native_server_pid')) != meta.get('native_server_start_ticks'):
                                update(directory, server_lifecycle='stopped', owner_pid=None, native_server_pid=None)
                                break
                            time.sleep(0.05)
                        if read(directory).get('server_lifecycle') != 'stopped':
                            raise ValueError('Backend shutdown not confirmed; inspect private lifecycle state')
            result = {'run_id': directory.name, 'status': 'cancel_requested' if args.action == 'cancel' else 'stopped'}
    if args.action == 'start' and args.wait:
        print(json.dumps(result), flush=True)
        result = wait(batch.STATE / result['run_id'], result.get('request_id'))
    print(json.dumps(result))
    if args.action == 'wait' or (args.action == 'start' and args.wait):
        return {'complete': 0, 'incomplete': 2, 'needs_input': 3, 'superseded': 4, 'interrupted': 124}.get(result['status'], 1)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print('api-worker: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
