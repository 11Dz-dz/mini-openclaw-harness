import asyncio
import os
import stat
from functools import partial
from pathlib import Path
from tempfile import NamedTemporaryFile

from miniclaw.tools.types import (
    RegisteredTool,
    ToolCapabilities,
    ToolContext,
    ToolSpec,
)
from miniclaw.workspace.paths import resolve_workspace_path

MAX_FILE_READ_BYTES = 1024 * 1024
MAX_FILE_PAGE_BYTES = 12 * 1024
MAX_FILE_PAGE_LINES = 300
MAX_SEARCH_BYTES = 32 * 1024 * 1024
MAX_SEARCH_FILES = 5000
SEARCH_EXCLUDED = {'.git', '.venv', '__pycache__', 'node_modules'}


async def read_file(
    arguments: dict[str, object],
    context: ToolContext,
    *,
    mounts: tuple[str, ...] = (),
) -> str:
    path = resolve_workspace_path(context.workspace, str(arguments["path"]), mounts=mounts)
    with path.open('rb') as file:
        data = file.read(MAX_FILE_READ_BYTES + 1)
    if len(data) > MAX_FILE_READ_BYTES:
        raise ValueError('file exceeds read_file limit of 1 MiB')
    start = int(arguments.get("start_line", 1)) - 1
    limit = int(arguments.get("max_lines", MAX_FILE_PAGE_LINES))
    requested_page = "max_lines" in arguments or "start_line" in arguments
    lines = data.splitlines(keepends=True)
    if start >= len(lines):
        if not lines and not requested_page:
            return ""
        return f"[No lines at or after line {start + 1}. File has {len(lines)} lines.]"
    page = []
    length = 0
    for lineno, line in enumerate(lines[start:start + limit], start + 1):
        numbered = f"{lineno}: ".encode() + line
        if length + len(numbered) > MAX_FILE_PAGE_BYTES:
            break
        length += len(numbered)
        page.append(numbered)
    if not page:
        raise ValueError(f"line {start + 1} exceeds the 12 KiB page limit; use a bounded command to inspect part of that line")

    end = start + len(page)
    content = b"".join(page).decode("utf-8")
    if end == len(lines) and not requested_page:
        return content
    footer = f"[Read lines {start + 1}-{end} of {len(lines)}.]"
    if end < len(lines):
        footer += f" Continue with start_line={end + 1}."
    separator = "" if content.endswith("\n") else "\n"
    return f"{content}{separator}{footer}"


def _search_files(path: Path):
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        raise ValueError('search path must be an existing file or directory')
    for directory, subdirs, files in os.walk(path, followlinks=False):
        subdirs[:] = sorted(name for name in subdirs if name not in SEARCH_EXCLUDED)
        for name in sorted(files):
            yield Path(directory) / name


def _original_offset(line: str, folded_offset: int) -> int:
    for index, character in enumerate(line):
        folded_offset -= len(character.casefold())
        if folded_offset < 0:
            return index
    return len(line)


def _find_text(
    workspace: Path, path: Path, pattern: str, max_results: int, *, ignore_case: bool = False,
) -> str:
    matches = []
    scanned = 0
    output_bytes = 0
    needle = pattern.casefold() if ignore_case else pattern
    for index, candidate in enumerate(_search_files(path), 1):
        if index > MAX_SEARCH_FILES or scanned >= MAX_SEARCH_BYTES:
            return '\n'.join([*matches, '[Search limit reached. Narrow the path to continue.]'])
        try:
            details = candidate.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_FILE_READ_BYTES:
                continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(workspace):
                continue
            with resolved.open('rb') as stream:
                data = stream.read(min(MAX_FILE_READ_BYTES, MAX_SEARCH_BYTES - scanned) + 1)
            scanned += len(data)
            if scanned > MAX_SEARCH_BYTES:
                return '\n'.join([*matches, '[Search limit reached. Narrow the path to continue.]'])
            if len(data) > MAX_FILE_READ_BYTES or b'\0' in data:
                continue
            lines = data.decode('utf-8').splitlines()
        except (OSError, RuntimeError, UnicodeDecodeError):
            continue
        relative = resolved.relative_to(workspace).as_posix()
        for lineno, line in enumerate(lines, 1):
            haystack = line.casefold() if ignore_case else line
            offset = haystack.find(needle)
            if offset < 0:
                continue
            if len(haystack) != len(line):
                offset = _original_offset(line, offset)
            start = max(0, offset - 100)
            excerpt = ('…' if start else '') + line[start:start + 400]
            if start + 400 < len(line):
                excerpt += '…'
            result = f'{relative}:{lineno}:{excerpt}'
            output_bytes += len(result.encode('utf-8')) + 1
            if len(matches) >= max_results or output_bytes > MAX_FILE_PAGE_BYTES:
                return '\n'.join([*matches, '[Results truncated. Narrow the path or search text.]'])
            matches.append(result)
    return '\n'.join(matches) if matches else 'No matches found.'


async def search_text(
    arguments: dict[str, object], context: ToolContext, *, mounts: tuple[str, ...] = (),
) -> str:
    workspace = context.workspace.resolve()
    path = resolve_workspace_path(workspace, str(arguments.get('path', '.')), mounts=mounts)
    return await asyncio.to_thread(
        _find_text, workspace, path, str(arguments['pattern']), int(arguments.get('max_results', 40)),
        ignore_case=bool(arguments.get('ignore_case', False)),
    )


async def write_file(
    arguments: dict[str, object],
    context: ToolContext,
    *,
    mounts: tuple[str, ...] = (),
) -> str:
    path = resolve_workspace_path(context.workspace, str(arguments["path"]), mounts=mounts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(arguments["content"]))
    return f"wrote {path.relative_to(context.workspace)}"


async def replace_text(
    arguments: dict[str, object],
    context: ToolContext,
    *,
    mounts: tuple[str, ...] = (),
) -> str:
    path = resolve_workspace_path(context.workspace, str(arguments["path"]), mounts=mounts)
    relative = path.relative_to(context.workspace.resolve())
    old_text = str(arguments["old_text"]).encode("utf-8")
    new_text = str(arguments["new_text"]).encode("utf-8")
    if not old_text or old_text == new_text:
        raise ValueError("old_text must be nonempty and different from new_text")
    if not path.is_file():
        raise ValueError("replace_text requires an existing regular file")

    with path.open("rb") as reader:
        original = os.fstat(reader.fileno())
        data = reader.read(MAX_FILE_READ_BYTES + 1)
    if len(data) > MAX_FILE_READ_BYTES:
        raise ValueError("file exceeds replace_text limit of 1 MiB")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("replace_text requires a UTF-8 text file") from None
    if b"\0" in data or b"\0" in new_text:
        raise ValueError("replace_text does not accept NUL bytes")

    index = data.find(old_text)
    if index == -1:
        raise ValueError("old_text was not found. Re-read the file and copy the exact text and whitespace.")
    if data.find(old_text, index + 1) != -1:
        raise ValueError("old_text occurs more than once. Include more surrounding text to identify one match.")

    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".miniclaw-edit-", delete=False) as writer:
            temporary = Path(writer.name)
            writer.write(data[:index])
            writer.write(new_text)
            writer.write(data[index + len(old_text):])
            if hasattr(os, "fchown"):
                owner = os.fstat(writer.fileno())
                if (owner.st_uid, owner.st_gid) != (original.st_uid, original.st_gid):
                    os.fchown(writer.fileno(), original.st_uid, original.st_gid)
            os.chmod(temporary, stat.S_IMODE(original.st_mode))
            writer.flush()
            os.fsync(writer.fileno())
        current = path.stat()
        if (
            not os.path.samestat(original, current)
            or original.st_mtime_ns != current.st_mtime_ns
            or original.st_ctime_ns != current.st_ctime_ns
            or original.st_size != current.st_size
        ):
            raise ValueError("file changed while preparing the replacement. Re-read the file before retrying.")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return f"replaced text in {relative}"


def make_file_tools(
    *, execution_identity: tuple[int, int] | None = None, mounts: tuple[str, ...] = (),
) -> tuple[RegisteredTool, ...]:
    async def write_for_executor(arguments, context):
        path = resolve_workspace_path(context.workspace, str(arguments["path"]), mounts=mounts)
        created: list[Path] = []
        parent = path.parent
        while not parent.exists():
            created.append(parent)
            parent = parent.parent
        output = await write_file(arguments, context, mounts=mounts)
        if execution_identity is not None:
            # Only paths touched by this file operation, never a recursive
            # permission change on a tree already modified by a command.
            for entry in (*reversed(created), path):
                os.chown(entry, *execution_identity, follow_symlinks=False)
        return output

    closed_object = {"additionalProperties": False}
    path_description = "Workspace-relative file path"
    if mounts:
        path_description += f" or a path under a container workspace mount ({', '.join(mounts)})"
    path_schema = {"type": "string", "description": path_description}
    return (
        RegisteredTool(
            ToolSpec(
                "read_file",
                "Read a UTF-8 text file inside the workspace. "
                "Returns at most 300 lines or 12 KiB of numbered file content per call. "
                "Use start_line and max_lines to inspect the relevant code after searching.",
                {
                    "type": "object",
                    "properties": {
                        "path": path_schema,
                        "start_line": {"type": "integer", "minimum": 1, "description": "First line to read (1-based, default 1)"},
                        "max_lines": {"type": "integer", "minimum": 1, "maximum": MAX_FILE_PAGE_LINES},
                    },
                    "required": ["path"],
                    **closed_object,
                },
                ToolCapabilities(filesystem="read"),
            ),
            partial(read_file, mounts=mounts),
        ),
        RegisteredTool(
            ToolSpec(
                "write_file",
                "Write a UTF-8 text file inside the workspace",
                {
                    "type": "object",
                    "properties": {
                        "path": path_schema,
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    **closed_object,
                },
                ToolCapabilities(
                    filesystem="workspace-write",
                    side_effects=True,
                    idempotent=True,
                ),
            ),
            write_for_executor,
        ),
        RegisteredTool(
            ToolSpec(
                "search_text",
                "Search UTF-8 workspace files for literal text (case-sensitive by default). "
                "Return file:line:match and use read_file on the relevant lines. "
                "Search a directory recursively or one file, skipping binary files and common dependency caches.",
                {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "minLength": 1, "maxLength": 256},
                        "path": path_schema,
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                        "ignore_case": {"type": "boolean", "description": "Match case-insensitively when true"},
                    },
                    "required": ["pattern"],
                    **closed_object,
                },
                ToolCapabilities(filesystem="read"),
            ),
            partial(search_text, mounts=mounts),
        ),
        RegisteredTool(
            ToolSpec(
                "replace_text",
                "Edit an existing UTF-8 file without sending the entire file. "
                "Read the target region first and provide text that occurs exactly once, without display line numbers. "
                "Use read_file to inspect the surrounding lines if the edit fails.",
                {
                    "type": "object",
                    "properties": {
                        "path": path_schema,
                        "old_text": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Exact text to replace, including whitespace and line endings",
                        },
                        "new_text": {"type": "string", "description": "Replacement text, empty to delete"},
                    },
                    "required": ["path", "old_text", "new_text"],
                    **closed_object,
                },
                ToolCapabilities(
                    filesystem="workspace-write",
                    side_effects=True,
                    idempotent=False,
                ),
            ),
            partial(replace_text, mounts=mounts),
        ),
    )
