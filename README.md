# Perspective Enthymeme Map

**The site: <https://zycheng114514.github.io/perspective-enthymeme-map/>**

Most arguments on social media leave part of their reasoning unsaid. This project asks what a post leaves unsaid, and which readers would fill in what.

A chain of language-model calls writes down what each post states, has several simulated readers complete its reasoning, and builds a map of the claims that recur across posts: where each claim is stated, where it is left unsaid, and which reader believes it.

The site shows two maps built from the same 247 posts about vaccines, written on X by members of the U.S. Congress and candidates in the 2026 congressional elections. One map was read by GPT-6.1 Sol, the other by Claude Opus 5.5.

## The steps

| Step | What it does | Prompt |
| --- | --- | --- |
| extract | Writes down what the post states, in its own words, as one or more arguments. Nothing is filled in. | `prompts/pipeline/extract.txt` |
| aim | Says who or what an argument is about, and whether the post backs or opposes it. | `prompts/pipeline/aim.txt` |
| complete | Each simulated reader lays out how the post gets to its point, what it takes for granted, where it is heading, and what they would say back. The reader sees only the post. | `prompts/pipeline/complete.txt`, with a persona from `prompts/readers/` |
| minimal | The same laying-out with no persona and as little added as possible. | `prompts/pipeline/complete_minimal.txt` |
| merge | One call groups what the readers wrote into statements that say the same thing. | `prompts/pipeline/merge.txt` |
| test | For each unstated premise: does the reasoning lean on it, and does it only restate the point? | `prompts/pipeline/test.txt` |
| rate | Each reader says whether they believe each merged statement. | `prompts/pipeline/rate.txt` |
| score | On tweets that carry human annotations: what the readers supplied, set against the annotators' reconstructions. | `prompts/pipeline/score.txt` |
| map | Across posts: the claims that recur, the links posts make between them, and where a post the map has not seen lands. | `prompts/map/` |

There are five simulated readers. Three are defined by how they stand to the post: one agrees with the side it takes, one disagrees, one is scrolling past. Two are defined by what they already believe about the topic; for vaccines, one trusts them and one doubts them.

## What is in this repository

| Path | Contents |
| --- | --- |
| `src/pipeline.py` | The steps for one post, from extract to score |
| `src/build_map.py` | The map across posts |
| `src/prompts.py` | Reads the prompt files |
| `prompts/` | Every prompt, one text file each |
| `job.sh` | Runs the pipeline and then the map as one job that goes on by itself |
| `web/` | `map_html.py` draws a map as a web page; `build_site.py` writes the site |
| `docs/` | The site, as GitHub Pages serves it |

The posts, the outputs of the runs and the working reports are not in this repository. The pages in `docs/` were built from them and are kept as built.

## Running it on posts of your own

You need Python 3 (3.12 is the version in use), the packages in `requirements.txt`, and one way to reach a model:

- `--via codex`, the default: the Codex command line, logged in.
- `--via cli`: the Claude Code command line, logged in.
- `--via direct` or `--via batch`: the Anthropic API, with the key in `ANTHROPIC_API_KEY`.

The posts go in a `.jsonl` file, one post per line, with at least `id` and `text`. `party`, `actor_name`, `handle`, `published_at` and `url` are used where they are present.

```bash
./job.sh start NAME N --data posts.jsonl --workers 6   # the first N posts of the file: the pipeline, then the map
./job.sh status NAME                                   # how far the run has got
python3 web/map_html.py runs/NAME                      # draws runs/NAME/map.json as runs/NAME/map.html
```

The results of a run are written to `runs/NAME/`. `job.sh` was written and used on macOS.

Two things to know before running it on other material:

- The two readers defined by belief, and the map, are set up for two topics: vaccines and immigration. A post on another topic is read by the other readers only, and is left off the map.
- Without `--data`, the pipeline runs on the tweets of Pastor and Oostdijk's dataset (see below), which carry human annotations of missing premises. Set `ENTHYMEMES_CSV` to where its `enthymemes.csv` is.

## Limits

- **No real readers were asked.** Every reader is a language model playing a role. "Believes" means what the model answers in that role.
- **The personas are hand-written,** one for each side of a topic. Real readers are more varied than that.
- **Every judgment is the model's.** Whether two statements say the same thing, and whether the reasoning leans on a premise, are decided by model calls. People have not checked them.
- **The claims are not checked for truth.** A claim on the map means that a post made it or relied on it, or that a simulated reader brought it up.

## Sources

- The posts were collected with Junkipedia.
- The steps were developed and scored against the human annotations in Pastor and Oostdijk (2026), *A Resource for Enthymeme Detection in Controversial Political Discourse*, <https://doi.org/10.48550/arXiv.2606.12186> (CC BY 4.0).
