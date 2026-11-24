"""The prompts for the pipeline (pipeline.py) and for the map (build_map.py).

The texts are in prompts/ at the top of the project folder, one file each. This file reads them in under the
names the code uses. A text is sent as its file has it, with each {word in braces} filled in by the code;
empty lines at the end of a file do not count.

What changed from the first design (src/legacy/round1/prompts.py), and why (see the pilot report, sections 4 to 6):
- No reference reconstruction. Every reader lays out the post's reasoning
  themselves; "add as little as possible" is one reading among the others.
- No reconstruct/check loop. Each premise the readers supplied gets two
  yes/no tests instead (does the reasoning lean on it; does it only restate
  the conclusion), after Quan et al. 2026 and ReviewScore's triviality score.
- The explicit layer keeps the post's own words and what they assert apart,
  and treats what a rhetorical question plainly implies as stated
  (Pastor & Oostdijk's convention). It no longer writes a conclusion the post
  does not state, and it no longer lists fallacies.
- Every proposition also gets a "general claim": the wider claim it is an
  instance of. The map is built on those.
- Readers are no longer asked for "at most three" things, which the model
  treated as "exactly three".

Changed again after the 50-tweet run (see the first-map report, section 7):
- A reader now writes three things apart: what the post relies on to reach
  its point, where the post is heading beyond that point, and what the reader
  would say against it. Before, all three ended up in one list of "added
  steps", and the merge had to guess which was which.
- A general claim makes one assertion only.
- A post that answers something not shown is no longer dropped; it is
  flagged and read as it stands.

Changed again after the 10-tweet trial of that (first-map report, section 8):
- A post can make more than one argument. The extraction lists them, and
  each argument goes through the readers on its own, with a note telling
  them which part of the post to consider.
- A reader's first reaction and their laying-out of the reasoning are one
  call, to spend fewer calls.
- Readers say whether they would argue with the post before listing
  objections. Before, every reader filled the objections list, including
  the reader who agreed with the post.
"""

from pathlib import Path

DIR = Path(__file__).resolve().parents[1] / "prompts"


def block(name):
    """A prompt of several lines: the text of prompts/NAME.txt, ending in exactly one newline."""
    return (DIR / f"{name}.txt").read_text(encoding="utf-8").rstrip("\n") + "\n"


def inline(name):
    """A sentence or a paragraph that stands alone or goes inside another prompt: the text of prompts/NAME.txt
    with no newline at its end."""
    return (DIR / f"{name}.txt").read_text(encoding="utf-8").rstrip("\n")


# ---------- who is speaking ----------

ANALYST_SYSTEM = inline("system/analyst")       # the system prompt of every call that is not made as a reader
READER_SYSTEM = block("system/reader")          # the system prompt of a reader; {persona} is one of the texts below

# Reading stances, defined relative to the side the post takes.
STANCE_READERS = {n: inline(f"readers/{n}") for n in ("agrees", "disagrees", "scroller")}

# Readers defined by what they already believe about the topic.
# Hand-written for this trial; not taken from survey data.
BELIEF_READERS = {
    "vaccine": {n: inline(f"readers/{n}") for n in ("trusts_vaccines", "doubts_vaccines")},
    "immigration": {n: inline(f"readers/{n}") for n in ("favours_immigration", "wants_less_immigration")},
}

# ---------- one post (pipeline.py), in the order the calls are made ----------

EXTRACT = block("pipeline/extract")                     # what the post states, as one or more arguments
AIM = block("pipeline/aim")                             # who or what an argument is about, and which way the post leans
FOCUS = block("pipeline/focus")                         # the note that points at one argument of a post that makes several
COMPLETE = block("pipeline/complete")                   # a reader lays out the post's reasoning
COMPLETE_MINIMAL = block("pipeline/complete_minimal")   # the same with as little added as possible, and no persona
MERGE = block("pipeline/merge")                         # one call groups what the readers wrote
TEST = block("pipeline/test")                           # does the reasoning lean on each unstated premise; does it only restate the point
RATE = block("pipeline/rate")                           # a reader says whether they believe each merged item
SCORE = block("pipeline/score")                         # what the readers supplied against the human annotators' reconstructions

# ---------- the map across posts (build_map.py), in the order of its steps ----------

POINTS = block("map/points")                            # the general points that two or more posts make or rely on
ASSIGN = block("map/assign")                            # places statements on those points, and on the claims found by NODES
NODES = block("map/nodes")                              # the claims that recur among what readers added beyond the posts
NODES_WHAT = {n: inline(f"map/nodes_what_{n}") for n in ("heading", "against")}     # says which kind of statement NODES is shown
TARGETS = block("map/targets")                          # which part of the argument each objection goes against
RATE_NODES = block("map/rate_nodes")                    # a reader defined by beliefs says whether they believe each claim
AIM_GROUPS = block("map/aim_groups")                    # the targets that the arguments about a topic share
AIM_ASSIGN = block("map/aim_assign")                    # places further arguments on those targets
