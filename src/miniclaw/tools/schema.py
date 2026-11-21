import math
from inspect import Parameter, signature
from types import UnionType
from typing import Literal, Union, get_args, get_origin, get_type_hints

from miniclaw.core.errors import ToolArgumentsInvalid, ToolSchemaError


def _schema_for_annotation(
    annotation: object,
    parameter_name: str,
) -> dict[str, object]:
    scalar_types: dict[object, str] = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
    }
    if annotation in scalar_types:
        return {"type": scalar_types[annotation]}
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is list and len(arguments) == 1 and arguments[0] in scalar_types:
        return {
            "type": "array",
            "items": {"type": scalar_types[arguments[0]]},
        }
    if origin is Literal and arguments:
        return {"enum": list(arguments)}
    if origin in {Union, UnionType} and type(None) in arguments:
        non_none = [item for item in arguments if item is not type(None)]
        if len(non_none) == 1:
            return _schema_for_annotation(non_none[0], parameter_name)
    raise ToolSchemaError(
        f"unsupported annotation for {parameter_name}: {annotation!r}"
    )


def schema_for_callable(function) -> dict[str, object]:
    parameters = signature(function).parameters
    hints = get_type_hints(function)
    properties: dict[str, object] = {}
    required: list[str] = []
    for name, parameter in parameters.items():
        if parameter.kind not in {
            Parameter.POSITIONAL_ONLY,
            Parameter.POSITIONAL_OR_KEYWORD,
            Parameter.KEYWORD_ONLY,
        }:
            raise ToolSchemaError(f"unsupported parameter kind for {name}")
        annotation = hints.get(name, parameter.annotation)
        if annotation is Parameter.empty:
            raise ToolSchemaError(f"missing annotation for {name}")
        property_schema = _schema_for_annotation(annotation, name)
        if parameter.default is Parameter.empty:
            required.append(name)
        else:
            property_schema["default"] = parameter.default
        properties[name] = property_schema
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_TYPES = {'object', 'array', 'string', 'number', 'integer', 'boolean', 'null'}
_KEYWORDS = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum',
             'anyOf', 'allOf', 'oneOf', 'not', 'minimum', 'maximum', 'minLength',
             'maxLength', 'minItems', 'maxItems', 'description', 'title', 'default',
             '$schema', 'examples', 'deprecated', 'readOnly', 'writeOnly'}


def validate_schema(schema, *, _depth=0, _budget=None):
    budget = [2000] if _budget is None else _budget
    budget[0] -= 1
    if budget[0] < 0 or _depth > 32:
        raise ToolSchemaError('schema exceeds complexity limit')
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict) or set(schema) - _KEYWORDS:
        raise ToolSchemaError('unsupported schema keyword')
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    if (not isinstance(types, list) or ('type' in schema and not types)
            or any(not isinstance(t, str) or t not in _TYPES for t in types)):
        raise ToolSchemaError('unsupported schema type')
    required = schema.get('required', [])
    properties = schema.get('properties', {})
    if (not isinstance(required, list) or not all(isinstance(v, str) for v in required)
            or not isinstance(properties, dict)
            or not all(isinstance(name, str) for name in properties)):
        raise ToolSchemaError('invalid object schema')
    children = list(properties.values())
    for key in ('items', 'additionalProperties', 'not'):
        if key in schema:
            children.append(schema[key])
    for key in ('anyOf', 'allOf', 'oneOf'):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise ToolSchemaError('invalid schema alternatives')
            children.extend(schema[key])
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']):
        raise ToolSchemaError('invalid enum')
    for key in ('minimum', 'maximum', 'minLength', 'maxLength', 'minItems', 'maxItems'):
        if key in schema and (isinstance(schema[key], bool)
                or not isinstance(schema[key], (int, float)) or not math.isfinite(schema[key])):
            raise ToolSchemaError('invalid schema bound')
        if key in schema and key not in ('minimum', 'maximum') and (
                type(schema[key]) is not int or schema[key] < 0):
            raise ToolSchemaError('length and item bounds must be nonnegative integers')
    if 'enum' in schema:
        for option in schema['enum']:
            try:
                _json_value(option)
            except ToolArgumentsInvalid as error:
                raise ToolSchemaError('enum must contain JSON values') from error
    for child in children:
        validate_schema(child, _depth=_depth + 1, _budget=budget)


def _json_value(value, depth=0):
    if depth > 32:
        raise ToolArgumentsInvalid('arguments exceed nesting limit')
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ToolArgumentsInvalid('object keys must be strings')
        for child in value.values():
            _json_value(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _json_value(child, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ToolArgumentsInvalid('nonfinite number')
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ToolArgumentsInvalid('arguments must contain JSON values')


def _validate(schema, value, path='$'):
    def fail(reason):
        raise ToolArgumentsInvalid(f'{path}: {reason}')

    if schema is True:
        return
    if schema is False:
        fail('value is forbidden')
    checks = {
        'object': isinstance(value, dict), 'array': isinstance(value, list),
        'string': isinstance(value, str), 'boolean': type(value) is bool,
        'number': type(value) in (int, float),
        'integer': type(value) is int or (type(value) is float and value.is_integer()),
        'null': value is None,
    }
    types = schema.get('type')
    if types is not None:
        names = [types] if isinstance(types, str) else types
        if not any(checks[name] for name in names):
            fail('invalid type')
    if 'enum' in schema and not any(_json_equal(value, option) for option in schema['enum']):
        fail('value is outside enum')
    for kind in ('anyOf', 'allOf', 'oneOf'):
        if kind in schema:
            matches = 0
            for alternative in schema[kind]:
                try:
                    _validate(alternative, value, path)
                    matches += 1
                except ToolArgumentsInvalid:
                    pass
            if ((kind == 'anyOf' and not matches)
                    or (kind == 'allOf' and matches != len(schema[kind]))
                    or (kind == 'oneOf' and matches != 1)):
                fail(f'{kind} constraint failed')
    if 'not' in schema:
        try:
            _validate(schema['not'], value, path)
        except ToolArgumentsInvalid:
            pass
        else:
            fail('not constraint failed')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if any(key not in value for key in schema.get('required', [])):
            fail('missing required property')
        additional = schema.get('additionalProperties', True)
        for key, child in value.items():
            _validate(properties.get(key, additional), child, f'{path}.{key}')
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate(schema.get('items', True), child, f'{path}[{index}]')
    for low, high, actual in (
        ('minimum', 'maximum', value if type(value) in (int, float) else None),
        ('minLength', 'maxLength', len(value) if isinstance(value, str) else None),
        ('minItems', 'maxItems', len(value) if isinstance(value, list) else None),
    ):
        if actual is not None and ((low in schema and actual < schema[low])
                                  or (high in schema and actual > schema[high])):
            fail('value exceeds bounds')


def validate_arguments(schema: dict[str, object], arguments: dict[str, object]) -> dict[str, object]:
    validate_schema(schema)
    if not isinstance(arguments, dict):
        raise ToolArgumentsInvalid('tool arguments must be an object')
    _json_value(arguments)
    _validate(schema, arguments)
    return dict(arguments)


def _json_equal(first, second):
    if isinstance(first, bool) or isinstance(second, bool):
        return type(first) is type(second) and first == second
    if isinstance(first, dict) and isinstance(second, dict):
        return first.keys() == second.keys() and all(_json_equal(first[k], second[k]) for k in first)
    if isinstance(first, list) and isinstance(second, list):
        return len(first) == len(second) and all(_json_equal(a, b) for a, b in zip(first, second, strict=True))
    return first == second
