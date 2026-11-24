#!/bin/bash
# Run one experiment from start to finish by itself: the pipeline on N posts, then the map.
# Every model call goes through the Codex command line, on the ChatGPT subscription; with --via cli,
# through the claude command line, on the Claude subscription (the account it is logged in with).
# No Claude session is involved: start it from any terminal, and close the terminal if you like.
# The run waits by itself when the usage limit is reached, picks up where it stopped when
# it is started again, and posts a macOS notification when it ends.
#
#   ./job.sh start NAME N [options]   start a run on N posts, or continue a stopped one
#   ./job.sh status [NAME]            how far a run has got; without NAME, which runs are going
#   ./job.sh stop NAME                stop a run; the same "start" line continues it later
#
# Which posts: with --data FILE.jsonl, the first N posts of that file (one post per line: id, text,
# party, ...). Without it, N tweets from Pastor & Oostdijk's enthymeme data, half with annotated
# missing premises and half without; a larger N includes the tweets of a smaller one.
# Options are handed to src/pipeline.py:
#   --data FILE.jsonl       the posts to run on, see above
#   --baseline v3-50        add a comparison with an earlier run of the same tweets to the tables
#   --via cli               send the calls through the claude command line instead of Codex
#   --model gpt-6.1-sol     the model (with --via cli, for example claude-opus-5-5)
#   --workers 4             how many calls run at the same time
#   --stop-after extract    only find the arguments: no readers, no map
#
# Example:  ./job.sh start congress-vax 247 --data data/congress_vaccine_x.jsonl --workers 6
#
# Results: runs/NAME/tables.md, outputs.md, map_tables.md, map.json.  Log: runs/NAME.log
# While a run is going the Mac is kept from idle sleep. Closing the lid still puts it to sleep:
# the run then stands still, and goes on when the Mac wakes. Where there is no caffeinate
# (not a Mac), the run goes without this and without the notification at its end.
cd "$(dirname "$0")" || exit 1
SELF="$PWD/$(basename "$0")"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"      # the code is in src/
PY="$HOME/.pyenv/shims/python3"; [ -x "$PY" ] || PY="$(command -v python3)"
NAME="$2"
PIDFILE="runs/$NAME.pid"
LOG="runs/$NAME.log"
running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
stamp() { while IFS= read -r line || [ -n "$line" ]; do printf '%s  %s\n' "$(date '+%m-%d %H:%M')" "$line"; done; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$SELF"; }

case "$1" in
start)
  N="$3"
  if [ -z "$NAME" ] || ! [ "$N" -gt 0 ] 2>/dev/null; then usage; exit 1; fi
  shift 3
  if running; then echo "$NAME is already running. Progress:  \"$SELF\" status $NAME"; exit 1; fi
  DATA=""; prev=""
  for o in "$@"; do [ "$prev" = "--data" ] && DATA="$o"; prev="$o"; done
  if [ -n "$DATA" ]; then      # posts from a file of our own: its first N
    IDS=$("$PY" -c "import json, sys; print(','.join(json.loads(l)['id'] for l in list(open(sys.argv[1]))[:int(sys.argv[2])]))" "$DATA" "$N") || exit 1
  else
    IDS=$("$PY" -c "import pipeline; print(','.join(r['id'] for r in pipeline.sample($N)))") || exit 1
  fi
  mkdir -p runs
  # A session of its own, so the run outlives the terminal that started it;
  # caffeinate, where there is one, keeps the Mac from idle sleep for as long as the run lasts.
  AWAKE=(); [ -x /usr/bin/caffeinate ] && AWAKE=(/usr/bin/caffeinate -i)
  "$PY" - "$PIDFILE" "$LOG" "${AWAKE[@]}" /bin/bash "$SELF" _run "$NAME" "$IDS" "$@" <<'EOF'
import subprocess, sys
pidfile, log, *cmd = sys.argv[1:]
p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=open(log, "a"), stderr=subprocess.STDOUT, start_new_session=True)
open(pidfile, "w").write(str(p.pid))
EOF
  echo "started $NAME; it now runs by itself"
  echo "progress:  \"$SELF\" status $NAME"
  ;;
_run)
  # The run itself. "start" puts this in the background; it is not meant to be called by hand.
  IDS="$3"; shift 3
  map=yes; case " $* " in *" --stop-after "*) map=no ;; esac
  steps() {
    "$PY" -u src/pipeline.py --ids "$IDS" --name "$NAME" --chunk 8 --wait "$@" || return
    [ "$map" = no ] || "$PY" -u src/build_map.py "runs/$NAME"
  }
  echo "--- started" | stamp
  steps "$@" 2>&1 | stamp
  code=${PIPESTATUS[0]}
  if [ "$code" = 0 ]; then end="has finished"; else end="stopped with an error (exit code $code); see $LOG"; fi
  echo "--- $end" | stamp
  osascript -e "display notification \"Run $NAME $end.\" with title \"Enthymeme experiment\"" >/dev/null 2>&1
  rm -f "$PIDFILE"
  ;;
status)
  if [ -z "$NAME" ]; then
    going=
    for f in runs/*.pid; do
      [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null && { echo "running: $(basename "$f" .pid)"; going=1; }
    done
    [ -n "$going" ] || echo "no run is going"
    exit 0
  fi
  if running; then echo "$NAME is running; it is now $(date '+%m-%d %H:%M')"; else echo "$NAME is not running"; fi
  "$PY" - "$NAME" <<'EOF'
import json, sys
from pathlib import Path
import pipeline as V
run = Path("runs") / sys.argv[1]
if (run / "state.json").exists():
    st = json.loads((run / "state.json").read_text())
    ts = list(st["tweets"].values())
    args = [a for t in ts if "extraction" in t for a in V.arguments_of(t["extraction"])]
    print(f"tweets finished: {sum(t['done'] for t in ts)} of {len(ts)}; arguments merged: {len(V.units_of(ts))} of {len(args)}; "
          f"calls answered: {sum(1 for _ in open(run / 'calls.jsonl'))}")
    if st.get("codex_limits"):
        x = st["codex_limits"][-1]
        print(f"Codex allowance at the last reading ({x['when']} {x.get('at', '')}): "
              f"{x.get('five_hour_used_percent', '?')}% of the 5-hour window, {x.get('week_used_percent', '?')}% of the week")
    print("map built:", (run / "map.json").exists())
else:
    print("nothing saved yet")
EOF
  echo "--- last lines of $LOG"
  grep -Ev "^([0-9-]{5} [0-9:]{5}  )?wrote " "$LOG" 2>/dev/null | tail -6 | cut -c1-200
  ;;
stop)
  if running; then
    kill -- "-$(cat "$PIDFILE")" && rm -f "$PIDFILE"
    echo "--- stopped by hand" | stamp >> "$LOG"
    echo "stopped $NAME; the same start line continues it"
  else
    echo "$NAME is not running"
  fi
  ;;
*)
  usage
  ;;
esac
