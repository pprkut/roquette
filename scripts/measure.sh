#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT
#
# Measures roquette's window for each variant: the floor (an empty window
# with Python, beets, Slint and GStreamer loaded), startup, memory and CPU
# while idle, and CPU, frame rate and memory while scrolling the track
# list (the library and ten times the library) and the cover grid (with
# a cold and a warm thumbnail cache). Needs a graphical session (X11 or
# Wayland); don't touch the window while it runs.
#
# Usage: scripts/measure.sh [VARIANT...]
#   VARIANT: a Slint renderer with the winit backend: software, femtovg,
#   skia (Skia on OpenGL) or skia-software. Default: software femtovg skia.
#
# Environment:
#   BEETSDIR      the beets data to measure with (default dev/beets, the
#                 copy of `just dev-library`; build/fake-library/beets is
#                 made by scripts/fake-library.py)
#   STARTUP_RUNS  number of startup measurements (default 5)
#   SETTLE_SECS   wait after startup before idle sampling starts (default 5)
#   IDLE_SECS     length of the idle measurements (default 30)
#   SCALE         how many times over the library is shown for the large
#                 track list (default 10)
#   TRACK_SPEED   scroll speed through the track list, px/s (default 8000;
#                 the large one is scrolled SCALE times as fast)
#   COVER_SPEED   scroll speed through the cover grid, px/s (default 2000)
#   SCROLL_SECS   longest scroll (default 900); roquette is stopped then
#   OUT_DIR       logs, samples and the thumbnail cache the cover runs use
#                 (default build/measure/<timestamp>)
#
# Prints a Markdown summary, also written to $OUT_DIR/summary.md.

set -euo pipefail
cd "$(dirname "$0")/.."

export BEETSDIR=${BEETSDIR:-dev/beets}
STARTUP_RUNS=${STARTUP_RUNS:-5}
SETTLE_SECS=${SETTLE_SECS:-5}
IDLE_SECS=${IDLE_SECS:-30}
SCALE=${SCALE:-10}
TRACK_SPEED=${TRACK_SPEED:-8000}
COVER_SPEED=${COVER_SPEED:-2000}
SCROLL_SECS=${SCROLL_SECS:-900}
OUT_DIR=${OUT_DIR:-build/measure/$(date +%Y%m%d-%H%M%S)}

variants=("$@")
if [[ ${#variants[@]} -eq 0 ]]; then
    variants=(software femtovg skia)
fi
for v in "${variants[@]}"; do
    case $v in
        software | femtovg | skia | skia-software) ;;
        *)
            echo "unknown variant: $v" >&2
            exit 1
            ;;
    esac
done

if [[ -z ${DISPLAY:-} && -z ${WAYLAND_DISPLAY:-} ]]; then
    echo "no graphical session (neither DISPLAY nor WAYLAND_DISPLAY is set)" >&2
    exit 1
fi
if [[ ! -e $BEETSDIR/config.yaml ]]; then
    echo "no beets data in $BEETSDIR (just dev-library)" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
clk_tck=$(getconf CLK_TCK)

log() { echo "$*" >&2; }

now() { date +%s.%N; }

# Starts roquette in the background with variant $1, the rest as its
# arguments, stderr to log file $LOG; sets $pid.
start() {
    local variant=$1
    shift
    : >"$LOG"
    ROQUETTE_T0=$(now) SLINT_BACKEND=winit-$variant \
        python3 -m roquette "$@" 2>"$LOG" &
    pid=$!
}

# User + system CPU time of a process, in clock ticks.
cpu_ticks() {
    local stat
    stat=$(<"/proc/$1/stat")
    # Fields after "pid (comm) " start with field 3; utime and stime are 14 and 15.
    read -r -a f <<<"${stat##*) }"
    echo $((f[11] + f[12]))
}

# "rss_kb anon_kb file_kb shmem_kb pss_kb threads" of a process.
mem_sample() {
    local pss
    pss=$(awk '/^Pss:/ { print $2 }' "/proc/$1/smaps_rollup")
    awk -v pss="$pss" '
        /^VmRSS:/ { rss = $2 }
        /^RssAnon:/ { anon = $2 }
        /^RssFile:/ { file = $2 }
        /^RssShmem:/ { shmem = $2 }
        /^Threads:/ { threads = $2 }
        END { print rss, anon, file, shmem, pss, threads }
    ' "/proc/$1/status"
}

# Milliseconds from a "roquette: <event> after <n> ms" line in log $1.
event_ms() {
    sed -n "s/^roquette: $2.* after \([0-9.]*\) ms$/\1/p" "$1" | head -1 | grep .
}

peak_rss() { awk '/^VmHWM:/ { print $2 }' "/proc/$1/status"; }

stop() {
    kill "$1" 2>/dev/null || true
    wait "$1" 2>/dev/null || true
}

# Waits until a line matching $2 shows up in log file $1 ($3 s at most,
# default 30).
wait_for_line() {
    local i
    for ((i = 0; i < ${3:-30} * 10; i++)); do
        grep -q "$2" "$1" && return 0
        sleep 0.1
    done
    return 1
}

# Samples process $1 every $3 seconds into CSV $2 while it runs, for at most
# $4 seconds (0: until it exits). Prints "cpu_percent last_sample_line".
sample() {
    local pid=$1 csv=$2 interval=$3 limit=$4
    local t0 c0 t c m line="" end last_t="" last_c=""
    echo "time_s,rss_kb,anon_kb,file_kb,shmem_kb,pss_kb,threads,cpu_ticks" >"$csv"
    t0=$(now)
    c0=$(cpu_ticks "$pid")
    # printf: awk prints numbers with 6 significant digits, which rounds
    # the time (about 1.8e9 s) to 10,000 s.
    end=$(awk -v t="$t0" -v l="$limit" 'BEGIN { printf "%.3f", (l > 0) ? t + l : 1e18 }')
    while kill -0 "$pid" 2>/dev/null; do
        sleep "$interval"
        # The process may exit between the check and the reads.
        c=$(cpu_ticks "$pid" 2>/dev/null) || break
        m=$(mem_sample "$pid" 2>/dev/null) || break
        # An exiting process has no memory figures left.
        [[ $m =~ ^[0-9] ]] || break
        t=$(now)
        line=$m last_t=$t last_c=$c
        echo "$(awk -v t="$t" -v t0="$t0" 'BEGIN { printf "%.2f", t - t0 }'),${line// /,},$c" >>"$csv"
        awk -v t="$t" -v e="$end" 'BEGIN { exit !(t >= e) }' && break
    done
    awk -v c0="$c0" -v c="${last_c:-$c0}" -v t0="$t0" -v t="${last_t:-$t0}" -v hz="$clk_tck" \
        'BEGIN { d = t - t0; printf "%.1f", (d > 0) ? (c - c0) * 100 / hz / d : 0 }'
    echo " $line"
}

mib() { awk -v k="$1" 'BEGIN { printf "%.1f", k / 1024 }'; }

# Median of the numbers given as arguments.
median() {
    [[ $# -gt 0 && -n $1 ]] || {
        echo -
        return
    }
    printf '%s\n' "$@" | sort -g | awk '{ v[NR] = $1 } END {
        if (NR % 2) print v[(NR + 1) / 2];
        else printf "%.1f", (v[NR / 2] + v[NR / 2 + 1]) / 2 }'
}

# Starts variant $1 $STARTUP_RUNS times with the arguments after it and
# prints the medians of the startup events "event loop running", "window
# created" and "library loaded" (or "-").
startup() {
    local variant=$1 name=$2 created=() running=() loaded=()
    shift 2
    for ((i = 1; i <= STARTUP_RUNS; i++)); do
        LOG=$OUT_DIR/$variant-$name-startup-$i.log
        start "$variant" "$@" --measure
        wait_for_line "$LOG" "${STARTUP_UNTIL:-library loaded}" || log "no startup line in $LOG"
        stop "$pid"
        created+=("$(event_ms "$LOG" "window created" || echo)")
        running+=("$(event_ms "$LOG" "event loop running" || echo)")
        loaded+=("$(event_ms "$LOG" "library loaded" || echo)")
        sleep 1
    done
    echo "$(median "${running[@]}") $(median "${created[@]}") $(median "${loaded[@]}")"
}

# Runs variant $1 idle with the arguments after it; prints
# "cpu rss anon file shmem pss threads peak".
idle() {
    local variant=$1 name=$2 cpu rest
    shift 2
    LOG=$OUT_DIR/$variant-$name-idle.log
    start "$variant" "$@" --measure
    sleep "$SETTLE_SECS"
    read -r cpu rest < <(sample "$pid" "$OUT_DIR/$variant-$name-idle.csv" 1 "$IDLE_SECS")
    echo "$cpu $rest $(peak_rss "$pid")"
    stop "$pid"
}

# Scrolls with variant $1 and the arguments after it; prints
# "cpu fps_median fps_min rss_after peak", and the scroll's own line
# ("autoscroll over; …") to $OUT_DIR/$variant-$name-scroll.note.
scroll() {
    local variant=$1 name=$2 cpu rss fps fps_median fps_min
    shift 2
    LOG=$OUT_DIR/$variant-$name-scroll.log
    SLINT_DEBUG_PERFORMANCE=refresh_lazy,console start "$variant" "$@" --measure
    wait_for_line "$LOG" "autoscroll started" 60 || log "autoscroll did not start, see $LOG"
    read -r cpu rss _ < <(sample "$pid" "$OUT_DIR/$variant-$name-scroll.csv" 0.5 "$SCROLL_SECS")
    if kill -0 "$pid" 2>/dev/null; then
        log "the scroll took more than $SCROLL_SECS s, see $LOG"
    fi
    stop "$pid"
    # Frame rate reports between start and end of the scroll, without the
    # first one (partial second).
    mapfile -t fps < <(awk '/autoscroll started/ { on = 1; next } /autoscroll over/ { on = 0 }
        on && /average frames per second:/ && ++n > 1' "$LOG" |
        sed -n 's/.*average frames per second: \([0-9]*\).*/\1/p')
    fps_median=$(median "${fps[@]}")
    fps_min=$(median "$(printf '%s\n' "${fps[@]}" | sort -n | head -1)")
    sed -n 's/^roquette: autoscroll over; //p' "$LOG" >"$OUT_DIR/$variant-$name-scroll.note"
    # The last sample is taken after the scroll settled (--autoscroll-settle).
    local peak
    peak=$(awk -F, 'NR > 1 && $2 > p { p = $2 } END { print p + 0 }' "$OUT_DIR/$variant-$name-scroll.csv")
    echo "$cpu $fps_median $fps_min ${rss:-0} $peak"
}

library_size=$(python3 -c '
import sqlite3
from beets import config
path = config["library"].as_filename()
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
count = lambda table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
print(count("items"), count("albums"))
')
read -r tracks releases <<<"$library_size"

summary=$OUT_DIR/summary.md
{
    echo "## roquette measurements, $(date -u +'%Y-%m-%d %H:%M UTC')"
    echo
    echo "- roquette: $(git describe --always --dirty), library $BEETSDIR: $tracks tracks, $releases releases"
    echo "- Python $(python3 -c 'import platform; print(platform.python_version())'), slint $(python3 -c 'import importlib.metadata as m; print(m.version("slint"))'), beets $(python3 -c 'import importlib.metadata as m; print(m.version("beets"))')"
    echo "- kernel: $(uname -sr)"
    echo "- CPU: $(awk -F': ' '/^model name/ { print $2; exit }' /proc/cpuinfo)"
    echo "- session: ${XDG_SESSION_TYPE:-unknown}, ${XDG_CURRENT_DESKTOP:-unknown} (WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}, DISPLAY=${DISPLAY:-})"
    if command -v glxinfo >/dev/null; then
        echo "- GL: $(glxinfo -B 2>/dev/null | awk -F': ' '/OpenGL renderer string/ { print $2 }')"
    fi
    echo "- idle: ${IDLE_SECS} s after ${SETTLE_SECS} s settling; startup: median of ${STARTUP_RUNS} runs, from starting python3"
    echo "- scrolling: tracks at $TRACK_SPEED px/s, ${SCALE}× the tracks at $((TRACK_SPEED * SCALE)) px/s, covers at $COVER_SPEED px/s"
    echo
} >"$summary"

startup_rows=() idle_rows=() scroll_rows=() notes=() backends=()
for v in "${variants[@]}"; do
    log "== $v: floor"
    read -r f_running f_created _ < <(STARTUP_UNTIL="event loop running" startup "$v" floor --floor)
    read -r f_cpu f_rss f_anon f_file f_shmem f_pss f_threads f_peak < <(idle "$v" floor --floor)
    startup_rows+=("| $v | floor | $f_running | $f_created | - |")
    idle_rows+=("| $v | floor | $(mib "$f_rss") | $(mib "$f_pss") | $(mib "$f_anon") / $(mib "$f_file") / $(mib "$f_shmem") | $f_threads | $f_cpu | $(mib "$f_peak") |")

    log "== $v: window"
    read -r running created loaded < <(startup "$v" window)
    read -r cpu rss anon file shmem pss threads peak < <(idle "$v" window)
    startup_rows+=("| $v | window | $running | $created | $loaded |")
    idle_rows+=("| $v | window | $(mib "$rss") | $(mib "$pss") | $(mib "$anon") / $(mib "$file") / $(mib "$shmem") | $threads | $cpu | $(mib "$peak") |")

    log "== $v: window, ${SCALE}× the library"
    read -r running created loaded < <(startup "$v" "window-x$SCALE" --scale "$SCALE")
    read -r cpu rss anon file shmem pss threads peak < <(idle "$v" "window-x$SCALE" --scale "$SCALE")
    startup_rows+=("| $v | window, ${SCALE}× | $running | $created | $loaded |")
    idle_rows+=("| $v | window, ${SCALE}× | $(mib "$rss") | $(mib "$pss") | $(mib "$anon") / $(mib "$file") / $(mib "$shmem") | $threads | $cpu | $(mib "$peak") |")

    log "== $v: scrolling the tracks"
    read -r cpu fps fps_min rss peak < <(scroll "$v" tracks --autoscroll tracks --autoscroll-speed "$TRACK_SPEED")
    scroll_rows+=("| $v | tracks | $cpu | $fps / $fps_min | $(mib "$peak") | $(mib "$rss") |")
    notes+=("- $v, tracks: $(<"$OUT_DIR/$v-tracks-scroll.note")")
    backends+=("$v: $(sed -n 's/^Slint: Build config: [a-z]*; Backend: //p' "$OUT_DIR/$v-tracks-scroll.log" | head -1)")

    log "== $v: scrolling ${SCALE}× the tracks"
    read -r cpu fps fps_min rss peak < <(scroll "$v" "tracks-x$SCALE" --scale "$SCALE" --autoscroll tracks --autoscroll-speed "$((TRACK_SPEED * SCALE))")
    scroll_rows+=("| $v | tracks, ${SCALE}× | $cpu | $fps / $fps_min | $(mib "$peak") | $(mib "$rss") |")
    notes+=("- $v, tracks ${SCALE}×: $(<"$OUT_DIR/$v-tracks-x$SCALE-scroll.note")")

    # A thumbnail cache of its own: empty for the first run, filled by it
    # for the second.
    thumbnails=$OUT_DIR/$v-cache
    rm -rf "$thumbnails"
    for cache in cold warm; do
        log "== $v: scrolling the covers, $cache thumbnail cache"
        read -r cpu fps fps_min rss peak < <(XDG_CACHE_HOME=$thumbnails scroll "$v" "covers-$cache" --autoscroll covers --autoscroll-speed "$COVER_SPEED")
        scroll_rows+=("| $v | covers, $cache cache | $cpu | $fps / $fps_min | $(mib "$peak") | $(mib "$rss") |")
        notes+=("- $v, covers ($cache): $(<"$OUT_DIR/$v-covers-$cache-scroll.note")")
    done
done

{
    echo "### Startup (ms from starting python3)"
    echo
    echo "| variant | what | event loop running | window created | library loaded |"
    echo "|---|---|---|---|---|"
    printf '%s\n' "${startup_rows[@]}"
    echo
    echo "### Idle"
    echo
    echo "| variant | what | RSS MiB | PSS MiB | anon / file / shmem MiB | threads | CPU % | peak RSS MiB |"
    echo "|---|---|---|---|---|---|---|---|"
    printf '%s\n' "${idle_rows[@]}"
    echo
    echo "### Scrolling"
    echo
    echo "| variant | what | CPU % | fps (median / min) | peak RSS MiB | RSS after MiB |"
    echo "|---|---|---|---|---|---|"
    printf '%s\n' "${scroll_rows[@]}"
    echo
    echo "What the scrolls read and loaded:"
    echo
    printf '%s\n' "${notes[@]}"
    echo
    echo "Slint backends in use:"
    printf -- '- %s\n' "${backends[@]}"
    echo
    echo "The floor's window is created before the event loop runs, the main"
    echo "window's once it runs. CPU % is of one core. Logs and per-second"
    echo "samples: \`$OUT_DIR\`"
} >>"$summary"

cat "$summary"
