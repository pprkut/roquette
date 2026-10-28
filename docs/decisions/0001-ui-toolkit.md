<!--
SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
SPDX-License-Identifier: MIT
-->

# 0001 — UI toolkit: Slint from Python (spike S1)

**Status:** proposed. Built and measured in a cloud container under Xvfb;
the measurements on the desktop (KDE) are still to be run, and the
budgets below are to be confirmed with them.

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

### Not checked

- Drag and drop with other applications (files from Dolphin): Xvfb has
  no drag source. To check on the desktop.
- Wayland, HiDPI scaling (the covers are decoded for the window's scale
  factor, untested above 1), following the desktop's colour scheme and
  accent.
- Accessibility (AccessKit).
- PySide6: not needed, S1 doesn't fall back.

## Measurements

### In the cloud container, under Xvfb

Run with `just measure` at 2ef2c6c, on the fake library
(`scripts/fake-library.py`: 11,800 tracks, 800 releases, generated
1000 px JPEG covers):

- CPU: Intel Xeon @ 2.10GHz (4 cores of a VM); Linux 6.18; Xvfb at
  1600×1000, no window manager, no compositor, no D-Bus session; the GL
  renderers run on Mesa's llvmpipe (OpenGL on the CPU)
- Python 3.13.17, slint 1.18.1b1, beets 2.14.1
- idle: 30 s after 5 s settling; startup: median of 5 runs, from starting
  `python3`; the window is 1280×800 logical pixels at scale 1
- scrolling: the tracks at 8,000 px/s, ten times the tracks at
  80,000 px/s (the same 38 seconds), the covers at 2,000 px/s; each
  scroll ends with 3 s to settle, and "after" is the last sample then

Startup (ms from starting `python3`):

| renderer | what | event loop running | window created | library loaded |
|---|---|---|---|---|
| software | floor | 299.8 | 298.9 | - |
| software | window | 344.2 | 355.0 | 438.6 |
| software | window, 10× | 337.5 | 347.3 | 467.1 |
| femtovg | floor | 322.1 | 321.2 | - |
| femtovg | window | 330.9 | 341.0 | 582.1 |
| skia | floor | 305.4 | 304.6 | - |
| skia | window | 319.2 | 329.4 | 504.2 |

Idle:

| renderer | what | RSS MiB | PSS MiB | anon / file / shmem MiB | threads | CPU % |
|---|---|---|---|---|---|---|
| software | floor | 88.9 | 83.6 | 41.2 / 43.7 / 3.9 | 4 | 0.0 |
| software | window | 127.7 | 122.3 | 74.5 / 49.3 / 3.9 | 4 | 0.0 |
| software | window, 10× | 129.7 | 124.3 | 76.4 / 49.4 / 3.9 | 4 | 0.0 |
| femtovg | floor | 178.5 | 154.3 | 78.8 / 95.8 / 3.9 | 13 | 0.0 |
| femtovg | window | 244.9 | 220.7 | 129.9 / 111.0 / 3.9 | 13 | 0.0 |
| skia | floor | 164.2 | 142.1 | 64.5 / 95.8 / 3.9 | 13 | 0.0 |
| skia | window | 247.6 | 225.5 | 125.6 / 118.1 / 3.9 | 13 | 0.0 |

Scrolling (CPU % of one core; the cover worker's decoding included):

| renderer | what | CPU % | fps (median / min) | peak RSS MiB | RSS after MiB |
|---|---|---|---|---|---|
| software | tracks | 55.3 | 62 / 57 | 129.4 | 129.4 |
| software | tracks, 10× | 89.2 | 49 / 38 | 129.7 | 129.7 |
| software | covers, cold thumbnail cache | 118.3 | 62 / 60 | 158.4 | 158.4 |
| software | covers, warm thumbnail cache | 40.9 | 61 / 60 | 155.1 | 155.1 |
| femtovg | tracks | 127.4 | 58 / 46 | 248.7 | 248.7 |
| femtovg | covers, warm thumbnail cache | 129.2 | 61 / 58 | 280.4 | 280.4 |
| skia | tracks | 90.4 | 62 / 56 | 251.5 | 251.5 |
| skia | covers, warm thumbnail cache | 79.5 | 61 / 52 | 285.5 | 285.5 |

The full summary, with the rows left out here, is in the pull request.
What the scrolls read: the table 11,800 rows for 11,800 tracks (67,452
of 118,000 at 10×, where a frame skips rows), the grid about 4 reads per
cell and one decode per cover (767–774 of the 769 covers; a few more
when a cover was evicted and came back into view).

### Reading the numbers

- **The floor is about 89 MiB RSS** with the software renderer: a bare
  Python is 9 MiB; importing beets adds about 17, slint 30, PyGObject
  with GStreamer initialised and a `playbin3` 22, Pillow 4 (each measured
  on its own; together 62 MiB before any window).
- **The window adds about 39 MiB**, 33 of them anonymous: about 25 MiB
  for the compiled `.slint` files, about 9 for the library index and the
  rest for the window. Ten times the library adds 2 MiB: the index isn't
  repeated, only its order, and rows exist only on screen.
- **Startup**: the window is up about 350 ms after `python3` starts and
  filled at about 440 ms. About 220 ms of it are imports (beets and
  asyncio most), 90–100 ms compiling the `.slint` files. Ten times the
  library costs nothing measurable.
- **Idle CPU is zero** for every renderer: no timers run while nothing
  happens.
- **Scrolling the table** at the benchmark's deliberately high speed
  takes 55 % of a core at 62 fps with the software renderer; the
  ten-times run at ten times the speed builds a whole screen of new rows
  every frame (89 %, 49 fps). Rows cost Python time: each is a dict
  built by `row_data`.
- **Covers**: with a warm thumbnail cache the grid scrolls at 41 % of a
  core including the decoding; a cold cache decodes the 1000 px JPEGs
  and writes thumbnails, 118 % (the worker's core and the UI's).
  Afterwards RSS is idle plus the cover cache's limit (32 MiB) and a
  little: memory doesn't grow with the number of covers seen.
- **The GL renderers** need about twice the memory (Mesa's driver: 50
  more MiB of file-backed pages, 50 more of anonymous memory, nine more
  threads) and, on llvmpipe, more CPU; on a real GPU their CPU figures
  will be lower. ren measured the same picture on the desktop (0001
  there: 42 MiB software, about 90 MiB with a GPU renderer).
- Under Xvfb there's no compositor, no HiDPI and nothing shared with
  other Qt or GL programs, so these figures are for comparing, not the
  desktop's.

### On the desktop

Still to run, with the development copy of the library:

```
just dev-library   # if dev/beets doesn't exist yet
just measure software femtovg skia
```

It takes about 15 minutes; don't touch the window meanwhile. The summary
it prints replaces the numbers above in this record, and the budgets
below are checked against it. A check by hand belongs with it: scrolling
by mouse wheel and scrollbar, dragging files from Dolphin onto the
playlist, following the desktop's colour scheme, and a look on Wayland
and at HiDPI if at hand.

## Budgets

Proposed, for the dev library on the desktop with the software renderer,
to be confirmed by the desktop measurements (they replace "Budgets" in
the plan's "Performance" once accepted):

- **Floor**: about 90 MiB RSS. It's fixed by Python, beets, Slint and
  GStreamer; the budgets below are what roquette adds to it.
- **Idle** (window open, paused, a module showing): under 150 MiB RSS,
  as planned: S1's window is at 128, which leaves about 20 MiB for what
  the real modules, the live playlist, MPRIS and scrobbling add. 0 % CPU.
- **After browsing** every release: idle plus the cover cache's limit
  (32 MiB by default) plus at most 10 MiB.
- **Ten times the library**: idle within 10 MiB of the library's.
- **Startup**: the window under 400 ms from starting `python3`, filled
  under 500 ms, on a warm cache; the plan's "under a second" stays the
  hard limit.
- **Scrolling**: smooth (60 fps on a 60 Hz screen) through the table and
  the grid at normal speeds; CPU is recorded at every milestone, not
  budgeted.

## Decision

- **Slint from Python: yes**, if the desktop measurements confirm the
  container's. Every part of the window could be built and every check
  passed, with workarounds that are local and understood (lazy models,
  a model per grid row, no Slint calls from workers). None of them is a
  reason to fall back to PySide6, which therefore wasn't built.
- **Renderer: software**, as in ren: half the memory of the GL renderers,
  no GPU driver loaded, and 60 fps where it was measured. FemtoVG and
  Skia stay selectable through `SLINT_BACKEND`.
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
- Open issues:
  - The desktop measurements and the checks by hand (above).
  - Python's cyclic garbage collector running on a worker thread could
    still release Slint objects there; if it shows, collect only on the
    event loop's thread.
  - `row_data` builds a dict per row; if the table's scrolling CPU
    matters, cache rows or return NamedTuples.
  - The thumbnail cache's "fail" entries (covers that can't be read)
    aren't written; they're tried again at every start.
  - Accessibility (AccessKit) to measure and enable by M8, as in ren.
