import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from miniclaw.config import (
    CONFIG_FIELDS,
    INTEGER_CONFIG_FIELDS,
    RUNTIME_FIELDS,
    ConfigDocument,
    coerce_config_value,
    default_config_values,
    validate_config_values,
)

_TOML_ESCAPES = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    '"': '\\"',
    "\\": "\\\\",
}
_CANONICAL_INTEGER = re.compile(r"[1-9][0-9]*")
_BARE_KEY_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
_BASIC_KEY_ESCAPES = {
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _skip_horizontal_whitespace(source: str, index: int) -> int:
    while index < len(source) and source[index] in " \t":
        index += 1
    return index


def _parse_basic_key(source: str, index: int) -> tuple[str, int] | None:
    value: list[str] = []
    index += 1
    while index < len(source):
        character = source[index]
        if character == '"':
            return "".join(value), index + 1
        if character in "\r\n":
            return None
        if character != "\\":
            value.append(character)
            index += 1
            continue
        index += 1
        if index >= len(source):
            return None
        escape = source[index]
        if escape in _BASIC_KEY_ESCAPES:
            value.append(_BASIC_KEY_ESCAPES[escape])
            index += 1
            continue
        if escape not in {"u", "U"}:
            return None
        width = 4 if escape == "u" else 8
        digits = source[index + 1 : index + 1 + width]
        if len(digits) != width or any(
            character not in "0123456789abcdefABCDEF" for character in digits
        ):
            return None
        codepoint = int(digits, 16)
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            return None
        value.append(chr(codepoint))
        index += width + 1
    return None


def _parse_literal_key(source: str, index: int) -> tuple[str, int] | None:
    end = source.find("'", index + 1)
    if end < 0 or "\n" in source[index + 1 : end]:
        return None
    return source[index + 1 : end], end + 1


def _parse_key_segment(source: str, index: int) -> tuple[str, int] | None:
    if index >= len(source):
        return None
    if source[index] == '"':
        return _parse_basic_key(source, index)
    if source[index] == "'":
        return _parse_literal_key(source, index)
    start = index
    while index < len(source) and source[index] in _BARE_KEY_CHARACTERS:
        index += 1
    if index == start:
        return None
    return source[start:index], index


def _parse_key_path(
    source: str,
    index: int,
    terminator: str,
) -> tuple[tuple[str, ...], int] | None:
    segments: list[str] = []
    while True:
        index = _skip_horizontal_whitespace(source, index)
        segment = _parse_key_segment(source, index)
        if segment is None:
            return None
        value, index = segment
        segments.append(value)
        index = _skip_horizontal_whitespace(source, index)
        if index >= len(source):
            return None
        if source[index] == terminator:
            return tuple(segments), index + 1
        if source[index] != ".":
            return None
        index += 1


def _quote_run_end(source: str, index: int, quote: str) -> int:
    while index < len(source) and source[index] == quote:
        index += 1
    return index


def _skip_to_next_statement(source: str, index: int) -> int:
    nesting = 0
    state: str | None = None
    while index < len(source):
        character = source[index]
        if state is None:
            if character == "#":
                while index < len(source) and source[index] not in "\r\n":
                    index += 1
                if nesting == 0:
                    return index
                continue
            if source.startswith('"""', index):
                state = "multiline-basic"
                index += 3
                continue
            if source.startswith("'''", index):
                state = "multiline-literal"
                index += 3
                continue
            if character == '"':
                state = "basic"
                index += 1
                continue
            if character == "'":
                state = "literal"
                index += 1
                continue
            if character in "[{":
                nesting += 1
            elif character in "]}" and nesting:
                nesting -= 1
            elif character in "\r\n" and nesting == 0:
                return index + 1
            index += 1
            continue
        if state in {"basic", "multiline-basic"} and character == "\\":
            index += 2
            continue
        if state == "basic" and character == '"':
            state = None
            index += 1
            continue
        if state == "literal" and character == "'":
            state = None
            index += 1
            continue
        if state == "multiline-basic" and character == '"':
            end = _quote_run_end(source, index, '"')
            if end - index >= 3:
                state = None
            index = end
            continue
        if state == "multiline-literal" and character == "'":
            end = _quote_run_end(source, index, "'")
            if end - index >= 3:
                state = None
            index = end
            continue
        index += 1
    return index


def _skip_space_and_comments(source: str, index: int) -> int:
    while index < len(source):
        if source[index] in " \t\r\n":
            index += 1
            continue
        if source[index] == "#":
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            continue
        return index
    return index


def _has_canonical_integer_value(source: str, index: int) -> bool:
    index = _skip_horizontal_whitespace(source, index)
    match = _CANONICAL_INTEGER.match(source, index)
    if match is None:
        return False
    index = _skip_horizontal_whitespace(source, match.end())
    return index == len(source) or source[index] == "#" or source[index] in "\r\n"


def _inline_value_end(source: str, index: int) -> int | None:
    nesting = 0
    state: str | None = None
    while index < len(source):
        character = source[index]
        if state is None:
            if source.startswith('"""', index):
                state = "multiline-basic"
                index += 3
                continue
            if source.startswith("'''", index):
                state = "multiline-literal"
                index += 3
                continue
            if character == '"':
                state = "basic"
                index += 1
                continue
            if character == "'":
                state = "literal"
                index += 1
                continue
            if character in "[{":
                nesting += 1
            elif character in "]}":
                if nesting:
                    nesting -= 1
                else:
                    return index
            elif character == "," and nesting == 0:
                return index
            elif character in "#\r\n":
                return None
            index += 1
            continue
        if state in {"basic", "multiline-basic"} and character == "\\":
            index += 2
            continue
        if state == "basic" and character == '"':
            state = None
            index += 1
            continue
        if state == "literal" and character == "'":
            state = None
            index += 1
            continue
        if state == "multiline-basic" and character == '"':
            end = _quote_run_end(source, index, '"')
            if end - index >= 3:
                state = None
            index = end
            continue
        if state == "multiline-literal" and character == "'":
            end = _quote_run_end(source, index, "'")
            if end - index >= 3:
                state = None
            index = end
            continue
        index += 1
    return None


def _validate_inline_table_integers(
    source: str,
    index: int,
    validated: set[str],
) -> None:
    index = _skip_horizontal_whitespace(source, index)
    if index >= len(source) or source[index] != "{":
        return
    index += 1
    while True:
        index = _skip_horizontal_whitespace(source, index)
        if index >= len(source) or source[index] == "}":
            return
        assignment = _parse_key_path(source, index, "=")
        if assignment is None:
            return
        key_path, value_index = assignment
        value_end = _inline_value_end(source, value_index)
        if value_end is None:
            return
        if len(key_path) == 1 and key_path[0] in INTEGER_CONFIG_FIELDS:
            field = key_path[0]
            lexeme = source[value_index:value_end].strip(" \t")
            if _CANONICAL_INTEGER.fullmatch(lexeme) is None:
                raise ValueError(f"{field} must be a positive integer")
            validated.add(field)
        if source[value_end] == "}":
            return
        index = value_end + 1


def _validate_integer_lexemes(source: str) -> frozenset[str]:
    table_path: tuple[str, ...] = ()
    validated: set[str] = set()
    index = 0
    while True:
        index = _skip_space_and_comments(source, index)
        if index >= len(source):
            break
        if source.startswith("[[", index):
            table_path = ()
            index = _skip_to_next_statement(source, index)
            continue
        if source[index] == "[":
            table = _parse_key_path(source, index + 1, "]")
            if table is not None:
                table_path, index = table
            index = _skip_to_next_statement(source, index)
            continue
        assignment = _parse_key_path(source, index, "=")
        if assignment is None:
            index = _skip_to_next_statement(source, index)
            continue
        key_path, value_index = assignment
        semantic_path = (*table_path, *key_path)
        if semantic_path == ("miniclaw",):
            _validate_inline_table_integers(source, value_index, validated)
        if (
            len(semantic_path) == 2
            and semantic_path[0] == "miniclaw"
            and semantic_path[1] in INTEGER_CONFIG_FIELDS
        ):
            field = semantic_path[1]
            if not _has_canonical_integer_value(source, value_index):
                raise ValueError(f"{field} must be a positive integer")
            validated.add(field)
        index = _skip_to_next_statement(source, value_index)
    return frozenset(validated)


def _toml_basic_string(value: str) -> str:
    escaped: list[str] = ['"']
    for character in value:
        codepoint = ord(character)
        if character in _TOML_ESCAPES:
            escaped.append(_TOML_ESCAPES[character])
        elif codepoint < 0x20 or codepoint == 0x7F:
            escaped.append(f"\\u{codepoint:04X}")
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("config strings must contain only Unicode scalar values")
        else:
            escaped.append(character)
    escaped.append('"')
    return "".join(escaped)


def encode_scalar(value: object) -> str:
    if isinstance(value, Path):
        return _toml_basic_string(str(value))
    if isinstance(value, str):
        return _toml_basic_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    raise TypeError(f"unsupported config value type: {value.__class__.__name__}")


def encode_config(values: Mapping[str, object]) -> str:
    lines = ["[miniclaw]"]
    for field in CONFIG_FIELDS:
        if field in values and values[field] is not None:
            lines.append(f"{field} = {encode_scalar(values[field])}")
    return "\n".join(lines) + "\n"


def decode_config(source: str, path: Path) -> ConfigDocument:
    try:
        payload = tomllib.loads(source)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"invalid config file: {path}") from error
    if set(payload) - {"miniclaw"}:
        raise ValueError(
            f"unknown root config fields: {sorted(set(payload) - {'miniclaw'})}"
        )
    section = payload.get("miniclaw", {})
    if not isinstance(section, dict):
        raise ValueError("[miniclaw] must be a TOML table")
    unknown = set(section) - set(CONFIG_FIELDS)
    if unknown:
        raise ValueError(f"unknown config fields: {sorted(unknown)}")
    validated_integer_fields = _validate_integer_lexemes(source)
    for field in INTEGER_CONFIG_FIELDS:
        if field in section and field not in validated_integer_fields:
            raise ValueError(f"{field} must be a positive integer")
    converted = {
        field: coerce_config_value(field, value) for field, value in section.items()
    }
    merged = default_config_values()
    merged.update(
        {
            field: value
            for field, value in converted.items()
            if field in RUNTIME_FIELDS
        }
    )
    validate_config_values(merged)
    return ConfigDocument(converted)
