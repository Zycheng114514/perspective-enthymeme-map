"""Write the project's web site into docs/: a front page and one map page for each run named below.

  python3 web/build_site.py        # from the project folder

GitHub Pages serves docs/ as the site. The pages are built from runs/ and data/, which are not part of the
public repository, so the built pages are kept in docs/ as they are.
"""

import collections
import json
from pathlib import Path

import map_html

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/Zycheng114514/perspective-enthymeme-map"
RUNS = [("congress-vax", "map-gpt.html"), ("opus-congress-vax", "map-opus.html")]     # run folder, page
POSTS = ROOT / "data" / "congress_vaccine_x.jsonl"          # the sample both runs read
ROLES = ROOT / "data" / "sample_poster_roles.json"          # who the posters are

INDEX = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Perspective Enthymeme Map</title>
<meta name="description" content="What do posts about vaccines leave unsaid, and which readers would fill in what? A map built by having simulated readers complete each post's reasoning.">
<style>
  :root { color-scheme: light; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --line: #dddcd6; --wash: #f0efec; }
  body { margin: 0; font: 16px/1.6 -apple-system, "Segoe UI", sans-serif; color: var(--ink); background: var(--surface); }
  main { max-width: 720px; margin: 0 auto; padding: 48px 20px 64px; }
  h1 { font-size: 30px; line-height: 1.2; margin: 0 0 12px; } h2 { font-size: 19px; margin: 40px 0 8px; }
  p, li { margin: 8px 0; } ol, ul { padding-left: 22px; }
  a { color: inherit; text-decoration-color: #b5b4ad; text-underline-offset: 3px; }
  .lede { font-size: 19px; color: var(--ink-2); margin-bottom: 28px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
  .card { display: block; border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; text-decoration: none; }
  .card:hover { background: var(--wash); }
  .card b { display: block; font-size: 17px; margin-bottom: 4px; } .card span { color: var(--ink-2); font-size: 14px; }
  .card em { display: block; font-style: normal; margin-top: 10px; text-decoration: underline; text-decoration-color: #b5b4ad; text-underline-offset: 3px; }
  .muted { color: var(--ink-2); } footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--line); color: var(--ink-2); font-size: 14px; }
</style></head><body><main>
<h1>Perspective Enthymeme Map</h1>
<p class="lede">Most arguments on social media leave part of their reasoning unsaid. This project asks what a post leaves unsaid, and which readers would fill in what.</p>

<div class="cards">__CARDS__</div>
<p class="muted">Both maps are built from the same posts by the same steps and prompts. Only the language model differs.</p>

<h2>What you are looking at</h2>
<p>The posts are __N__ public posts about vaccines on X, written between __FROM__ and __TO__ by __PEOPLE__ people in U.S. congressional politics: __SITTING__ sitting members of Congress and __CANDIDATES__ candidates in the 2026 elections. __SIDES__. For each post, a chain of language-model calls does the following.</p>
<ol>
  <li><b>It writes down what the post states</b>: its reasons and its point, in the post's own words, with nothing filled in.</li>
  <li><b>Six readings complete the reasoning.</b> Five are simulated readers, each a language model asked to speak as one person: someone who agrees with the side the post takes, someone who disagrees with it, someone scrolling past, someone who trusts vaccines, and someone who doubts them. Each reader sees only the post, and lays out how it gets to its point, what it takes for granted, where it is heading, and what they would say back. The sixth reading has no persona and adds as little as it can.</li>
  <li><b>One call merges what the readers wrote</b> into groups of statements that say the same thing. Each unstated premise is then tested: does the reasoning lean on it, or does it only restate the point?</li>
  <li><b>Every reader says whether they believe each statement.</b></li>
  <li><b>A map is built across posts</b>: the claims that recur, where each is stated and where it is left unsaid, how posts link them, and which reader believes them. About one post in six is kept aside and placed on the finished map afterwards, to see where a post the map has not seen lands.</li>
</ol>

<h2>What this is not</h2>
<ul>
  <li><b>No real readers were asked.</b> Every reader is a language model playing a role. "Believes" means what the model answers in that role.</li>
  <li><b>The personas are hand-written,</b> one for each side of the topic. Real readers are more varied than that.</li>
  <li><b>The claims are not checked for truth.</b> A claim on the map means that a post made it or relied on it, or that a simulated reader brought it up. It does not mean the claim is true.</li>
  <li><b>The posts are a sample,</b> not every post on the topic. Some of the __N__ are not on a map, mostly because they were judged not to make an argument or to be about another topic.</li>
</ul>

<h2>Sources</h2>
<p>The posts were collected with Junkipedia. The steps were developed and scored against the human annotations in Pastor and Oostdijk (2026), <i>A Resource for Enthymeme Detection in Controversial Political Discourse</i>, <a href="https://doi.org/10.48550/arXiv.2606.12186">arXiv:2606.12186</a> (CC BY 4.0).</p>
<p>The code and the prompts are on GitHub: <a href="__REPO__">__REPO_SHORT__</a>.</p>
<footer>Built __BUILT__.</footer>
</main></body></html>
"""

CARD = """<a class="card" href="{page}"><b>Read by {model}</b>
<span>{posts} posts on the map. {own} claims that posts state or rely on, {held} posts kept aside as new.</span><em>Open the map</em></a>"""


def month(day):
    y, m, _ = day.split("-")
    return f"{'January February March April May June July August September October November December'.split()[int(m) - 1]} {y}"


def main():
    out = ROOT / "docs"
    out.mkdir(exist_ok=True)
    (out / ".nojekyll").write_text("")          # the pages are plain files; GitHub Pages need not process them
    runs = [(page, map_html.load(ROOT / "runs" / run)) for run, page in RUNS]
    cards = []
    for i, (page, data) in enumerate(runs):
        other = [(p, d) for p, d in runs if p != page]
        nav = '<span><a href="index.html">About this map</a>' + "".join(f' · <a href="{p}">The same posts read by {d["model"]}</a>' for p, d in other) + "</span>"
        text = map_html.page(data, nav=nav)
        (out / page).write_text(text, encoding="utf-8")
        print(f"wrote docs/{page} ({len(text.encode()) // 1024} KB)")
        cards.append(CARD.format(page=page, model=data["model"], posts=len(data["posts"]), held=sum(p["new"] for p in data["posts"]),
                                 own=sum(n["layer"] == "own" and not n["parent"] for n in data["nodes"])))
    rows = [json.loads(line) for line in POSTS.open(encoding="utf-8") if line.strip()]      # one post per line; a post's own text can hold other line breaks
    roles = json.loads(ROLES.read_text())
    people = {str(r["actor_id"]) for r in rows}
    sitting = sum(roles.get(a, {}).get("role", "").startswith("Sitting") for a in people)
    sides = collections.Counter(r["party"] for r in rows)
    days = sorted(r["published_at"][:10] for r in rows)
    fill = {"__CARDS__": "\n".join(cards), "__N__": len(rows), "__PEOPLE__": len(people), "__SITTING__": sitting, "__CANDIDATES__": len(people) - sitting,
            "__FROM__": month(days[0]), "__TO__": month(days[-1]),
            "__SIDES__": "Of the posts, " + " and ".join(f"{n} are by {'Democrats' if s == 'Democratic' else s + 's'}" for s, n in sides.most_common()),
            "__REPO__": REPO, "__REPO_SHORT__": REPO.split("//")[1], "__BUILT__": "from the runs of November 2026"}
    text = INDEX
    for k, v in fill.items():
        text = text.replace(k, str(v))
    (out / "index.html").write_text(text, encoding="utf-8")
    print(f"wrote docs/index.html ({len(text.encode()) // 1024} KB)")


if __name__ == "__main__":
    main()
