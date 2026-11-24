"""Build an argument map across posts from a finished pipeline.py run, in three layers.

Every statement in the run carries a "general claim". The statements fall into three layers:
  own      what a post states or relies on: its stated reasons, its main point, its unstated premises,
           and what readers say it takes for granted
  heading  where readers think the post is heading beyond its main point
  against  what readers said against the post
This script
  1. sets some tweets aside as "new posts",
  2. asks the model, per topic, for the general points that two or more of the other posts make or
     rely on, with narrower versions under each. It is shown statements of the own layer only, so
     what readers wrote cannot crowd out what the posts say,
  3. asks it to place every statement of every layer on one of those points, or on none,
  4. takes the heading and against statements that found no point, asks which claims recur among
     them (nodes of those two layers), and places those statements on them,
  5. asks, argument by argument, which part each objection goes against,
  6. asks the two readers defined by their beliefs whether they believe each node,
  7. draws links between general points: a stated reason or a leaned-on unstated premise -> the
     post's main point; the main point -> where readers think the post is heading; an objection ->
     the part it goes against,
  8. checks where each tweet lands, counting the own layer only: new parts, known parts in a new
     combination, or all seen before.

  python3 src/build_map.py runs/codex-50     # from the project folder; calls go the same way the run's own calls went
writes map.json and map_tables.md into the run folder; progress is kept in map_state.json
"""

import collections
import json
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

import pipeline as V
import prompts as P

CHUNK = 40          # statements placed per call
RATE_CHUNK = 50     # nodes a reader is asked about per call
WORKERS = 6         # calls sent side by side
HOLD_EVERY = 6      # every 6th argument is set aside as a "new post"
TOPICS = {"vaccine": "COVID vaccines", "immigration": "immigration"}      # what the prompts call each topic
# The Pastor & Oostdijk tweets are all about COVID vaccines. Posts from a file of our own (--data) may be about something wider,
# so the name the prompts use is set per file. The Congress sample is about vaccines of any kind.
TOPICS_BY_DATA = {"congress_vaccine_x.jsonl": {"vaccine": "vaccines"}}
LAYER = {"stated reason": "own", "stated conclusion": "own", "unstated premise": "own", "taken for granted": "own", "reader conclusion": "own",
         "heading": "heading", "objection": "against"}
KINDS = list(LAYER)
C = collections.Counter


class Point(BaseModel):
    text: str
    narrower: list[str]


class Points(BaseModel):
    points: list[Point]


class Node(BaseModel):
    text: str


class Nodes(BaseModel):
    nodes: list[Node]


class Placed(BaseModel):
    id: str
    node: str
    fit: Literal["same", "instance", "none"]


class Assign(BaseModel):
    items: list[Placed]


class Target(BaseModel):
    id: str
    target: str


class Targets(BaseModel):
    items: list[Target]


class AimGroup(BaseModel):
    about: str
    ids: list[str]


class AimGroups(BaseModel):
    groups: list[AimGroup]


class AimPlaced(BaseModel):
    id: str
    target: str


class AimAssign(BaseModel):
    items: list[AimPlaced]


V.SCHEMAS.update(points=Points, nodes=Nodes, assign=Assign, targets=Targets, aim_groups=AimGroups, aim_assign=AimAssign)


def lead_conclusion(m):
    """Where a post leaves its point unsaid: the reading of it that most readers share."""
    return max(m["conclusions"], key=lambda c: len(c["readers"]))


def statements(t):
    """Everything this tweet contributes, argument by argument, with its layer and the part it plays.

    role: support (a stated reason, or an unstated premise the reasoning leans on),
          main (the argument's point: its stated conclusion, else the reading most readers share),
          further (where readers think the post is heading beyond that point),
          aside (everything else: premises the reasoning does not lean on, objections, other readings).
    """
    out = []
    for _, a, u in V.units_of([t]):
        m, pre = u["merge"], f"{t['id']}:{a['id']}"
        leans = {g["id"] for g in V.group_view(u) if g["cls"] in ("premise", "granted") and g["needed"] == "needed"}
        add = lambda i, kind, claim, wording, role: out.append(
            dict(key=f"{pre}:{i}", tweet=t["id"], unit=a["id"], kind=kind, layer=LAYER[kind], claim=claim, wording=wording, role=role))
        for r in a["reasons"]:
            add(r["id"], "stated reason", r["general_claim"], r["says"], "support")
        if a["conclusion_says"]:
            add("C", "stated conclusion", a["conclusion_claim"] or a["conclusion_says"], a["conclusion_says"], "main")
        elif m["conclusions"]:
            lead = lead_conclusion(m)["id"]
            for c in m["conclusions"]:
                add(c["id"], "reader conclusion", c.get("general_claim") or c["text"], c["text"], "main" if c["id"] == lead else "aside")
        for g in m["premises"]:
            add(g["id"], "unstated premise", g["general_claim"], g["text"], "support" if g["id"] in leans else "aside")
        for g in m.get("granted", []):
            add(g["id"], "taken for granted", g["general_claim"], g["text"], "support" if g["id"] in leans else "aside")
        for f in m.get("further", []):
            add(f["id"], "heading", f["general_claim"], f["text"], "further")
        for o in m.get("objections", []):
            add(o["id"], "objection", o["general_claim"], o["text"], "aside")
    return out


def argument_parts(a, m):
    """The parts of one argument an objection can go against, as (id, line)."""
    parts = [(r["id"], f"{r['id']} (stated reason): {r['says']}") for r in a["reasons"]]
    if a["conclusion_says"]:
        parts.append(("C", f"C (stated conclusion): {a['conclusion_says']}"))
    elif m["conclusions"]:
        c = lead_conclusion(m)
        parts.append((c["id"], f"{c['id']} (the main point, as readers put it): {c['text']}"))
    return parts + [(g["id"], f"{g['id']} (unstated premise): {g['text']}") for g in m["premises"]] \
        + [(g["id"], f"{g['id']} (taken for granted): {g['text']}") for g in m.get("granted", [])]


SEND = {}           # set in main(): the run's own way of sending calls, so the map is built by the same model


def req(stage, user, effort, system=P.ANALYST_SYSTEM, timeout=300):
    return {"tweet": "-", "stage": stage, "reader": None, "system": system, "user": user, "effort": effort, "timeout": timeout}


def long_wait(n_posts):
    """Seconds to allow the one call that reads every post of a topic: 10 minutes, more when there are many posts."""
    return min(1800, max(600, 6 * n_posts))


def wait_for_allowance(cwd, error):
    """A usage limit: wait for the allowance to come back, as the pipeline does with --wait. The reset time is
    the one last read; without it, wait 20 minutes at a time. Gives up after as many waits as the pipeline allows."""
    SEND["waited"] = SEND.get("waited", 0) + 1
    if SEND["waited"] > V.MAX_WAITS:
        raise SystemExit(f"Stopped: {error} Run the same command again later to continue.")
    known = [x.get("five_hour_resets_at") for x in SEND.get("limits", []) if x.get("five_hour_resets_at")]
    said = V.reset_time(str(error))       # the claude command line names the time in its message
    until = known[-1] if known and known[-1] > time.time() else \
        said if said and said > time.time() else time.time() + 20 * 60
    print(f"usage limit reached at {time.strftime('%H:%M')}; waiting until "
          f"{time.strftime('%H:%M', time.localtime(until + 120))} to go on", flush=True)
    probe = (lambda: V.allowance_back(cwd, SEND["model"])) if SEND.get("via") == "codex" else None
    if V.wait_out_limit(until + 120, probe):
        print(f"the allowance came back early, at {time.strftime('%H:%M')}", flush=True)
    if SEND.get("via") == "codex":          # the new window's reset time, for the next wait
        x = V.codex_limits(cwd, SEND["model"])
        if "error" not in x:
            SEND.setdefault("limits", []).append(x)


def ask_many(reqs, cwd):
    """Send the requests side by side and return (answer, usage) for each, in order.
    A call that gives nothing is sent again, up to three times. A usage limit is waited out and does not count as a try."""
    out, todo, tries, last = {}, list(range(len(reqs))), 0, None
    while todo and tries < 3:
        send = [reqs[i] for i in todo]
        res = V.run_codex(send, WORKERS, cwd, SEND["model"]) if SEND.get("via") == "codex" else V.run_cli(send, WORKERS, cwd, SEND["model"])
        left, limited = [], None
        for i, (_, obj, usage, _, error) in zip(todo, res):
            if obj is not None:
                out[i] = (V.SCHEMAS[reqs[i]["stage"]].model_validate(obj).model_dump(), usage)
                continue
            left.append(i)
            last = error
            if error and error.startswith("usage limit"):
                limited = error
            else:
                print(f"  a {reqs[i]['stage']} call gave no answer ({error}); trying again", flush=True)
        todo = left
        if limited:
            wait_for_allowance(cwd, limited)
        else:
            tries += 1
    if todo:
        raise SystemExit(f"{reqs[todo[0]]['stage']}: no answer after three tries: {last}")
    return [out[i] for i in range(len(reqs))]


def place(todo, nodes, name, store, cache, cwd, save):
    """Ask where each statement belongs among the nodes, CHUNK statements per call, and keep the answers in store."""
    if not todo or not nodes:
        return
    listing = "\n".join(("  " if n["parent"] else "") + f"{n['id']}: {n['text']}" for n in nodes)
    valid = {n["id"] for n in nodes}
    chunks = [todo[k:k + CHUNK] for k in range(0, len(todo), CHUNK)]
    reqs = [req("assign", P.ASSIGN.format(topic=name, nodes=listing,
                                          statements="\n".join(f"s{i + 1}: {s['claim']}" for i, s in enumerate(ch))), "low")
            for ch in chunks]
    for ch, (out, usage) in zip(chunks, ask_many(reqs, cwd)):
        got = {x["id"]: x for x in out["items"]}
        for i, s in enumerate(ch):
            x = got.get(f"s{i + 1}")
            if x is None:
                cache["skipped"] += 1
                x = {"node": "none", "fit": "none"}
            node = x["node"] if x["node"] in valid and x["fit"] != "none" else None
            store[s["key"]] = {"node": node, "fit": x["fit"] if node else "none"}
        cache["usage"].append(usage)
    save()


def blocks_by_post(tids, items, keep):
    """The claims of each post, one block per post that has any, for the calls that draw up nodes."""
    per = [list(dict.fromkeys(s["claim"].strip() for s in items[tid] if keep(s))) for tid in tids]
    return [f"Post {i + 1}\n" + "\n".join(f"- {c}" for c in cs) for i, cs in enumerate(c for c in per if c)]


def spare_name(path):
    """path, or path with a number before its suffix if something is already there, so that nothing gets overwritten."""
    out, k = path, 1
    while out.exists():
        k += 1
        out = path.with_name(f"{path.stem}{k}{path.suffix}")
    return out


def main(run_dir):
    st = json.loads((run_dir / "state.json").read_text())
    SEND.update(via=st.get("via"), model=st.get("model"), limits=list(st.get("codex_limits", [])))
    names ={**TOPICS, **TOPICS_BY_DATA.get(Path(st.get("data") or "").name, {})}
    args = [t for t in st["tweets"].values() if V.units_of([t]) and t["extraction"]["topic"] in TOPICS]
    held = {t["id"] for i, t in enumerate(args) if i % HOLD_EVERY == HOLD_EVERY - 1}
    cwd = run_dir / "_empty"
    cwd.mkdir(exist_ok=True)
    cache_path = run_dir / "map_state.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    ids = sorted(t["id"] for t in args)
    if cache.get("design") != "layers" or cache.get("posts", ids) != ids or cache.get("names", TOPICS) != names:
        # progress of a map built another way, from another set of posts, or with a topic named differently in the prompts:
        # keep it in history/ under another name and start again. Which part an objection goes against is asked one
        # argument at a time and without naming the topic, so those answers carry over.
        keep = cache.get("targets", {}) if cache.get("design") == "layers" else {}
        if cache:
            (run_dir / "history").mkdir(exist_ok=True)
            cache_path.rename(spare_name(run_dir / "history" / ("map_state.previous.json" if cache.get("design") == "layers"
                                                                 else "map_state.one_layer.json")))
        cache = {"design": "layers", "posts": ids, "names": names, "points": {}, "placed": {}, "more": {}, "placed_more": {},
                 "targets": keep, "targets_kept": len(keep), "holds": {}, "aims": {}, "skipped": 0, "usage": []}
    cache.setdefault("aims", {})        # maps built before posts carried a target
    save = lambda: V.save(cache_path, cache)

    items = {t["id"]: statements(t) for t in args}
    topic_of = {t["id"]: t["extraction"]["topic"] for t in args}
    dropped = set()
    for topic, name in names.items():
        tids = [tid for tid in items if topic_of[tid] == topic]
        build = [tid for tid in tids if tid not in held]
        if not build:
            continue
        every = [s for tid in tids for s in items[tid]]

        # 2. the points posts share, drawn from what the posts themselves state and rely on
        if topic not in cache["points"]:
            blocks = blocks_by_post(build, items, lambda s: s["layer"] == "own")
            print(f"{topic}: what {len(blocks)} posts state and rely on -> asking for the points they share", flush=True)
            (out, usage), = ask_many([req("points", P.POINTS.format(topic=name, claims="\n".join(blocks)), "medium",
                                          timeout=long_wait(len(blocks)))], cwd)
            nodes = []
            for i, pt in enumerate(out["points"]):
                pid = f"{topic[:3]}-p{i + 1}"
                nodes.append({"id": pid, "text": pt["text"], "layer": "own", "parent": None})
                nodes += [{"id": f"{pid}.{j + 1}", "text": x, "layer": "own", "parent": pid} for j, x in enumerate(pt["narrower"])]
            cache["points"][topic] = nodes
            cache["usage"].append(usage)
            save()
        points = cache["points"][topic]

        # 3. every statement, of every layer, against those points
        todo = [s for s in every if s["key"] not in cache["placed"]]
        if todo:
            print(f"{topic}: placing {len(todo)} statements on {len(points)} points", flush=True)
            place(todo, points, name, cache["placed"], cache, cwd, save)

        # a narrower version that fewer than two of the posts used to build the map share falls back to its general point
        used = collections.defaultdict(set)
        for s in every:
            n = cache["placed"][s["key"]]["node"]
            if n and s["layer"] == "own" and s["tweet"] not in held:
                used[n].add(s["tweet"])
        dropped |= {n["id"] for n in points if n["parent"] and len(used[n["id"]]) < 2}

        # 4. heading-to items and objections that match nothing the posts say get nodes of their own
        more = cache["more"].setdefault(topic, {})
        for layer in ("heading", "against"):
            left = [s for s in every if s["layer"] == layer and not cache["placed"][s["key"]]["node"]]
            if layer not in more:
                blocks = blocks_by_post(build, {tid: [s for s in left if s["tweet"] == tid] for tid in build}, lambda s: True)
                more[layer] = []
                if len(blocks) >= 2:
                    print(f"{topic}: {layer} statements left over from {len(blocks)} posts -> asking which claims recur", flush=True)
                    (out, usage), = ask_many([req("nodes", P.NODES.format(topic=name, claims="\n".join(blocks),
                                                                         what=P.NODES_WHAT[layer]), "medium",
                                              timeout=long_wait(len(blocks)))], cwd)
                    more[layer] = [{"id": f"{topic[:3]}-{layer[0]}{i + 1}", "text": n["text"], "layer": layer, "parent": None}
                                   for i, n in enumerate(out["nodes"])]
                    cache["usage"].append(usage)
                save()
            todo = [s for s in left if s["key"] not in cache["placed_more"]]
            if todo and more[layer]:
                print(f"{topic}: placing {len(todo)} {layer} statements on {len(more[layer])} nodes of that layer", flush=True)
                place(todo, more[layer], name, cache["placed_more"], cache, cwd, save)

        # 5. which part of the argument each objection goes against
        todo = []
        for t in args:
            for _, a, u in V.units_of([t]) if topic_of[t["id"]] == topic else []:
                ukey, m = f"{t['id']}:{a['id']}", u["merge"]
                if m.get("objections") and ukey not in cache["targets"]:
                    parts = argument_parts(a, m)
                    todo.append((ukey, {i for i, _ in parts}, req("targets", P.TARGETS.format(
                        post=t["tweet"], focus=V.focus_of(t["extraction"], a), parts="\n".join(line for _, line in parts),
                        objections="\n".join(f"{o['id']}: {o['text']}" for o in m["objections"])), "low")))
        if todo:
            print(f"{topic}: asking what the objections to {len(todo)} arguments go against", flush=True)
            for (ukey, valid, _), (out, usage) in zip(todo, ask_many([r for _, _, r in todo], cwd)):
                cache["targets"][ukey] = {x["id"]: x["target"] if x["target"] in valid else None for x in out["items"]}
                cache["usage"].append(usage)
            save()

        # 6. do the two readers defined by their beliefs believe each node
        nodes = [n for n in points if n["id"] not in dropped] + more["heading"] + more["against"]
        todo = []
        for reader, text in P.BELIEF_READERS[topic].items():
            have = cache["holds"].setdefault(topic, {}).setdefault(reader, {})
            ask = [n for n in nodes if n["id"] not in have]
            for k in range(0, len(ask), RATE_CHUNK):
                listing = "\n".join(f"{n['id']}: {n['text']}" for n in ask[k:k + RATE_CHUNK])
                todo.append((reader, req("rate", P.RATE_NODES.format(topic=name, claims=listing), "low",
                                         system=P.READER_SYSTEM.format(persona=text))))
        if todo:
            print(f"{topic}: asking the two belief readers about {len(nodes)} nodes", flush=True)
            valid = {n["id"] for n in nodes}
            for (reader, _), (out, usage) in zip(todo, ask_many([r for _, r in todo], cwd)):
                cache["holds"][topic][reader].update({x["id"]: x["believe"] for x in out["items"] if x["id"] in valid})
                cache["usage"].append(usage)
            save()

        # 7. who or what each argument is about. The posts' own words for it come from the pipeline (its "aim" stage);
        #    here they are brought together into shared targets, from the posts used to build the map. An argument of a
        #    post that was set aside is then matched to one of those targets, or to none.
        aimed = {f"{t['id']}:{a['id']}": {**u["aim"], "tweet": t["id"], "main": V.main_conclusion(a, u["merge"])}
                 for t in args if topic_of[t["id"]] == topic for _, a, u in V.units_of([t]) if u.get("aim")}
        named = {k: v for k, v in aimed.items() if not v["general"]}
        got = cache["aims"].setdefault(topic, {"groups": [], "of": {}})
        line = lambda i, k: f"a{i + 1}: {named[k]['about']} [main point: {named[k]['main']}]"
        todo = [k for k in named if k not in got["of"] and named[k]["tweet"] not in held]
        if not got["groups"] and len(todo) >= 2:
            print(f"{topic}: what {len(todo)} arguments are about -> asking for the targets they share", flush=True)
            (out, usage), = ask_many([req("aim_groups", P.AIM_GROUPS.format(topic=name, items="\n".join(line(i, k) for i, k in enumerate(todo))),
                                          "medium", timeout=long_wait(len(todo)))], cwd)
            by_id = {f"a{i + 1}": k for i, k in enumerate(todo)}
            for g in out["groups"]:
                ks = [by_id[i] for i in dict.fromkeys(g["ids"]) if i in by_id and by_id[i] not in got["of"]]
                if ks:
                    gid = f"{topic[:3]}-t{len(got['groups']) + 1}"
                    got["groups"].append({"id": gid, "about": g["about"].strip()})
                    got["of"].update({k: gid for k in ks})
            for k in todo:      # a line the answer left out is a target of its own
                if k not in got["of"]:
                    gid = f"{topic[:3]}-t{len(got['groups']) + 1}"
                    got["groups"].append({"id": gid, "about": named[k]["about"]})
                    got["of"][k] = gid
            cache["usage"].append(usage)
            save()
        todo = [k for k in named if k not in got["of"]]
        if todo and got["groups"]:
            print(f"{topic}: matching {len(todo)} more arguments to the {len(got['groups'])} targets", flush=True)
            listing, valid = "\n".join(f"{g['id']}: {g['about']}" for g in got["groups"]), {g["id"] for g in got["groups"]}
            chunks = [todo[k:k + CHUNK] for k in range(0, len(todo), CHUNK)]
            reqs = [req("aim_assign", P.AIM_ASSIGN.format(topic=name, groups=listing, items="\n".join(line(i, k) for i, k in enumerate(ch))), "low")
                    for ch in chunks]
            for ch, (out, usage) in zip(chunks, ask_many(reqs, cwd)):
                ans = {x["id"]: x["target"] for x in out["items"]}
                for i, k in enumerate(ch):
                    got["of"][k] = ans.get(f"a{i + 1}") if ans.get(f"a{i + 1}") in valid else None
                cache["usage"].append(usage)
            save()

    # ---------- assemble the map ----------
    meta = {n["id"]: {**n, "topic": tp} for tp, ns in cache["points"].items() for n in ns if n["id"] not in dropped}
    meta.update({n["id"]: {**n, "topic": tp} for tp, by in cache["more"].items() for ns in by.values() for n in ns})
    parent = {n["id"]: n["parent"] for ns in cache["points"].values() for n in ns}
    root = lambda n: parent.get(n) or n
    info = {n: {"tweets": set(), "own": set(), "own_build": set(), "kinds": C(), "believe": collections.defaultdict(C),
                "side": collections.defaultdict(set), "aim": collections.defaultdict(lambda: collections.defaultdict(set))} for n in meta}
    aim_of = {}     # "tweet:argument" -> (shared target, or "general", or "none"; the post's attitude to it)
    for t in args:
        for _, a, u in V.units_of([t]):
            if u.get("aim"):
                k = f"{t['id']}:{a['id']}"
                g = "general" if u["aim"]["general"] else (cache["aims"].get(topic_of[t["id"]], {}).get("of", {}).get(k) or "none")
                aim_of[k] = (g, u["aim"]["attitude"])
    side_of = {t["id"]: (t.get("meta") or {}).get("party") for t in args}      # who posted it, where the data says
    sides = sorted({s for s in side_of.values() if s})
    views = {(t["id"], a["id"]): {g["id"]: g for g in V.group_view(u)} for t in args for _, a, u in V.units_of([t])}
    posts, edges, gen_edges, post_edges = [], collections.defaultdict(set), collections.defaultdict(set), {}
    for t in args:
        tid, parts = t["id"], []
        for s in items[tid]:
            p = cache["placed"].get(s["key"], {})
            node, fit = p.get("node"), p.get("fit", "none")
            if node in dropped:
                node, fit = parent[node], "instance"
            if not node:
                p = cache["placed_more"].get(s["key"], {})
                node, fit = p.get("node"), p.get("fit", "none")
            part = {**s, "node": node, "fit": fit}
            if s["kind"] == "objection":
                part["target"] = cache["targets"].get(f"{tid}:{s['unit']}", {}).get(s["key"].split(":")[2])
            parts.append(part)
            g = views[(tid, s["unit"])].get(s["key"].split(":")[2])
            for n in dict.fromkeys([node, root(node)] if node else []):     # a general point also counts what sits on its narrower versions
                x = info[n]
                x["tweets"].add(tid)
                if side_of[tid] and (s["layer"] == "own" or meta[n]["layer"] != "own"):
                    x["side"][side_of[tid]].add(tid)     # posts of each side that state or rely on the point; for reader-only nodes, posts it was said about
                    if f"{tid}:{s['unit']}" in aim_of:
                        x["aim"][aim_of[f"{tid}:{s['unit']}"]][side_of[tid]].add(tid)
                if s["layer"] == "own":
                    x["own"].add(tid)
                    if tid not in held:
                        x["own_build"].add(tid)
                x["kinds"][s["kind"]] += 1
                for reader, v in (g["rated"].items() if g else []):
                    x["believe"][reader][v] += 1
        mine, general = set(), set()
        for unit in dict.fromkeys(p["unit"] for p in parts):      # links stay inside one argument
            own = [p for p in parts if p["unit"] == unit]
            by_id = {p["key"].split(":")[2]: p for p in own}
            main = next((p["node"] for p in own if p["role"] == "main" and p["node"]), None)
            for p in own:
                link = None
                if p["node"] and p["role"] == "support" and main:
                    link = (p["node"], main, "supports")
                elif p["node"] and p["role"] == "further" and main:
                    link = (main, p["node"], "read as")
                elif p["node"] and p["kind"] == "objection":
                    tgt = by_id.get(p.get("target"))
                    link = (p["node"], tgt["node"], "against") if tgt and tgt["node"] else None
                if link and root(link[0]) != root(link[1]):       # not a link from a point to itself or to its own narrower version
                    mine.add(link)
                    general.add((root(link[0]), root(link[1]), link[2]))
        for k in mine:
            edges[k].add(tid)
        for k in general:
            gen_edges[k].add(tid)
        post_edges[tid] = general
        aims = {a["id"]: {"about": u["aim"]["about"], "attitude": u["aim"]["attitude"], "target": aim_of[f"{tid}:{a['id']}"][0]}
                for _, a, u in V.units_of([t]) if u.get("aim")}
        posts.append({"id": tid, "tweet": t["tweet"], "topic": topic_of[tid], "held_out": tid in held, "parts": parts, "aims": aims})

    # where each post lands: only what the post itself states and relies on counts, judged against the other posts the map was built from
    for post in posts:
        tid = post["id"]
        core = [p for p in post["parts"] if p["role"] in ("support", "main")]
        known = [p for p in core if p["node"] and info[root(p["node"])]["own_build"] - {tid}]
        links = {k for k in post_edges[tid] if k[2] != "against"}
        seen = {k for k in links if gen_edges[k] - held - {tid}}
        verdict = ("has parts that are new to the map" if len(known) < len(core)
                   else "known parts, no links to compare" if not links
                   else "known parts, combination seen before" if seen == links
                   else "known parts, new combination")
        post["placing"] = {"core": len(core), "known": len(known), "links": len(links), "seen": len(seen), "verdict": verdict}

    holds = {n: {r: v[n] for r, v in cache["holds"].get(m["topic"], {}).items() if n in v} for n, m in meta.items()}
    nodes_out = [{"id": n, "text": m["text"], "topic": m["topic"], "layer": m["layer"], "parent": m["parent"],
                  "n_tweets": len(info[n]["tweets"]), "n_own": len(info[n]["own"]), "tweets": sorted(info[n]["tweets"]),
                  "kinds": dict(info[n]["kinds"]), "holds": holds[n], "believe": {r: dict(c) for r, c in info[n]["believe"].items()},
                  "posts_by_side": {s: len(info[n]["side"][s]) for s in sides},
                  "by_target": [{"target": g, "attitude": att, "posts_by_side": {s: len(v[s]) for s in sides}}
                                for (g, att), v in sorted(info[n]["aim"].items(), key=lambda kv: -sum(len(x) for x in kv[1].values()))]}
                 for n, m in meta.items()]
    targets_out = [{"id": g["id"], "about": g["about"], "topic": tp} for tp, x in cache["aims"].items() for g in x["groups"]]
    # sorted, so that building the same map twice writes the same file (the links are gathered in sets)
    edges_out = [{"from": a, "to": b, "kind": k, "tweets": sorted(ts)} for (a, b, k), ts in sorted(edges.items())]
    general_out = [{"from": a, "to": b, "kind": k, "tweets": sorted(ts)} for (a, b, k), ts in sorted(gen_edges.items())]
    gone = [{"id": n["id"], "text": n["text"], "parent": n["parent"]} for ns in cache["points"].values() for n in ns if n["id"] in dropped]
    V.save(run_dir / "map.json", {"model": st["model"], "design": "layers", "nodes": nodes_out, "edges": edges_out, "edges_between_general_points": general_out,
                                      "posts": posts, "narrower_versions_dropped": gone, "targets": targets_out})

    # ---------- tables ----------
    text = {n["id"]: n["text"] for n in nodes_out}
    of = lambda tp, layer, top=None: [n for n in nodes_out if n["topic"] == tp and n["layer"] == layer
                                      and (top is None or (n["parent"] is None) == top)]
    T = [f"# Map in three layers, built from {len(args)} posts ({len(args) - len(held)} to build it, {len(held)} set aside as new posts)\n",
         "Layers: own = what a post states or relies on (stated reasons, main point, unstated premises); heading = where readers "
         "think it is heading; against = what readers said against it. The points of the own layer were drawn up from own-layer "
         "statements only. Heading-to items and objections were first tried against those points; the ones that matched none got "
         "nodes of their own layer.\n",
         "What the prompts called each topic: " + ", ".join(f"{k} = \"{v}\"" for k, v in names.items() if k in cache["points"]) + ".\n"]
    T.append(V.table(["topic", "posts used to build", "set aside", "general points", "stated or relied on in 2+ tweets", "in 3+", "in 5+",
                      "narrower versions kept", "narrower versions dropped", "heading-only nodes", "against-only nodes"], [
        [tp, sum(topic_of[i] == tp and i not in held for i in items), sum(topic_of[i] == tp and i in held for i in items),
         len(of(tp, "own", True))] + [sum(n["n_own"] >= k for n in of(tp, "own", True)) for k in (2, 3, 5)]
        + [len(of(tp, "own", False)), sum(n["id"] in dropped for n in cache["points"][tp]), len(of(tp, "heading")), len(of(tp, "against"))]
        for tp in cache["points"]]))

    T.append("## How many statements found a node\n")
    layer_of = {n["id"]: n["layer"] for n in nodes_out}
    for tp in cache["points"]:
        parts = [p for post in posts if post["topic"] == tp for p in post["parts"]]
        rows = []
        for k in KINDS + ["all"]:
            sel = [p for p in parts if k == "all" or p["kind"] == k]
            on = [p for p in sel if p["node"] and layer_of[p["node"]] == "own"]
            other = [p for p in sel if p["node"] and layer_of[p["node"]] != "own"]
            pct = lambda x: f"{len(x)} ({100 * len(x) / len(sel):.0f}%)" if sel else "0"
            rows.append([k, len(sel), pct(on), sum(p["fit"] == "same" for p in on), sum(p["fit"] == "instance" for p in on),
                         pct(other), pct([p for p in sel if not p["node"]])])
        T.append(f"{tp}:\n")
        T.append(V.table(["kind of statement", "statements", "on a point posts share", "as the same point", "as an instance",
                          "on a heading-only or against-only node", "on no node"], rows))

    T.append("## The points posts share (own layer)\n")
    T.append("\"Tweets\" counts the posts that state the point or rely on it. \"Others head there\" and \"raised against others\" count "
             "heading-to items and objections from other arguments that say the same thing. \"Asked directly\" is each belief "
             "reader's answer when shown the claim on its own; \"in posts\" counts that reader's yes/no answers to the reader-written "
             "items placed on the node. Lines starting with a dot are narrower versions of the point above them.\n")
    k_of = lambda n, *ks: sum(n["kinds"].get(k, 0) for k in ks)
    for tp in cache["points"]:
        a, b = list(P.BELIEF_READERS[tp])
        cnt = lambda n, r: f"{n['believe'].get(r, {}).get('yes', 0)}/{n['believe'].get(r, {}).get('no', 0)}"
        rows = []
        for n in sorted(of(tp, "own", True), key=lambda n: -n["n_own"]):
            for x in [n] + sorted((c for c in of(tp, "own", False) if c["parent"] == n["id"]), key=lambda c: -c["n_own"]):
                rows.append([x["id"], ("· " if x["parent"] else "") + x["text"], x["n_own"], *[x["posts_by_side"][s] for s in sides],
                             k_of(x, "stated reason", "stated conclusion"),
                             k_of(x, "unstated premise"), k_of(x, "taken for granted"), k_of(x, "reader conclusion"), k_of(x, "heading"), k_of(x, "objection"),
                             x["holds"].get(a, "-"), x["holds"].get(b, "-"), cnt(x, a), cnt(x, b)])
        T.append(V.table(["node", "claim", "tweets", *[f"{s} posts" for s in sides], "stated", "left unsaid: a step", "left unsaid: taken for granted", "read as the main point", "others head there",
                          "raised against others", f"{a}: asked directly", f"{b}: asked directly", f"{a}: in posts", f"{b}: in posts"], rows))
        for layer, title in (("heading", "Where readers think posts are heading, and no post says or relies on"),
                             ("against", "What readers raise against posts, and no post says or relies on")):
            T.append(f"### {title} ({tp})\n")
            T.append(V.table(["node", "claim", "tweets", *[f"about {s} posts" for s in sides], f"{a}: asked directly", f"{b}: asked directly",
                              f"{a}: in posts", f"{b}: in posts"], [
                [n["id"], n["text"], n["n_tweets"], *[n["posts_by_side"][s] for s in sides], n["holds"].get(a, "-"), n["holds"].get(b, "-"), cnt(n, a), cnt(n, b)]
                for n in sorted(of(tp, layer), key=lambda n: -n["n_tweets"])]))
        for layer in ("own", "heading", "against"):
            pairs = C((n["holds"].get(a, "-"), n["holds"].get(b, "-")) for n in of(tp, layer))
            T.append(f"{layer} layer, asked directly, ({a}, {b}) answered: " + ", ".join(f"{x}/{y}: {c}" for (x, y), c in pairs.most_common()) + ".\n")
        agree = C()
        for n in nodes_out:
            for r in (a, b):
                c = n["believe"].get(r, {})
                if n["topic"] == tp and c.get("yes", 0) != c.get("no", 0) and r in n["holds"]:
                    agree[("yes" if c.get("yes", 0) > c.get("no", 0) else "no") == n["holds"][r]] += 1
        T.append(f"Node-reader pairs where the reader's answers in posts lean one way: {sum(agree.values())}; "
                 f"the direct answer goes the same way in {agree[True]}.\n")

    hidden = [n for n in nodes_out if n["layer"] == "own" and n["parent"] is None and n["n_own"] >= 2 and not k_of(n, "stated reason", "stated conclusion")]
    mostly = [n for n in nodes_out if n["layer"] == "own" and n["parent"] is None and n["n_own"] >= 2
              and k_of(n, "unstated premise", "taken for granted", "reader conclusion") > k_of(n, "stated reason", "stated conclusion")]
    T.append(f"## Points posts rely on and seldom state\n\nGeneral points in 2 or more tweets that no tweet states outright: {len(hidden)}. "
             f"Left unsaid more often than stated: {len(mostly)}.\n")
    T += [f"- {n['id']} ({n['n_own']} tweets; stated {k_of(n, 'stated reason', 'stated conclusion')}, left unsaid "
          f"{k_of(n, 'unstated premise', 'taken for granted', 'reader conclusion')}): {n['text']}" for n in sorted(mostly, key=lambda n: -n["n_own"])]

    T.append("\n## Links\n")
    T.append("A link runs between the nodes the two statements sit on, narrower versions included. The count \"between general points\" "
             "treats a narrower version as its general point; that is the level at which a post's combination is compared with other posts.\n")
    for kind, label in (("supports", "A reason or a leaned-on unstated premise -> the post's main point"),
                        ("read as", "The main point -> where readers think the post is heading"),
                        ("against", "An objection -> the part of the argument it goes against")):
        es, gs = [e for e in edges_out if e["kind"] == kind], [e for e in general_out if e["kind"] == kind]
        rec = sorted((e for e in es if len(e["tweets"]) >= 2), key=lambda e: -len(e["tweets"]))
        T.append(f"{label}: {len(es)} links, {len(rec)} of them in 2 or more tweets; between general points {len(gs)}, "
                 f"{sum(len(e['tweets']) >= 2 for e in gs)} in 2 or more tweets.\n")
        T += [f"- {text[e['from']]} -> {text[e['to']]} ({len(e['tweets'])} tweets)" for e in rec[:15]]
        T.append("")
    objs = [p for post in posts for p in post["parts"] if p["kind"] == "objection"]
    by_id = {p["key"]: p for post in posts for p in post["parts"]}
    kind_of_target = C(by_id[p["key"].rsplit(":", 1)[0] + ":" + p["target"]]["kind"] if p.get("target") else "no part" for p in objs)
    T.append(f"What the {len(objs)} objections go against: " + ", ".join(f"{k}: {c}" for k, c in kind_of_target.most_common()) + ".\n")

    if aim_of and sides:        # these tables set the sides side by side: only for posts whose data says who posted them
        label = {g["id"]: g["about"] for g in targets_out}
        label.update(general="(no one in particular)", none="(matched no shared target)")
        size = lambda v: len(set().union(*v.values())) if v else 0
        every = lambda v, k=1: all(len(v[s]) >= k for s in sides)
        T.append("## Who or what the posts are about\n")
        T.append("Each argument was asked once who or what its main point is about and which way the post leans on it (backs, opposes, "
                 "neither). The answers of the posts used to build the map were brought together into shared targets; posts set aside "
                 "were matched to those, or to none. Counts are posts.\n")
        for tp in cache["points"]:
            per = collections.defaultdict(lambda: collections.defaultdict(set))      # target -> (side, attitude) -> posts
            for post in posts:
                if post["topic"] == tp and side_of[post["id"]]:
                    for a in post["aims"].values():
                        per[a["target"]][(side_of[post["id"]], a["attitude"])].add(post["id"])
            of_side = lambda v, s: set().union(*[ps for (sd, _), ps in v.items() if sd == s] or [set()])
            rows = [[g, label.get(g, g)] + [len(of_side(v, s)) for s in sides]
                    + [" / ".join(str(len(v.get((s, att), ()))) for s in sides) for att in ("backs", "opposes", "neither")]
                    for g, v in sorted(per.items(), key=lambda kv: -size(kv[1]))]
            T.append(f"{tp}: {len(per)} targets, {sum(size(v) >= 2 for v in per.values())} of them in 2 or more posts.\n")
            T.append(V.table(["target", "about", *[f"{s} posts" for s in sides],
                              *[f"{att} ({' / '.join(sides)})" for att in ("backs", "opposes", "neither")]], rows))
        T.append("## The points, counted by what the posts that use them are about\n")
        T.append("A unit is a point of the posts' own layer together with one target. A post counts for a unit if it states or relies on "
                 "the point in an argument that is about that target.\n")
        for tp in cache["points"]:
            pts = [n for n in of(tp, "own", True) if n["n_own"] >= 2]
            by_ta = {n["id"]: {k: {s: set(v[s]) for s in sides} for k, v in info[n["id"]]["aim"].items()} for n in pts}
            by_t = {}
            for n in pts:
                m = collections.defaultdict(lambda: {s: set() for s in sides})
                for (g, _), v in by_ta[n["id"]].items():
                    for s in sides:
                        m[g][s] |= v[s]
                by_t[n["id"]] = m
            units = [v for m in by_t.values() for v in m.values()]
            units_a = [v for m in by_ta.values() for v in m.values()]
            both = [n for n in pts if all(n["posts_by_side"][s] for s in sides)]
            T.append(f"{tp}: the {len(pts)} points in 2 or more posts make {len(units)} units; {sum(size(v) >= 2 for v in units)} of them are in 2 or more posts. "
                     f"Counting the attitude as well: {len(units_a)} units, {sum(size(v) >= 2 for v in units_a)} in 2 or more posts.\n")
            T.append(f"Points used by posts of every side: {len(both)}. With a target that posts of every side share: "
                     f"{sum(any(every(v) for v in by_t[n['id']].values()) for n in both)}; with a shared target and the same attitude to it: "
                     f"{sum(any(every(v) for v in by_ta[n['id']].values()) for n in both)}. Units with 2 or more posts from every side: "
                     f"{sum(every(v, 2) for v in units)}; with the same attitude as well: {sum(every(v, 2) for v in units_a)}.\n")
            rows = []
            for n in sorted(both, key=lambda n: -min(n["posts_by_side"][s] for s in sides) * 1000 - n["n_own"]):
                for (g, att), v in sorted(by_ta[n["id"]].items(), key=lambda kv: -size(kv[1])):
                    rows.append([n["id"], n["text"], label.get(g, g), att, *[len(v[s]) for s in sides]])
            T.append(V.table(["point", "claim", "what the posts are about", "attitude", *[f"{s} posts" for s in sides]], rows))

    T.append("## Where tweets land\n")
    T.append("A tweet's core parts are its stated reasons, its main point, and the unstated premises its reasoning leans on. "
             "A part counts as known if another tweet used to build the map states or relies on the same general point; "
             "a link counts as seen if another such tweet has it.\n")
    T.append(V.table(["tweet", "topic", "core parts known", "links seen", "verdict"], [
        [p["id"], p["topic"], f"{p['placing']['known']} of {p['placing']['core']}",
         f"{p['placing']['seen']} of {p['placing']['links']}", p["placing"]["verdict"]] for p in posts if p["held_out"]]))
    rows = []
    for tp in cache["points"]:
        for label, sel in (("set aside", [p for p in posts if p["topic"] == tp and p["held_out"]]),
                           ("used to build (each judged against the others)", [p for p in posts if p["topic"] == tp and not p["held_out"]])):
            core, known = sum(p["placing"]["core"] for p in sel), sum(p["placing"]["known"] for p in sel)
            v = C(p["placing"]["verdict"] for p in sel)
            rows.append([tp, label, len(sel), f"{known} of {core} ({100 * known / core:.0f}%)" if core else "-",
                         sum(p["placing"]["known"] == p["placing"]["core"] for p in sel), sum(p["placing"]["known"] == 0 for p in sel),
                         v["has parts that are new to the map"], v["known parts, new combination"],
                         v["known parts, combination seen before"], v["known parts, no links to compare"]])
    T.append(V.table(["topic", "tweets", "n", "core parts known", "tweets with every core part known", "tweets with none known",
                      "has new parts", "known parts, new combination", "known parts, combination seen before", "known parts, no links"], rows))
    kept = (f" The answers on what each objection goes against were kept from an earlier build for {cache['targets_kept']} arguments."
            if cache.get("targets_kept") else "")
    T.append(f"Calls used to build the map: {len(cache['usage'])}.{kept} Statements the placing step skipped: {cache['skipped']}.\n")
    (run_dir / "map_tables.md").write_text("\n".join(T))
    print(f"wrote {run_dir / 'map.json'} and {run_dir / 'map_tables.md'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
