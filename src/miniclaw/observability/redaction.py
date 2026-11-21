import re
from collections.abc import Mapping


class Redactor:
    _sensitive_keys = {
        "authorization",
        "api_key",
        "token",
        "secret",
        "password",
        "cookie",
        "set-cookie",
    }

    def __init__(self, secrets: tuple[str, ...] = (), *, truncate: bool = True) -> None:
        self.secrets = tuple(secret for secret in secrets if secret)
        self.truncate = truncate

    def redact(self, value: object, *, _depth: int = 0) -> object:
        if self.truncate and _depth >= 12:
            return "<max-depth>"
        if isinstance(value, bytes):
            return f"<bytes:{len(value)}>"
        if isinstance(value, str):
            result = value
            for secret in self.secrets:
                result = result.replace(secret, "[REDACTED]")
            result = re.sub(
                r'(?i)((?:password|passwd|secret|token|api[_-]?key|authorization)'
                r'\s*[=:]\s*)(?:Bearer\s+)?[^\s,;&]+', r'\1[REDACTED]', result)
            if self.truncate and len(result) > 16 * 1024:
                return result[: 16 * 1024] + f"<truncated:{len(result)}>"
            return result
        if isinstance(value, Mapping):
            return {
                str(key): (
                    "[REDACTED]"
                    if str(key).casefold() in self._sensitive_keys
                    else self.redact(item, _depth=_depth + 1)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                self.redact(item, _depth=_depth + 1)
                for item in value
            ]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return value.__class__.__name__


def audit_payload(payload_type: str, payload: dict, redactor: Redactor) -> dict:
    """Audit streams store metadata; recovery history lives in checkpoints.

    Omitting deltas also prevents credentials split across chunks (including
    previously unknown credentials) from being reconstructed from the log.
    """
    omitted = {
        'RunStarted': ('prompt',), 'RunCompleted': ('output',),
        'TextDelta': ('text',), 'ToolCallDelta': ('arguments_fragment',),
    }
    safe = dict(payload)
    for key in omitted.get(payload_type, ()):
        if key in safe:
            safe[key] = '<omitted>'
    return redactor.redact(safe)
