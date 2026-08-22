"""Explicitly clearing nullable fields on `update_*` tools.

The Plane SDK serialises every update payload with `model_dump(exclude_none=True)`
(plane-sdk 0.2.16, e.g. `plane/api/projects.py:52`), so a `None` argument is
indistinguishable from an omitted one — the key never reaches the wire and the
PATCH returns 200 having changed nothing. That makes clearing a nullable field
impossible through the SDK, and worse, makes the failure SILENT: the tool reports
success and the caller believes the field was nulled.

`update_*` tools therefore take an explicit `clear=[...]` list. `build_clear_payload`
merges the caller's set fields with a real `None` for each named field, producing a
payload to send with a raw PATCH that bypasses the SDK's serialisation.

See The1Studio/plane-mcp-server#21.
"""

from typing import Any

from pydantic import BaseModel


def build_clear_payload(data: BaseModel, clear: list[str], tool: str) -> dict[str, Any]:
    """Merge `data`'s set fields with an explicit ``None`` for each name in `clear`.

    Args:
        data: The populated Update* model the tool would otherwise hand to the SDK.
        clear: Field names to null out. Must be non-empty; callers skip this
            function entirely when the caller asked for no clearing.
        tool: Tool name, used only to make the error messages self-locating.

    Returns:
        A JSON-ready payload carrying both the caller's values and the explicit nulls.

    Raises:
        ValueError: on a name the model does not carry (a typo must fail loudly
            rather than silently clear nothing), or on a field that is both
            assigned a value and listed for clearing (contradictory intent —
            resolving it either way would guess at what the caller meant).
    """
    known = set(type(data).model_fields)

    unknown = sorted({f for f in clear if f not in known})
    if unknown:
        raise ValueError(
            f"{tool}: cannot clear unknown field(s) {unknown}. "
            f"Clearable fields are: {sorted(known)}"
        )

    payload = data.model_dump(mode="json", exclude_none=True)

    conflicting = sorted({f for f in clear if f in payload})
    if conflicting:
        raise ValueError(
            f"{tool}: field(s) {conflicting} were given a value and also listed in "
            "`clear`. Pass a value to set the field, or list it in `clear` to null "
            "it — not both."
        )

    for field in clear:
        payload[field] = None

    return payload
