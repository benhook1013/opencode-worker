#!/usr/bin/env python3
"""Copy a prepared checkout into a fresh prefix; never upgrade an existing install."""
import argparse
from pathlib import Path
import os
import shutil
import tempfile

SOURCE = Path(__file__).resolve().parent
MODULES = ('worker.py', 'live_worker.py', 'native_transport.py', 'cli_router.py', 'command_environment.js')


def install(prefix):
    prefix = prefix.expanduser().resolve()
    destination = prefix / 'share/api-worker'
    launcher = prefix / 'bin/api-worker'
    if destination.exists() or destination.is_symlink() or launcher.exists() or launcher.is_symlink():
        raise ValueError('Refusing existing api-worker destination or launcher; choose a fresh --prefix')
    runtime = SOURCE / 'runtime'
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
    except Exception:
        if created_launcher is not None:
            try:
                info = launcher.lstat()
                if (info.st_dev, info.st_ino) == created_launcher:
                    launcher.unlink()
            except FileNotFoundError:
                pass
        if created_destination:
            shutil.rmtree(destination)
        raise
    finally:
        shutil.rmtree(staging)
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
