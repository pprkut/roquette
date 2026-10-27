<!--
SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
SPDX-License-Identifier: MIT
-->

# roquette — implementation plan

A music player for obsessive-compulsive music geeks, built on top of a
[beets](https://beets.io/) library. beets stays the tool that manages the
music *and* everything roquette knows about it; roquette plays the music
and makes everything beets (and the MetaBrainz universe behind it) knows
visible and explorable.

This plan restarts the project. The 2017 code (PyQt5, QtQuick Controls 1,
beets 1.x) no longer runs: PyQt5 and QtQuick Controls 1 are gone from Qt 6,
and beets 2.x removed `FixedFieldSort`. What carries over is the idea and
the collection grouping (album artist → release by `album_id` → medium →
track), not the code.

## Goals

- **Don't reinvent beets.** roquette has no database of its own. Ratings
  and play statistics are beets fields, playlists are M3U files the beets
  `playlist` plugin understands, smart playlists are beets queries,
  settings are a section of the beets config. Where a beets plugin already
  does something (importing listening history, writing tags), roquette
  relies on it instead of duplicating it.
- **Follow beets, never second-guess it.** Releases are beets albums
  (`album_id`), so an album and a single with the same name stay apart.
  Singletons, compilations, multi-disc releases, disambiguations and
  `albumtype` are shown the way beets has them. Searching uses the beets
  query language.
- **Recordings, not just files.** Tracks group by MusicBrainz recording
  everywhere (see "Recording versions").
- **Correct playback.** Gapless, ReplayGain (track and album mode), no
  glitches when the library is busy.
- **As little RAM and CPU as possible.** Like ren, roquette is meant to
  run all day. Paused, it uses no CPU; playing, it costs little more than
  decoding the audio; memory doesn't grow with uptime or with how much of
  the library has been browsed. Covers, external data and several views
  of a large library make this harder, so it's designed in from the start
  (see "Performance") and measured at every milestone, not fixed at the
  end. Python, the price of using beets in-process, has a higher baseline
  than ren's Rust; S1 measures that baseline and sets the budgets.
- **A modular main view.** Every way of looking at the music is a module;
  adding a new one doesn't touch the others. "Whatever you want to see about
  your music, there is a way to see it."
- **Local only.** roquette talks to one beets library and plays local files.
  How the files get onto a machine (e.g. synced through Nextcloud) is not
  roquette's concern.

## Development data: read-only rule

The live beets library (`~/data/beets.blb`, config in `~/.config/beets/`)
and the music files (`/mnt/media/Music`) are **read-only** for development:

- Development runs against a **copy**: `just dev-library` (M0) copies the
  beets config directory and database into `dev/` (gitignored) and points
  the copied config's `library:` (and the `playlist` plugin's
  `playlist_dir`) at copies. roquette is started with `BEETSDIR=dev/beets`,
  the beets mechanism for picking the config directory. Every `beet`
  command run during development uses the same variable.
- The music files are read, never written. roquette writes no file tags at
  all; tests that need files use generated ones in a temporary directory.
- roquette refuses to write to a beets library unless its settings say
  `roquette.library.writable: yes`. The default is read-only, so a
  development build pointed at the live config by accident can't change
  anything. Only the `dev/` copy enables writing.
- Scrobbling is off unless configured, and the development config never
  holds real ListenBrainz/Last.fm credentials (see M13).

## Technology choices

Initial picks, validated by the spikes in phase 1; pivots are recorded in
`docs/decisions/`.

| Concern        | Choice | Why |
|----------------|--------|-----|
| Language       | **Python ≥ 3.12** | beets is a Python library. Its query parser, flexible attributes and their types, path templates, config and plugin system are only available in-process. |
| beets access   | **beets 2.x as a library** (`beets.library.Library`, `beets.config`, plugins loaded from the user's config) | No re-implementation of the query language; the library copy and the live one are the same code path. |
| beets plugin   | **`beetsplug/roquette.py`**, shipped with roquette | Declares the types of the fields roquette writes (so `beet ls play_count:10..` works without roquette running), maps the `FMPS_RATING` tag to a field, and holds the `roquette:` config defaults. Enabled by adding `roquette` to `plugins:`. |
| UI toolkit     | **Slint**, Python bindings (`slint` on PyPI, beta, 1.18.1b1) | Known from ren; light and fast; custom-drawn UI suits the module layout and the recording dropdowns better than stock desktop widgets; ren's components (tree, table, splitter, tab bar, theme, icons) can be reused. The event loop is an asyncio loop. Beta status is the main risk, hence S1. |
| UI fallback    | **PySide6 + Qt Quick (QML)** | If S1 fails: mature, LGPL, Qt 6 has a `TreeView` again; heavier. Qt Widgets is not considered: the emby-like browse and the multi-line playlist are custom-drawn either way. No GTK. |
| Playback       | **GStreamer `playbin3`** via PyGObject | Gapless through `about-to-finish`, ReplayGain via `rgvolume`/`rglimiter` or a `volume` element driven from beets' values, PipeWire/Pulse output, every format beets imports. The beets `replaygain` plugin already uses GStreamer here. |
| MPRIS          | **dbus-fast** (asyncio) | Media keys, Plasma's media controls; asyncio fits Slint's loop. |
| HTTP           | **aiohttp** or **httpx** (decided in S1) | Scrobbling, MusicBrainz, Wikipedia; asyncio-native. |
| Settings       | The **`roquette:` section of the beets config** | One config file for beets and roquette, read through beets' own config layer (confuse), overridable the same way as any beets setting. |
| Secrets        | In the beets config where a beets plugin already keeps them (`listenbrainz.token`); the Last.fm session key in the Secret Service via `keyring` | Follow beets' convention; the session key is created by roquette and has no beets equivalent. |
| Tooling        | `pyproject.toml`, ruff (lint + format), mypy, pytest, `just` | |

### What lives where

- **beets library** (flexible attributes on items): the rating, `play_count`,
  `skip_count`, `last_played`. The statistics fields use the names of the
  beets `mpdstats` plugin, so beets' ecosystem understands them. The
  rating is `fmps_rating` (float 0–1 in 0.1 steps, i.e. half stars): the
  same name and scale as the `FMPS_RATING` file tag, so syncing files to
  other players (e.g. on a phone) keeps the ratings, and already typed in
  the beets config. `rating` is taken: `mpdstats` uses it for a computed
  score. beets has no convention for ratings yet; if it gets one, roquette
  follows it. `amarok_rating` (0–10, kept in step by
  `sync-fmps-rating.sh`) is updated along with `fmps_rating` until it's
  no longer needed. Listening history comes from ListenBrainz
  and Last.fm, which have it anyway; beets' `listenbrainz` and `lastimport`
  plugins can import it.
- **beets config**: roquette's settings (`roquette:`), saved playlists
  through the `playlist` plugin's `playlist_dir`, smart playlists through
  the `smartplaylist` plugin's `playlists`.
- **Files outside beets**, all regenerable or small:
  - `$XDG_STATE_HOME/roquette/`: window layout, the live playlist (an
    M3U8 file), the scrobble outbox (listens not yet submitted, one JSON
    line each).
  - `$XDG_CACHE_HOME/roquette/`: responses from MusicBrainz, Wikidata,
    Wikipedia. Cover thumbnails use the shared freedesktop thumbnail cache
    (`$XDG_CACHE_HOME/thumbnails/`) instead of a private one.
- **Music files**: never written by roquette. The ratings in their
  `FMPS_RATING` tags (set by Amarok) are imported into beets once (M9),
  with the existing `/usr/local/bin/sync-fmps-rating.sh` or its successor.
  Writing ratings back to the files is `beet write`'s job, through the
  field mapping in the roquette beets plugin, if you ever want it.

## UI concept

```
┌──────────┬──────────────────────────────────┬───────────────┐
│ Nav      │                                  │ Live playlist │
│          │   Main view: the active module   │               │
│ Browse   │                                  │ ▣ Album       │
│ Tree     │                                  │   Artist·Year │
│ Related  │                                  │   1. Title ★★ │
│ Lyrics   │                                  │   2. Title  ▾ │
│ Wiki     │                                  │ ▣ Album       │
│ Playlists│                                  │   …           │
│ Stats    │                                  │               │
├──────────┴──────────────────────────────────┴───────────────┤
│ ▣ Now playing · ⏮ ⏯ ⏭ · ━━━━━━━●━━━━━━ 2:13 / 4:51 · 🔊    │
└─────────────────────────────────────────────────────────────┘
```

- **Transport bar** at the bottom: now playing (cover, title, artist,
  rating), play controls, seek bar, volume.
- **Live playlist** on one side: the queue that is playing. Which side is a
  setting; navigation is always opposite.
- **Navigation** on the other side, listing the modules.
- **Main view**: the active module.

### Recording versions

Tracks are grouped by MusicBrainz recording (`mb_trackid` in beets) wherever
tracks are listed. The library has 11,276 recordings; 491 of them exist
in more than one version (1,064 tracks, up to five versions of one
recording).

- **Playlist.** An entry is a *recording*. A row with several versions in
  the library gets a collapsible dropdown listing them (release, format,
  bit depth/sample rate, length). By default the best version plays, even
  if another one was added: highest quality first (lossless over lossy,
  then bit depth, sample rate, bitrate), then the longest. Picking a
  version in the dropdown pins it. The playing entry can't be changed.
- **Whole releases keep their own versions.** Tracks queued as part of a
  whole release (or medium) are pinned to that release, so gapless
  playback and album gain aren't broken by a track swapped in from a
  compilation. The best-version rule applies to tracks added on their own.
- **Collection browser.** The same grouping, so a recording appears once
  with its versions available, instead of once per release.
- **Open, for the experiment:** whether the default selection picks the
  right version in practice (quality first, "longest" as tie-breaker, or
  something else, e.g. preferring the original release); tracks without a
  recording ID are their own group.

The UI is unproven, so it starts as an experiment (M11) once the basic
playlist exists. The data model supports it from the start: a playlist
entry is a recording plus an optional pinned item (M3), and the library
layer indexes recordings (M1). The live playlist keeps pins as an
extended-M3U comment line other players ignore.

### Modules

A module is a view model in plain Python plus a Slint component, registered
with the module host. The host gives each module:

- the **library** (queries, releases, items, recordings), read-only, plus
  explicit operations for ratings and statistics;
- the **context**: the track/release/artist the user is looking at, which
  is either the current selection or the playing track (followed by
  default, pinnable per module, so "Lyrics" follows playback while you
  browse);
- **actions** on the live playlist (append, play next, replace);
- access to the **external data services** (MusicBrainz, Wikipedia, cover
  art) with shared caching and rate limiting.

Modules are loaded when first shown, and only the visible one gets updates.
Planned modules:

- **Browse**: emby-like: grids of album artists and releases with covers,
  sections by `albumtype` (albums, EPs, singles, compilations,
  soundtracks), a release page with tracklist and details.
- **Tree**: the 2017 collection browser, done properly.
- **Playlists**: the M3U playlists in beets' `playlist_dir` and the
  `smartplaylist` queries (evaluated live, not from generated files), in
  tabs; the live playlist can be saved to one and loaded from one.
- **Related**: same artist, same album artist, other releases in the same
  release group, other recordings of the same work (covers, found locally
  through `mb_workid`), and MusicBrainz relationships (covers, samples,
  remixes) that go beyond the library, marking what's in the library.
- **Lyrics**: the beets `lyrics` field (75% of the library has it).
- **Wikipedia**: artist, release group and work articles, found through
  MusicBrainz URL relations and Wikidata.
- **Details**: everything beets knows about a track or release, including
  flexible attributes, technical data and MusicBrainz links.
- **Statistics**: from the beets fields (most played, top rated, "not
  played in a year", rating distribution) and from ListenBrainz's
  statistics API.

## Architecture

```
beetsplug/
  roquette.py  The beets plugin: field types, the FMPS_RATING mapping,
               config defaults.
roquette/
  library/     beets access: config discovery, read-only guard, queries,
               the release/medium/track grouping, the recording index,
               ratings and statistics.
  playback/    GStreamer engine: playbin3, gapless, ReplayGain, events.
  queue/       The live playlist: recording entries with optional pins,
               version choice, current track, repeat/shuffle, ReplayGain
               mode decisions, persistence as M3U8.
  playlists/   M3U playlists and smart playlists, following the beets
               `playlist` and `smartplaylist` plugins' config.
  scrobble/    ListenBrainz and Last.fm clients, the outbox.
  sources/     External data (MusicBrainz, Wikidata/Wikipedia, Cover Art
               Archive) with an on-disk cache and per-service rate limits.
  settings/    The `roquette:` config section, the UI state file, secrets.
  modules/     The module API and the modules (view models only).
  mpris/       MPRIS D-Bus service.
  ui/          Slint glue: .slint files, models, adapters for the
               view models.
  app.py       Wiring.
```

Data flow: the UI and modules call view models; view models call `library`,
`queue` and `playback`. Blocking work (beets queries, writes to beets,
image decoding) runs on worker threads; results come back to the event
loop (asyncio `call_soon_threadsafe`, or Slint's equivalent; S1 checks
which works). GStreamer messages are taken off its bus and forwarded the
same way.

### Playback notes

- **Gapless**: `playbin3`'s `about-to-finish` signal fires in a streaming
  thread; the next URI must be set there, synchronously. The queue
  therefore keeps "what plays next" (including the chosen version of the
  next recording) resolved ahead, available without the event loop.
- **ReplayGain**: the gain has to change exactly at the track boundary, not
  when the bus message arrives on the event loop. Either `rgvolume` reads
  the tags in the stream (the files have `REPLAYGAIN_*` tags), or a pad
  probe applies gain from the beets database when a new stream starts. S2
  decides which; the beets database is preferred as the source, so that a
  `beet replaygain` run is followed without depending on file tags.
- **Mode**: album gain while consecutive tracks of the same release play in
  order, track gain otherwise (shuffle, mixed playlists, a swapped-in
  version from another release); pre-amp and clipping prevention via the
  peaks. Configurable: auto / track / album / off.

### Library notes

- The library has ~11,800 items in ~800 albums, almost all FLAC, with
  MusicBrainz recording IDs on every item. Grouping, sorting and the model
  must stay smooth at 10× that.
- beets connections are per thread; queries run on a worker thread.
- Writing flexible attributes while `beet` runs on the same database (an
  import, `mbsync`) must not lose data or block the UI: short transactions,
  retries on `database is locked` (S3).
- roquette loads the plugins listed in the beets config, so field types
  (`types`, `inline`, its own plugin) are known. Loading a plugin only
  registers it; side effects such as `fetchart` or `lyrics` happen on
  import events, which roquette never triggers. S3 verifies that for the
  plugins in use.
- roquette's play-count/skip/last-played logic would conflict with
  `mpdstats` only if both ran for the same library at once; they don't.

## Performance

### Budgets

Initial targets, for the dev library (~11,800 tracks, ~800 releases),
measured as RSS and PSS on KDE. S1 measures the floor (Python, beets,
Slint and GStreamer loaded, an empty window) and replaces these with real
budgets in its decision record:

- **Idle** (window open, paused, a module showing): under 150 MB RSS,
  0% CPU, no wake-ups beyond what the toolkit needs.
- **Playing**: CPU dominated by decoding (FLAC: well under 1% of a core);
  no more than a few wake-ups per second for the position display, none
  while the window is hidden or minimised.
- **After browsing** every release in Browse and back: memory returns to
  near the idle figure, not "idle plus every cover".
- **Long runs**: RSS flat over an 8-hour playback run.
- **Startup** to an interactive window: under a second on a warm cache.

### Design rules

- **Never hold the whole library as beets objects.** A beets `Item`
  carries every field and its flexible attributes. The resident index
  (releases, recordings, the tree) keeps ids and the few fields it shows,
  in compact form (tuples, arrays, interned strings). Full `Item`s are
  fetched for what's visible and dropped again. S3 measures both.
- **Views are virtualised.** Models build rows when the view asks for
  them; nothing is materialised for rows that aren't on screen.
- **Modules are lazy.** A module is imported and built when first shown;
  hidden modules get no updates and drop their caches (covers, fetched
  articles) after a while. Heavy imports (aiohttp, dbus-fast, the
  external sources) happen on first use, not at startup.
- **Images at display size.** Covers are decoded off the UI thread
  straight to the size they're shown at (freedesktop thumbnails, 256 px
  "large", for grids), into a bounded in-memory cache; full-size art is
  decoded only for the one cover shown large.
- **No polling.** Changes arrive as events (GStreamer bus, D-Bus, file
  watches on the beets database for outside changes); the position
  display ticks at a few Hz and only while visible.
- **Bounded caches everywhere**: size limits for memory caches, age
  limits for the on-disk ones.
- **Background work is cheap and polite.** Long-running work (indexing,
  fetching external data) runs on worker threads at low priority and in
  chunks, so it neither competes with audio nor blocks the UI. Audio
  runs in GStreamer's own threads and never waits for Python.

### Measuring

`scripts/measure.sh` (`just measure`, from S1 on) starts roquette with
the dev library, runs scripted scenarios (startup, idle, playing, browsing
everything, a long run with `--duration`) and samples RSS/PSS
(anonymous/file/shared), CPU time, thread count and wake-ups from `/proc`.
It prints a Markdown summary for the milestone report. roquette gets the
flags the scenarios need (`--measure` for startup timings, `--autoscroll`
or a scripted browse), as in ren. Python-side allocations are checked with
`tracemalloc` when a figure grows. Every milestone summary includes a
`just measure` run compared with the previous one.

## Designing for testability

- **Logic in plain Python, Slint only binds.** View models (collection
  grouping, playlist layout, recording versions, module context) don't
  import `slint` and are tested without a window.
- **The library layer works on any beets library.** Tests build small
  libraries and configs in a temporary directory, with albums, singletons,
  multi-disc releases, a same-named album and single, compilations, and
  one recording in several versions of different quality.
- **The playback engine is tested with real GStreamer** on generated audio
  files (short FLAC files with known content and ReplayGain values) and
  `appsink`, so gaplessness and gain can be checked sample-exactly.
- **Seams only where a second implementation exists**: `Clock` (statistics,
  scrobble rules), `ScrobbleService` (fake in tests), `SecretStore`, HTTP
  for external sources (recorded responses as fixtures).
- **No hidden paths.** The beets config directory, state and cache
  directories are passed in, never looked up deep in library code.

## Milestones

Work on **one milestone at a time**. Each ends with a short summary (what
was done, measurements, open issues) and a review before the next one
starts. Keep semantically different changes in separate commits.

### Phase 0 — Restart

- **M0 — Scaffold.** `pyproject.toml` (Python ≥ 3.12; beets, slint,
  PyGObject as dependencies; `roquette` and `beetsplug` packages; a `dev`
  dependency group for CI), ruff, mypy, pytest, `.editorconfig`,
  `typos.toml`, `justfile` (`just check` runs what CI runs), and the CI
  workflows from "Conventions": general, dependencies and code tests,
  Codecov, Dependabot. `scripts/setup-ci.sh`, shared by CI and cloud
  sessions. Licensing and REUSE (see "Licensing").
  `just dev-library` creates the development copy of the beets config and
  library. The 2017 code is removed; it stays in the git history. The
  README is updated.
  *Done when:* `just check` passes locally and in CI, with one test that
  opens the dev library copy (skipped in CI).

### Phase 1 — Spikes

Goal: answer the risky questions with working code and numbers. Tests are
optional, module boundaries are not. Each spike ends with a decision record
`docs/decisions/NNNN-<topic>.md`.

- **S1 — Slint from Python.** The window from "UI concept" with
  placeholders: navigation with two dummy modules, a playlist pane, the
  transport bar. One module lists all tracks of the dev library through a
  custom `slint.Model` (rows built on demand); one shows a grid of ~800
  album covers loaded and scaled on a worker thread. The playlist pane
  shows a few multi-line rows, one with an expandable list of versions.
  Check: asyncio and worker threads talking to the UI, custom models at 10×
  the library size, image loading, rows of different heights, reusing
  ren's `.slint` components, drag and drop, keyboard focus, the system
  colour scheme.
  Add `scripts/measure.sh` (see "Measuring") and measure the floor: an
  empty window with Python, beets, Slint and GStreamer loaded.
  *Done when:* the floor, startup time, RSS/PSS and CPU while idle and
  while scrolling the list and the cover grid, and RSS after scrolling the
  whole grid are recorded, with a list of what worked, what needed
  workarounds and what didn't. If S1 falls back to PySide6, the same
  measurements are taken there, for comparison.
  *Decides:* Slint-Python, or PySide6 + QML; the performance budgets.
- **S2 — Playback.** A minimal engine on `playbin3` under the S1 event loop:
  play a list of files, gapless across a live album, ReplayGain from tags
  and from the beets database (track and album), seek, position updates,
  output through PipeWire. An automated check renders a transition into an
  `appsink` and verifies there's no gap and the gain changes at the right
  sample.
  *Done when:* the automated check passes, a listening test on a few
  known gapless releases is clean, and CPU and wake-ups while playing
  (window shown and hidden) are recorded.
  *Decides:* ReplayGain source and mechanism, threading model of the engine.
- **S3 — beets as the only backend.** On the dev copy: time to load and
  group the whole library and typical queries, and to build the recording
  index; the memory of the whole library as beets `Item`s against a compact
  index (see "Design rules"), and whether the compact index can be built
  through beets' API or needs a narrower query; per-thread connections; loading the configured plugins without
  side effects; writing flexible attributes while a `beet modify`/`mbsync`
  runs on the same copy; reading `FMPS_RATING` through a field registered
  by the roquette plugin (for M9's import, see there); reading
  `roquette:` settings through `beets.config`; the `playlist` plugin's
  M3U path handling.
  *Done when:* numbers and the locking behaviour are recorded.
  *Decides:* the library layer's structure and caching, the write strategy.

Order: S1 first (it decides the toolkit and the event loop S2 runs under),
then S2, the riskiest functionally, then S3.

### Phase 2 — Core, with tests

From here on every milestone ships with tests.

- **M1 — Library layer.** Config discovery (`BEETSDIR`, `--beets-config`,
  default), the read-only guard, queries with sort, the release/medium/track
  grouping (releases by `album_id`, singletons in their own group, artist
  sort names), the recording index with version ranking, incremental
  search.
- **M2 — Playback engine.** Hardened S2: state machine, gapless, ReplayGain
  modes, errors (missing file, unsupported format: skip and report),
  output device selection, events.
- **M3 — Live playlist.** Entries are recordings with an optional pinned
  version; append, insert, move, remove, play next; current track; repeat
  (off/track/playlist) and shuffle (tracks, releases); the ReplayGain mode
  decision; next track resolved ahead for gapless; persisted as M3U8.
  Version choice is in the model, but defaults to "the item that was
  added" until M11.
- **M4 — Settings and state.** The `roquette:` config section with defaults
  from the beets plugin and validation, the UI state file. Settings are
  edited by hand in the beets config for now; a settings dialog would have
  to rewrite the YAML preserving comments (`ruamel.yaml` round-trip), which
  is decided when there is one.

### Phase 3 — Minimum viable player

- **M5 — Main window.** The layout, the module host and module API
  (context, actions, lazy loading), the transport bar wired to the engine.
  A smoke test starts the window under Xvfb in CI (in the jobs for pull
  requests, `master` and weekly).
- **M6 — Tree module.** The collection tree with beets-query search,
  adding to the playlist (double-click, context menu, drag and drop).
- **M7 — Playlist pane, basic.** One line per track, reordering, removing,
  current track marker, jump to track.
- **M8 — Desktop integration.** MPRIS (media keys, Plasma's media
  controls), single instance, `.desktop` file, icon, AppStream metadata;
  CI validates the `.desktop` file and the metadata.

**Milestone: MVP** — browse, queue and play music, gapless and with
ReplayGain, within the budgets, including the 8-hour run.

### Phase 4 — Minimum lovable player

- **M9 — Ratings and play statistics.** The one-time import of the
  `FMPS_RATING` tags into beets, run on the copy first and checked against
  the tags. Starting point is `/usr/local/bin/sync-fmps-rating.sh`
  (`metaflac` per FLAC file, `beet modify` per rated track, matched by
  `mb_releasetrackid`). Run against the copy with `BEETSDIR=dev/beets`
  and `--nowrite`. Its limits: FLAC only (the 24 AAC/MP3 tracks are
  skipped), one `beet` process per track (slow at ~11k files), and a
  rating removed from a file isn't cleared in beets. If those matter, it
  becomes a `beet` command in the roquette plugin that reads all formats
  through the `FMPS_RATING` field mapping in one process, with a dry
  run. Rating from the
  transport bar and the playlist. `play_count`, `skip_count`,
  `last_played` updated in beets: a play counts at ≥ 50% or ≥ 4 minutes
  (the scrobbling rule), a skip is leaving a track before 50% (after a
  minimum, so skimming isn't a skip). Only with
  `roquette.library.writable`.
- **M10 — Playlist pane, Amarok-style.** Grouped by release: a header with
  cover, release, album artist and year, then multi-line track rows with
  rating, length and the track artist where it differs; editable ratings;
  play-count/last-played hints.
- **M11 — Recording versions (experiment).** The dropdown of versions in
  the playlist, the "best version" rule, pinning, and the edge cases from
  "Recording versions". Then the same grouping in the tree. Ends with a
  decision record: what stays, what changes, what's dropped.
- **M12 — Browse module.** The emby-like browser: album artist and release
  grids with covers (freedesktop thumbnails, bounded cache; the "after
  browsing" budget is the done-criterion), `albumtype` sections, sorting
  and filtering, a release page with tracklist, disc titles and details,
  recordings grouped as decided in M11.
- **M13 — Playlists module.** M3U playlists in beets' `playlist_dir`
  (paths relative to the library, as the `playlist` plugin expects, so
  `beet ls playlist:name` works and beets keeps them current when files
  move), `smartplaylist` queries evaluated live, tabs, saving and loading
  the live playlist.
- **M14 — Scrobbling.** ListenBrainz (the token from the `listenbrainz`
  plugin's config; now-playing and listens, with MusicBrainz IDs) and
  Last.fm (desktop auth, session key in the Secret Service), both through
  the outbox so offline listens are submitted later. Tests against fakes
  only; the development config never has real credentials.

**Milestone: main player** — roquette replaces whatever you use today.

### Phase 5 — Modules for music geeks

One milestone each, in an order decided at the time:

- **Details**, **Lyrics** (cheap, local data only).
- **Related**: local relations first (artist, album artist, release group,
  work), then MusicBrainz relationships through the API (1 request/s,
  cached, identified with a proper User-Agent).
- **Wikipedia** via MusicBrainz URL relations and Wikidata, in a
  configurable language.
- **Statistics** from the beets fields and ListenBrainz.
- **Credits**: composer, lyricist, arranger, remixer, performers from
  MusicBrainz.

### Backlog

- Performance tracking in CI with Bencher, following ren's design
  (`docs/decisions/0019-ci.md` there, "Performance tracking"): memory
  under Xvfb from `just measure` per pull request, with the budgets as
  thresholds; time and CPU from runs on the desktop. After the MVP, and
  after ren has tried it out.
- A settings dialog (see M4).
- ListenBrainz recommendations and "fresh releases" for artists in the
  library.
- Synced (timed) lyrics.
- Several beets libraries; a second machine with the music synced via
  Nextcloud (works already if beets works there).
- `FMPS_RATING` support in mediafile itself instead of the roquette
  plugin's field mapping.
- Equalizer, crossfade (off by default; gapless takes precedence).

## Conventions

- Keep semantically different changes in separate commits.
- `just check` passes before each commit.
- No virtual environment and no installation for development: roquette
  runs from the checkout (`python3 -m roquette`), and beets finds the
  roquette plugin through `pluginpath:` in the dev config, which
  `just dev-library` points at the repository's `beetsplug/`. The
  dependencies and tools are system packages. Locally, `just check` skips
  a tool that isn't installed, with a note, and CI runs all of them.
- Cloud sessions (Claude Code on the web) are set up like CI: a
  throwaway Ubuntu container where `scripts/setup-ci.sh` (`just
  setup-ci`) installs GStreamer and PyGObject's system libraries with apt
  and the Python dependencies with pip. CI runs the same script, so the
  two can't drift. On Ubuntu's own Python (PEP 668) the script installs
  into a venv inside the container. The cloud environment runs it when
  a session starts (its setup script, or a `SessionStart` hook limited
  to remote sessions; M0 picks one). Without the dev library, music, a
  display or a sound card, cloud sessions suit work covered by the tests
  (library layer, queue, settings, CI). Spikes, listening tests,
  `just measure` and anything against the dev library run locally.
- In CI, pip installs into the runner's Python (thrown away after each
  run). Runtime dependencies are ranges in `pyproject.toml` (no lock
  file); the development tools (ruff, mypy, pytest, …) are pinned exactly
  in the `dev` dependency group (`pip install --group dev`), so their
  findings don't change between runs. The oldest supported versions are
  tested through a pip constraints file (`ci/oldest.txt`) with the
  minimum of each range.
- CI (`.github/workflows/`) is split by when the checks run, and its jobs
  run the `justfile`'s recipes, so `just check` runs what CI runs:
  - `general.yml`, on every push: REUSE, typos, EditorConfig, shellcheck
    (`scripts/`), zizmor (the workflows); from M8 the `.desktop` file and
    the AppStream metadata.
  - `dependencies.yml`, on every push and weekly: the dependencies'
    licences (`pip-licenses`, the list from "Licensing"), security
    advisories (`pip-audit`), unused, missing and transitive-only
    dependencies (`deptry`).
  - `code-tests.yml`, on every push and weekly: ruff check, ruff format,
    mypy, pytest with GStreamer from the runner's packages, for the
    oldest supported Python (3.12) and the newest the dependencies
    support (3.14, as beets 2.14 requires < 3.15), and for the oldest
    supported beets 2.x and the newest; coverage and JUnit results to
    Codecov (OIDC, tokenless for forks); building the sdist and wheel and
    checking them (`twine check`, `validate-pyproject`).
  - From M5, jobs that need a display (the Xvfb smoke test) run for pull
    requests, `master`, weekly and on demand.

  Keep it green. Actions are pinned by commit, jobs ask for their
  permissions (`permissions: {}` at the top), checkouts don't keep
  credentials, a new push cancels the branch's previous run (not on
  `master`), scheduled runs skip forks, and zizmor must pass. Dependabot
  updates the actions and the pinned development tools weekly, with a
  7-day cooldown; the runtime ranges are only raised when needed
  (`increase-if-necessary`). A new check gets a recipe of its own, which CI runs.
- Each milestone is developed on a branch with a draft pull request,
  opened with the first push so CI runs on it, and marked ready when the
  milestone is done.
- Task automation goes into the `justfile`; longer scripts into `scripts/`.
- Prefer small, maintained dependencies; justify heavy ones in the commit
  message, including what they cost at startup and in RSS. Before adding
  one, check whether beets or a beets plugin already covers it.
- Milestone summaries include a `just measure` run, compared with the
  previous milestone; a regression against the budgets is either fixed or
  explained.

## Licensing

The project stays **MIT**, like beets, and follows the
[REUSE specification](https://reuse.software/) from M0 on.

- roquette doesn't bundle Slint; it's a dependency installed alongside.
  MIT code is compatible with Slint under either of its licences: GPLv3
  (the combination is then GPLv3, as for any GPL-compatible code) or the
  royalty-free licence for desktop applications (which asks for
  attribution, e.g. Slint's `AboutSlint` element in the about dialog).
  The about dialog shows the attribution either way.
- The fallback, PySide6, is LGPL; PyGObject and GStreamer are LGPL;
  dbus-fast, keyring, aiohttp/httpx are MIT/Apache/BSD. All fine with MIT.
- beets brings **mutagen** (through mediafile) and **Unidecode**, which
  are GPL-2.0-or-later: beets, MIT itself, is in the same position. As
  roquette doesn't bundle its dependencies, its own code stays MIT; the
  running program is under the GPL's terms, as with Slint's GPLv3.
- Allowed licences for dependencies (`just licenses`, in CI): MIT, BSD,
  Apache-2.0, ISC, MPL-2.0, LGPL, PSF, Zlib, Unlicense, CC0, and
  GPL-2.0-or-later / GPL-3.0-or-later. Not allowed: GPL-2.0-only (it
  can't be combined with GPLv3 code such as Slint's, or with Apache-2.0),
  proprietary or non-commercial licences, and anything unclear.
- ren's `.slint` components are GPL-3.0-or-later. Copies taken into
  roquette are relicensed as MIT by their copyright holder (Heinz, the
  sole author) in the commit that adds them, with a note where they came
  from.
- If a dependency ever forces it, switching to GPL-3.0-or-later (as in
  ren) is the fallback.

Every file carries SPDX headers (comment syntax adapted to the file type):

```
# SPDX-FileCopyrightText: Copyright 2017-2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT
```

Trivial files (build and tool configuration, CI workflows) use `CC0-1.0`,
as in ren. Files that can't carry a header are annotated in `REUSE.toml`.
`reuse lint` must pass before each commit. The 2017 icons are dropped
with the old code (or their origin checked and annotated, if they come
back).
