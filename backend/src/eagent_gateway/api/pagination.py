"""Opt-in list pagination; legacy callers keep their original wire format."""
from dataclasses import dataclass, field
import re
from typing import Callable

from fastapi import Query, Request

from ..errors import ValidationError


@dataclass(frozen=True)
class Page:
    number: int = 1
    size: int = 10
    filters: dict[str, str] = field(default_factory=dict)


def optional_page(
    request: Request,
    page: int | None = Query(default=None, ge=1),
    pageSize: int | None = Query(default=None, ge=1, le=200),
) -> Page | None:
    if page is None and pageSize is None:
        return None
    reject_mixed_pagination(request)
    return Page(page or 1, pageSize or 10, dict(request.query_params))


def reject_mixed_pagination(request: Request) -> None:
    keys = request.query_params
    if ("page" in keys or "pageSize" in keys) and ("limit" in keys or "offset" in keys):
        raise ValidationError("page/pageSize cannot be combined with limit/offset")


def query_page(db, sql: str, order: str, params: dict, page: Page,
               transform: Callable | None = None, **metadata) -> dict:
    """SQL and ordering are owned by callers, never supplied by HTTP clients."""
    total = db.query_one(f"SELECT COUNT(*) AS n FROM ({sql}) page_count", params)["n"]
    rows = db.query_all(
        f"{sql} ORDER BY {order} LIMIT :page_limit OFFSET :page_offset",
        {**params, "page_limit": page.size, "page_offset": (page.number - 1) * page.size},
    )
    return {
        "items": [transform(row) for row in rows] if transform else rows,
        "total": int(total), "page": page.number, "pageSize": page.size,
        **metadata,
    }


def add_filter(sql: str, condition: str) -> str:
    depth, quote, outer = 0, None, []
    for char in sql:
        if quote:
            if char == quote:
                quote = None
            outer.append(" ")
        elif char in "'\"`":
            quote = char; outer.append(" ")
        elif char == "(":
            depth += 1; outer.append(" ")
        elif char == ")":
            depth -= 1; outer.append(" ")
        else:
            outer.append(char if depth == 0 else " ")
    has_where = re.search(r"\bWHERE\b", "".join(outer), re.I)
    return sql + (" AND " if has_where else " WHERE ") + condition


def search_filter(sql: str, columns: tuple[str, ...], params: dict, page: Page) -> str:
    q = page.filters.get("q", "").strip()
    if q and columns:
        # Treat search text literally rather than as LIKE wildcard syntax.
        params["page_search"] = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        sql = add_filter(sql, "(" + " OR ".join(
            f"{column} LIKE :page_search ESCAPE '!'" for column in columns
        ) + ")")
    return sql
