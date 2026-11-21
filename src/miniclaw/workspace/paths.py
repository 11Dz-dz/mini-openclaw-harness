from pathlib import Path, PurePosixPath


class WorkspaceEscape(ValueError):
    pass


def resolve_workspace_path(
    root: Path, relative: str | Path, *, mounts: tuple[str, ...] = (),
) -> Path:
    workspace = root.resolve()
    requested = Path(relative)
    guest_path = PurePosixPath(str(relative))
    if guest_path.is_absolute():
        for mount in mounts:
            if guest_path.is_relative_to(mount):
                suffix = guest_path.relative_to(mount)
                if ".." in suffix.parts or "\\" in str(suffix):
                    raise WorkspaceEscape("invalid path under a workspace mount")
                requested = Path(*suffix.parts)
                break
        else:
            raise WorkspaceEscape("absolute paths are outside the workspace")
    if requested.anchor:
        raise WorkspaceEscape("absolute paths are outside the workspace")
    candidate = (workspace / requested).resolve()
    if not candidate.is_relative_to(workspace):
        raise WorkspaceEscape("path escapes the workspace")
    return candidate
