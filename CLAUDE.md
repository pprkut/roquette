<!--
SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
SPDX-License-Identifier: MIT
-->

# roquette

Music player built on a beets library, in Python (beets in-process, Slint
UI, GStreamer playback).

- The roadmap, architecture and design notes are in `docs/PLAN.md`. The
  technology choices there are provisional until the phase 1 spikes
  confirm them; decisions and pivots go to `docs/decisions/`.
- Work on **one milestone at a time**, in the order of `docs/PLAN.md`. When
  a milestone is complete, mark it done in the plan, summarise the result
  (including measurements and open issues) and stop for review. Don't start
  the next milestone without the go-ahead.
- Keep semantically different changes in separate commits. Each milestone
  gets its own branch with a pull request.
- **The live beets data is read-only.** Never write to `~/data/beets.blb`,
  `~/.config/beets/` or the music files in `/mnt/media/Music`. Development
  uses the copy made by `just dev-library` in `dev/`: run roquette and
  every `beet` command with `BEETSDIR=dev/beets`. Reading the live data is
  fine; opening the live database directly with `sqlite3` only with
  `?mode=ro`.
- **No database of roquette's own.** Data about the music goes into beets
  (flexible attributes, the `roquette:` config section, the `playlist` and
  `smartplaylist` plugins). Before building something, check whether beets
  or a beets plugin already covers it.
- **Low RAM and CPU is a goal**, with budgets and design rules in
  "Performance" in the plan. Every milestone summary includes a
  `just measure` run compared with the previous milestone.
- Never use real ListenBrainz/Last.fm credentials in tests or the
  development config.
- Before committing: `just check` (ruff, ruff format, mypy, pytest,
  `reuse lint`). Tests are optional in phase 1 (spikes), mandatory from
  phase 2 on — but follow "Designing for testability" in the plan from the
  start.
- Licence: MIT with REUSE headers; see "Licensing" in the plan.
