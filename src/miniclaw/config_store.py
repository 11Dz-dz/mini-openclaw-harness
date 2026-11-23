import contextlib
import errno
import inspect
import os
import stat
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

from miniclaw.config import (
    CONFIG_FIELDS,
    RUNTIME_FIELDS,
    ConfigDocument,
    coerce_config_value,
    default_config_path,
    default_config_values,
    validate_config_values,
)
from miniclaw.config_toml import decode_config, encode_config, encode_scalar

_SECURE_DIR_FD_SUPPORTED = (
    os.name == "posix"
    and bool(getattr(os, "O_DIRECTORY", 0))
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and all(
        function in os.supports_dir_fd
        for function in (os.open, os.mkdir, os.stat, os.unlink)
    )
    and os.stat in os.supports_follow_symlinks
    and {"src_dir_fd", "dst_dir_fd"}.issubset(inspect.signature(os.replace).parameters)
)


class ConfigPathError(ValueError):
    """Raised when a config path is unsafe or not a regular file."""


class ConfigPersistenceError(OSError):
    """Raised when an atomic config write cannot be completed."""


def redact_secret(value: object | None) -> str:
    if value is None:
        return "not configured"
    text = str(value)
    if len(text) <= 4:
        return "********"
    return f"********{text[-4:]}"


def render_config_view(store: "ConfigStore") -> str:
    values = store.resolved_view()
    lines = [
        "# MiniClaw configuration display (display-only; not writable TOML)",
        f"# path = {store.path}",
        f"# exists = {str(store.exists()).lower()}",
        "",
        "[miniclaw]",
    ]
    for field in CONFIG_FIELDS:
        value = values.get(field)
        if field == "api_key":
            rendered = redact_secret(value)
        elif value is None:
            rendered = "not configured"
        else:
            rendered = value
        lines.append(f"{field} = {encode_scalar(rendered)}")
    return "\n".join(lines) + "\n"


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        selected = path or default_config_path()
        expanded = selected.expanduser()
        self._path = Path(os.path.abspath(expanded))
        if self._path == Path(self._path.anchor):
            raise ConfigPathError(f"config path must be a regular file: {self._path}")
        self._uses_default_path = path is None
        self._transaction_lock = threading.RLock()
        self._transaction_descriptor: int | None = None
        self._transaction_depth = 0
        self._transaction_owner: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_secure_primitives(self) -> None:
        if not _SECURE_DIR_FD_SUPPORTED or os.name != "posix":
            raise ConfigPathError(
                f"secure config path operations are unsupported on {os.name}"
            )

    def _component_error(
        self,
        parent_descriptor: int,
        component: str,
        error: OSError,
    ) -> ConfigPathError:
        try:
            status = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            status = None
        if status is not None and stat.S_ISLNK(status.st_mode):
            return ConfigPathError(
                f"config path must not contain a symbolic link: {self.path}"
            )
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            return ConfigPathError(
                f"config path parent must be a directory: {self.path.parent}"
            )
        return ConfigPathError(f"unsafe config path: {self.path}")

    def _verify_parent_binding(self, descriptor: int) -> None:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ConfigPathError(
                f"config parent must be a directory: {self.path.parent}"
            )
        try:
            current = os.stat(self.path.parent, follow_symlinks=False)
        except FileNotFoundError as error:
            raise ConfigPathError(
                f"config parent changed while opening: {self.path.parent}"
            ) from error
        if (
            stat.S_ISLNK(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise ConfigPathError(
                f"config parent changed while opening: {self.path.parent}"
            )

    def _verify_parent_path(self, descriptor: int) -> None:
        current_descriptor = self._open_parent(create=False)
        if current_descriptor is None:
            raise ConfigPathError(
                f"config parent changed while opening: {self.path.parent}"
            )
        try:
            expected = os.fstat(descriptor)
            current = os.fstat(current_descriptor)
            if expected.st_dev != current.st_dev or expected.st_ino != current.st_ino:
                raise ConfigPathError(
                    f"config parent changed while opening: {self.path.parent}"
                )
        finally:
            os.close(current_descriptor)

    def _open_parent(self, *, create: bool) -> int | None:
        self._ensure_secure_primitives()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.path.anchor, flags)
        components = self.path.parent.parts[1:]
        try:
            for component in components:
                created = False
                try:
                    child = os.open(
                        component,
                        flags,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        os.close(descriptor)
                        return None
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                        created = True
                    except FileExistsError:
                        pass
                    try:
                        child = os.open(
                            component,
                            flags,
                            dir_fd=descriptor,
                        )
                    except OSError as error:
                        raise self._component_error(
                            descriptor,
                            component,
                            error,
                        ) from error
                except OSError as error:
                    raise self._component_error(
                        descriptor,
                        component,
                        error,
                    ) from error
                os.close(descriptor)
                descriptor = child
                if created:
                    os.fchmod(descriptor, 0o700)
            if self._uses_default_path:
                os.fchmod(descriptor, 0o700)
            self._verify_parent_binding(descriptor)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def _parent_descriptor(self, *, create: bool) -> Iterator[int | None]:
        with self._transaction_lock:
            if self._transaction_descriptor is not None:
                if self._transaction_owner != threading.get_ident():
                    raise RuntimeError("config transaction ownership is inconsistent")
                self._verify_parent_binding(self._transaction_descriptor)
                try:
                    yield self._transaction_descriptor
                except BaseException:
                    raise
                else:
                    self._verify_parent_path(self._transaction_descriptor)
                return
            descriptor = self._open_parent(create=create)
            try:
                yield descriptor
            except BaseException:
                raise
            else:
                if descriptor is not None:
                    self._verify_parent_path(descriptor)
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def _verify_config_descriptor(
        self,
        parent_descriptor: int,
        descriptor: int,
    ) -> os.stat_result:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ConfigPathError(f"config path must be a regular file: {self.path}")
        if stat.S_IMODE(opened.st_mode) & 0o077:
            raise ConfigPathError(f"config file must be owner-only: {self.path}")
        try:
            current = os.stat(
                self.path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise ConfigPathError(
                f"config path changed while opening: {self.path}"
            ) from error
        if stat.S_ISLNK(current.st_mode):
            raise ConfigPathError(
                f"config path must not be a symbolic link: {self.path}"
            )
        if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
            raise ConfigPathError(f"config path changed while opening: {self.path}")
        return opened

    def _open_config(
        self,
        parent_descriptor: int,
    ) -> tuple[int, os.stat_result] | None:
        try:
            descriptor = os.open(
                self.path.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ConfigPathError(
                    f"config path must not be a symbolic link: {self.path}"
                ) from error
            if error.errno == errno.EISDIR:
                raise ConfigPathError(
                    f"config path must be a regular file: {self.path}"
                ) from error
            raise
        try:
            return descriptor, self._verify_config_descriptor(
                parent_descriptor,
                descriptor,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def exists(self) -> bool:
        with self._parent_descriptor(create=False) as parent_descriptor:
            if parent_descriptor is None:
                return False
            opened = self._open_config(parent_descriptor)
            if opened is None:
                return False
            descriptor, _ = opened
            os.close(descriptor)
            return True

    def _read_text(self) -> str | None:
        with self._parent_descriptor(create=False) as parent_descriptor:
            if parent_descriptor is None:
                return None
            result = self._open_config(parent_descriptor)
            if result is None:
                return None
            descriptor, opened = result
            try:
                with os.fdopen(descriptor, "r", encoding="utf-8") as file:
                    descriptor = -1
                    source = file.read()
                current = os.stat(
                    self.path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    stat.S_ISLNK(current.st_mode)
                    or current.st_dev != opened.st_dev
                    or current.st_ino != opened.st_ino
                ):
                    raise ConfigPathError(
                        f"config path changed while reading: {self.path}"
                    )
                return source
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    def read(self) -> ConfigDocument:
        try:
            source = self._read_text()
        except IsADirectoryError as error:
            raise ConfigPathError(
                f"config path must be a regular file: {self.path}"
            ) from error
        if source is None:
            return ConfigDocument({})
        return decode_config(source, self.path)

    def resolved_view(self) -> Mapping[str, object]:
        document = self.read()
        values = default_config_values()
        values.update(document.values)
        return MappingProxyType(values)

    @contextmanager
    def _transaction(self) -> Iterator[int]:
        import fcntl

        with self._transaction_lock:
            owner = threading.get_ident()
            if self._transaction_descriptor is not None:
                if self._transaction_owner != owner:
                    raise RuntimeError("config transaction ownership is inconsistent")
                self._transaction_depth += 1
                try:
                    yield self._transaction_descriptor
                finally:
                    self._transaction_depth -= 1
                return
            descriptor = self._open_parent(create=True)
            if descriptor is None:
                raise ConfigPathError(
                    f"config parent is unavailable: {self.path.parent}"
                )
            locked = False
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
                self._verify_parent_binding(descriptor)
                self._transaction_descriptor = descriptor
                self._transaction_depth = 1
                self._transaction_owner = owner
                try:
                    yield descriptor
                except BaseException:
                    raise
                else:
                    self._verify_parent_path(descriptor)
            finally:
                self._transaction_descriptor = None
                self._transaction_depth = 0
                self._transaction_owner = None
                try:
                    if locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

    @contextmanager
    def coordinate_first_run(self) -> Iterator[bool]:
        """Serialize first-run checks with kernel-owned cross-process locks.

        POSIX locks the verified parent directory inode and creates no lock
        artifact. Other platforms fail explicitly rather than silently use
        pathname-only coordination with stale-file semantics.
        """
        if os.name != "posix":
            raise ConfigPersistenceError(
                f"first-run coordination is unsupported on {os.name}"
            )
        with self._transaction():
            yield not self.exists()

    def _create_temporary(self, parent_descriptor: int) -> tuple[int, str]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        for _ in range(10):
            name = f".{self.path.name}.miniclaw.{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(
                    name,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            return descriptor, name
        raise ConfigPersistenceError(f"failed to create temporary config: {self.path}")

    def _write_locked(
        self,
        values: Mapping[str, object],
        parent_descriptor: int,
    ) -> None:
        candidate = dict(values)
        self._validate_candidate(candidate)
        existing = self._open_config(parent_descriptor)
        if existing is not None:
            existing_descriptor, _ = existing
            os.close(existing_descriptor)
        descriptor, temporary_name = self._create_temporary(parent_descriptor)
        try:
            temporary_status = os.fstat(descriptor)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                descriptor = -1
                os.fchmod(file.fileno(), 0o600)
                file.write(encode_config(candidate))
                file.flush()
                os.fsync(file.fileno())
                os.replace(
                    temporary_name,
                    self.path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            persisted = os.stat(
                self.path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                stat.S_ISLNK(persisted.st_mode)
                or persisted.st_dev != temporary_status.st_dev
                or persisted.st_ino != temporary_status.st_ino
                or stat.S_IMODE(persisted.st_mode) != 0o600
            ):
                raise ConfigPersistenceError(f"failed to persist config: {self.path}")
            self._verify_parent_binding(parent_descriptor)
            os.fsync(parent_descriptor)
        except OSError as error:
            raise ConfigPersistenceError(
                f"failed to persist config: {self.path}"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_descriptor)

    def _validate_candidate(self, candidate: Mapping[str, object]) -> None:
        merged = default_config_values()
        merged.update(
            {
                field: value
                for field, value in candidate.items()
                if field in RUNTIME_FIELDS
            }
        )
        validate_config_values(merged)
        ConfigDocument(candidate)

    def set(self, field: str, value: object) -> None:
        converted = coerce_config_value(field, value)
        if not self.exists():
            self._validate_candidate({field: converted})
        with self._transaction() as parent_descriptor:
            document = self.read()
            candidate = dict(document.values)
            candidate[field] = converted
            self._validate_candidate(candidate)
            self._write_locked(candidate, parent_descriptor)

    def unset(self, field: str) -> None:
        if field not in CONFIG_FIELDS:
            raise ValueError(f"unknown config field: {field}")
        if not self.exists():
            self._validate_candidate({})
        with self._transaction() as parent_descriptor:
            document = self.read()
            candidate = dict(document.values)
            candidate.pop(field, None)
            self._validate_candidate(candidate)
            self._write_locked(candidate, parent_descriptor)

    def initialize(
        self,
        *,
        base_url: str | None,
        model: str | None,
        api_key: str | None,
    ) -> None:
        provided = {
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
        }
        converted = {
            field: coerce_config_value(field, value)
            for field, value in provided.items()
            if value is not None
        }
        if not self.exists():
            missing = set(provided) - set(converted)
            if missing:
                raise ValueError(
                    "base_url, model, and api_key are required for initialization"
                )
            initial = {**converted, "workspace": Path(".")}
            self._validate_candidate(initial)
        with self._transaction() as parent_descriptor:
            document = self.read()
            candidate = dict(document.values)
            candidate.update(converted)
            candidate.setdefault("workspace", Path("."))
            for field in provided:
                if candidate.get(field) is None:
                    raise ValueError(f"{field} is required for initialization")
            self._validate_candidate(candidate)
            self._write_locked(candidate, parent_descriptor)
