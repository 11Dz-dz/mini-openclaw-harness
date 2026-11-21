import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from miniclaw.core.events import EventEnvelope
from miniclaw.permissions.preview import arguments_preview
from miniclaw.permissions.types import PermissionRequest
from miniclaw.sessions.events import JsonlEventStore
from miniclaw.tools.types import RiskLevel, ToolCapabilities
from miniclaw.workspace.apply import apply_transaction, file_state, safe_apply_path
from miniclaw.workspace.copying import check_copy_bounds, ignored_entries
from miniclaw.workspace.diff import snapshot_files
from miniclaw.workspace.paths import WorkspaceEscape, resolve_workspace_path


class ApprovalProvider(Protocol):
    async def resolve(self, request: PermissionRequest): ...


@dataclass(frozen=True, slots=True)
class ArtifactApplied:
    destination: str
    paths: tuple[str, ...]


@dataclass(slots=True)
class WorkspaceSession:
    sandbox_id: str
    root: Path
    workspace: Path
    artifacts: Path
    baseline: dict[Path, str] = field(default_factory=dict)
    baseline_modes: dict[Path, int] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        root: Path,
        source: Path | None = None,
    ) -> "WorkspaceSession":
        sandbox_id = str(uuid4())
        session_root = root.resolve() / sandbox_id
        if source is not None:
            source_root = source.resolve()
            if session_root.is_relative_to(source_root):
                raise ValueError("sandbox root must be outside the source workspace")
            check_copy_bounds(source_root)
        workspace = session_root / "workspace"
        artifacts = session_root / "artifacts"
        session_root.mkdir(parents=True)
        artifacts.mkdir()
        if source is None:
            workspace.mkdir()
        else:
            source_root = source.resolve()

            shutil.copytree(
                source_root,
                workspace,
                symlinks=False,
                ignore=ignored_entries,
            )
        return cls(sandbox_id, session_root, workspace, artifacts,
                   snapshot_files(workspace),
                   {p.relative_to(workspace): stat.S_IMODE(p.stat().st_mode)
                    for p in workspace.rglob("*") if p.is_file() and not p.is_symlink()})

    def _preflight(self, destination, paths, expected_artifacts=None):
        current = snapshot_files(self.workspace)
        states = {}
        for relative in paths:
            source = safe_apply_path(self.workspace, relative)
            target = safe_apply_path(destination, relative)
            if relative not in current and relative not in self.baseline:
                raise WorkspaceEscape("artifact is not in the workspace manifest")
            state = file_state(source)
            states[relative] = state
            if expected_artifacts is not None and state != expected_artifacts[relative]:
                raise PermissionError(f"artifact changed after approval: {relative}")
            actual = file_state(target)
            actual_digest = actual.split('@')[0] if actual else None
            expected_mode = self.baseline_modes.get(relative)
            if (actual_digest != self.baseline.get(relative)
                    or (actual and expected_mode is not None
                        and int(actual.split('@')[1], 8) != expected_mode)):
                raise PermissionError(f"destination changed since workspace copy: {relative}")
        return states

    def manifest(self) -> tuple[Path, ...]:
        return tuple(sorted(set(snapshot_files(self.workspace)) | set(self.baseline)))

    def apply_artifacts(self, destination: Path, approved_paths: tuple[Path, ...], *,
                        expected_artifacts: dict | None = None) -> tuple[Path, ...]:
        normalized = tuple(dict.fromkeys(Path(path) for path in approved_paths))
        return apply_transaction(self, destination, normalized, expected_artifacts)

    async def apply_artifacts_approved(
        self,
        destination: Path,
        approved_paths: tuple[Path, ...],
        *,
        approval_provider: ApprovalProvider,
        event_store: JsonlEventStore,
        request_id: str | None = None,
    ) -> tuple[Path, ...]:
        normalized = tuple(Path(path) for path in approved_paths)
        manifest = self._preflight(destination, normalized)
        for relative in normalized:
            if relative not in manifest:
                raise WorkspaceEscape("artifact is not in the workspace manifest")
            resolve_workspace_path(self.workspace, relative)
            resolve_workspace_path(destination, relative)
        path_values = tuple(path.as_posix() for path in normalized)
        operation_id = request_id or str(uuid4())
        resolution = await approval_provider.resolve(
            PermissionRequest(
                operation_id,
                f"artifact-{self.sandbox_id}",
                operation_id,
                "apply_artifacts",
                arguments_preview({"destination": str(destination.resolve()),
                                   "paths": path_values,
                                   "sha256_and_mode": {str(p): manifest[p] for p in normalized},
                                   "operations": {str(p): ('delete' if manifest[p] is None else
                                       'modify' if p in self.baseline else 'create') for p in normalized}}),
                ToolCapabilities(
                    filesystem="workspace-write",
                    risk_level=RiskLevel.HIGH,
                    side_effects=True,
                    idempotent=False,
                ),
                "apply sandbox artifacts to the destination workspace",
            )
        )
        if resolution.request_id != operation_id or not resolution.approved:
            raise PermissionError(f"artifact application denied: {resolution.reason}")
        applied = self.apply_artifacts(destination, normalized, expected_artifacts=manifest)
        previous = await event_store.load()
        run_id = f"artifact-{self.sandbox_id}"
        run_events = tuple(event for event in previous if event.run_id == run_id)
        envelope = EventEnvelope.create(
            run_id=run_id,
            session_id=self.sandbox_id,
            sequence=len(run_events) + 1,
            payload=ArtifactApplied(
                str(destination.resolve()),
                tuple(path.as_posix() for path in applied),
            ),
            parent_event_id=run_events[-1].event_id if run_events else None,
        )
        await event_store.append(envelope)
        return applied
