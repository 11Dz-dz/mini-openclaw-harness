import os
from pathlib import Path

MAX_COPY_FILES = 20_000
MAX_COPY_BYTES = 512 * 1024 * 1024
MAX_COPY_FILE_BYTES = 64 * 1024 * 1024
MAX_COPY_DEPTH = 64
IGNORED_NAMES = frozenset({'.venv', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache'})


def ignored_entries(current: str, names: list[str]) -> set[str]:
    return {name for name in names if name in IGNORED_NAMES
            or (Path(current) / name).is_symlink()}


def check_copy_bounds(source: Path) -> None:
    count, total = 0, 0
    for current, directories, files in os.walk(source, followlinks=False):
        ignored = ignored_entries(current, [*directories, *files])
        directories[:] = [name for name in directories if name not in ignored]
        if len(Path(current).relative_to(source).parts) > MAX_COPY_DEPTH:
            raise ValueError('workspace exceeds copy depth limit')
        for name in files:
            if name in ignored:
                continue
            path = Path(current) / name
            if not path.is_file():
                raise ValueError('workspace contains unsupported special files')
            size = path.stat().st_size
            count += 1
            total += size
            if size > MAX_COPY_FILE_BYTES or count > MAX_COPY_FILES or total > MAX_COPY_BYTES:
                raise ValueError('workspace exceeds copy size or file count limit')
