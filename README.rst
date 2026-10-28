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

Roquette is being restarted and can't play music yet. The roadmap is in
`docs/PLAN.md <docs/PLAN.md>`_.

Development
-----------

Roquette needs Python 3.12 or later, beets 2.14 or later, the Python bindings of
`Slint <https://slint.dev/>`_ (1.18.1b1 or later), Pillow, and PyGObject with
GStreamer. It runs from the checkout, without installing; tasks are recipes of a
``justfile`` (`just <https://just.systems/>`_):

- ``just dev-library`` copies your beets config and library into ``dev/beets``.
  Development uses this copy: the live library is only read, never changed.
  Music files are never written.
- ``just run`` starts Roquette on that copy.
- ``just fake-library`` makes a fake library of the same size with generated
  covers in ``build/fake-library``, for running Roquette without one
  (``BEETSDIR=build/fake-library/beets``).
- ``just check`` runs the checks CI runs (ruff, mypy, pytest, REUSE, typos, …);
  tools that aren't installed are skipped locally.

The beets plugin ``roquette`` is in ``beetsplug/``; the development copy's config
loads it from the checkout.

License
-------

Roquette is licensed under the MIT license and follows the
`REUSE specification <https://reuse.software/>`_; see ``LICENSES/`` and the
headers of the files.
