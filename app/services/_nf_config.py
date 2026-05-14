"""Lightweight Nextflow (Groovy DSL) configuration builder.

Converts Python data structures into Nextflow config DSL text, replacing
raw f-strings with typed, structured config assembly.
"""

from __future__ import annotations

from typing import Any


class Raw(str):
    """Raw Groovy/Nextflow DSL expression — emitted verbatim, no quoting applied.

    Use for closures, method calls, size/time literals, and any Groovy expression
    that cannot be represented by a plain Python value.

    Examples::

        Raw("{ task.memory < 128.GB ? 'normalbw' : 'normal' }")
        Raw("System.getenv(\\"PROJECT\\")")
        Raw("256.GB")
    """


# Shared trace footer used by Gadi Nextflow configs.
# Uses a Groovy string interpolation for the timestamp variable.
GADI_TRACE_SECTION = (
    "def trace_timestamp = new java.util.Date().format('yyyy-MM-dd_HH-mm-ss')\n"
    "trace {\n"
    "    enabled = true\n"
    "    overwrite = false\n"
    '    file = "./gadi-nf-core-trace-${trace_timestamp}.txt"\n'
    "    fields = 'name,status,exit,duration,realtime,cpus,%cpu,memory,%mem,rss'\n"
    "}"
)


def _serialize(v: Any, depth: int = 1) -> str:
    """Serialize a Python value to its Nextflow DSL string representation."""
    if isinstance(v, Raw):
        return str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return f"'{v}'" if "'" not in v else f'"{v}"'
    if isinstance(v, list):
        return "[" + ", ".join(_serialize(item) for item in v) + "]"
    if isinstance(v, dict):
        ind = "    " * (depth + 1)
        closing = "    " * depth
        pairs = [f'{ind}"{k}": {_serialize(val)}' for k, val in v.items()]
        return "[\n" + ",\n".join(pairs) + f"\n{closing}]"
    raise TypeError(f"Cannot serialize value of type {type(v).__name__}: {v!r}")


def _block(name: str, entries: dict[str, Any], depth: int = 0) -> str:
    """Render a named Nextflow DSL block, including any withName/withLabel sub-blocks."""
    pad = "    " * depth
    inner = "    " * (depth + 1)
    lines: list[str] = [f"{pad}{name} {{"]
    for key, val in entries.items():
        if isinstance(val, dict) and (key.startswith("withName:") or key.startswith("withLabel:")):
            lines.append(_block(key, val, depth + 1))
        else:
            lines.append(f"{inner}{key} = {_serialize(val, depth + 1)}")
    lines.append(f"{pad}}}")
    return "\n".join(lines)


def build_nf_config(*sections: str | tuple[str, dict[str, Any]]) -> str:
    """Assemble a complete Nextflow config string from blocks and raw sections.

    Each positional argument is either:
    - A ``(name, dict)`` tuple describing a named DSL block.
    - A plain ``str`` for a comment line, a raw Groovy statement, or a
      pre-formatted section (e.g. :data:`GADI_TRACE_SECTION`).

    Sections are joined with a single blank line between them.
    """
    parts: list[str] = []
    for section in sections:
        if isinstance(section, str):
            parts.append(section)
        else:
            name, entries = section
            parts.append(_block(name, entries))
    return "\n\n".join(parts)
