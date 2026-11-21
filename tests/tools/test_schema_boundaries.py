import pytest

from miniclaw.core.errors import ToolArgumentsInvalid, ToolSchemaError
from miniclaw.tools.schema import validate_arguments, validate_schema


@pytest.mark.parametrize('schema', [
    {'type': []}, {'type': [{}]}, {'type': 'array', 'minItems': -.5},
    {'type': 'string', 'maxLength': 1.5}, {'properties': {1: {'type': 'string'}}},
])
def test_invalid_schema_values_fail_as_schema_errors(schema):
    with pytest.raises(ToolSchemaError):
        validate_schema(schema)


def test_nested_boolean_is_not_equal_to_json_integer_in_enum():
    schema = {'type': 'object', 'properties': {'value': {'enum': [{'a': [1]}]}}}
    with pytest.raises(ToolArgumentsInvalid):
        validate_arguments(schema, {'value': {'a': [True]}})
    assert validate_arguments(schema, {'value': {'a': [1.0]}})
