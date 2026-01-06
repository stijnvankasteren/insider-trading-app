from __future__ import annotations


def sql_like_escape(value: str) -> str:
    """
    Escape user input for use inside a SQL LIKE pattern.

    We use backslash as the escape character, so callers should pass `escape="\\\\"`
    to SQLAlchemy's `.like(..., escape="\\\\")`.
    """

    # Order matters: escape the escape character first.
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sql_like_contains(value: str) -> str:
    return f"%{sql_like_escape(value)}%"

