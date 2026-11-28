<!--
SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
SPDX-License-Identifier: MIT
-->

# 0005 — The live playlist (M3)

**Status:** proposed (2026-11-27).

## Context

M3 builds the live playlist (`docs/PLAN.md`, M3): entries that are
recordings with an optional pinned version; append, insert, move,
remove, play next; the current entry; repeat and shuffle; the
ReplayGain mode decision; the next track resolved ahead for gapless
playback, as the engine's provider (`Upcoming`, told of changes through
`next_changed()`, `docs/decisions/0004-playback-engine.md`); Stop and
"stop after the current track"; persisted as M3U8.

M3 was built in a cloud session, without the dev library, music or a
sound card. The tests play generated FLAC files through the live
playlist and the real engine into an `appsink`; the window's part is
tested with a stand-in for the Slint component, as before.

## What was built

- **`roquette/queue/playlist.py`**, the model, plain Python: entries,
  the play order, the current entry, repeat, shuffle, "stop after", and
  the "auto" ReplayGain rule. Nothing of the engine or the library.
- **`roquette/queue/live.py`**, `LivePlaylist`: the model played by the
  engine. It resolves the next sources ahead, implements `Upcoming`,
  owns the current entry, and has the transport's operations (play,
  play/pause, Stop, next, previous).
- **`roquette/queue/m3u.py`**: the M3U8 file, read and written, and
  `PlaylistFile`, which writes it a little after each change.
- **`roquette/library/index.py`**: `PlaylistItem`, what an entry keeps
  of its track (id, release, disc, track number, path), read with one
  narrow query, by id or by path.
- **The window** plays the live playlist instead of S2's list, and the
  playlist pane shows it instead of S1's made-up entries.

## Decisions

### Entries are recordings

An entry is the item that was added and whether it's pinned to it
(`Entry(item, pinned)`). Tracks queued as part of a whole release (a
double click on a track adds its release, a cover its release) are
pinned; tracks added on their own (the track menu, drops from the
track list, `--play`) aren't. Which version plays is
`Playlist.version(entry)`: the item that was added, until M11 puts the
best-version rule there for entries that aren't pinned. The recording
an entry stands for is the item's `mb_trackid`, through the recording
index (M1); nothing else is stored for it.

An entry compares by identity: the same track added twice is two
entries.

### The play order

- Without shuffle, the playlist's order. With shuffle, an order of its
  own, made once when shuffle is turned on: the current entry first (by
  releases: its release first), then the rest at random. By releases,
  the runs of entries of the same release (in the playlist's order) are
  shuffled as blocks and keep their order inside.
- Entries added while shuffled go to random places after the current
  entry (by releases, between runs, not inside the current one's).
  Moving entries in the playlist doesn't change the shuffled order.
  "Play next" puts entries right after the current one in both orders.
- Repeat off, track or playlist. With a repeated playlist the shuffled
  order comes round again as it is, not reshuffled. Next goes to the
  next entry when the track repeats, and stops at the end unless the
  playlist repeats.

### The current entry

The live playlist owns it, not the engine (whose `stop()` forgets its
source):

- Play sets it right away; when a track change reaches the sink, the
  engine's `on_started` names the source, and through it the entry,
  which becomes current.
- **Stop** stops the engine and keeps the current entry; Play plays it
  from its start.
- **The end**: when the engine finishes (nothing after the last track,
  repeat off), there is no current entry; Play starts from the first
  one.
- **"Stop after the current track"** ends the plan at the current
  entry, so the engine finishes at the next track boundary; the entry
  that would have played becomes current, and the flag is cleared. A
  next track already queued is dropped by the engine as stale (M2).
  Stop clears the flag too.
- **Removing the current entry** keeps it current and playing,
  detached: what follows it is what followed it where it was. When
  nothing plays it, what followed it becomes current.
- Next and previous while stopped move the current entry without
  playing; playing or paused, they play (paused stays paused). Previous
  goes back to the start of the track after 3 s, and at the first one.

### The plan: what plays next, resolved ahead

The engine asks for the source after another on GStreamer's streaming
threads (`about-to-finish`), so the answer can't wait for the event
loop or the library. The live playlist keeps the sources of the
**two** entries after the current one ready (their files and
ReplayGain read from beets beforehand, `library.playables`) and
publishes them as a dictionary from each source to the next. It's
replaced whole on the event loop, never changed, so the streaming
threads read it without a lock. Two, because the engine asks for the
one after the next source when the next file is short (it's read to
its end before it's audible); after that, on_started has moved the
plan on.

- After each change of the plan, `next_changed()` tells the engine.
  A change that doesn't change the plan (an entry appended further on)
  doesn't.
- The engine compares sources by identity, so the plan keeps a source
  it published while it's still what follows (same entry, same file,
  same mode): a change further on doesn't make the engine drop the next
  one it may have queued. An entry that comes twice in the plan (repeat
  track, a playlist of one repeated) gets a second source, so the same
  source is never in the engine's chain twice.
- While a file is being looked up, the plan ends before its entry; the
  engine then finds nothing and queues the source late when told,
  which is gapless while the audible file hasn't all passed the filter.
- Tracks that aren't in the library (any more) are skipped. After an
  outside change of the library, the entries' items are read again: a
  moved file is followed, a deleted track skipped, a returned one played
  again.
- A failing look-up (a query error) is logged and asked again with the
  next change, not in a loop.
- The sources are a subclass of the engine's (`QueuedSource`) that
  carries the entry and the track's data for the now-playing display.

### The ReplayGain mode ("auto")

Album gain for an entry next to another track of its release **in the
play order**, in the release's order (by disc and track number: the
next track, or a later one with tracks left out); track gain otherwise.
So a release played in order (also with shuffle by releases) gets album
gain, shuffled tracks and mixed playlists track gain, and a track
played twice in a row track gain. The playing track keeps the gain it
started with: a change around it only changes the tracks to come.

### The M3U8 file

`$XDG_STATE_HOME/roquette/playlist.m3u8`, absolute paths so other
players read it, and a comment line before each path that they ignore:

```
#EXTM3U
#ROQUETTE:item=1234 pinned current
/mnt/media/Music/…/04 Wish You Were Here.flac
```

The item id finds a file that has moved while roquette wasn't running
(`beet mv`); an entry is its item while that's still its file, else the
file's item, else the item (moved). Paths that aren't UTF-8 are kept
byte for byte (surrogate escapes). Files beets doesn't know are left out
when it's loaded.

Written 2 s after a change (changes in a row once), on a thread of its
own, atomically; at the end synchronously. A file that can't be read at
start isn't written over in that session. The paths are kept in the
entries (`PlaylistItem.path`) so the last write needs no library.

Not in the file: repeat and shuffle (the M4 state file), the shuffled
order, the position in the current track.

### The window, until M5–M7

- A double click on a track adds its release (pinned) and plays it from
  the track; on a cover, the release from its start. Before, it replaced
  what played; with the playlist kept, appending loses nothing. M6
  decides what the tree does.
- The track menu appends or plays next; drops append (a release dragged
  from the cover grid pinned).
- `--play QUERY` replaces the live playlist. `--repeat` and `--shuffle`
  stand in for the settings (M4) and the transport bar's controls (M5);
  Stop and "stop after the current track" have no control yet (M5).
- The pane shows the entries in runs of a release (singletons of an
  artist together) with the playing marker. It's rebuilt whole after
  each change, with the text of every entry read once and kept.
- `just measure` keeps the live playlist of its runs in its output
  directory, not the user's.

## Measurements

The live playlist with the whole fake library queued (11,800 entries),
in the container:

| what | |
|---|---|
| the entries (items read with one query: 58 ms) | 3.7 MiB, 327 B an entry |
| the pane's text for them | 4.7 MiB, 421 B an entry |
| the pane rebuilt | 24 ms (12,756 rows) |
| shuffle by releases turned on | 2 ms |
| what follows an entry (shuffled) | 14 µs |
| the file made (1.3 MiB) | 12 ms |
| the file's entries found by path | 241 ms |

`SCENARIOS="startup idle" just measure femtovg software` under Xvfb on
the fake library, M2 (`master`) and M3 in the same container (startup
the median of 5 runs, in ms from starting `python3`; idle after 5 s,
for 30 s). M3 once with an empty live playlist, once with the whole
library in it (restored at every start):

| variant | what | window created | library loaded | RSS MiB | PSS MiB | threads | CPU % |
|---|---|---|---|---|---|---|---|
| femtovg | M2 | 349.2 | 509.4 | 244.0 | 221.1 | 16 | 0.0 |
| femtovg | M3 | 272.6 | 396.1 | 235.8 | 211.7 | 17 | 0.0 |
| femtovg | M3, 11,800 entries | 285.0 | 406.0 | 265.9 | 241.8 | 17 | 0.0 |
| software | M2 | 294.8 | 385.4 | 119.1 | 113.8 | 7 | 0.0 |
| software | M3 | 277.0 | 378.2 | 117.1 | 111.8 | 8 | 0.0 |
| software | M3, 11,800 entries | 248.0 | 346.6 | 139.1 | 133.8 | 8 | 0.0 |

(The floors: 181.4 / 181.9 MiB with FemtoVG, 85.3 / 85.6 with the
software renderer.)

- With an empty live playlist, M3 is M2 within the runs' spread; the
  one more thread is the live playlist file's, idle after the start.
  M2's 349 ms to the window (FemtoVG) is one slow set of runs; its
  other variants are where M3's are.
- The whole library in the live playlist costs about 22 MiB (30 with
  FemtoVG): the 8.4 MiB of entries and text above, the pane's 12,756
  rows, and what loading them (the file, the look-ups by path and by id,
  the text) leaves behind. The startup doesn't change, as the playlist
  is restored after the window is filled. A playlist of a few hundred
  entries costs well under a megabyte; the whole library in it is M7's
  case for keeping only the rows on screen.
- The playing scenario needs the dev library's music, on the desktop.

## Consequences

- **M4**: repeat and shuffle in the state file; the ReplayGain settings
  reach `LivePlaylist` as now through the window.
- **M5**: the transport bar gets Stop and "stop after the current
  track" (`LivePlaylist.stop()`, `stop_after`), and repeat and shuffle.
- **M6**: what a double click and the menus do in the tree.
- **M7**: the pane only rebuilds what changed, keeps the text of the
  rows on screen rather than of every entry, and edits the live
  playlist (move, remove, jump).
- **M11**: `Playlist.version()` picks the best version for entries
  that aren't pinned; the pane shows the versions.
- Open issues:
  - The playing track's gain doesn't follow a change next to it: a
    track playing alone with track gain, followed by the next track of
    its release appended while it plays, changes to album gain at the
    boundary.
  - Turning repeat off or "stop after" on while the repeated (or next)
    track has already passed the filter, in the last second or so, lets
    a moment of it be heard (the engine's stale stream, M2).
  - Shuffle isn't reshuffled when a repeated playlist comes round.
