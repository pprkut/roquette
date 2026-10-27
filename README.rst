..
   SPDX-FileCopyrightText: Copyright 2017-2026  Heinz Wiesinger, Amsterdam, The Netherlands
   SPDX-License-Identifier: MIT

roquette
========

roquette is the music player for obsessive-compulsive music geeks, powered by
`beets <https://beets.io/>`_.

beets manages the music, and everything roquette knows about it: ratings and
play counts are beets fields, playlists are the M3U files of beets' ``playlist``
plugin, settings are a section of the beets config. roquette plays the music
(gapless, with ReplayGain) and shows everything beets, MusicBrainz, ListenBrainz
and the rest of the MetaBrainz universe know about it, in modules for every way
of looking at a collection.

Current status
--------------

roquette is being restarted and can't play music yet. The roadmap is in
`docs/PLAN.md <docs/PLAN.md>`_.

Development
-----------

roquette needs Python 3.12 or later and beets 2.14 or later. It runs from the
checkout, without installing; tasks are recipes of a ``justfile``
(`just <https://just.systems/>`_):

- ``just dev-library`` copies your beets config and library into ``dev/beets``.
  Development uses this copy: the live library is only read, never changed.
  Music files are never written.
- ``just run`` starts roquette on that copy.
- ``just check`` runs the checks CI runs (ruff, mypy, pytest, REUSE, typos, …);
  tools that aren't installed are skipped locally.

The roquette beets plugin is in ``beetsplug/``; the development copy's config
loads it from the checkout.

License
-------

roquette is licensed under the MIT license and follows the
`REUSE specification <https://reuse.software/>`_; see ``LICENSES/`` and the
headers of the files.
