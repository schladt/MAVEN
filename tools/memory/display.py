"""Lossless bounded-line JSON display for agent tools that truncate giant lines."""
from __future__ import annotations

import json


def agent_display(value) -> str:
    """Emit valid JSON with every string escaped token on its own bounded line.

    JSON string tokens cannot be split over lines. Represent strings as explicit
    UTF-8 text chunks in a typed tree instead: concatenation restores exact text,
    including trailing newlines. Keys use the same encoding; no data is omitted.
    """
    def encode(item):
        if isinstance(item, str):
            return {'type': 'string', 'chunks': [item[i:i + 120] for i in range(0, len(item), 120)]}
        if isinstance(item, dict):
            return {'type': 'object', 'entries': [[encode(key), encode(val)] for key, val in item.items()]}
        if isinstance(item, list):
            return {'type': 'array', 'items': [encode(val) for val in item]}
        return {'type': 'scalar', 'value': item}
    return json.dumps({'format': 'maven-lossless-tree-v1', 'value': encode(value)},
                      ensure_ascii=False, indent=2, allow_nan=False)
