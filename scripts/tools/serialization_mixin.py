import dataclasses
from datetime import datetime
from typing import Any, Dict


class JsonSerializable:
    """
    Mixin for dataclasses to automatically serialize attributes to a JSON-compatible dictionary.
    Handles nested dataclasses and converts standard formats like datetime to ISO strings.
    """

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for field in dataclasses.fields(self):  # type: ignore[arg-type]
            val = getattr(self, field.name)
            if isinstance(val, datetime):
                result[field.name] = val.isoformat()
            elif hasattr(val, "to_dict") and callable(val.to_dict):
                result[field.name] = val.to_dict()
            elif dataclasses.is_dataclass(val):
                result[field.name] = dataclasses.asdict(val)  # type: ignore[arg-type]
            else:
                result[field.name] = val
        return result
