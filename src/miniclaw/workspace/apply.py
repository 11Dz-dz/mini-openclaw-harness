import contextlib
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from miniclaw.sessions.locking import file_lock
from miniclaw.workspace.paths import WorkspaceEscape, resolve_workspace_path


def file_state(path):
    if not path.is_file():
        return None
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return f'{digest}@{stat.S_IMODE(path.stat().st_mode):o}'


def safe_apply_path(root, relative):
    if '..' in relative.parts or not relative.parts:
        raise WorkspaceEscape('invalid artifact path')
    target = resolve_workspace_path(root, relative)
    current = root.resolve()
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceEscape('artifact path contains a symlink')
        if current.exists() and current != target and not current.is_dir():
            raise WorkspaceEscape('artifact parent is not a directory')
    if target.exists() and not target.is_file():
        raise WorkspaceEscape('artifact target is not a regular file')
    return target


def replace_file(source, target, mode):
    descriptor, name = tempfile.mkstemp(prefix='.miniclaw-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as writer, source.open('rb') as reader:
            shutil.copyfileobj(reader, writer, length=65536)
            os.fchmod(writer.fileno(), mode)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def apply_transaction(session, destination, paths, expected):
    lock_root = Path(tempfile.gettempdir()) / 'miniclaw-apply-locks'
    lock_root.mkdir(mode=0o700, exist_ok=True)
    lock_name = hashlib.sha256(str(destination.resolve()).encode()).hexdigest()
    with file_lock(lock_root / lock_name):
        manifest = session._preflight(destination, paths, expected)
        journal = Path(tempfile.mkdtemp(prefix='apply-', dir=session.artifacts))
        record = {'destination': str(destination.resolve()), 'status': 'prepared', 'operations': []}
        staged, originals = {}, {}
        for index, relative in enumerate(paths):
            source = safe_apply_path(session.workspace, relative)
            target = safe_apply_path(destination, relative)
            if source.is_file():
                staged[relative] = journal / f'new-{index}'
                shutil.copy2(source, staged[relative])
                if file_state(staged[relative]) != manifest[relative]:
                    raise PermissionError(f'artifact changed while staging: {relative}')
            if target.is_file():
                originals[relative] = journal / f'original-{index}'
                shutil.copy2(target, originals[relative])
            record['operations'].append({'path': str(relative), 'state': manifest[relative],
                                         'backup': str(originals.get(relative, ''))})
        session._preflight(destination, paths, manifest)

        def save_status(status):
            record['status'] = status
            temporary = journal / 'journal.tmp'
            with temporary.open('w') as file:
                json.dump(record, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(journal / 'journal.json')

        save_status('prepared')
        applied, created = [], []
        try:
            for relative in paths:
                session._preflight(destination, (relative,), manifest)
                target = safe_apply_path(destination, relative)
                missing = []
                parent = target.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for parent in reversed(missing):
                    parent.mkdir()
                    created.append(parent)
                if relative in staged:
                    replace_file(staged[relative], target, stat.S_IMODE(staged[relative].stat().st_mode))
                else:
                    target.unlink()
                applied.append(relative)
            save_status('complete')
        except BaseException as error:
            incomplete = []
            for relative in reversed(applied):
                target = safe_apply_path(destination, relative)
                if file_state(target) != manifest[relative]:
                    incomplete.append(str(relative))
                    continue
                try:
                    if relative in originals:
                        saved = originals[relative]
                        replace_file(saved, target, stat.S_IMODE(saved.stat().st_mode))
                    else:
                        target.unlink(missing_ok=True)
                except OSError:
                    incomplete.append(str(relative))
            for parent in reversed(created):
                with contextlib.suppress(OSError):
                    parent.rmdir()
            save_status('rollback_incomplete' if incomplete else 'rolled_back')
            if incomplete:
                raise OSError(f'apply rollback incomplete: {incomplete}; backups: {journal}') from error
            raise
        for relative in paths:
            if manifest[relative] is None:
                session.baseline.pop(relative, None)
                session.baseline_modes.pop(relative, None)
            else:
                session.baseline[relative], mode = manifest[relative].split('@')
                session.baseline_modes[relative] = int(mode, 8)
        return paths
