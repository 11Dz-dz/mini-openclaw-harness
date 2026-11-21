import pytest

from miniclaw.core.errors import ToolArgumentsInvalid, ToolSchemaError
from miniclaw.tools.schema import validate_arguments, validate_schema

SCHEMA = {'type': 'object', 'properties': {'tasks': {'type': 'array', 'items': {
    'type': 'object', 'required': ['name'], 'properties': {'name': {'type': 'string'}},
    'additionalProperties': False}}}, 'additionalProperties': False}


@pytest.mark.parametrize('arguments', [
    {'tasks': [3]}, {'tasks': [{}]}, {'tasks': [{'name': 4}]},
    {'tasks': [{'name': 'ok', 'unexpected': True}]}, {'tasks': ['not an object']},
])
def test_nested_arrays_and_objects_are_validated(arguments):
    with pytest.raises(ToolArgumentsInvalid):
        validate_arguments(SCHEMA, arguments)


def test_open_and_typed_additional_properties_follow_schema_semantics():
    assert validate_arguments({'type': 'object'}, {'extra': {'value': 1}})
    assert validate_arguments({'type': 'object', 'additionalProperties': {'type': 'integer'}}, {'a': 1})
    with pytest.raises(ToolArgumentsInvalid):
        validate_arguments({'type': 'object', 'additionalProperties': {'type': 'integer'}}, {'a': True})


def test_unsupported_schema_is_rejected_instead_of_silently_ignored():
    with pytest.raises(ToolSchemaError, match='unsupported'):
        validate_schema({'type': 'string', 'pattern': '.*'})
