import json
from datetime import UTC, datetime

from miniclaw.memory.store import SqliteMemoryStore
from miniclaw.tools.types import (
    RegisteredTool,
    ToolCapabilities,
    ToolContext,
    ToolSpec,
)
from miniclaw.workspace.paths import resolve_workspace_path


def make_memory_tools(store: SqliteMemoryStore) -> tuple[RegisteredTool, ...]:
    async def remember(arguments, context: ToolContext) -> str:
        reference = str(arguments['source_ref'])
        if reference == 'request' and context.request_text is not None:
            source = context.request_text
        elif reference.startswith('file:'):
            path = resolve_workspace_path(context.workspace, reference[5:])
            with path.open('rb') as file:
                source_bytes = file.read(65537)
            if len(source_bytes) > 65536:
                raise ValueError('memory source exceeds 64 KiB')
            source = source_bytes.decode('utf-8')
        else:
            raise ValueError("source_ref must be request or file:<workspace-relative-path>")
        record = await store.put_with_source(
            memory_id=str(arguments['memory_id']), text=str(arguments['text']),
            source_content=source, tags=tuple(str(tag) for tag in arguments.get('tags', [])),
            created_at=datetime.now(UTC).isoformat(),
        )
        return record.memory_id

    async def search_memory(arguments, context: ToolContext) -> str:
        records = await store.search(str(arguments["query"]))
        return json.dumps(
            [
                {
                    "memory_id": record.memory_id,
                    "text": record.text,
                    "source_ref": record.source_ref,
                }
                for record in records
            ],
            separators=(",", ":"),
        )

    remember_schema = {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string"},
            "text": {"type": "string"},
            "source_ref": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["memory_id", "text", "source_ref"],
        "additionalProperties": False,
    }
    search_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    return (
        RegisteredTool(
            ToolSpec(
                "remember",
                "Persist a memory; source_ref is request or file:<workspace-relative-path>. "
                "The harness verifies and hashes the actual source.",
                remember_schema,
                ToolCapabilities(side_effects=True),
            ),
            remember,
        ),
        RegisteredTool(
            ToolSpec(
                "search_memory",
                "Search sourced long-term memory",
                search_schema,
                ToolCapabilities(),
            ),
            search_memory,
        ),
    )
