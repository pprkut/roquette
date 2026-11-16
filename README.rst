..
   SPDX-FileCopyrightText: Copyright 2017-2026  Heinz Wiesinger, Amsterdam, The Netherlands
   SPDX-License-Identifier: MIT

Roquette
========

Roquette is the music player for obsessive-compulsive music geeks, powered by
`beets <https://beets.io/>`_.

beets manages the music, and everything Roquette knows about it: ratings and
play counts are beets fields, playlists are the M3U files of beets' ``playlist``
plugin, settings are a section of the beets config. Roquette plays the music
(gapless, with ReplayGain) and shows everything beets, MusicBrainz, ListenBrainz
and the rest of the MetaBrainz universe know about it, in modules for every way
of looking at a collection.

Current status
--------------

Roquette is being restarted. It plays a release, gapless and with
ReplayGain, when you double-click it, and not much more yet. The roadmap is in
`docs/PLAN.md <docs/PLAN.md>`_.

Your beets library
------------------

Roquette finds the beets config as ``beet`` does (``$BEETSDIR``, or beets'
default directory); ``--beets-config FILE`` reads a file over it, like
``beet -c``. It reads the library through read-only connections and doesn't
write to it unless the config says so::

    roquette:
      library:
        writable: yes

Of the config's plugins, Roquette loads only those listed in
``roquette.beets_plugins`` (by default ``roquette``, ``types``, ``inline``,
``playlist`` and ``smartplaylist``) that the config enables; the rest are
``beet``'s business. Add ``roquette`` to ``plugins:`` (with the checkout's
``beetsplug/`` in ``pluginpath:``): it declares the types of the ratings and
play statistics Roquette keeps in beets, so that ``beet ls play_count:10..``
works, and runs queries on them in SQL. If ``beet`` often writes to the library
while Roquette plays, switching the database to SQLite's WAL journal
(``sqlite3 library.db 'PRAGMA journal_mode=WAL'``) keeps Roquette's reads from
waiting for it; that changes the file for every program using it, so it's your
choice, not Roquette's.

Development
-----------

Roquette needs Python 3.12 or later, beets 2.14 or later, the Python bindings of
`Slint <https://slint.dev/>`_ (1.18.1b1 or later), Pillow, and PyGObject with
GStreamer. It runs from the checkout, without installing; tasks are recipes of a
``justfile`` (`just <https://just.systems/>`_):

- ``just dev-library`` copies your beets config and library into ``dev/beets``.
  Development uses this copy: the live library is only read, never changed.
  Music files are never written.
- ``just run`` starts Roquette on that copy; ``just run --play QUERY`` plays
  what a beets query finds (``just run --help`` lists the options).
- ``just fake-library`` makes a fake library of the same size with generated
  covers in ``build/fake-library``, for running Roquette without one
  (``BEETSDIR=build/fake-library/beets``).
- ``just measure`` measures startup, memory, CPU and wake-ups of the window,
  idle, scrolling and playing; it needs a graphical session.
- ``just measure-library``, ``just library-locking`` and ``just beets-plugins``
  measure the library layer: loading and querying the library, writing to it
  while ``beet`` does, and loading the beets plugins.
- ``just check`` runs the checks CI runs (ruff, mypy, pytest, REUSE, typos, …);
  tools that aren't installed are skipped locally.

The beets plugin ``roquette`` is in ``beetsplug/``; the development copy's config
loads it from the checkout.

License
-------

Roquette is licensed under the MIT license and follows the
`REUSE specification <https://reuse.software/>`_; see ``LICENSES/`` and the
headers of the files.
