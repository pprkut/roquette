# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""beets queries, as the ids of the tracks they find.

A query is parsed by beets (with the loaded plugins' field types and
queries) and runs as SQL for ids, in the order of its sort or else the
collection's: under 20 ms for any query on fixed fields in the dev
library, fast enough to search as the user types. Making Items for
what a query finds costs 0.11 ms each and leaves their memory behind
(docs/decisions/0003-library.md).

What beets can only match or sort in Python (flexible attributes of
plugins roquette didn't load, computed fields) is matched on Items made
in chunks of the tracks the rest of the query finds, so that they never
all exist at once; the roquette plugin's fields run in SQL
(`beetsplug.roquette`).
"""

from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import beets.library
from beets.dbcore import query as dbquery
from beets.dbcore import sort as dbsort
from beets.dbcore import types

from roquette.library.index import chunks, rows
from roquette.library.order import COLLECTION_ORDER

Item = beets.library.Item

# Items made at once for what has to be matched or sorted in Python.
SLOW_CHUNK = 500

# The fields only albums have, as the albums' table.
_ALBUM_FIELDS = (
    "LEFT JOIN (SELECT id AS roquette_album_id, "
    + ", ".join(sorted(Item.other_db_fields))
    + " FROM albums) AS albums ON albums.roquette_album_id = items.album_id"
)


class QueryError(Exception):
    """A query beets can't parse; the message is for the user."""


class Stopped(Exception):
    """The query was stopped (`stop` returned true)."""


@dataclass(frozen=True, slots=True)
class Query:
    text: str
    # The part beets runs in SQL, and the part it matches in Python.
    where: str
    subvals: tuple[Any, ...]
    slow: dbquery.Query | None
    # ORDER BY terms of the query's sort (the collection's order
    # breaks ties), or a sort only Python can do.
    order: str
    order_subvals: tuple[Any, ...]
    slow_sort: list[dbsort.FieldSort] | None
    # Whether the query has a sort of its own.
    sorted: bool
    # Whether the query and its order use fields of albums.
    joins_albums: bool
    sorts_by_albums: bool


def parse(library: beets.library.Library, text: str) -> Query:
    """Parse a query; the plugins must be loaded."""
    try:
        # Path queries are relative to the music directory.
        with library.music_dir_context():
            parsed, sort = beets.library.parse_query_string(text, Item)
    except ValueError as error:  # beets' ParsingError
        raise QueryError(str(error)) from error
    where, subvals, slow = _split(parsed)
    order, order_subvals, slow_sort = _order(sort)
    return Query(
        text,
        where or "1",
        tuple(subvals),
        slow,
        order,
        tuple(order_subvals),
        slow_sort,
        not isinstance(sort, dbsort.NullSort),
        bool(parsed.field_names & Item.other_db_fields),
        slow_sort is None and bool(sort.field_names & Item.other_db_fields),
    )


def query_ids(
    library: beets.library.Library,
    query: Query | str,
    stop: Callable[[], bool] = lambda: False,
) -> array[int]:
    """The ids of the tracks the query finds, in its order; blocking, for
    the reader worker. `stop` is asked between chunks of Items, and the
    query raises `Stopped` when it returns true."""
    if isinstance(query, str):
        query = parse(library, query)
    if query.joins_albums:
        # Fields of both tables are ambiguous in a join, as beets'
        # clauses don't name the items' table: the join is a subquery,
        # as in beets.
        sql = (
            "SELECT items.id FROM items WHERE items.id IN"
            f" (SELECT items.id FROM items {Item.relation_join}"
            f" WHERE {query.where})"
        )
    else:
        sql = f"SELECT items.id FROM items WHERE {query.where}"
    if query.sorts_by_albums:
        # beets names the albums' table in a sort by their fields: they
        # are joined without the fields items have too.
        sql = sql.replace("FROM items", f"FROM items {_ALBUM_FIELDS}", 1)
    order = query.order if query.slow_sort is None else COLLECTION_ORDER
    sql += f" ORDER BY {order}"
    subvals = query.subvals + (query.order_subvals if query.slow_sort is None else ())
    found = array("q")
    with library.transaction():
        found.extend(int(row[0]) for row in rows(library, sql, subvals))
    if query.slow is None and query.slow_sort is None:
        return found
    return _in_python(library, found, query, stop)


def _in_python(
    library: beets.library.Library,
    ids: array[int],
    query: Query,
    stop: Callable[[], bool],
) -> array[int]:
    slow, sorts = query.slow, query.slow_sort
    matched = array("q")
    keys: list[dict[int, Any]] = [{} for _ in sorts or ()]
    for chunk in chunks(ids, SLOW_CHUNK):
        if stop():
            raise Stopped
        with library.music_dir_context():
            items = {
                item.id: item for item in library.items(dbquery.InQuery("id", chunk))
            }
        for item_id in chunk:
            item = items.get(item_id)
            if item is None or (slow is not None and not slow.match(item)):
                continue
            matched.append(item_id)
            for part, part_keys in zip(sorts or (), keys, strict=True):
                part_keys[item_id] = _sort_key(part, item)
        del items
    if sorts:
        # Stable sorts, the last part first: the collection's order
        # breaks ties.
        order = matched.tolist()
        for part, part_keys in zip(reversed(sorts), reversed(keys), strict=True):
            order.sort(key=part_keys.__getitem__, reverse=not part.ascending)
        matched = array("q", order)
    return matched


def _split(
    parsed: dbquery.Query,
) -> tuple[str | None, Sequence[Any], dbquery.Query | None]:
    """The SQL clause of a query and the part only Python can match."""
    where, subvals = parsed.clause()
    if where is not None:
        return where, subvals, None
    if isinstance(parsed, dbquery.AndQuery):
        clauses: list[str] = []
        values: list[Any] = []
        slow: list[dbquery.Query] = []
        for part in parsed.subqueries:
            part_where, part_subvals = part.clause()
            if part_where is None:
                slow.append(part)
            else:
                clauses.append(f"({part_where})")
                values.extend(part_subvals)
        return " AND ".join(clauses) or None, values, dbquery.AndQuery(slow)
    return None, (), parsed


_NUMERIC = (types.BaseInteger, types.BaseFloat, types.Boolean)


def _order(
    sort: dbsort.Sort,
) -> tuple[str, Sequence[Any], list[dbsort.FieldSort] | None]:
    """ORDER BY terms for a sort, or the sort's parts if only Python can
    sort by them."""
    parts: list[dbsort.Sort]
    if isinstance(sort, dbsort.MultipleSort):
        parts = list(sort.sorts)
    elif isinstance(sort, dbsort.NullSort):
        parts = []
    else:
        parts = [sort]
    terms: list[str] = []
    subvals: list[Any] = []
    getters = Item._getters()
    for part in parts:
        if not isinstance(part, dbsort.FieldSort):
            return COLLECTION_ORDER, (), _field_sorts(parts)
        if not part.is_slow():
            clause = part.order_clause()
            if clause is None:
                return COLLECTION_ORDER, (), _field_sorts(parts)
            terms.append(clause)
            continue
        name = part.field_name
        if name in getters or name in Item.all_db_fields:
            # Computed fields: beets' getters and the inline plugin's.
            return COLLECTION_ORDER, (), _field_sorts(parts)
        # A flexible attribute, as its type sorts it; without one, as
        # text. An item without it sorts as the type's null value.
        value = (
            "(SELECT value FROM item_attributes WHERE entity_id = items.id AND key = ?)"
        )
        kind = Item._types.get(name)
        if isinstance(kind, _NUMERIC):
            expression = f"CAST({value} AS REAL)"
            # Nullable types' missing values sort first, as SQL's NULL.
            if kind.null is not None:
                expression = f"IFNULL({expression}, {float(kind.null)})"
        elif part.case_insensitive:
            expression = f"LOWER(IFNULL({value}, ''))"
        else:
            expression = f"IFNULL({value}, '')"
        terms.append(f"{expression} {'ASC' if part.ascending else 'DESC'}")
        subvals.append(name)
    return ", ".join([*terms, COLLECTION_ORDER]), subvals, None


def _field_sorts(parts: Sequence[dbsort.Sort]) -> list[dbsort.FieldSort]:
    return [part for part in parts if isinstance(part, dbsort.FieldSort)]


def _sort_key(part: dbsort.FieldSort, item: beets.library.Item) -> tuple[int, Any]:
    """beets' key of a sort (`FieldSort.sort`), comparable across types."""
    if isinstance(part, dbsort.SmartArtistSort):
        value: Any = item.get(f"{part.field_name}_sort") or item.get(part.field_name)
    else:
        value = item.get(part.field_name)
    if value is None:
        kind = item._types.get(part.field_name)
        value = kind.null if kind is not None else ""
    if value is None:
        # Nullable types' missing values come first, as in beets.
        return (-1, 0)
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if isinstance(value, str):
        return (1, value.lower() if part.case_insensitive else value)
    if isinstance(value, int | float):
        return (0, value)
    return (2, str(value))
