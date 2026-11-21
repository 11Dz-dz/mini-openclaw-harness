import re

from miniclaw.core.errors import ToolNotFound
from miniclaw.tools.schema import validate_schema
from miniclaw.tools.types import RegisteredTool


class ToolRegistry:
    def __init__(self, *, execution_recorder=None) -> None:
        self.execution_recorder = execution_recorder
        self._tools: dict[str, RegisteredTool] = {}
        self._disabled: dict[str, str] = {}

    def register(self, tool: RegisteredTool) -> None:
        validate_schema(tool.spec.parameters)
        name = tool.spec.name
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ValueError(f"invalid tool name: {name}")
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(f"tool not found: {name}") from exc

    def values(self) -> tuple[RegisteredTool, ...]:
        return tuple(self._tools.values())

    def disable(self, name: str, reason: str) -> None:
        """Hide a tool from the model while safely handling stale calls.

        Keep the registered name so saved histories and child allowlists can
        still resolve it. ToolExecutor enforces the refusal before dispatch.
        """
        self.get(name)
        if not reason.strip():
            raise ValueError("disabled tool requires a reason")
        self._disabled[name] = reason

    def disabled_reason(self, name: str) -> str | None:
        return self._disabled.get(name)

    def subset(self, names: tuple[str, ...]) -> "ToolRegistry":
        registry = ToolRegistry(execution_recorder=self.execution_recorder)
        for name in dict.fromkeys(names):
            registry.register(self.get(name))
            if (reason := self.disabled_reason(name)) is not None:
                registry.disable(name, reason)
        return registry

    def specs(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "type": "function",
                "function": {
                    "name": tool.spec.name,
                    "description": tool.spec.description,
                    "parameters": tool.spec.parameters,
                },
            }
            for tool in self._tools.values()
            if tool.spec.name not in self._disabled
        )
