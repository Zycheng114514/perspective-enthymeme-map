"""Draw the map built by build_map.py as one self-contained web page.

  python3 web/map_html.py runs/congress-vax                      # from the project folder; writes runs/congress-vax/map.html
  python3 web/map_html.py runs/congress-vax docs/map-gpt.html    # writes there instead

Reads map.json of the run (the "layers" design: claims posts state or rely on, where readers think posts are
heading, what readers say against them) and, for who posted each post, state.json of the same run.
The page needs an internet connection to load the graph library. web/build_site.py writes the whole site.
"""

import collections
import json
import sys
from pathlib import Path

MODELS = {"gpt-6.1-sol": "GPT-6.1 Sol", "claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5"}
TOPICS = {"vaccine": "Vaccines", "immigration": "Immigration"}

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  :root { color-scheme: light;
    --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --line: #dddcd6; --wash: #f0efec; --edge: #b5b4ad;
    --stated: #2a78d6; --unsaid: #eb6834; --heading: #1baf7a; --against: #8a8984;
    --reader-a: #6250d6; --reader-b: #eb6834; --side-a: #2a78d6; --side-b: #e34948; --both: #52514e; --neither: #c3c2b7; }
  html, body { height: 100%; }
  body { margin: 0; font: 14px/1.5 -apple-system, "Segoe UI", sans-serif; color: var(--ink); background: var(--surface);
         display: flex; height: 100vh; overflow: hidden; }
  #left { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  #bar { padding: 10px 14px 8px; border-bottom: 1px solid var(--line); display: flex; gap: 8px 16px; align-items: center; flex-wrap: wrap; }
  #keys { padding: 6px 14px; border-bottom: 1px solid var(--line); display: flex; gap: 4px 16px; flex-wrap: wrap; color: var(--ink-2); font-size: 13px; }
  #net, #table { flex: 1; min-height: 0; position: relative; }
  #table { display: none; overflow: auto; padding: 0 14px 14px; }
  #side { width: 34%; max-width: 460px; border-left: 1px solid var(--line); padding: 14px 16px; overflow: auto; }
  select, input, button { font: inherit; color: inherit; } select { max-width: 280px; }
  button { border: 1px solid var(--line); background: var(--surface); border-radius: 6px; padding: 2px 10px; cursor: pointer; }
  a { color: inherit; }
  h1 { font-size: 16px; margin: 0 0 6px; } h2 { font-size: 14px; margin: 18px 0 6px; } p { margin: 6px 0; }
  .muted, .crumb, .by { color: var(--ink-2); } .crumb { margin: 0 0 4px; font-size: 13px; } .by { font-size: 13px; margin-bottom: 4px; }
  .post { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin: 8px 0; }
  .go { cursor: pointer; } .post.go:hover, tr.go:hover { background: var(--wash); }
  a.go { text-decoration: underline; text-decoration-color: var(--edge); }
  .uses { margin-top: 6px; }
  .key i, .tag i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }
  .tag { font-size: 12px; color: var(--ink-2); }
  .line { display: inline-block; width: 26px; border-top: 2px solid var(--edge); vertical-align: 4px; margin-right: 6px; }
  .line.dash { border-top-style: dashed; } .line.dot { border-top-style: dotted; }
  .box { display: inline-block; width: 16px; height: 10px; border: 2px solid var(--ink-2); border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
  .box.dash { border-style: dashed; } .box.dot { border-style: dotted; }
  .layers label { margin-right: 10px; white-space: nowrap; }
  table { border-collapse: collapse; width: 100%; }
  td, th { text-align: left; padding: 4px 8px 4px 0; border-bottom: 1px solid var(--line); font-size: 13px; vertical-align: top; }
  th { color: var(--ink-2); font-weight: 600; position: sticky; top: 0; background: var(--surface); }
  @media (max-width: 820px) {       /* a narrow screen: the controls, then the map, then the panel, one under the other */
    body { display: block; height: auto; overflow: auto; }
    #left { display: block; } #net, #table { height: 70vh; }
    #side { width: auto; max-width: none; border-left: 0; border-top: 1px solid var(--line); overflow: visible; }
  }
</style></head><body>
<div id="left">
  <div id="bar">
    __NAV__
    <label id="topicBox">Topic <select id="topic"></select></label>
    <span class="layers">Show
      <label title="Claims that at least one post states or relies on."><input type="checkbox" id="L_own" checked> claims posts state or rely on</label>
      <label title="Claims that no post states or relies on: only readers bring them, as where they think a post is heading."><input type="checkbox" id="L_heading"> where readers think posts are heading</label>
      <label title="Claims that no post states or relies on: only readers bring them, as something to say against a post."><input type="checkbox" id="L_against"> what readers say against posts</label>
    </span>
    <label title="For the claims that posts state or rely on.">Found in at least <input id="min" type="number" min="1" value="2" style="width:3.6em"> posts</label>
    <label id="min2Box" title="Claims that only readers bring are found in fewer posts, so they have a number of their own.">Readers' claims: at least <input id="min2" type="number" min="1" value="2" style="width:3.6em"> posts</label>
    <label title="A link is drawn when at least this many posts make it. The links of a post you place are always drawn.">Links in at least <input id="minLink" type="number" min="1" value="2" style="width:3.2em"> posts</label>
    <label>Colour by <select id="mode"><option value="use">how posts use the claim</option><option value="belief">which reader believes it</option><option value="party">whose posts it is found in</option></select></label>
    <label>Place a post <select id="post"></select></label>
    <button id="view">Show as table</button>
  </div>
  <div id="keys"></div>
  <div id="net"></div>
  <div id="table"></div>
</div>
<div id="side"></div>
<script>
const MAP = __DATA__;
const el = id => document.getElementById(id);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : "";
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const byId = Object.fromEntries(MAP.nodes.map(n => [n.id, n]));
const postById = Object.fromEntries(MAP.posts.map(p => [p.id, p]));
const rootOf = id => (byId[id] && byId[id].parent) || id;       // a narrower version is drawn as the general claim it belongs to
const kids = {};
for (const n of MAP.nodes) if (n.parent) (kids[n.parent] = kids[n.parent] || []).push(n);
const SIDES = MAP.sides;
const READERS = {vaccine: [["trusts_vaccines", "reader who trusts vaccines"], ["doubts_vaccines", "reader who doubts vaccines"]],
                 immigration: [["favours_immigration", "reader who favours immigration"], ["wants_less_immigration", "reader who wants less immigration"]]};
const WHO = {agrees: "agrees with the side the post takes", disagrees: "disagrees with the side the post takes", scroller: "is scrolling past",
             trusts_vaccines: "trusts vaccines", doubts_vaccines: "doubts vaccines",
             favours_immigration: "favours immigration", wants_less_immigration: "wants less immigration"};
const LAYER = {own: "A claim that posts state or rely on", heading: "A claim only readers bring: where they think posts are heading",
               against: "A claim only readers bring: what they say against posts"};
const KIND = {"stated reason": ["--stated", "stated as a reason"], "stated conclusion": ["--stated", "stated as the point"],
              "unstated premise": ["--unsaid", "left unsaid: a step in the reasoning"], "taken for granted": ["--unsaid", "left unsaid: taken for granted"],
              "reader conclusion": ["--unsaid", "left unsaid: the point, as readers put it"],
              "heading": ["--heading", "where readers think the post is heading"], "objection": ["--against", "what a reader says against it"]};
const LINK = {supports: ["", "offered or leaned on as support for"], "read as": ["dash", "readers think a post making the first point is heading to the second"],
              against: ["dot", "said by readers against"]};
const DASH = {"": false, dash: [8, 6], dot: [2, 6]};

const K = (n, ...ks) => ks.reduce((s, k) => s + (n.kinds[k] || 0), 0);
const stated = n => K(n, "stated reason", "stated conclusion");
const unsaid = n => K(n, "unstated premise", "taken for granted", "reader conclusion");
const tag = kind => `<span class="tag"><i style="background:var(${KIND[kind][0]})"></i>${KIND[kind][1]}</span>`;
function useVar(n) {       // the way the claim is used most often; a tie goes to the earlier one
  return [[stated(n), "--stated"], [unsaid(n), "--unsaid"], [K(n, "heading"), "--heading"], [K(n, "objection"), "--against"]].reduce((a, b) => b[0] > a[0] ? b : a)[1];
}
function beliefVar(n) {
  const [a, b] = (READERS[n.topic] || []).map(r => n.holds[r[0]]);
  return a === "yes" && b === "yes" ? "--both" : a === "yes" ? "--reader-a" : b === "yes" ? "--reader-b" : "--neither";
}
function partyVar(n) {
  const [a, b] = SIDES.map(s => n.side[s] || 0);
  return a && b ? "--both" : a ? "--side-a" : b ? "--side-b" : "--neither";
}
const colourOf = n => css({use: useVar, belief: beliefVar, party: partyVar}[el("mode").value](n));
const tint = hex => "#" + [1, 3, 5].map(i => Math.round(parseInt(hex.slice(i, i + 2), 16) * 0.2 + parseInt(css("--surface").slice(i, i + 2), 16) * 0.8).toString(16).padStart(2, "0")).join("");
function tip(text) {
  const d = document.createElement("div");
  d.style.cssText = "white-space:pre-line;max-width:340px;font:12px/1.45 -apple-system,'Segoe UI',sans-serif";
  d.textContent = text; return d;
}
function summary(n) {
  const bits = [[stated(n), "stated outright"], [K(n, "unstated premise"), "left unsaid as a step in the reasoning"], [K(n, "taken for granted"), "left unsaid as something taken for granted"],
                [K(n, "reader conclusion"), "read as a post's unstated point"], [K(n, "heading"), "named as where a post is heading"], [K(n, "objection"), "said against a post"]]
    .filter(b => b[0]).map(b => `${b[1]} ${b[0]} time${b[0] === 1 ? "" : "s"}`);
  return `Found in ${plural(n.n, "post")}: ${bits.join(", ")}.`;
}
const sideLine = n => SIDES.length ? `${n.layer === "own" ? "Posts that state or rely on it" : "Posts it was said about"}: ${SIDES.map(s => `${s} ${n.side[s] || 0}`).join(", ")}.` : "";
const direct = n => (READERS[n.topic] || []).map(r => `${cap(r[1])}: ${n.holds[r[0]] || "not asked"}`).join(" · ");
const byline = p => `<div class="by">${[p.who ? esc(p.who) + (p.party ? ` (${esc(p.party)})` : "") : "", esc(p.date)].filter(Boolean).join(" · ")}` +
  (p.url ? ` · <a href="${esc(p.url)}" target="_blank" rel="noopener">original post</a>` : "") + `</div>`;

let network, nodesDS, shown = [], current = null, linkKinds = new Set();
const narrow = matchMedia("(max-width: 820px)");
const toPanel = () => { el("side").scrollTop = 0; if (narrow.matches) el("side").scrollIntoView({behavior: "smooth"}); };     // on a narrow screen the panel is below the map
const layersOn = () => new Set(["own", "heading", "against"].filter(l => el("L_" + l).checked));
function atMost(layers, k) {     // the smallest number of posts that leaves at most k claims of each of these layers: few enough to read at a glance
  let best = 2;
  for (const l of layers) {
    const ns = MAP.nodes.filter(n => !n.parent && n.topic === el("topic").value && n.layer === l).map(n => n.n).sort((a, b) => b - a);
    if (ns.length > k) best = Math.max(best, ns[k - 1] + (ns[k] === ns[k - 1] ? 1 : 0));
  }
  return best;
}
function defaultMin() { el("min").value = atMost(["own"], 24); el("min2").value = atMost(["heading", "against"], 8); }
function keys() {
  const R = READERS[el("topic").value] || [["", "first reader"], ["", "second reader"]], on = layersOn();
  const items = {use: [["--stated", "mostly stated outright"], ["--unsaid", "mostly left unsaid"], ["--heading", "mostly where readers think posts are heading"], ["--against", "mostly said against posts"]],
    belief: [["--reader-a", `only the ${R[0][1]} believes it`], ["--reader-b", `only the ${R[1][1]} believes it`], ["--both", "both believe it"], ["--neither", "neither says yes"]],
    party: [["--side-a", `only ${SIDES[0]} posts`], ["--side-b", `only ${SIDES[1]} posts`], ["--both", "posts of both parties"], ["--neither", "no posts"]]}[el("mode").value];
  const what = el("mode").value === "party" ? "For a claim that posts state or rely on: the posts that do. For a claim only readers bring: the posts it was said about." : "";
  let h = items.map(([c, t]) => `<span class="key" title="${what}"><i style="background:var(${c})"></i>${t}</span>`).join("");
  if (on.has("heading")) h += `<span><span class="box dash"></span>only readers bring it: where posts are heading</span>`;
  if (on.has("against")) h += `<span><span class="box dot"></span>only readers bring it: said against posts</span>`;
  for (const k of ["supports", "read as", "against"]) if (linkKinds.has(k))
    h += `<span><span class="line ${LINK[k][0]}"></span>${k === "supports" ? "offered or leaned on as support" : k === "read as" ? "from a post's point to where readers think it is heading" : "said by readers against the claim it points to"}</span>`;
  el("keys").innerHTML = h;
}
function nodeStyle(n) {
  const c = colourOf(n), on = current && current.nodes.has(n.id);
  return {id: n.id, opacity: current && !on ? 0.3 : 1, borderWidth: on ? 4 : 2,
          color: {background: tint(c), border: on ? css("--ink") : c, highlight: {background: tint(c), border: css("--ink")}, hover: {background: tint(c), border: css("--ink-2")}}};
}
function spread() {       // the layout packs the boxes closely so their text stays readable; this moves apart the ones that overlap, as little as needed
  const ids = shown.map(n => n.id), pos = network.getPositions(ids), box = {};
  for (const i of ids) { const b = network.getBoundingBox(i); box[i] = {w: b.right - b.left + 18, h: b.bottom - b.top + 14}; }
  for (let pass = 0; pass < 300; pass++) {
    let moved = false;
    for (let a = 0; a < ids.length; a++) for (let b = a + 1; b < ids.length; b++) {
      const p = pos[ids[a]], q = pos[ids[b]], A = box[ids[a]], B = box[ids[b]];
      const dx = q.x - p.x, dy = q.y - p.y, ox = (A.w + B.w) / 2 - Math.abs(dx), oy = (A.h + B.h) / 2 - Math.abs(dy);
      if (ox > 0.5 && oy > 0.5) { moved = true;
        if (ox < oy) { const s = (dx < 0 ? -1 : 1) * ox / 2; p.x -= s; q.x += s; } else { const s = (dy < 0 ? -1 : 1) * oy / 2; p.y -= s; q.y += s; } }
    }
    if (!moved) break;
  }
  for (const i of ids) network.moveNode(i, pos[i].x, pos[i].y);
}
function draw() {
  const topic = el("topic").value, min = +el("min").value, min2 = +el("min2").value, minLink = +el("minLink").value, on = layersOn();
  el("min2Box").style.display = on.has("heading") || on.has("against") ? "" : "none";
  shown = MAP.nodes.filter(n => !n.parent && n.topic === topic && ((on.has(n.layer) && n.n >= (n.layer === "own" ? min : min2)) || (current && current.nodes.has(n.id))));
  const ids = new Set(shown.map(n => n.id));
  nodesDS = new vis.DataSet(shown.map(n => ({...nodeStyle(n), label: n.text, value: n.n,
    shapeProperties: {borderRadius: 6, borderDashes: n.layer === "heading" ? [6, 4] : n.layer === "against" ? [2, 3] : false},
    title: tip([summary(n), sideLine(n), direct(n)].filter(Boolean).join("\n"))})));
  const links = MAP.edges.filter(e => ids.has(e.from) && ids.has(e.to) && (e.posts.length >= minLink || (current && e.posts.includes(current.id))));
  const edges = links.map(e => { const hit = current && e.posts.includes(current.id);
    return {from: e.from, to: e.to, arrows: "to", dashes: DASH[LINK[e.kind][0]], width: hit ? 3 : Math.min(5, e.posts.length),
            color: {color: hit ? css("--ink") : css("--edge"), opacity: current && !hit ? 0.35 : 1, highlight: css("--ink-2"), hover: css("--ink-2")},
            title: tip(`${cap(LINK[e.kind][1])}${e.kind === "read as" ? "" : " the claim it points to"}, in ${plural(e.posts.length, "post")}`)}; });
  if (network) network.destroy();
  network = new vis.Network(el("net"), {nodes: nodesDS, edges}, {layout: {randomSeed: 7},
    physics: {solver: "forceAtlas2Based", forceAtlas2Based: {gravitationalConstant: -120, centralGravity: 0.04, springLength: 150, springConstant: 0.06, avoidOverlap: 1}, stabilization: {iterations: 1000}},
    nodes: {shape: "box", margin: 9, widthConstraint: {maximum: 190}, borderWidthSelected: 4,
            font: {size: 14, color: css("--ink"), face: "-apple-system, 'Segoe UI', sans-serif"}, scaling: {label: {enabled: true, min: 15, max: 20}}},
    edges: {smooth: {type: "continuous"}}, interaction: {hover: true, tooltipDelay: 120}});
  network.once("stabilizationIterationsDone", () => { network.setOptions({physics: false}); spread(); network.fit(); });
  network.on("click", ev => { if (ev.nodes.length) showNode(ev.nodes[0]); });
  linkKinds = new Set(links.map(e => e.kind)); keys(); table();
}
function table() {
  const R = READERS[el("topic").value] || [];
  const short = {own: "posts state or rely on it", heading: "only readers: heading", against: "only readers: against"};
  const rows = [...shown].sort((a, b) => b.n - a.n).map(n =>
    `<tr class="go" data-node="${esc(n.id)}"><td>${esc(n.text)}</td><td>${short[n.layer]}</td><td>${n.n}</td>${SIDES.map(s => `<td>${n.side[s] || 0}</td>`).join("")}` +
    `<td>${stated(n)}</td><td>${unsaid(n)}</td><td>${K(n, "heading")}</td><td>${K(n, "objection")}</td>${R.map(r => `<td>${esc(n.holds[r[0]] || "-")}</td>`).join("")}</tr>`);
  el("table").innerHTML = `<table><tr><th>Claim</th><th>Kind</th><th>Posts</th>${SIDES.map(s => `<th>${esc(s)} posts</th>`).join("")}` +
    `<th>Times stated</th><th>Times left unsaid</th><th>Times named as where a post is heading</th><th>Times said against a post</th>${R.map(r => `<th>${cap(r[1])}</th>`).join("")}</tr>${rows.join("")}</table>`;
}
function home() {
  const ps = MAP.posts.filter(p => p.topic === el("topic").value), held = ps.filter(p => p.new).length;
  el("side").innerHTML = `<h1>What posts say and what they leave unsaid</h1>
    <p>Each box is a claim that recurs across posts. The more posts it is found in, the larger it is drawn. A claim can be stated outright in one post and left unsaid in another. What a post leaves unsaid was filled in by simulated readers.</p>
    <p>Click a claim to see the posts behind it and which reader believes it. Choose a post under "Place a post" to see where its parts fall on the map. "Show" adds the claims that only readers bring.</p>
    <p class="muted">Built from ${plural(ps.length - held, "post")} on this topic; ${held} more were kept aside to test placing a post the map had not seen.</p>
    <p class="muted">Every reader is simulated by a language model (${esc(MAP.model)}). No real readers were asked: "believes" means what the model answers when it is asked to speak as that reader. The claims are not checked for truth, and a claim shown here is not a statement that it is true.</p>`;
}
function showNode(id) {
  const n = byId[id]; if (!n) return;
  const R = READERS[n.topic] || [];
  let h = `<p class="crumb">${LAYER[n.layer]}</p><h1>${esc(n.text)}</h1><p class="muted">${esc(summary(n))} ${esc(sideLine(n))}</p>`;
  if (n.parent) h += `<p>A narrower version of: <a class="go" data-node="${esc(n.parent)}">${esc(byId[n.parent].text)}</a></p>`;
  if (R.length) h += `<h2>Asked directly, does the reader believe it?</h2><table>` + R.map(r => `<tr><td>${cap(r[1])}</td><td>${esc(n.holds[r[0]] || "not asked")}</td></tr>`).join("") + `</table>`;
  const readers = Object.keys(n.believe);
  if (readers.length) h += `<h2>When readers rated it as part of a post</h2><table><tr><th>Reader who</th><th>yes</th><th>no</th><th>unsure</th></tr>` +
    readers.map(r => `<tr><td>${esc(WHO[r] || r)}</td><td>${n.believe[r].yes || 0}</td><td>${n.believe[r].no || 0}</td><td>${n.believe[r].unsure || 0}</td></tr>`).join("") + `</table>`;
  const ks = (kids[id] || []).slice().sort((a, b) => b.n - a.n);
  if (ks.length) h += `<h2>Narrower versions</h2>` + ks.map(k => `<p><a class="go" data-node="${esc(k.id)}">${esc(k.text)}</a> <span class="muted">${plural(k.n, "post")}</span></p>`).join("");
  const aims = n.aims.filter(a => MAP.targets[a.target]);
  if (aims.length) h += `<h2>What those posts are about</h2><table><tr><th>About</th><th>The posts</th>${SIDES.map(s => `<th>${esc(s)}</th>`).join("")}</tr>` +
    aims.map(a => `<tr><td>${esc(MAP.targets[a.target])}</td><td>${esc({backs: "back it", opposes: "oppose it"}[a.attitude] || "take no side")}</td>${SIDES.map(s => `<td>${a.posts_by_side[s] || 0}</td>`).join("")}</tr>`).join("") + `</table>`;
  h += `<h2>Posts</h2>` + n.posts.map(t => { const p = postById[t];
    const here = p.parts.filter(x => x.node && (x.node === id || rootOf(x.node) === id)).map(x => `${tag(x.kind)} ${esc(x.wording)}`).join("<br>");
    return `<div class="post go" data-post="${esc(t)}">${byline(p)}<div>${esc(p.text)}</div><div class="uses">${here}</div></div>`; }).join("");
  el("side").innerHTML = h; toPanel();
  if (network && nodesDS.get(rootOf(id))) network.selectNodes([rootOf(id)]);
  history.replaceState(null, "", "#claim=" + encodeURIComponent(id));
}
function pick(id) {
  const p = postById[id], pl = p && p.placing; if (!p) return;
  el("post").value = id;
  if (el("topic").value !== p.topic) { el("topic").value = p.topic; defaultMin(); }
  current = {id, nodes: new Set(p.parts.map(x => x.node && rootOf(x.node)).filter(Boolean))};
  draw();
  let h = `<p class="crumb">${p.new ? "A post the map had not seen" : "A post used to build the map"}</p>${byline(p)}<div class="post">${esc(p.text)}</div>
    <p><b>${cap(pl.verdict)}.</b> ${pl.known} of its ${pl.core} core parts ${pl.known === 1 ? "sits" : "sit"} on a claim that other posts also state or rely on` +
    (pl.links ? `, and ${pl.seen} of its ${plural(pl.links, "link")} between claims ${pl.seen === 1 ? "was" : "were"} seen in other posts` : "") +
    `. <span class="muted">Core parts are the stated reasons, the main point, and the unstated parts the reasoning leans on.</span></p>`;
  const units = [...new Set(p.parts.map(x => x.unit))];
  for (const u of units) {
    const own = p.parts.filter(x => x.unit === u), aim = p.aims[u];
    h += `<h2>${units.length > 1 ? "Argument " + esc(u.replace(/^A/, "")) + ": its parts" : "Its parts"}</h2>`;
    if (aim) h += `<p class="muted">About: ${esc(aim.about)}${aim.attitude === "backs" ? " (backed by the post)" : aim.attitude === "opposes" ? " (opposed by the post)" : ""}</p>`;
    for (const x of own) {
      const hit = x.kind === "objection" && own.find(y => y.i === x.target);
      const note = x.kind === "unstated premise" || x.kind === "taken for granted" ? (x.role === "support" ? "the reasoning leans on it" : "the reasoning does not lean on it")
        : x.kind === "reader conclusion" ? (x.role === "main" ? "the reading most readers share" : "another reading of its point")
        : hit ? `against: "${hit.wording}"` : "";
      h += `<p>${tag(x.kind)}${note ? `<span class="tag"> · ${esc(note)}</span>` : ""}<br>${esc(x.wording)}<br><span class="muted">` +
        (x.node ? `on the map as: <a class="go" data-node="${esc(x.node)}">${esc(byId[x.node].text)}</a>` : "not on the map") + `</span></p>`;
    }
  }
  el("side").innerHTML = h; toPanel();
  history.replaceState(null, "", "#post=" + encodeURIComponent(id));
}
function reset() { el("post").value = ""; current = null; draw(); home(); history.replaceState(null, "", location.pathname + location.search); }

el("topic").innerHTML = MAP.topics.map(t => `<option value="${esc(t[0])}">${esc(t[1])} (${plural(t[2], "post")})</option>`).join("");
if (MAP.topics.length < 2) el("topicBox").style.display = "none";
if (SIDES.length !== 2) el("mode").querySelector('[value="party"]').remove();
const sel = el("post");
sel.innerHTML = `<option value="">(choose)</option>` + MAP.posts.map(p => `<option value="${esc(p.id)}">${p.new ? "NEW · " : ""}${esc((p.who ? p.who + ": " : "") + p.text.slice(0, 60))}</option>`).join("");
sel.onchange = () => sel.value ? pick(sel.value) : reset();
el("topic").onchange = () => { defaultMin(); reset(); };
for (const i of ["min", "min2", "minLink"]) el(i).onchange = () => draw();
for (const l of ["own", "heading", "against"]) el("L_" + l).onchange = () => draw();
el("mode").onchange = () => { nodesDS.update(shown.map(nodeStyle)); keys(); };
el("view").onclick = () => { const t = el("table").style.display !== "block";
  el("table").style.display = t ? "block" : "none"; el("net").style.display = t ? "none" : "block";
  el("view").textContent = t ? "Show as map" : "Show as table"; if (!t) network.fit(); };
document.addEventListener("click", ev => {
  if (ev.target.closest("a[href]")) return;
  const a = ev.target.closest("[data-node], [data-post]");
  if (a) a.dataset.node ? showNode(a.dataset.node) : pick(a.dataset.post);
});
window.addEventListener("resize", () => network && network.fit());
function fromLink() {       // a link can name a claim or a post: ...#claim=ID or ...#post=ID
  const at = location.hash.match(/^#(claim|post)=(.+)$/), id = at && decodeURIComponent(at[2]);
  if (at && at[1] === "post" && postById[id]) pick(id);
  else if (at && byId[id]) { if (el("topic").value !== byId[id].topic) { el("topic").value = byId[id].topic; defaultMin(); draw(); } showNode(id); }
}
window.addEventListener("hashchange", fromLink);
defaultMin(); draw(); home(); fromLink();
</script></body></html>
"""


def load(run_dir):
    """What the page needs from a run: the map, slimmed to the fields the page shows, and who posted each post."""
    m = json.loads((run_dir / "map.json").read_text())
    if m.get("design") != "layers":
        raise SystemExit(f"{run_dir / 'map.json'} was built by an earlier design; this page draws the 'layers' design only.")
    state = run_dir / "state.json"
    meta = {i: t.get("meta") or {} for i, t in json.loads(state.read_text())["tweets"].items()} if state.exists() else {}
    posts = []
    for p in m["posts"]:
        x = meta.get(p["id"], {})
        parts = [{"i": s["key"].split(":")[2], **{k: s[k] for k in ("unit", "kind", "role", "wording", "node", "target") if s.get(k) is not None}} for s in p["parts"]]
        posts.append({"id": p["id"], "text": p["tweet"], "topic": p["topic"], "new": p["held_out"], "who": x.get("actor_name"), "party": x.get("party"),
                      "date": (x.get("published_at") or "")[:10], "url": x.get("url"), "aims": p.get("aims", {}), "placing": p["placing"], "parts": parts})
    nodes = [{"id": n["id"], "text": n["text"], "topic": n["topic"], "layer": n["layer"], "parent": n["parent"], "n": n["n_tweets"], "posts": n["tweets"],
              "kinds": n["kinds"], "holds": n["holds"], "believe": n["believe"], "side": n["posts_by_side"], "aims": n["by_target"][:8]} for n in m["nodes"]]
    edges = [{"from": e["from"], "to": e["to"], "kind": e["kind"], "posts": e["tweets"]} for e in m["edges_between_general_points"]]
    count = collections.Counter(p["topic"] for p in posts)
    return {"model": MODELS.get(m["model"], m["model"]), "topics": [[k, TOPICS.get(k, k), n] for k, n in count.most_common()],
            "sides": sorted({p["party"] for p in posts if p["party"]}), "targets": {t["id"]: t["about"] for t in m["targets"]},
            "nodes": nodes, "edges": edges, "posts": posts}


def page(data, title=None, nav=""):
    """The page as one string. nav is HTML put at the start of the bar (links to the rest of a site), or nothing."""
    title = title or f"Argument map ({data['model']}): what posts say and what they leave unsaid"
    return PAGE.replace("__TITLE__", title).replace("__NAV__", nav).replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))


def main(run_dir, out=None):
    out = Path(out) if out else run_dir / "map.html"
    text = page(load(run_dir))
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.encode()) // 1024} KB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]), *sys.argv[2:3])
