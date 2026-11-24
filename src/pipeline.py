"""The pipeline: what a post states is written down, readers complete its reasoning, one call merges what they wrote.

Stages:
  extract   per tweet: what the post states, in its own words, as one or more arguments; no filling in
  aim       per argument: who or what it is about, in the post's own terms, and which way the post leans on it
  complete  per argument and reader: a first reaction, then three things kept apart: the steps the post
            relies on to reach its point, where the post is heading beyond it, and the reader's objections
            (only if the reader says they would argue with the post)
  minimal   per argument: the same laying-out with as little added as possible, no persona
  merge     per argument: one call groups what the readers wrote, keeping the three kinds apart
  test      per argument: each unstated premise: does the reasoning lean on it, does it only restate the point
  rate      per argument and reader: does the reader believe each merged item
  score     per tweet: what the readers supplied vs. the human annotators' reconstructions

Four ways to send the calls:
  --via codex   Codex command line in exec mode, on the logged-in ChatGPT subscription (an OpenAI model)
  --via cli     Claude Code command line in print mode, on the logged-in Claude subscription
  --via direct  ordinary Anthropic API calls (full price)
  --via batch   Anthropic Message Batches API (half price)

Usage, from the project folder:
  python3 src/pipeline.py --ids 1006,1549,757 --name NAME [--stop-after extract] [--baseline OTHER_RUN]
  python3 src/pipeline.py --data FILE.jsonl --ids ID,ID --name NAME      posts of our own instead of the Pastor & Oostdijk tweets
  python3 src/pipeline.py --summarize runs/NAME
job.sh runs this and then build_map.py as one job that goes on by itself.

Until 2026-11-17 this file was pilot_v3.py at the top of the project folder, and the prompts were in prompts_v3.py.
The run folders of earlier rounds hold the code that produced them under code/, under those names.
The first design (round 1) is in src/legacy/round1/.
"""

import argparse
import collections
import csv
import datetime
import json
import os
import random
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from pydantic import BaseModel, ValidationError

import prompts as P

ROOT = Path(__file__).resolve().parents[1]      # the project folder: this file is in src/, runs/ and data/ are beside src/
# The tweets of rounds 1 to 10 (Pastor & Oostdijk's enthymeme data): what a run works on when it is not given posts with --data.
# ENTHYMEMES_CSV names the file when it is somewhere else.
DATA = Path(os.environ.get("ENTHYMEMES_CSV") or Path.home() / (
    "projects/argmining-social-media-review/datasets/enthymeme/"
    "enthymeme-resource-pastor2026/enthymeme-resource-main/data/enthymemes.csv"))
KEY_FILE = Path.home() / ".config/anthropic/api_key"      # the Anthropic API key, for --via direct and --via batch

EFFORT = {"extract": "low", "aim": "low", "complete": "low", "minimal": "medium", "merge": "medium",
          "test": "low", "score": "low", "rate": "low"}
MODEL = "claude-sonnet-5-5"
MAX_WAVES = 12
MAX_WAITS = 40      # with --wait: how many times a run waits for the usage allowance to come back before it stops
CLI_TIMEOUT = 900   # seconds a call through the claude command line may take (a request may ask for longer)
# The Codex command line: CODEX_BIN if it is set, else the one inside the ChatGPT app on a Mac, else "codex" on the PATH.
APP_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
CODEX = os.environ.get("CODEX_BIN") or (APP_CODEX if Path(APP_CODEX).exists() else "codex")
CODEX_MODEL = "gpt-6.1-sol"
# Switches that strip what can be stripped of Codex's own context. What stays on every call, about
# 7,500 tokens: the definitions of its code-running and sub-agent tools, a notice that the model is
# the root agent of a team, and the user's global AGENTS.md.
CODEX_FLAGS = ["--skip-git-repo-check", "--ignore-user-config", "-s", "read-only",
               "-c", "project_doc_max_bytes=0", "-c", "skills.enabled=false"] + \
    [x for k in ("environment_context", "permissions_instructions", "collaboration_mode_instructions", "apps_instructions",
                 "skills_usage_instructions", "plugin_usage_instructions", "apps_usage_instructions")
     for x in ("-c", f"include_{k}=false")] + \
    [x for f in ("multi_agent", "shell_tool", "unified_exec", "view_image", "apps", "plugins", "browser_use",
                 "browser_use_external", "computer_use", "image_generation", "goals", "skill_search", "sleep_tool",
                 "tool_suggest", "in_app_browser", "hooks", "plugin_discovery_prefetch", "remote_plugin",
                 "code_mode_host", "workspace_dependencies") for x in ("--disable", f)] + \
    ["--enable", "omit_skill_descriptions", "--enable", "skip_host_skill_discovery"]
LIMIT_WORDS = ("usage limit", "session limit", "weekly limit", "hit your", "rate limit", "rate_limit",
               "limit reached", "429", "overloaded", "quota")
TRANSIENT = ("timed out", "no JSON from the command line", "usage limit")
CLASSES = (("premise", "premises"), ("granted", "granted"), ("further", "further"), ("objection", "objections"))
NAMES = {"premise": "premises", "granted": "taken for granted", "further": "further points", "objection": "objections"}
STAGES = ("extract", "aim", "complete", "minimal", "merge", "test", "rate", "score")


# ---------- schemas ----------

class Reason(BaseModel):
    id: str
    span: str
    says: str
    form: Literal["plain", "rhetorical_question", "sarcasm"]
    general_claim: str


class Argument(BaseModel):
    id: str
    reasons: list[Reason]
    conclusion_span: str
    conclusion_says: str
    conclusion_claim: str


class Aim(BaseModel):
    """Who or what one argument is about, in the post's own terms, and which way the post leans on it."""
    about: str
    general: bool
    attitude: Literal["backs", "opposes", "neither"]


class Extract(BaseModel):
    status: Literal["argument", "not_argument"]
    why: str
    context_missing: bool
    arguments: list[Argument]
    topic: Literal["vaccine", "immigration", "other"]


class StepItem(BaseModel):
    text: str
    in_post: bool
    mine: Literal["yes", "no", "unsure"]


class FurtherItem(BaseModel):
    text: str
    mine: Literal["yes", "no", "unsure"]


class MinimalCompletion(BaseModel):
    point: str
    point_in_post: bool
    steps: list[StepItem]


class Completion(BaseModel):
    says: str
    getting_at: str
    agree: Literal["yes", "no", "unsure"]
    gut: str
    point: str
    point_in_post: bool
    steps: list[StepItem]
    takes_for_granted: list[FurtherItem]
    leads_to: list[FurtherItem]
    pushback: bool
    objections: list[str]
    verdict: Literal["yes", "no", "partly"]


class ConclusionGroup(BaseModel):
    id: str
    text: str
    general_claim: str
    readers: list[str]


class PremiseGroup(BaseModel):
    id: str
    text: str
    general_claim: str
    kind: Literal["bridge", "presupposition", "value", "background", "other"]
    members: list[str]
    narrower_than: list[str]
    conflicts_with: list[str]


class PlainGroup(BaseModel):
    id: str
    text: str
    general_claim: str
    members: list[str]


class Merge(BaseModel):
    conclusions: list[ConclusionGroup]
    premises: list[PremiseGroup]
    granted: list[PlainGroup]
    further: list[PlainGroup]
    objections: list[PlainGroup]
    restates: list[str]


class TestItem(BaseModel):
    id: str
    needed: Literal["needed", "not_needed"]
    carries: Literal["restates_conclusion", "has_content"]
    trivial: bool


class Test(BaseModel):
    items: list[TestItem]


class RateItem(BaseModel):
    id: str
    believe: Literal["yes", "no", "unsure"]


class Rate(BaseModel):
    items: list[RateItem]


class HumanMatch(BaseModel):
    human_index: int
    best_item_id: str
    relation: Literal["same", "narrower", "broader", "opposite", "different"]


class Score(BaseModel):
    matches: list[HumanMatch]


SCHEMAS = {"extract": Extract, "aim": Aim, "complete": Completion, "minimal": MinimalCompletion,
           "merge": Merge, "test": Test, "score": Score, "rate": Rate}


# ---------- the Pastor & Oostdijk tweets, and saving ----------

def norm(s):
    return " ".join(s.split()).lower()


def new_state(row):
    human = [{"annotator": i, "label": row[f"ann{i}_label"], "text": row[f"ann{i}_implicit"].strip()}
             for i in (1, 2, 3)]   # annotators 4 and 5 saw the earlier annotations
    return {"id": row["id"], "tweet": row["tweet_text"], "majority_label": row["majority_label"],
            "human_labels": [h["label"] for h in human],
            "human": [h for h in human if h["label"] != "none" and h["text"]],
            "attempts": [], "readers": {}, "errors": {}, "done": False}


def tweet_rows():
    """Every row of the Pastor & Oostdijk file."""
    if not DATA.exists():
        raise SystemExit(f"The Pastor & Oostdijk tweets are not at {DATA}.\n"
                         "Set ENTHYMEMES_CSV to where enthymemes.csv is, or run on posts of your own with --data FILE.jsonl.")
    csv.field_size_limit(10 ** 9)
    return list(csv.DictReader(open(DATA, newline="", encoding="utf-8", errors="replace")))


def sample(n, seed=13):
    rows = [r for r in tweet_rows() if r["split"] == "dev"]
    rng = random.Random(seed)
    with_e = [r for r in rows if r["majority_label"] != "none"]
    without = [r for r in rows if r["majority_label"] == "none"]
    rng.shuffle(with_e), rng.shuffle(without)
    return with_e[: n // 2] + without[: n - n // 2]


def save(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


# ---------- one tweet, one argument ----------

def readers_for(ext):
    return ["minimal"] + list(P.STANCE_READERS) + list(P.BELIEF_READERS.get(ext["topic"], {}))


def persona(ext, name):
    return P.STANCE_READERS.get(name) or P.BELIEF_READERS[ext["topic"]][name]


def focus_of(ext, arg):
    """The note that tells readers which part of a post with several arguments to consider."""
    if len(ext["arguments"]) < 2:
        return ""
    return "\n" + P.FOCUS.format(span=arg["conclusion_span"] or " ... ".join(r["span"] for r in arg["reasons"]))


def stated_text(arg):
    lines = [f"{r['id']}: {r['says']}" for r in arg["reasons"]] or ["(no separate reasons)"]
    lines.append(f"Conclusion (stated in the post): {arg['conclusion_says']}" if arg["conclusion_says"]
                 else "Conclusion: not stated in the post")
    return "\n".join(lines)


def added(u):
    """id -> (reader, class, text, the reader's own belief) for everything a reader wrote beyond the post:
    the steps they filled in (agrees.1), what they say it takes for granted (agrees.t1), where the post
    is heading (agrees.f1), and their objections
    (agrees.o1), which count only if the reader said they would argue with the post."""
    out = {}
    for name, r in u["readers"].items():
        c = r.get("completion")
        if not c:
            continue
        k = 0
        for step in c["steps"]:
            if not step["in_post"]:
                k += 1
                out[f"{name}.{k}"] = (name, "premise", step["text"], step["mine"])
        for k, b in enumerate(c.get("takes_for_granted", []), 1):
            out[f"{name}.t{k}"] = (name, "granted", b["text"], b["mine"])
        for k, f in enumerate(c.get("leads_to", []), 1):
            out[f"{name}.f{k}"] = (name, "further", f["text"], f["mine"])
        if c.get("pushback", True):
            for k, o in enumerate(c.get("objections", []), 1):
                out[f"{name}.o{k}"] = (name, "objection", o, "yes")
    return out


def readings_text(u, names):
    items, lines = added(u), []
    label = {"premise": "added step", "granted": "takes for granted", "further": "heading to", "objection": "objection"}
    for n in names:
        c = u["readers"][n]["completion"]
        where = "stated in the post" if c["point_in_post"] else "not stated in the post"
        lines.append(f"[{n}] main point ({where}): {c.get('point') or c.get('conclusion')}")
        mine = [(i, v) for i, v in items.items() if v[0] == n]
        for i, (_, cls, text, belief) in mine:
            note = "" if cls == "objection" else f"; this reader believes it: {belief}"
            lines.append(f"  {i} ({label[cls]}{note}): {text}")
        if not mine:
            lines.append("  (added nothing)")
    return "\n".join(lines)


def main_conclusion(arg, merge):
    if arg["conclusion_says"]:
        return arg["conclusion_says"]
    groups = sorted(merge["conclusions"], key=lambda c: -len(c["readers"]))
    return groups[0]["text"] if groups else "(none)"


def req(s, stage, user, reader=None, system=P.ANALYST_SYSTEM, unit=None):
    return {"tweet": s["id"], "unit": unit, "stage": stage, "reader": reader, "system": system, "user": user,
            "effort": EFFORT[stage]}


def key_of(unit, stage, reader=None):
    return ":".join(x for x in (unit, stage, reader) if x)


def advance_unit(s, ext, arg, u, names):
    """The next calls for one argument of a post."""
    post, err, aid, out = s["tweet"], s["errors"], arg["id"], []
    focus = focus_of(ext, arg)
    if "aim" not in u and key_of(aid, "aim") not in err:      # asked once per argument; a finished run gets it added when run again
        out.append(req(s, "aim", P.AIM.format(post=post, focus=focus, stated=stated_text(arg)), unit=aid))
    for n in names:
        r = u["readers"].setdefault(n, {})
        stage = "minimal" if n == "minimal" else "complete"
        if "completion" in r or key_of(aid, stage, n) in err:
            continue
        if n == "minimal":
            out.append(req(s, stage, P.COMPLETE_MINIMAL.format(post=post, focus=focus), n, unit=aid))
        else:
            out.append(req(s, stage, P.COMPLETE.format(post=post, focus=focus), n,
                           P.READER_SYSTEM.format(persona=persona(ext, n)), aid))
    if out:
        return out
    have = [n for n in names if "completion" in u["readers"][n]]
    if "merge" not in u:
        if key_of(aid, "merge") in err or not have:
            return out
        return [req(s, "merge", P.MERGE.format(post=post, focus=focus, stated=stated_text(arg),
                                               readings=readings_text(u, have)), unit=aid)]
    m = u["merge"]
    tested = m["premises"] + m.get("granted", [])
    if tested and "test" not in u and key_of(aid, "test") not in err:
        out.append(req(s, "test", P.TEST.format(
            post=post, stated=stated_text(arg), conclusion=main_conclusion(arg, m),
            groups="\n".join(f"{g['id']}: {g['text']}" for g in tested)), unit=aid))
    listing = "\n".join(f"{g['id']}: {g['text']}" for g in tested + m["further"] + m["objections"])
    if listing:
        for n in have:
            if n != "minimal" and "rating" not in u["readers"][n] and key_of(aid, "rate", n) not in err:
                out.append(req(s, "rate", P.RATE.format(post=post, groups=listing), n,
                               P.READER_SYSTEM.format(persona=persona(ext, n)), aid))
    return out


def advance(s, stop_after=None):
    post, err = s["tweet"], s["errors"]
    ext = s.get("extraction")
    if ext is None:
        if "extract" in err:
            s["done"] = True
            return []
        return [req(s, "extract", P.EXTRACT.format(post=post))]
    if ext["status"] != "argument" or not ext["arguments"] or stop_after == "extract":
        s["done"] = ext["status"] != "argument" or not ext["arguments"]
        return []
    names, units = readers_for(ext), s.setdefault("units", {})
    out = [r for arg in ext["arguments"]
           for r in advance_unit(s, ext, arg, units.setdefault(arg["id"], {"readers": {}}), names)]
    merging = any(r["stage"] in ("complete", "minimal", "merge") for r in out)
    supplied = [f"{a['id']}.{g['id']} ({label}): {g['text']}" for a in ext["arguments"] if "merge" in units[a["id"]]
                for key, label in (("premises", "unstated premise"), ("granted", "taken for granted"), ("conclusions", "main point"),
                                   ("further", "where it is heading"))
                for g in units[a["id"]]["merge"].get(key, [])]
    if s["human"] and supplied and not merging and "score" not in s and "score" not in err:
        out.append(req(s, "score", P.SCORE.format(
            post=post, items="\n".join(supplied),
            human="\n".join(f"{h['annotator']} (implicit {h['label']}): {h['text']}" for h in s["human"]))))
    if not out:
        s["done"] = True
    return out


def ingest(s, r, obj, error):
    stage, reader, aid = r["stage"], r["reader"], r.get("unit")
    if error:
        s["errors"][key_of(aid, stage, reader)] = error
    elif stage == "extract":
        s["extraction"] = obj
        spans = [x["span"] for a in obj["arguments"] for x in a["reasons"]] + \
                [a["conclusion_span"] for a in obj["arguments"] if a["conclusion_span"]]
        s["spans_not_verbatim"] = [x for x in spans if norm(x) not in norm(s["tweet"])]
    elif stage == "score":
        s["score"] = obj
    else:
        u = s["units"][aid]
        if stage in ("complete", "minimal"):
            u["readers"][reader]["completion"] = obj
        elif stage == "rate":
            u["readers"][reader]["rating"] = obj
        else:
            u[stage] = obj
        if stage == "merge":
            items = added(u)
            filed = {m: cls for cls, key in CLASSES for g in obj.get(key, []) for m in g["members"]}
            placed = list(filed) + obj["restates"]
            u["merge_unplaced"] = sorted(set(items) - set(placed))
            u["merge_unknown_ids"] = sorted(set(placed) - set(items))
            # items the merge filed under a different heading than the reader gave them
            u["merge_refiled"] = sorted(m for m, cls in filed.items() if m in items and items[m][1] != cls)


# ---------- sending calls ----------

def schema_of(stage):
    return anthropic.transform_schema(SCHEMAS[stage])


def cli_env():
    """The environment for the claude command line: without the variables a Claude Code session sets for itself
    (a run started from inside a session would otherwise be refused, or be sent through that session) and without
    an API key, so that every call goes to the account the command line is logged in with: the subscription."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("CLAUDE", "ANTHROPIC_")) or k == "CLAUDE_CONFIG_DIR"}


def reset_time(text, now=None):
    """When a usage limit lifts, read from the words of the message ("resets 5:20am", "resets Nov 20, 5pm",
    "resets in 2h 10m", "...|1763000000"), in seconds since the epoch; None when the message gives no time.
    A clock time is taken in this computer's time zone."""
    now = now or time.time()
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"resets? in (\d+)\s*h\w*(?:\s*(\d+)\s*m)?", text, re.I)
    if m:
        return now + 3600 * int(m.group(1)) + 60 * int(m.group(2) or 0)
    m = re.search(r"resets?(?: at| on)?\s+(?:([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", text, re.I)
    if not m:
        return None
    mon, day, h, mi, ap = m.groups()
    hour = int(h) % 12 + (12 if ap.lower() == "p" else 0)
    t = datetime.datetime.fromtimestamp(now).replace(hour=hour, minute=int(mi or 0), second=0, microsecond=0)
    try:
        if mon:
            t = t.replace(month=datetime.datetime.strptime(mon.title(), "%b").month, day=int(day))
            if t.timestamp() < now - 86400:
                t = t.replace(year=t.year + 1)
        elif t.timestamp() <= now:
            t += datetime.timedelta(days=1)
    except ValueError:
        return None
    return t.timestamp()


def run_cli(reqs, workers, cwd, model=MODEL):
    """One `claude -p` process per call: our own system prompt, no tools, output held to the schema."""
    env = cli_env()

    def one(r):
        wait = max(r.get("timeout") or 0, CLI_TIMEOUT)
        cmd = ["claude", "-p", r["user"], "--system-prompt", r["system"], "--model", model,
               "--effort", r["effort"], "--tools", "", "--strict-mcp-config",
               "--disable-slash-commands", "--no-session-persistence", "--setting-sources", "project",
               "--safe-mode",       # no CLAUDE.md, skills, plugins or hooks of this computer's user in the call
               "--json-schema", json.dumps(schema_of(r["stage"])), "--output-format", "json"]
        try:
            p = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=wait, env=env)
            d = json.loads(p.stdout)
        except subprocess.TimeoutExpired:
            return r, None, {}, None, f"timed out after {wait} s"
        except ValueError:
            msg = (p.stderr or p.stdout)[:300]
            if any(w in msg.lower() for w in LIMIT_WORDS):
                return r, None, {}, None, "usage limit: " + msg
            return r, None, {}, None, f"no JSON from the command line: {msg[:200]}"
        usage = {**(d.get("usage") or {}), "nominal_cost_usd": d.get("total_cost_usd"),
                 "duration_ms": d.get("duration_ms")}
        used = ", ".join(d.get("modelUsage") or {}) or None      # the model(s) that answered, as the command line reports
        if d.get("is_error"):
            msg = str(d.get("result"))[:300]
            limited = any(w in msg.lower() for w in LIMIT_WORDS)
            return r, None, usage, used, ("usage limit: " + msg) if limited else msg
        obj = d.get("structured_output")
        if obj is None:
            try:
                obj = json.loads(d.get("result") or "")
            except ValueError:
                return r, None, usage, used, f"no structured output: {str(d.get('result'))[:200]}"
        return r, obj, usage, used, None
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(one, reqs))


def run_codex(reqs, workers, cwd, model):
    """One `codex exec` process per call. Our system prompt replaces Codex's base instructions and the
    answer is held to the schema; see CODEX_FLAGS for what Codex still adds to every call."""
    def one(r):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "system.txt").write_text(r["system"])
            (Path(d) / "schema.json").write_text(json.dumps(schema_of(r["stage"])))
            cmd = [CODEX, "exec", "--ephemeral", *CODEX_FLAGS, "-C", str(cwd), "-m", model,
                   "-c", f'model_reasoning_effort="{r["effort"]}"', "-c", f'model_instructions_file="{d}/system.txt"',
                   "--output-schema", f"{d}/schema.json", "--json", "-o", f"{d}/out.txt", "-"]
            try:
                p = subprocess.run(cmd, input=r["user"], capture_output=True, text=True, timeout=r.get("timeout", 300))
            except subprocess.TimeoutExpired:
                return r, None, {}, None, f"timed out after {r.get('timeout', 300)} s"
            events = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
            usage = next((e["usage"] for e in events if e.get("type") == "turn.completed"), {})
            failed = [json.dumps(e)[:300] for e in events if e.get("type") in ("error", "turn.failed")]
            out = Path(d) / "out.txt"
            if failed or p.returncode or not out.exists():
                msg = (failed[0] if failed else (p.stderr or "no answer")[-300:])
                limited = any(w in msg.lower() for w in LIMIT_WORDS)
                return r, None, usage, model, ("usage limit: " + msg) if limited else ("no JSON from the command line: " + msg)
            try:
                return r, json.loads(out.read_text()), usage, model, None
            except ValueError:
                return r, None, usage, model, f"no structured output: {out.read_text()[:200]}"
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(one, reqs))


def codex_limits(cwd, model):
    """How much of the ChatGPT plan's Codex allowance is used. Costs one small call: the readout only
    exists in a saved session, which is archived again straight away."""
    cmd = [CODEX, "exec", *CODEX_FLAGS, "-C", str(cwd), "-m", model, "-c", 'model_reasoning_effort="low"', "--json", "-"]
    try:
        p = subprocess.run(cmd, input="Reply with the single word: ok", capture_output=True, text=True, timeout=120)
        tid = next(json.loads(l)["thread_id"] for l in p.stdout.splitlines() if '"thread.started"' in l)
        path = next(Path.home().glob(f".codex/sessions/*/*/*/rollout-*{tid}.jsonl"))
        rows = [json.loads(l) for l in open(path)]
        rl = next(r["payload"]["rate_limits"] for r in reversed(rows)
                  if r.get("type") == "event_msg" and r["payload"].get("type") == "token_count")
        subprocess.run([CODEX, "archive", tid], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        return {"plan": rl.get("plan_type"), "five_hour_used_percent": rl["primary"]["used_percent"],
                "week_used_percent": rl["secondary"]["used_percent"], "five_hour_resets_at": rl["primary"].get("resets_at")}
    except Exception as e:      # the readout is a convenience; never let it stop a run
        return {"error": str(e)[:200]}


def allowance_back(cwd, model):
    """True when a small call goes through and the readout shows room in both windows: the limit has been
    lifted, for example by a reset on the account."""
    x = codex_limits(cwd, model)
    return "error" not in x and (x.get("five_hour_used_percent") or 0) < 100 and (x.get("week_used_percent") or 0) < 100


def wait_out_limit(until, probe=None, every=600):
    """Sleep until `until` (seconds since the epoch) in short naps, so that a computer that slept still wakes
    on time. With `probe`, ask every `every` seconds whether the allowance is already back, and stop waiting
    when it is. Returns True if the wait ended early."""
    last = time.time()
    while time.time() < until:
        time.sleep(30)
        if probe and time.time() - last >= every:
            last = time.time()
            if probe():
                return True
    return False


def load_key():
    """ANTHROPIC_API_KEY if set, else the contents of ~/.config/anthropic/api_key."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    if not key:
        raise SystemExit(f"No API key: set ANTHROPIC_API_KEY or put the key in {KEY_FILE}")
    return key


def run_direct(client, reqs, workers):
    def one(r):
        try:
            return r, client.messages.create(**r["params"]), None
        except anthropic.AuthenticationError:
            raise
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            return r, None, f"{type(e).__name__}: {e}"
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(one, reqs))


def submit_batch(client, reqs):
    batch = client.messages.batches.create(requests=[
        Request(custom_id=f"r{i}", params=MessageCreateParamsNonStreaming(**r["params"]))
        for i, r in enumerate(reqs)])
    return batch.id


def wait_batch(client, batch_id, reqs, poll=30):
    t0 = time.time()
    while True:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            break
        c = b.request_counts
        print(f"    {time.strftime('%H:%M:%S')} {batch_id} {b.processing_status}: "
              f"{c.processing} processing, {c.succeeded} succeeded, {c.errored} errored", flush=True)
        time.sleep(poll)
    print(f"    batch ended after {time.time() - t0:.0f}s", flush=True)
    got = {}
    for res in client.messages.batches.results(batch_id):
        kind = res.result.type
        got[int(res.custom_id[1:])] = (
            (res.result.message, None) if kind == "succeeded"
            else (None, f"errored: {res.result.error}"[:400]) if kind == "errored"
            else (None, kind))
    return [(r, *got.get(i, (None, "missing from the batch results"))) for i, r in enumerate(reqs)]


def api_params(r):
    return dict(model=MODEL, max_tokens=16000, system=r["system"],
                output_config={"effort": r["effort"],
                               "format": {"type": "json_schema", "schema": schema_of(r["stage"])}},
                messages=[{"role": "user", "content": r["user"]}])


def from_message(r, msg, error):
    if msg is None:
        return r, None, {}, None, error
    usage = msg.usage.model_dump(exclude_none=True)
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if msg.stop_reason in ("refusal", "max_tokens") or text is None:
        return r, None, usage, msg.model, f"no complete output (stop_reason={msg.stop_reason})"
    try:
        return r, json.loads(text), usage, msg.model, None
    except ValueError:
        return r, None, usage, msg.model, "output was not JSON"


class LimitReached(Exception):
    pass


def run(a):
    run_dir = ROOT / "runs" / a.name
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path, pending_path = run_dir / "state.json", run_dir / "pending.json"
    st = json.loads(state_path.read_text()) if state_path.exists() else \
        {"model": a.model or (CODEX_MODEL if a.via == "codex" else MODEL), "via": a.via, "wave": 0,
         "batches_ingested": [], "tweets": {}}
    if a.baseline:
        st["baseline"] = a.baseline
    data = a.data or st.get("data")
    if data:        # posts from a .jsonl file (id, text, and whatever is known about the post) instead of the Pastor & Oostdijk tweets
        st["data"] = data
        rows = {r["id"]: r for r in (json.loads(line) for line in open(data))}
        make = lambda r: {"id": r["id"], "tweet": r["text"], "majority_label": "", "human_labels": [], "human": [],
                          "meta": {k: r.get(k) for k in ("party", "actor_name", "handle", "published_at", "url")},
                          "attempts": [], "readers": {}, "errors": {}, "done": False}
    else:
        rows = {r["id"]: r for r in tweet_rows()}
        make = new_state
    ids = a.ids.split(",")
    for i in ids:
        st["tweets"].setdefault(i, make(rows[i]))["done"] = False   # a run stopped early can be taken further
    if a.extract_from:      # start from the arguments an earlier run found, so that both runs work on the same ones
        src = json.loads((run_dir.parent / a.extract_from / "state.json").read_text())["tweets"]
        for i in ids:
            if "extraction" not in st["tweets"][i] and "extraction" in src.get(i, {}):
                st["tweets"][i].update({k: src[i][k] for k in ("extraction", "spans_not_verbatim") if k in src[i]})
        st["extraction_from"] = a.extract_from
    client = None if a.via in ("cli", "codex") else anthropic.Anthropic(api_key=load_key(), max_retries=4)
    cwd = run_dir / "_empty"
    cwd.mkdir(exist_ok=True)
    calls = open(run_dir / "calls.jsonl", "a")

    def meter(when):
        if a.via == "codex":
            st.setdefault("codex_limits", []).append({"when": when, "at": time.strftime("%H:%M"), **codex_limits(cwd, st["model"])})
            print(f"Codex allowance {when}: {st['codex_limits'][-1]}", flush=True)
            save(state_path, st)

    def waves(active):
        """Take these tweets as far as they go, one wave of calls at a time."""
        for _ in range(MAX_WAVES):
            pend = json.loads(pending_path.read_text()) if pending_path.exists() else None
            if pend and pend["batch_id"] in st["batches_ingested"]:
                pending_path.unlink()
                continue
            if pend:
                st["wave"], reqs = pend["wave"], pend["reqs"]
                print(f"wave {st['wave']}: picking up batch {pend['batch_id']}", flush=True)
                results = [from_message(*x) for x in wait_batch(client, pend["batch_id"], reqs)]
                st["batches_ingested"].append(pend["batch_id"])
            else:
                reqs = [r for s in active if not s["done"] for r in advance(s, a.stop_after)]
                save(state_path, st)
                if not reqs:
                    return
                st["wave"] += 1
                print(f"wave {st['wave']}: {len(reqs)} calls "
                      f"{dict(collections.Counter(r['stage'] for r in reqs))} via {a.via}", flush=True)
                if a.via == "cli":
                    results = run_cli(reqs, a.workers, cwd, st["model"])
                elif a.via == "codex":
                    results = run_codex(reqs, a.workers, cwd, st["model"])
                else:
                    full = [{**r, "params": api_params(r)} for r in reqs]
                    if a.via == "direct":
                        results = [from_message(*x) for x in run_direct(client, full, a.workers)]
                    else:
                        batch_id = submit_batch(client, full)
                        slim = [{k: r[k] for k in ("tweet", "unit", "stage", "reader")} for r in reqs]
                        save(pending_path, {"batch_id": batch_id, "wave": st["wave"], "reqs": slim})
                        results = [from_message(*x) for x in wait_batch(client, batch_id, reqs)]
                        st["batches_ingested"].append(batch_id)
            retry = [x for x in results if x[4] and x[4].startswith(TRANSIENT)]
            for r, obj, usage, model, error in results:
                if error and error.startswith(TRANSIENT):
                    continue        # not an answer; the call is issued again in the next wave
                if obj is not None:
                    try:
                        obj = SCHEMAS[r["stage"]].model_validate(obj).model_dump()
                    except ValidationError as e:
                        obj, error = None, f"output did not match the schema: {str(e)[:200]}"
                ingest(st["tweets"][r["tweet"]], r, obj, error)
                calls.write(json.dumps({"wave": st["wave"], "via": a.via, "tweet": r["tweet"], "unit": r.get("unit"),
                                        "stage": r["stage"], "reader": r["reader"], "model": model,
                                        "usage": usage, "error": error}) + "\n")
            calls.flush()
            save(state_path, st)
            pending_path.unlink(missing_ok=True)
            if retry:
                print(f"  {len(retry)} of {len(results)} calls gave no answer and will be issued again: {retry[0][4][:200]}", flush=True)
            many = len(retry) * 2 > len(results)
            limit = next((x[4] for x in retry if x[4].startswith("usage limit")), None)
            if limit or (many and waves.bad):
                waves.bad = False
                raise LimitReached((limit or retry[0][4])[:300])
            waves.bad = many        # most calls failing once can be a computer that slept; twice in a row is treated as a limit

    waves.bad = False
    meter("start")
    chunks = [ids[k:k + a.chunk] for k in range(0, len(ids), a.chunk)] if a.chunk else [ids]
    waited = 0
    for n, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"tweets {(n - 1) * a.chunk + 1}-{(n - 1) * a.chunk + len(chunk)} of {len(ids)}", flush=True)
        while True:
            try:
                waves([st["tweets"][i] for i in chunk])
                break
            except LimitReached as e:
                if not a.wait or waited >= MAX_WAITS:
                    summarize(run_dir)
                    raise SystemExit(f"Stopped: the subscription seems to have reached a usage limit ({e}). "
                                     "Run the same command again later to continue.")
                waited += 1
                known = [x.get("five_hour_resets_at") for x in st.get("codex_limits", []) if x.get("five_hour_resets_at")]
                said = reset_time(str(e))       # the claude command line names the time in its message
                until = known[-1] if known and known[-1] > time.time() else \
                    said if said and said > time.time() else time.time() + 20 * 60
                print(f"usage limit reached at {time.strftime('%H:%M')}; waiting until "
                      f"{time.strftime('%H:%M', time.localtime(until + 120))} to go on", flush=True)
                probe = (lambda: allowance_back(cwd, st["model"])) if a.via == "codex" else None
                if wait_out_limit(until + 120, probe):
                    print(f"the allowance came back early, at {time.strftime('%H:%M')}", flush=True)
                meter("after waiting")
    meter("end")
    summarize(run_dir)


# ---------- summary ----------

def table(header, rows):
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    return "\n".join(out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]) + "\n"


def arguments_of(e):
    """A post's arguments; reads extractions made before a post could carry more than one."""
    if "arguments" in e:
        return e["arguments"]
    return [{"id": "A1", **{k: e.get(k, "") for k in ("conclusion_span", "conclusion_says", "conclusion_claim")},
             "reasons": e.get("reasons", [])}] if e["status"] == "argument" else []


def units_of(tweets):
    """(tweet, argument, unit) for every argument that got as far as merging."""
    out = []
    for t in tweets:
        e = t.get("extraction")
        if not e:
            continue
        for a in arguments_of(e):
            u = t.get("units", {}).get(a["id"]) if "arguments" in e else t      # older runs keep the one argument on the tweet
            if u and "merge" in u:
                out.append((t, a, u))
    return out


def group_view(u):
    """Each merged group (unstated premise, further point, objection) with who supplied it,
    who believes it, and, for premises, its test result."""
    items = added(u)
    tests = {x["id"]: x for x in u.get("test", {}).get("items", [])}
    out = []
    for cls, key in CLASSES:
        for g in u["merge"].get(key, []):
            mem = [items[m] for m in g["members"] if m in items]
            x = tests.get(g["id"], {})
            rated = {n: i["believe"] for n, r in u["readers"].items()
                     for i in r.get("rating", {}).get("items", []) if i["id"] == g["id"]}
            out.append({**g, "cls": cls, "by": sorted({m[0] for m in mem}), "rated": rated,
                        "all_yes": sorted(n for n, v in rated.items() if v == "yes"),
                        "all_no": sorted(n for n, v in rated.items() if v == "no"),
                        "believe": sorted({m[0] for m in mem if m[3] == "yes"}),
                        "reject": sorted({m[0] for m in mem if m[3] == "no"}),
                        "needed": x.get("needed"), "carries": x.get("carries"), "trivial": x.get("trivial")})
    return out


def joined(claim):
    """Rough check for a general claim that makes more than one assertion."""
    return bool(re.search(r"\b(and|so|because|therefore|which shows)\b|;", claim))


def tweet_cells(t):
    """One tweet's row in a side-by-side comparison of two runs."""
    us = units_of([t])
    if not us:
        return [t.get("extraction", {}).get("status", "-"), "-", "-", "-"]
    v = [g for _, _, u in us for g in group_view(u)]
    rel = collections.Counter(m["relation"] for m in t.get("score", {}).get("matches", []))
    return [len(us), " / ".join(str(sum(g["cls"] == c for g in v)) for c, _ in CLASSES),
            f"{sum(g['needed'] == 'needed' and g['cls'] == 'premise' for g in v)} of {sum(g['cls'] == 'premise' for g in v)}",
            f"{rel['same']} same, {rel['narrower'] + rel['broader']} near, of {sum(rel.values())}" if rel else "-"]


def summarize(run_dir):
    st = json.loads((run_dir / "state.json").read_text())
    calls = [json.loads(l) for l in open(run_dir / "calls.jsonl")]
    tweets = list(st["tweets"].values())
    got = [t for t in tweets if "extraction" in t]
    U = units_of(tweets)
    C = collections.Counter
    T = [f"# {len(tweets)} tweets, model {st['model']}, sent via {st['via']}\n"]
    if st.get("codex_limits"):
        T.append("ChatGPT plan allowance for Codex, percent used:\n")
        T.append(table(["when", "plan", "5-hour window", "week"],
                       [[f"{x['when']} {x.get('at', '')}".strip(), x.get("plan", "-"), x.get("five_hour_used_percent", x.get("error", "-")),
                         x.get("week_used_percent", "-")] for x in st["codex_limits"]]))

    by = collections.defaultdict(lambda: [0, 0, 0, 0.0])
    for c in calls:
        u, s = c["usage"], by[c["stage"]]
        s[0] += 1
        s[1] += u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        s[2] += u.get("output_tokens", 0) + u.get("reasoning_output_tokens", 0)
        s[3] += u.get("nominal_cost_usd") or 0
    T.append("## Calls and tokens\n")
    T.append(table(["stage", "calls", "input (incl. cached)", "output", "input/call", "output/call"],
                   [[k, n, i, o, round(i / n), round(o / n)] for k in STAGES if k in by for n, i, o, _ in [by[k]]]
                   + [["total", len(calls), sum(v[1] for v in by.values()), sum(v[2] for v in by.values()), "", ""]]))
    T.append(f"Calls per argument: {sum(1 for c in calls if c['stage'] != 'extract') / max(1, len(U)):.1f} after the extraction. "
             f"Waves: {st['wave']}. Errors: {sum(bool(c['error']) for c in calls)}. "
             f"Models reported: {sorted({c['model'] for c in calls if c['model']})}.\n")
    if st["via"] == "cli":
        T.append(f"Price of these calls at API list rates, as reported by the command line: "
                 f"${sum(v[3] for v in by.values()):.2f}. Not charged to the API key.\n")

    T.append("## What the posts state\n")
    T.append(table(["status", "tweets"], C(t["extraction"]["status"] for t in got).most_common()))
    n_args = C(len(arguments_of(t["extraction"])) for t in got if t["extraction"]["status"] == "argument")
    T.append(table(["arguments in the post", "tweets"], sorted(n_args.items())))
    A = [a for t in got for a in arguments_of(t["extraction"])]
    claims = [r["general_claim"] for a in A for r in a["reasons"]] + [a["conclusion_claim"] for a in A if a["conclusion_claim"]]
    maj = lambda t: sum(l != "none" for l in t["human_labels"]) >= 2
    T.append(f"Posts that answer something not shown: {sum(t['extraction'].get('context_missing', False) for t in got)} of {len(got)}. "
             f"Arguments whose conclusion the post states: {sum(bool(a['conclusion_says']) for a in A)} of {len(A)}. "
             f"Reasons by form: {dict(C(r['form'] for a in A for r in a['reasons']))}. "
             f"Tweets with a span that is not verbatim: {sum(bool(t.get('spans_not_verbatim')) for t in tweets)}. "
             f"General claims of stated parts that join two assertions (rough word check): {sum(map(joined, claims))} of {len(claims)}.\n")
    if any(t["human_labels"] for t in got):
        T.append(table(["", "annotators' majority: something left unstated", "annotators' majority: nothing left unstated"],
                       [[k, sum(t["extraction"]["status"] == k and maj(t) for t in got),
                         sum(t["extraction"]["status"] == k and not maj(t) for t in got)] for k in ("argument", "not_argument")]))
    if any(t.get("meta") for t in got):
        sides = sorted({t["meta"].get("party") or "?" for t in got if t.get("meta")})
        T.append(table(["", *sides], [[k] + [sum(t["extraction"]["status"] == k and (t.get("meta") or {}).get("party") == s for t in got) for s in sides]
                                      for k in ("argument", "not_argument")]))
    if not U:
        (run_dir / "tables.md").write_text("\n".join(T))
        print(f"wrote {run_dir / 'tables.md'}")
        return

    aims = [(t, u["aim"]) for t, _, u in U if u.get("aim")]
    if aims:
        T.append("## Who or what the arguments are about\n")
        sides = sorted({(t.get("meta") or {}).get("party") or "?" for t, _ in aims})
        T.append(f"Arguments with an answer: {len(aims)} of {len(U)}. About no one in particular: {sum(x['general'] for _, x in aims)}. "
                 "Targets are given in each post's own terms here; build_map.py brings them together into shared targets.\n")
        T.append(table(["attitude to what the argument is about", *sides],
                       [[k] + [sum(x["attitude"] == k and ((t.get("meta") or {}).get("party") or "?") == s for t, x in aims) for s in sides]
                        for k in ("backs", "opposes", "neither")]))

    views = {(t["id"], a["id"]): group_view(u) for t, a, u in U}
    groups = [g for v in views.values() for g in v]
    prem = [g for g in groups if g["cls"] == "premise"]
    T.append("## What the readers wrote\n")
    rows = []
    for name in dict.fromkeys(n for _, _, u in U for n in u["readers"]):
        done = [(t, a, u) for t, a, u in U if "completion" in u["readers"].get(name, {})]
        comp = [u["readers"][name]["completion"] for _, _, u in done]
        cnt = C(v[1] for _, _, u in done for v in added(u).values() if v[0] == name)
        only = C(g["cls"] for t, a, u in done for g in views[(t["id"], a["id"])] if g["by"] == [name])
        push = [c for c in comp if c.get("pushback")]
        rows.append([name, len(done), f"{cnt['premise'] / len(done):.1f}", f"{cnt['granted'] / len(done):.1f}", f"{cnt['further'] / len(done):.1f}",
                     "-" if name == "minimal" else f"{len(push)} of {len(done)}",
                     "-" if not push else f"{sum(len(c['objections']) for c in push) / len(push):.1f}",
                     "-" if name == "minimal" else dict(C(u["readers"][name]["completion"].get("agree")
                                                           or u["readers"][name].get("reaction", {}).get("agree", "?")
                                                           for _, _, u in done)), only["premise"]])
    T.append(table(["reader", "arguments", "steps filled in per argument", "taken-for-granted items per argument",
                    "heading-to items per argument", "would argue with the post",
                    "objections per argument when arguing", "first reaction: goes along with it", "premises only this reader supplied"], rows))
    T.append("After merging:\n")
    T.append(table(["kind", "groups", "per argument", "supplied by one reader only", "general claim joins two assertions (rough)"],
                   [[cls, len(gs), f"{len(gs) / len(U):.1f}", sum(len(g["by"]) == 1 for g in gs),
                     sum(joined(g["general_claim"]) for g in gs)]
                    for cls, _ in CLASSES for gs in [[g for g in groups if g["cls"] == cls]]]))
    wrote = C(v[1] for _, _, u in U for v in added(u).values())
    T.append(f"Items written before merging: {dict(wrote)}. "
             f"Added steps set aside as repeating the post or the reader's own point: {sum(len(u['merge'].get('restates', [])) for _, _, u in U)}. "
             f"Items the merge left unplaced: {sum(len(u.get('merge_unplaced', [])) for _, _, u in U)}; "
             f"ids it invented: {sum(len(u.get('merge_unknown_ids', [])) for _, _, u in U)}; "
             f"items it filed under a different heading than the reader did: {sum(len(u.get('merge_refiled', [])) for _, _, u in U)}.\n")
    T.append("Unstated premises:\n")
    T.append(table(["supplied by", "premises", "the reasoning leans on it"],
                   [[k, sum(len(g["by"]) == k for g in prem), sum(len(g["by"]) == k and g["needed"] == "needed" for g in prem)]
                    for k in sorted({len(g["by"]) for g in prem})]))
    T.append(table(["kind", "premises"], C(g["kind"] for g in prem).most_common()))
    T.append(table(["among the premises", "count"], [
        ["the minimal reader supplied it", sum("minimal" in g["by"] for g in prem)],
        ["only non-minimal readers supplied it", sum("minimal" not in g["by"] for g in prem)],
        ["a supplier believes it and another supplier rejects it", sum(bool(g["believe"]) and bool(g["reject"]) for g in prem)],
        ["every supplier who took a view rejects it", sum(bool(g["reject"]) and not g["believe"] for g in prem)],
        ["marked as conflicting with another premise", sum(bool(g["conflicts_with"]) for g in prem)],
        ["marked as a narrower version of another", sum(bool(g["narrower_than"]) for g in prem)]]))

    rated = [g for g in groups if g["rated"]]
    pairs = (("trusts_vaccines", "doubts_vaccines"), ("favours_immigration", "wants_less_immigration"), ("agrees", "disagrees"))
    T.append("## Do the readers believe what was supplied\n")
    T.append(table(["among the rated items"] + [NAMES[cls] for cls, _ in CLASSES], [
        [label] + [sum(f(g) for g in rated if g["cls"] == cls) for cls, _ in CLASSES] for label, f in (
            ("rated", lambda g: True),
            ("every reader believes it", lambda g: set(g["rated"].values()) == {"yes"}),
            ("no reader believes it", lambda g: "yes" not in g["rated"].values()),
            ("at least one believes it and at least one does not", lambda g: bool(g["all_yes"]) and bool(g["all_no"])))]
        + [[f"{a} and {b} give opposite answers"] +
           [f"{sum({g['rated'].get(a), g['rated'].get(b)} == {'yes', 'no'} for g in rated if g['cls'] == cls)}"
            f" of {sum(a in g['rated'] and b in g['rated'] for g in rated if g['cls'] == cls)}" for cls, _ in CLASSES]
           for a, b in pairs]))
    names = sorted({n for g in rated for n in g["rated"]})
    T.append(table(["reader", "believes", "does not", "unsure"],
                   [[n] + [sum(g["rated"].get(n) == v for g in rated) for v in ("yes", "no", "unsure")] for n in names]))
    again, own = C(), C()
    for (t, a, u) in U:
        items = added(u)
        for g in views[(t["id"], a["id"])]:
            for m in g["members"]:
                if m in items and items[m][0] in g["rated"]:
                    if items[m][1] == "objection":
                        own[g["rated"][items[m][0]]] += 1
                    else:
                        again[(items[m][3], g["rated"][items[m][0]])] += 1
    T.append(f"A reader's belief in a step or heading-to item when writing it, against the same reader's later answer: "
             f"same in {sum(v for (a, b), v in again.items() if a == b)} of {sum(again.values())}; "
             f"flipped between yes and no in {sum(v for (a, b), v in again.items() if {a, b} == {'yes', 'no'})}. "
             f"Readers' later answers to their own objections: {dict(own)}.\n")

    tested = [g for g in prem if g["needed"]]
    T.append("## Tests on the unstated premises\n")
    T.append(table(["test", "premises"], [
        ["tested", len(tested)],
        ["the reasoning leans on it", sum(g["needed"] == "needed" for g in tested)],
        ["only restates the conclusion", sum(g["carries"] == "restates_conclusion" for g in tested)],
        ["trivial", sum(bool(g["trivial"]) for g in tested)],
        ["leaned on, has content, not trivial", sum(g["needed"] == "needed" and g["carries"] == "has_content" and not g["trivial"] for g in tested)]]))
    gr = [g for g in groups if g["cls"] == "granted"]
    if gr:
        T.append("## What readers say the posts take for granted\n")
        T.append(table(["supplied by", "items", "the post leans on it"],
                       [[k, sum(len(g["by"]) == k for g in gr), sum(len(g["by"]) == k and g["needed"] == "needed" for g in gr)]
                        for k in sorted({len(g["by"]) for g in gr})]))
        T.append(f"Items: {len(gr)}, {len(gr) / len(U):.1f} per argument. The post leans on {sum(g['needed'] == 'needed' for g in gr)}; "
                 f"trivial: {sum(bool(g['trivial']) for g in gr)}; a supplier believes it and another supplier rejects it: "
                 f"{sum(bool(g['believe']) and bool(g['reject']) for g in gr)}; every supplier who took a view rejects it: "
                 f"{sum(bool(g['reject']) and not g['believe'] for g in gr)}.\n")
    T.append(table(["statements of the main point, groups per argument", "conclusion stated in the post", "conclusion not stated"],
                   [[k, sum(len(u["merge"]["conclusions"]) == k and bool(a["conclusion_says"]) for _, a, u in U),
                     sum(len(u["merge"]["conclusions"]) == k and not a["conclusion_says"] for _, a, u in U)]
                    for k in sorted({len(u["merge"]["conclusions"]) for _, _, u in U})]))

    scored = [t for t in tweets if "score" in t]
    if scored:
        T.append("## Against the annotators\n")
        rel = C(m["relation"] for t in scored for m in t["score"]["matches"])
        T.append(table(["closest reader-supplied item is", "human reconstructions"],
                       [[k, rel[k]] for k in ("same", "narrower", "broader", "opposite", "different")]))
        kind = C({"g": "unstated premise", "t": "taken for granted", "c": "main point", "f": "further point"}.get(m["best_item_id"].split(".")[-1][:1], "nothing")
                 for t in scored for m in t["score"]["matches"] if m["relation"] != "different")
        T.append(f"Tweets with at least one human reconstruction matched as 'same': "
                 f"{sum(any(m['relation'] == 'same' for m in t['score']['matches']) for t in scored)} of {len(scored)}. "
                 f"Matches that are not 'different' point to: {dict(kind)}.\n")

    base = st.get("baseline")
    if base and (run_dir.parent / base / "state.json").exists():
        other = json.loads((run_dir.parent / base / "state.json").read_text())
        T.append(f"## Same tweets: {other['model']} in {base} (before) vs. {st['model']} in this run (now)\n")
        T.append(table(["tweet", "before: arguments", "before: premises / taken for granted / further / objections", "before: premises leaned on",
                        "before vs. annotators", "now: arguments", "now: premises / taken for granted / further / objections",
                        "now: premises leaned on", "now vs. annotators"],
                       [[t["id"]] + tweet_cells(other["tweets"][t["id"]]) + tweet_cells(t) for t in tweets if t["id"] in other["tweets"]]))
    (run_dir / "tables.md").write_text("\n".join(T))

    O = []
    for t in tweets:
        O.append(f"### Tweet {t['id']}\n\n> " + t["tweet"].replace("\n", " ") + "\n")
        if t.get("meta"):
            m = t["meta"]
            O.append(f"{m.get('actor_name')} ({m.get('party')}), {(m.get('published_at') or '')[:10]}. {m.get('url') or ''}")
        if t["human_labels"]:
            O.append(f"Annotators 1-3: {', '.join(t['human_labels'])}.")
        O += [f"- annotator {h['annotator']} (implicit {h['label']}): {h['text']}" for h in t["human"]]
        if t["errors"]:
            O.append(f"\nErrors: {t['errors']}")
        e = t.get("extraction")
        if not e or e["status"] != "argument":
            O.append(f"\nStatus: {e['status'] + ' (' + e['why'] + ')' if e else 'extraction failed'}.\n\n---\n")
            continue
        O.append(f"\nTopic: {e['topic']}." + (" The post answers something that is not shown." if e.get("context_missing") else "") + "\n")
        for a in arguments_of(e):
            O.append(f"#### Argument {a['id']}\n\n**The post states**\n")
            O += [f"- {r['id']} ({r['form']}): \"{r['span']}\" = {r['says']}  \n  wider claim: {r['general_claim']}" for r in a["reasons"]]
            O.append(f"- conclusion: {a['conclusion_says'] or '*not stated in the post*'}\n")
            v = views.get((t["id"], a["id"]))
            if v is None:
                continue
            u = t["units"][a["id"]] if "arguments" in e else t
            if u.get("aim"):
                O.append(f"**About**: {u['aim']['about']}. Attitude of the post to it: {u['aim']['attitude']}.\n")
            O.append("**The main point, as readers put it**\n")
            O += [f"- {c['id']}: {c['text']} ({', '.join(c['readers'])})" for c in u["merge"]["conclusions"]]
            O.append("\n**Unstated premises: what the post relies on**\n")
            O.append(table(["id", "premise", "wider claim", "kind", "supplied by", "believe it", "do not", "leaned on", "content", "trivial"],
                           [[g["id"], g["text"], g["general_claim"], g["kind"], ", ".join(g["by"]),
                             ", ".join(g["all_yes"] or g["believe"]) or "-", ", ".join(g["all_no"] or g["reject"]) or "-",
                             g["needed"] or "", g["carries"] or "", g["trivial"]] for g in v if g["cls"] == "premise"]))
            for g in v:
                if g["cls"] == "premise" and (g["conflicts_with"] or g["narrower_than"]):
                    O.append(f"- {g['id']}" + (f" conflicts with {', '.join(g['conflicts_with'])}" if g["conflicts_with"] else "")
                             + (f"; narrower than {', '.join(g['narrower_than'])}" if g["narrower_than"] else ""))
            sel = [g for g in v if g["cls"] == "granted"]
            if sel:
                O.append("\n**What readers say the post takes for granted**\n")
                O.append(table(["id", "item", "wider claim", "supplied by", "believe it", "do not", "the post leans on it", "trivial"],
                               [[g["id"], g["text"], g["general_claim"], ", ".join(g["by"]), ", ".join(g["all_yes"] or g["believe"]) or "-",
                                 ", ".join(g["all_no"] or g["reject"]) or "-", g["needed"] or "", g["trivial"]] for g in sel]))
            for cls, title in (("further", "Where readers think the post is heading"), ("objection", "What readers say against it")):
                sel = [g for g in v if g["cls"] == cls]
                if sel:
                    O.append(f"\n**{title}**\n")
                    O.append(table(["id", "item", "wider claim", "supplied by", "believe it", "do not"],
                                   [[g["id"], g["text"], g["general_claim"], ", ".join(g["by"]),
                                     ", ".join(g["all_yes"]) or "-", ", ".join(g["all_no"]) or "-"] for g in sel]))
            O.append("")
        if "score" in t:
            O.append("Against the annotators: " + "; ".join(
                f"annotator {m['human_index']} vs {m['best_item_id'] or 'nothing'}: {m['relation']}" for m in t["score"]["matches"]) + ".")
        O.append("\n---\n")
    (run_dir / "outputs.md").write_text("\n".join(O))
    print(f"wrote {run_dir / 'tables.md'} and {run_dir / 'outputs.md'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--via", choices=["codex", "cli", "direct", "batch"], default="codex")
    ap.add_argument("--model", default=None, help="model name (default gpt-6.1-sol for --via codex, claude-sonnet-5-5 otherwise)")
    ap.add_argument("--baseline", default=None, help="name of an earlier run of the same tweets to compare with")
    ap.add_argument("--data", default=None, help="a .jsonl file of posts (id, text, party, ...) to run on instead of the Pastor & Oostdijk tweets")
    ap.add_argument("--extract-from", default=None, help="name of an earlier run whose extracted arguments to start from")
    ap.add_argument("--stop-after", choices=["extract"], default=None, help="stop once this stage is done for every tweet")
    ap.add_argument("--chunk", type=int, default=0, help="finish this many tweets at a time, so that a stopped run leaves whole tweets")
    ap.add_argument("--wait", action="store_true", help="on a usage limit, wait for the allowance to reset and go on")
    ap.add_argument("--name", default="v3")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--summarize", default=None)
    a = ap.parse_args()
    if a.summarize:
        return summarize(Path(a.summarize))
    run(a)


if __name__ == "__main__":
    main()
