#!/usr/bin/env python3
"""Copy a prepared checkout into a fresh prefix; never upgrade an existing install."""
import argparse
from pathlib import Path
import os
import shutil
import tempfile

SOURCE = Path(__file__).resolve().parent
MODULES = ('worker.py', 'live_worker.py', 'native_transport.py', 'cli_router.py', 'command_environment.js')


def remove_tree(path):
    def writable_retry(function, name, error):
        os.chmod(Path(name).parent, 0o700)
        if not Path(name).is_symlink():
            os.chmod(name, 0o700)
        function(name)
    shutil.rmtree(path, onerror=writable_retry)


def install(prefix):
    prefix = prefix.expanduser().resolve()
    runtime = (SOURCE / 'runtime').resolve()
    destination = prefix / 'share/api-worker'
    launcher = prefix / 'bin/api-worker'
    if any(path.is_relative_to(runtime) for path in (prefix, destination.parent.resolve(), launcher.parent.resolve())):
        raise ValueError('Choose a --prefix, share and bin directories outside the source runtime')
    if destination.exists() or destination.is_symlink() or launcher.exists() or launcher.is_symlink():
        raise ValueError('Refusing existing api-worker destination or launcher; choose a fresh --prefix')
    if not (runtime / 'node_modules/opencode-linux-x64/bin/opencode').is_file():
        raise ValueError('Prepare the pinned runtime first: npm ci --prefix runtime (Linux x86_64 required)')
    destination.parent.mkdir(parents=True, exist_ok=True)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.api-worker-install-', dir=destination.parent))
    created_destination = False
    created_launcher = None
    try:
        for name in (*MODULES, 'README.md', 'LICENSE'):
            shutil.copyfile(SOURCE / name, staging / name)
        shutil.copytree(runtime, staging / 'runtime', symlinks=True)
        # mkdir is the no-clobber reservation, including against concurrent installers.
        destination.mkdir(mode=0o700)
        created_destination = True
        for entry in staging.iterdir():
            entry.rename(destination / entry.name)
        with launcher.open('x') as target:
            info = os.fstat(target.fileno())
            created_launcher = (info.st_dev, info.st_ino)
            target.write((SOURCE / 'bin/api-worker').read_text())
            target.flush()
            os.fchmod(target.fileno(), 0o700)
    except BaseException as original:
        cleanup_errors = []
        if created_launcher is not None:
            try:
                info = launcher.lstat()
                if (info.st_dev, info.st_ino) == created_launcher:
                    launcher.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                cleanup_errors.append(str(error))
        for path in ([destination] if created_destination else []) + [staging]:
            try:
                remove_tree(path)
            except OSError as error:
                cleanup_errors.append(str(error))
        if cleanup_errors and not isinstance(original, Exception):
            # Keep cancellation/exit semantics even when rollback itself fails.
            if hasattr(original, 'add_note'):
                original.add_note('Rollback also failed: ' + '; '.join(cleanup_errors))
            raise
        if cleanup_errors:
            raise OSError(f'{original}; rollback also failed: ' + '; '.join(cleanup_errors)) from original
        raise
    else:
        remove_tree(staging)
    return launcher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True, help='Fresh installation prefix; launcher goes in PREFIX/bin')
    args = parser.parse_args()
    try:
        print(install(args.prefix))
    except (OSError, ValueError) as error:
        parser.exit(1, f'install: {error}\n')


if __name__ == '__main__':
    main()
