# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The collection's order, as SQL: for the index and for queries."""

# Sort by album artist (sort name first), release (by year, then title),
# disc and track: the collection's order.
ALBUMARTIST_SORT = "COALESCE(NULLIF(albumartist_sort, ''), albumartist) COLLATE NOCASE"

# Singletons keep the tags of their files: their album artist may be
# empty (then the artist counts) and their `album` set. They come after
# the artist's releases, by title, whatever their `album`.
_SINGLETON = "IFNULL(items.album_id, 0) = 0"
ARTIST = f"""CASE WHEN {_SINGLETON} AND IFNULL(items.albumartist, '') = ''
    THEN items.artist ELSE items.albumartist END"""
_ARTIST_SORT = f"""CASE WHEN {_SINGLETON} AND IFNULL(items.albumartist, '') = ''
    THEN COALESCE(NULLIF(items.artist_sort, ''), items.artist)
    ELSE COALESCE(NULLIF(items.albumartist_sort, ''), items.albumartist)
    END COLLATE NOCASE"""

# The collection's order of items; releases by year, then title, as the
# index's releases (`roquette.library.index`).
COLLECTION_ORDER = f"""{_ARTIST_SORT}, {ARTIST}, {_SINGLETON},
    CASE WHEN {_SINGLETON} THEN 0
        ELSE COALESCE(NULLIF(items.original_year, 0), items.year) END,
    CASE WHEN {_SINGLETON} THEN '' ELSE items.album END COLLATE NOCASE,
    items.album_id, items.disc, items.track, items.title COLLATE NOCASE,
    items.id"""
