def validate(schema, data, path=''):
    """
    Lightweight, zero-dependency JSON Schema Validator in Python.
    Handles type checking (integer, number, boolean, string, array, object),
    enum checks, minimum/maximum boundaries, required fields, and nested properties.
    """
    if not schema:
        return []
    errors = []

    if data is None:
        return errors

    # Check type
    schema_type = schema.get('type')
    if schema_type:
        if schema_type == 'integer':
            # In Python, isinstance(True, int) is True, so we must exclude bools
            if not isinstance(data, int) or isinstance(data, bool):
                errors.append(f"Path '{path}' must be an integer")
        elif schema_type == 'number':
            if not isinstance(data, (int, float)) or isinstance(data, bool):
                errors.append(f"Path '{path}' must be a number")
        elif schema_type == 'boolean':
            if not isinstance(data, bool):
                errors.append(f"Path '{path}' must be a boolean")
        elif schema_type == 'string':
            if not isinstance(data, str):
                errors.append(f"Path '{path}' must be a string")
        elif schema_type == 'array':
            if not isinstance(data, list):
                errors.append(f"Path '{path}' must be an array")
        elif schema_type == 'object':
            if not isinstance(data, dict):
                errors.append(f"Path '{path}' must be an object")

    # Check enum
    schema_enum = schema.get('enum')
    if schema_enum:
        if data not in schema_enum:
            errors.append(f"Path '{path}' must be one of enum values {schema_enum}, got '{data}'")

    # Check minimum / maximum
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        minimum = schema.get('minimum')
        if minimum is not None and data < minimum:
            errors.append(f"Path '{path}' must be greater than or equal to {minimum}")
        maximum = schema.get('maximum')
        if maximum is not None and data > maximum:
            errors.append(f"Path '{path}' must be less than or equal to {maximum}")

    # Check required and properties
    if schema_type == 'object' and isinstance(data, dict):
        required = schema.get('required', [])
        for req in required:
            if req not in data or data[req] is None:
                errors.append(f"Path '{path + '.' + req if path else req}' is required")

        properties = schema.get('properties', {})
        for key, value in data.items():
            if key in properties:
                next_path = f"{path}.{key}" if path else key
                errors.extend(validate(properties[key], value, next_path))

    return errors
