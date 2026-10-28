<!--
SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
SPDX-License-Identifier: MIT
-->

# 0001 — UI toolkit: Slint from Python (spike S1)

**Status:** accepted (2026-10-28).

## Context

`docs/PLAN.md` picks Slint's Python bindings as the UI toolkit,
provisionally: they are a beta (`slint` 1.18.1b1 on PyPI), and roquette
needs beets in-process, so the UI has to be driven from Python. S1 checks
whether they carry the window from "UI concept" and what it costs, and
measures the floor every later budget builds on. PySide6 + QML is the
fallback.

## What was built

`python3 -m roquette` (`just run`) opens the window from "UI concept" on
the beets library of `$BEETSDIR`:

- **Navigation** with two modules, **Tracks** and **Covers**. Only the
  module shown exists; the cover module and its model are created the
  first time it's shown.
- **Tracks**: every track of the library in a table with sortable and
  resizable columns (ren's item table), through a custom `slint.Model`
  whose rows are built when the list asks for them from a compact index:
  one narrow query per table, tuples with interned strings, no beets
  `Item`s (`roquette/library/index.py`). `--scale N` shows the library N
  times over.
- **Covers**: a grid of every release's cover. Covers are decoded and
  scaled to their display size (in physical pixels) with Pillow on a
  worker thread at low priority, newest request first; requests for
  covers that scrolled away before their turn are dropped. They come
  from the shared freedesktop thumbnail cache ("large", 256 px), which is
  filled where it has no current thumbnail. Decoded covers are kept in a
  cache bounded by size (32 MiB by default, `--cover-cache`).
- **Playlist pane**: made-up entries with multi-line rows of three
  heights (release headers, tracks, versions); a track whose recording
  has several versions expands to list them, and a version can be chosen.
  The layout is a plain view model (`roquette/queue/pane.py`).
- **Transport bar**: now playing, play controls, seek bar and volume; play
  counts the position up four times a second from an asyncio task.
- Tracks and releases can be **dragged** onto the playlist pane, or added
  with a double click, Return or the context menu.
- **Keyboard**: arrow keys, Page Up/Down, Home and End in the table, the
  grid, the navigation and the playlist (Left/Right collapse and expand
  versions); Tab moves between them.
- **Colour scheme**: the desktop's, or `--color-scheme light|dark`.
- The library index is read and GStreamer is initialised (a `playbin3` is
  created) on worker threads after the window is up.

ren's theme, splitter and item table were taken over (relicensed as MIT)
with small changes. For measuring: `--measure` (startup timings),
`--floor` (an empty window with Python, beets, Slint and GStreamer
loaded), `--autoscroll tracks|covers` and `scripts/measure.sh` (`just
measure`). `scripts/fake-library.py` (`just fake-library`) makes a fake
beets library of the dev library's size with generated covers, for
places without the dev library: CI, cloud sessions, and these
measurements.

Screenshots (Xvfb, fake library): `0001/tracks-light.webp`,
`0001/covers-light.webp`, `0001/tracks-dark.webp`.

## Findings

### Worked as hoped

- **Installation**: the wheel (abi3, manylinux_2_35) installs with pip
  and has the software, FemtoVG and Skia renderers built in, no Qt
  backend (so no native Qt style). Importing it needs `libinput` and,
  for X11, `libxkbcommon-x11`, which CI installs.
- **asyncio**: `slint.run_event_loop()` runs an asyncio loop on Slint's
  event loop. Tasks, `asyncio.sleep`, timers and sockets (an asyncio
  server and client on loopback) work, so aiohttp and httpx would too;
  the choice between them doesn't depend on the toolkit and is left to
  their first use (M14).
- **Worker threads** keep running while the window renders: Slint
  releases the GIL in its event loop, idle or scrolling (a counting
  thread ran at the same speed in both).
- **Custom models** are lazy: scrolling the whole table built each row
  once (11,800 rows read for 11,800 tracks); at 10× (118,000 rows) memory
  and startup barely change (see "Measurements").
- **Rows of different heights** in a `ListView` (60, 42 and 26 px in the
  playlist pane).
- **Images from Python**: `slint.Image.load_from_array()` takes a
  `memoryview` of RGB bytes; no numpy needed.
- **Drag and drop** inside the window: `DragArea` and `DropArea` (new in
  Slint 1.18) carry a `DataTransfer` whose `user_data` can be any Python
  object; the data is asked for only when a drag starts.
- **ren's components** needed only renames: `viewport-*` of `ListView` is
  deprecated for `content-*`, and properties called `row` or `alignment`
  clash with the base elements'.
- **Keyboard focus**: `FocusScope`s get the keys, Tab moves focus along
  them (table, playlist, the transport bar's sliders).
- **Colour scheme**: forcing it through `Palette.color-scheme` works;
  following the desktop (the XDG portal) couldn't be checked without a
  D-Bus session.
- Context menus (`ContextMenuArea`) work as in ren.

### Needed workarounds

- **Assigning a model reads all of it.** Setting a property to a Python
  model calls `row_data` for every row once (118,000 calls at 10×).
  Models are given to Slint empty and filled afterwards with
  `notify_row_added`, after which only the rows on screen are read
  (`LazyModel` in `roquette/ui/models.py`).
- **`model[index]` in a binding depends on the whole model.** The first
  grid was a flat model of cells indexed from a list of rows; every
  loaded cover then re-read every cell on screen (6,364 reads for 800
  cells, and covers loaded twice). The grid's model now has one row per
  grid row, so a cover updates only its row.
- **Arrays inside structs must be models** (`slint.ListModel`): a Python
  list fails to convert, and like other conversion errors in callbacks
  and `row_data` it is only printed, the value ends up empty.
- **Worker threads must not call into Slint.** pyo3 releases the Python
  objects Slint dropped without the GIL the next time any thread enters
  the native module. When that was the cover worker calling
  `call_soon_threadsafe` (which `run_in_executor` uses too), Slint's
  models were released on the wrong thread: `RuntimeError: PyModelBase
  is unsendable, but is being dropped on another thread`. Workers now
  queue their results and wake the loop through a pipe it watches
  (`roquette/threads.py`); the event loop alone talks to Slint. Worth
  reporting upstream. Python's cyclic garbage collector, which runs on
  whichever thread allocates, could in theory do the same; not seen.
- **The `.slint` files are compiled at every start**, as the Python
  bindings have no ahead-of-time compilation: about 90–100 ms, and about
  25 MiB of anonymous memory that stays as long as the window exists,
  whatever the widget style. It's the largest single item of the window's
  memory.
- **Async callbacks** (`@slint.callback` on a coroutine) need Python
  3.13; roquette supports 3.12, so callbacks start tasks themselves.
- No rendering notifier in Python: startup is measured until the event
  loop runs and the window is created, not until the first frame.
- A module in an `if` loses its state (scroll position) when hidden;
  state that should survive belongs in the view models (M5).

### Found along the way (for S3 and M1)

- Opening a `beets.library.Library` migrates the database to the
  installed beets' schema, which writes to it. S1 therefore refuses beets
  data whose config doesn't set `roquette.library.writable` (the copies
  of `just dev-library` and `scripts/fake-library.py`); M1's read-only
  guard has to open the live database without beets' migrations.
- beets 2.14 stores paths relative to the music directory and resolves
  them through a context variable that new threads don't inherit; a
  worker reading paths needs `Library.music_dir_context()`.

### Found on the desktop

- **Decoding covers left memory behind.** Scrolling once through the
  dev library's covers with a cold thumbnail cache left RSS about 86 MiB
  above idle plus the cover cache (271 against 185 MiB with a warm
  cache). glibc keeps the memory freed by decoding large originals on
  the worker thread for reuse instead of returning it. The cover worker
  now calls `malloc_trim()` whenever it runs out of requests: in the
  container, decoding 120 large covers (1400–3000 px, JPEG and PNG) left
  21–54 MiB behind without it and 2 MiB with it. The desktop figure
  after the fix is still to be measured (next `just measure`).
- **HiDPI**: the desktop runs at a scale factor of 2 (the software
  renderer's 15.6 MiB frame buffer is 2560×1600 pixels). A cell's 160 px
  are then 320 physical pixels, but the "large" thumbnails are 256 px,
  so the covers are drawn upscaled. The plan's "256 px for grids" holds
  only at scale 1; covers need the thumbnail size class for their
  physical size (`x-large`, 512 px, at scale 2). For M12.
- The main window takes about 80 ms from its creation to being shown on
  the desktop, against 10 ms under Xvfb; the empty window of the floor
  doesn't. Probably the system fonts being loaded for the first text;
  to look at in M5.

### Not checked

- Drag and drop with other applications (files from Dolphin), following
  the desktop's colour scheme and accent, Wayland: not checked by hand
  yet.
- Accessibility (AccessKit).
- PySide6: not needed, S1 doesn't fall back.

## Measurements

Run with `just measure` at 87c1ccf on the desktop, with the development
copy of the library (11,849 tracks, 807 releases):

- CPU: 11th Gen Intel Core i7-1185G7 @ 3.00GHz, GPU: Mesa Intel Iris Xe
  (TGL GT2); Linux 7.2.12, KDE on X11, scale factor 2
- Python 3.12.14, slint 1.18.1b1, beets 2.14.1
- idle: 30 s after 5 s settling; startup: median of 5 runs, from starting
  `python3`; the window is 1280×800 logical pixels
- scrolling: the tracks at 8,000 px/s, ten times the tracks at
  80,000 px/s (the same 38 seconds), the covers at 2,000 px/s; each
  scroll ends with 3 s to settle, and "after" is the last sample then

Startup (ms from starting `python3`):

| renderer | what | event loop running | window created | library loaded |
|---|---|---|---|---|
| software | floor | 210.1 | 209.7 | - |
| software | window | 224.0 | 303.8 | 400.3 |
| software | window, 10× | 215.8 | 295.2 | 399.0 |
| femtovg | floor | 232.3 | 231.8 | - |
| femtovg | window | 226.4 | 304.9 | 390.2 |
| skia | floor | 214.3 | 213.9 | - |
| skia | window | 218.7 | 297.6 | 396.2 |

Idle:

| renderer | what | RSS MiB | PSS MiB | anon / file / shmem MiB | threads | CPU % |
|---|---|---|---|---|---|---|
| software | floor | 112.2 | 82.5 | 44.3 / 52.3 / 15.6 | 6 | 0.0 |
| software | window | 151.8 | 121.3 | 80.0 / 56.1 / 15.6 | 6 | 0.0 |
| software | window, 10× | 153.9 | 123.2 | 82.1 / 56.2 / 15.6 | 6 | 0.0 |
| femtovg | floor | 168.4 | 108.3 | 66.0 / 102.4 / 0.0 | 13 | 0.0 |
| femtovg | window | 201.0 | 140.3 | 95.1 / 105.9 / 0.0 | 10 | 0.0 |
| skia | floor | 150.5 | 93.2 | 50.3 / 100.2 / 0.0 | 10 | 0.0 |
| skia | window | 206.3 | 144.1 | 96.1 / 110.2 / 0.0 | 10 | 0.0 |

Scrolling (CPU % of one core; the cover worker's decoding included):

| renderer | what | CPU % | fps (median / min) | peak RSS MiB | RSS after MiB |
|---|---|---|---|---|---|
| software | tracks | 75.8 | 60 / 58 | 160.0 | 160.0 |
| software | tracks, 10× | 91.0 | 36 / 32 | 161.4 | 160.9 |
| software | covers, cold thumbnail cache | 144.3 | 62 / 62 | 271.8 | 270.8 |
| software | covers, warm thumbnail cache | 58.6 | 62 / 62 | 185.9 | 184.9 |
| femtovg | tracks | 48.2 | 60 / 60 | 210.1 | 209.7 |
| femtovg | tracks, 10× | 90.0 | 57 / 55 | 211.0 | 210.5 |
| femtovg | covers, cold thumbnail cache | 115.4 | 60 / 60 | 315.5 | 305.8 |
| femtovg | covers, warm thumbnail cache | 48.6 | 60 / 60 | 237.7 | 236.7 |
| skia | tracks | 50.9 | 60 / 60 | 244.2 | 243.8 |
| skia | tracks, 10× | 91.7 | 56 / 52 | 245.0 | 244.5 |
| skia | covers, cold thumbnail cache | 116.3 | 60 / 60 | 366.3 | –\* |
| skia | covers, warm thumbnail cache | 48.5 | 60 / 60 | 278.2 | 277.1 |

\* The last sample was taken while roquette was exiting and had no
memory figures; `scripts/measure.sh` now skips such samples.

What the scrolls read: the table 11,849 rows for 11,849 tracks (67,732
of 118,490 at 10×, where a frame skips rows); the grid about 2 reads per
cell with a warm cache, and one decode per cover (718–726 with a warm
cache). With a cold cache, covers that scrolled away before the worker
decoded them were dropped (666 decoded with the software renderer), and
covers evicted from the 32 MiB cache and seen again were decoded again
(829–837 with the GL renderers).

The same run in the cloud container under Xvfb (scale 1, the fake
library, llvmpipe for GL; the full summary is in the pull request) gave
the same picture: floor 88.9 MiB, window 127.7 MiB, 10× 129.7 MiB RSS
with the software renderer, about 330 ms to the window.

### Reading the numbers

- **The floor is about 112 MiB RSS, 83 MiB PSS** with the software
  renderer. A bare Python is 9 MiB; importing beets adds about 17,
  slint 30, PyGObject with GStreamer initialised and a `playbin3` 22,
  Pillow 4 (each measured on its own in the container). At scale 2 the
  software renderer's frame buffer, shared with the X server, adds
  15.6 MiB; it grows with the window.
- **The window adds about 40 MiB** (RSS and PSS alike), 36 of them
  anonymous: about 25 MiB for the compiled `.slint` files, about 9 for
  the library index and the rest for the window. Ten times the library
  adds 2 MiB: the index isn't repeated, only its order, and rows exist
  only on screen.
- **Startup**: the window is up about 300 ms after `python3` starts and
  filled at 400 ms. The event loop runs after about 220 ms: the
  interpreter, the imports (beets and asyncio most) and compiling the
  `.slint` files (90–100 ms of 330 in the container). Showing the window
  takes about 80 ms (see "Found on the desktop"), reading the library
  index on the worker about 100. Ten times the library costs nothing
  measurable.
- **Idle CPU is zero** for every renderer: no timers run while nothing
  happens.
- **Scrolling at scale 2** is where the software renderer pays for its
  memory: the table takes 76 % of a core at 60 fps, against 48–51 % with
  FemtoVG or Skia, and the ten-times run (a whole screen of new rows
  every frame) drops it to 36 fps where the GL renderers keep 56–57. The
  grid with a warm thumbnail cache: 59 % against 49 %. The benchmark's
  speeds are deliberately extreme; scrolling by hand is far slower.
- **Covers**: with a warm thumbnail cache, RSS after scrolling every
  cover is idle plus the cover cache (32 MiB): memory doesn't grow with
  the number of covers seen. With a cold cache it was about 86 MiB more,
  fixed since (see "Found on the desktop").
- **The GL renderers** cost about 50 MiB more RSS and 19–23 MiB more PSS
  than the software renderer for the window (Mesa's driver and its
  threads), about as in ren's measurements on the same machine.

## Budgets

For the dev library on the desktop with the software renderer; they
replace the plan's first guesses in "Performance":

- **Floor**: about 112 MiB RSS, 83 MiB PSS, fixed by Python, beets,
  Slint and GStreamer; the budgets below include it.
- **Idle** (window open, paused, a module showing): under 175 MiB RSS and
  145 MiB PSS, 0 % CPU, no wake-ups beyond what the toolkit needs. The
  plan's 150 MB is already taken by S1's window at scale 2 (152 MiB RSS,
  121 PSS); this leaves about 23 MiB for what the real modules, the live
  playlist, MPRIS and scrobbling add. Where to win memory back if
  needed: the compiled `.slint` files (25 MiB), imports made lazily.
- **Playing**: as planned (S2 measures).
- **After browsing** every release: idle plus the cover cache's limit
  (32 MiB by default) plus at most 10 MiB, with a cold thumbnail cache
  too.
- **Ten times the library**: idle within 10 MiB of the library's.
- **Long runs**: RSS flat over an 8-hour playback run, as planned.
- **Startup**: the window under 400 ms from starting `python3`, filled
  under 500 ms, on a warm cache; "under a second" stays the hard limit.
- **Scrolling**: smooth (60 fps on a 60 Hz screen) through the table and
  the grid at the benchmark's speeds (not the ten-times run); CPU is
  recorded at every milestone, not budgeted.

## Decision

- **Slint from Python: yes.** Every part of the window could be built and
  every check passed, with workarounds that are local and understood
  (lazy models, a model per grid row, no Slint calls from workers). None
  of them is a reason to fall back to PySide6, which therefore wasn't
  built.
- **Renderer: software**, as in ren: 50 MiB less RSS and about 20 MiB
  less PSS than FemtoVG or Skia, and no GPU driver loaded, for a program
  that's idle most of the time. The price is CPU while scrolling at
  HiDPI (about 25 percentage points of a core more at the benchmark's
  speed) and frames dropped only in the extreme ten-times scroll.
  FemtoVG and Skia stay selectable through `SLINT_BACKEND`; M5 can make
  the renderer a setting.
- **Threading**: blocking work runs on worker threads, which never call
  into Slint; results reach the event loop through the inbox
  (`roquette/threads.py`), not `call_soon_threadsafe` or
  `run_in_executor`. S2 forwards GStreamer's bus the same way.
- **Models**: given to Slint empty and filled afterwards; one model row
  per repeated element (no `model[index]` in bindings); arrays inside
  structs as `ListModel`s.
- **HTTP**: not decided here. Slint's loop is a working asyncio loop, so
  aiohttp and httpx both fit; M14, their first user, picks by size and
  memory.

## Consequences

- The dependencies are `slint`, `Pillow` and `PyGObject`; CI installs
  the system libraries they need.
- S2 runs its engine under this event loop, with GStreamer's messages
  going through the inbox.
- M1's read-only guard must open the live database without beets'
  migrations (see "Found along the way"); S3 measures beets' API against
  the compact index.
- M5 keeps module state (scroll position, selection) in the view models,
  as modules in an `if` are rebuilt when shown again, and lets hidden
  modules drop their caches.
- To report upstream (Slint): models released on the wrong thread
  through pyo3's deferred reference release; assigning a model reading
  every row; conversion errors that are only printed; the memory of the
  compiled `.slint` files staying alive with the window.
- The plan's "Performance" takes the budgets above. M12 decodes covers
  from the thumbnail size class that fits their physical size.
- Open issues:
  - Measure the cold-cache cover scroll on the desktop again, with the
    `malloc_trim()` fix (next `just measure`).
  - The checks by hand: drag and drop from Dolphin, following the
    desktop's colour scheme, Wayland.
  - About 80 ms between creating and showing the window on the desktop
    (M5).
  - Python's cyclic garbage collector running on a worker thread could
    still release Slint objects there; if it shows, collect only on the
    event loop's thread.
  - `row_data` builds a dict per row; if the table's scrolling CPU
    matters, cache rows or return NamedTuples.
  - The thumbnail cache's "fail" entries (covers that can't be read)
    aren't written; they're tried again at every start.
  - Accessibility (AccessKit) to measure and enable by M8, as in ren.
