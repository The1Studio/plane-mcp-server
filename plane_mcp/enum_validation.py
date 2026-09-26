"""Strict validation for enum-valued tool arguments.

Several tools take a free-form ``str`` the SDK models as a ``Literal`` enum and
then guard it with a *coercing* expression::

    validated = value if value in get_args(SomeEnum) else None

That reads as validation but is not: an unrecognised value is replaced with
``None`` and the request succeeds. A caller that mistypes ``"Critical"`` for
``"urgent"`` receives HTTP 200 and a work item at the server default, with no
error, no warning, and no field in the response reporting the substitution —
the value they asked for is simply gone (plane-mcp-server#55, #56).

``require_enum_member`` keeps the ``None``-means-unset contract and rejects
everything else loudly.
"""

from typing import Any, TypeVar, get_args

E = TypeVar("E")


def require_enum_member(value: Any, enum: Any, field: str, tool: str) -> Any:
    """Return ``value`` narrowed to ``enum``, or ``None`` when unset.

    Args:
        value: The caller-supplied value. ``None`` means "not provided" and is
            returned unchanged so the SDK keeps its own default.
        enum: The ``Literal`` type the value must be a member of.
        field: The argument name, for the error message.
        tool: The tool name, for the error message.

    Returns:
        ``value``, typed as a member of ``enum``, or ``None``.

    Raises:
        ValueError: The value is neither ``None`` nor a member of ``enum``. The
            message lists the accepted values, so a calling model can correct
            itself on the next turn.
    """
    if value is None:
        return None

    allowed = get_args(enum)
    if value not in allowed:
        raise ValueError(f"{tool}: invalid {field} {value!r}. Expected one of: {', '.join(repr(a) for a in allowed)}.")
    return value
