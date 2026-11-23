---
name: tg-collab
description: Find peer Telegram channels, rank them by topic similarity and size, keep a local outreach CRM, and draft personal collaboration messages with the user's own AI agent. Use when the user asks to find partner channels, analyze channel metrics, import agent-found channels, export an outreach list, or write collaboration drafts. The tool never sends messages.
---

# tg-collab skill

Use this skill when the owner of a Telegram channel wants to find channels of
a comparable size and run white-hat outreach. The tool is local: SQLite plus
the user's own machine. The human sends every message.

## When to use

- The user asks to find partner or peer channels in Telegram.
- The user wants to profile their own channel and get a partner size range.
- The user wants an outreach list, similarity and size scores, quality flags,
  contact options, or a digest.
- The user wants personal collaboration drafts written for chosen channels.
- The user wants an external agent to search for channels and bring the
  results back into the tool.

## Prerequisites

- `uv` is installed and the package is synced (`uv sync`).
- No API keys are required. Telegram Public (`t.me/s/`) is the base source,
  Mojeek, Yandex, Google, DuckDuckGo, Bing, Yahoo, Startpage, Hacker News, and GitHub work without keys,
  and the local AI agent search is on by default.
- The user's own channel must have its public link preview enabled.
- Optional: `TGSTAT_TOKEN`, `GITHUB_TOKEN`, `BRAVE_SEARCH_API_KEY`,
  `SEARXNG_URL`.

## Discovery sources

`find` starts from the `t.me/s/` mention graph of the owner's channel, then
adds candidates from the sources that are on by default: the local AI agent,
GitHub repositories that link to Telegram channels, Hacker News through
`hn.algolia.com`, and Mojeek, Yandex, Google, DuckDuckGo, Bing, Yahoo, and
Startpage HTML results for queries built from the profile. TGStat joins when a
token is set. Seed channels
are an optional fallback in the Search and sources group, not a required step.

Web queries use five families: `site:t.me <term>`, `"<term>" telegram`,
`"<term>" телеграм` for a Cyrillic profile, `#<hashtag>`, and
`"<domain>" telegram`. They are interleaved diagonally, one query per family
per round, so the first queries cover several terms and families;
`search_max_queries` defaults to 10 and caps at 12. Engine relevance order is
kept before the per-query result limit; usernames are never sorted
alphabetically.

Bing, Yahoo, and Startpage are free HTML engines on by default; each turns off
with its own toggle (`sources_search_bing=0`, `sources_search_yahoo=0`,
`sources_search_startpage=0`). They can answer CAPTCHA or `403` from a
network, and a normal page without `t.me` links is `empty`, never an error.

Hacker News (`search_hn`) queries up to three profile terms with
`tags=(story,comment)` and 50 hits per query, and reads `t.me` links from story
and comment text. It is on by default and off with `sources_search_hn=0`; an
`empty` answer is normal.

Optional sources appear only when configured, and report a status instead of
failing the run:

- `search_brave` needs `brave_search_api_key` (or `BRAVE_SEARCH_API_KEY`);
  without it the status is `no_key`.
- `search_searxng` needs `searxng_url` pointing at one or more self-hosted
  instances (comma or newline separated) with the JSON API enabled; instances
  are tried in order and the last failure sets the status when all fail. A blank
  URL is `disabled`.
- `search_lyzem` is off by default for terms reasons and turns on with
  `sources_search_lyzem=1`.

GitHub extraction covers each repository's homepage, description, and README,
plus strict `owner/repo` references in the owner's posts, with
`topic:telegram` and `in:readme` queries. The repository budget is one per
plugin run across all queries combined: about 10 repositories without a token
and 20 with one.

The local agent runs automatically as `agent_search` unless
`sources_agent_search=0`. It gets the compact `search.*` brief (at most 10
channels, at most 3 web queries, a 90-second stop, JSON-only `{username, why}`),
starts in parallel with the other sources, may use its own tools and network,
and is bounded by `agent_search_max` (30) and `agent_search_timeout_sec` (600).
A blank `agent_command` auto-detects OpenCode, Claude Code, Codex, or Gemini
CLI; on Windows `.cmd`/`.bat` shims run through `cmd.exe` and `.ps1` through
PowerShell. The richer `research.*` brief with 10-30 items stays for the manual
`agent-prompt`.
`agent_commands` (one command per line) runs several agents in one batch under
that single deadline and cap; `agent_search_concurrency` (1-3, default 2)
bounds parallelism, blank lines are ignored, and a repeated line runs once.
Agents that finish before the deadline keep their results when the rest are
cancelled; a malformed command line is skipped with a warning naming its
1-based index and the other agents still run. A missing `agent_command`
executable ends as `disabled` without a wait. Its results pass the same `t.me`
gate as every other source. A partial answer with valid items is `ok` plus a
warning; no output or no valid item is `error`; an explicit `[]` is `empty`.

`find_max_candidates` (default 500) caps the first round of candidates;
dynamic graph edges continue beyond it on purpose. `find_max_channels` and the
UI cancel button bound the whole run; a full default run can take tens of
minutes, and `--max-channels` is the quick mode. For a bounded live check use
`find --depth 2 --max-channels 40`.

The research brief is the packaged `research.en.md` or `research.ru.md`: the
owner profile as JSON, up to 8 post samples of 400 characters each inside a
neutralized untrusted-data block, up to 12 terms, 5 hashtags, 5 domains, size
numbers, language and region, a six-point strategy checklist, an evidence
rule, and a JSON-only 10-30 item schema with a no-send boundary.

Ranking fields on every candidate: `similarity` is a 0 to 100 TF-IDF cosine
between the owner's niche profile and the candidate text (words, bigrams,
hashtags, domains); `size_fit` is 1.0 inside the target range, 0.5 within one
order of magnitude, 0.1 beyond, and 0.3 when the subscriber count is unknown;
`score` is `similarity / 100 * size_fit`.
Similarity is lexical. There are no embeddings, so a low score can hide a
topical match; read `matched_topics` too.

If `find` adds 0 web or graph candidates, the owner's posts mention no peers.
Ask for 3 to 5 known peer channels and put them into Settings, Search and
sources, Seed channels (one per line). TGStat keyword search needs a paid API
plan; a FREE token only enriches channels the owner owns. The GitHub plugin
mostly returns project chats, not niche partners.

## JSON contracts

Every read command prints exactly one JSON document to stdout; diagnostics go
to stderr. Exit codes are stable: `0` success, `1` user or data error, `2`
usage error. Always pass `--json` when you parse output.

| Command | JSON shape |
| --- | --- |
| `tg-collab init @channel --json` | `{"profile": {...subscribers, avg_views, median_views, err, posts_per_week, links...}, "range": {"low": 300, "high": 3000, "min_mult": 0.3, "max_mult": 3.0}, "warnings": []}` |
| `tg-collab find --json` | `{"added": 0, "updated": 0, "skipped_fresh": 0, "seeds": 0, "sources_ok": 0, "errors": [{"username": "...", "error": "..."}], "warnings": ["..."], "providers": {"tg_graph": 0, "tg_seed": 0, "plugin_github": 0, "agent_search": 0, "search_brave": 0, "search_searxng": 0, "search_lyzem": 0, "search_mojeek": 0, "search_bing": 0, "search_yahoo": 0, "search_startpage": 0, "search_yandex": 0, "search_google": 0, "search_duckduckgo": 0, "search_hn": 0, "tgstat": 0}, "source_status": {"tg_graph": "empty", "...": "ok"}, "source_remedy": {"tg_graph": "Broaden the niche terms or add seed channels.", "...": ""}}` |
| `tg-collab explain TARGET --json` | `{"target": "name", "in_db": true, "verdict": "present|absent|stored_low|out_of_range|inactive|hidden|hidden_language|invite_only", "reason": "...", "channel": {...} | null, "range": {"low": 300, "high": 3000} | null, "invite": false, "check": {"exists": true, "error": null}}` (`check` only with `--check`; `invite_only` has `"invite": true`) |
| `tg-collab list --json` | `{"channels": [{"username", "subscribers", "avg_views", "err", "status", "contact_status", "contact_value", "lang", "flags", "activity", "tab", "range_reason", "source", "last_post_at", "similarity", "size_fit", "score", "matched_topics"}], "count": 0}` |
| `tg-collab list @channel --json` | channel context: `{"channel": {"username", "description", "notes", "latest_draft", "similarity", "size_fit", "score", "matched_topics", "posts": [{"message_id", "posted_at", "views", "text"}]}}` |
| `tg-collab agent-prompt --json` | `{"prompt": "...", "profile": {...}, "range": {...}, "niche_terms": ["..."], "hashtags": ["..."], "link_domains": ["..."], "schema": [{"username", "url", "why"}], "language": "en", "template": "research.en", "posts_sample": ["..."]}` |
| `tg-collab niche --refresh --json` | `{"username": "...", "source": "agent|manual|deterministic", "generated_at": "...", "terms": ["..."], "hashtags": ["..."], "domains": ["..."], "stopwords": ["..."], "summary": "..."}` |
| `tg-collab import TARGET... --json` | `{"accepted": 0, "rejected": 0, "total": 0, "limited": 0, "results": [{"username", "status", "reason"}], "leads": [{"url": "https://t.me/+hash", "title": null}], "problems": [{"target", "reason"}]}` |
| `tg-collab draft @channel "text" --json` | `{"username": "...", "draft_id": 1, "status": "drafted", "text": "..."}` |
| `tg-collab status @channel sent --json` | `{"username": "...", "status": "sent"}` |
| `tg-collab export --format md --json` | `{"path": "outreach.md", "format": "md", "count": 10}` |

`warnings` in `find` carries soft failures such as a Yandex CAPTCHA, a Google
empty page, or a GitHub rate limit. `errors` carries per-channel or per-source
failures. `providers` counts candidates per source key; `seeds` counts the
seed channels that entered the run. `sources_ok` counts sources whose status is
`ok` or `empty`.

The effective niche profile feeds `find` web queries and adds a small capped
similarity bonus when it comes from an agent or manual list. `tg-collab niche`
prints it, `--refresh` asks the local agent, `--set "a,b,c"` stores manual
terms, and `--clear` restores the deterministic tags. On a `find` with an empty
stored profile the agent runs once before the sources and reports 5-10%
progress; a stored profile is reused without asking again, and the `--refresh`
flag is the explicit way to re-ask. A failed or invalid answer keeps the
deterministic terms with a warning.

The dashboard profile card has an "Edit tags" editor for the effective terms,
hashtags, and link domains. A valid save stores `source="manual"` with a fresh
timestamp, keeps the stored summary and stopwords, and rescores the profile's
channels; an invalid or empty list returns the page with an error. This editor
is the only place in the UI to edit those tags; `tg-collab niche --set "a,b,c"`,
`--refresh`, and `--clear` edit them from the CLI as well.

While a `find` runs, its job reports phases: start (0-5%), the niche profile
(5-10%), candidate collection (10-25%), per-channel enrichment with `done/total`
in the message (25-95%), and finalize (95-100%). The bar animates until the
first real count arrives. Opening the dashboard again restores the running job
with its 1s polling; starting the same kind again returns that same panel with
HTTP 200 and "A run is already in progress" instead of an error, and the Find
button stays disabled while the run is live. A finished job is never restored,
so tell the user to read the finished summary rather than reloading for it.

`source_status` has exactly one entry per source key:

| Status | Meaning | Remedy |
| --- | --- | --- |
| `ok` | The source ran and offered channels. | Nothing. |
| `empty` | The source answered but found nothing. | Broaden niche terms, enable another engine, or add seed channels. |
| `disabled` | The source is off or not configured. | Enable it in Settings or set the URL or toggle it needs. |
| `no_key` | A credential is missing. | Add the token in Settings. |
| `captcha` | The engine refused a scripted request. | Try another engine or the user's own network. CAPTCHA is never bypassed. |
| `quota` | An API quota is exhausted. | Raise the plan or retry later. |
| `blocked` | The source blocked this network. | Change the proxy or VPN and retry. |
| `error` | The source failed. | Read the job log or `errors` and retry. |

The Web UI job result shows the same table with a "What to do" column. An
all-empty niche is a finished run: `empty` counts as an answer and exits `0`.
When every enabled source failed, `find` prints the payload and exits `1` with
"every enabled source failed".

`source_remedy` is keyed exactly like `source_status` and carries one short
English remedy per source, derived from the status contract above; `ok` maps to
an empty string. `find --json` and the Web UI job result both include it, so a
JSON consumer gets the same advice as the UI column.

Tabs: `suitable` = inside the current range, `work` = outreach in progress,
`growing` = outside it (kept, not rejected), `hidden` = status `ignored`.
Statuses: `found`, `shortlisted`, `drafted`, `sent`, `replied`, `agreed`,
`done`, `ignored`. The list carries `activity` (`active`, `inactive`, or
`unknown`) per channel. Rows inactive for `flag_inactive_days` (14 by default)
are hidden; pass `--activity all` to see them, and rows without a last post
stay visible as `unknown`. The default language view keeps the owner's language
plus English and always keeps `unknown`; `--lang all|cyrillic|latin|cjk`
overrides it with the old exact meaning (`all` shows everything, a code shows
that language). The Web UI has no visible language control; the default view
applies, but `?lang=` still filters and is carried to exports. Language never
changes `similarity`, `size_fit`, or `score`.

`tg-collab explain TARGET` accepts `@name`, `t.me/name`, `t.me/s/name`, or
`t.me/+hash` and returns one verdict: `absent`, `stored_low` (inside the range
but under the diagnostic score floor 5.0), `out_of_range`, `inactive` (older
than the activity window), `hidden`, `hidden_language` (outside the default
language set), `invite_only`, or `present`. `--check` makes one `t.me`
existence probe; without it there is no network call. An invite reports
`invite_only` and is never opened.

`t.me/+hash` links from the owner's own posts at `init` and from explicit
`import` targets are stored in the `leads` table as canonical
`https://t.me/+hash` URLs with a best-effort title and subscriber count. A
failed landing page keeps the URL and a note. They stay in the `leads` table for
manual verification; the dashboard has no invite card and the tool never joins.

Every channel lives in a profile. `tg-collab profile list` shows the profiles
with their ids and the active one; `profile add @name` creates one (duplicates
are rejected), `profile use @name` or `profile use <id>` switches the active
profile, and `profile remove @name --yes` deletes it with its channels, leads,
and jobs. The last profile cannot be removed. Every read and write command works
inside the active profile, so `list`, `export`, `explain`, notes, drafts, and
running jobs follow the same scope. The Web UI mirrors this with a header
switcher and a Profiles card in Settings. The Web UI add form makes the new
profile active, while CLI `profile add` only creates it and `profile use`
switches. Settings shows the active profile's username and seed channels;
saving renames the profile and stores its seeds. With the agent search toggle
on, a new or switched profile asks the local agent for niche tags in a
background `niche` job only when its niche is still empty, then rescores its
channels; a profile that already has tags is not asked again, and loading a
profile for the first time follows the same rule. The manual
`niche --refresh/--set/--clear` commands rescore the active profile too.

## Agent search loop and workflow

There are two distinct agent paths. Automatic agent search is a discovery
source: `find` runs the local agent itself (on unless `sources_agent_search=0`)
with the same task, and its channels pass the normal `t.me` gate. Nothing is
imported by it. The manual path below is the only way results reach the
database with `source="agent_import"`; `tg-collab import` never runs on its
own, and the Web UI does not expose import in this stage.

`tg-collab agent-prompt --json` prints a task for the agent: the owner's
profile JSON, the target range, up to 12 niche terms, up to 5 hashtags and 5
link domains, and the required output schema, plus `template` and
`posts_sample`. The agent runs its own web search and page reads, then returns
only a JSON array of items shaped `{"username": "...", "url":
"https://t.me/...", "why": "one sentence"}`.

`tg-collab import TARGET... --json` verifies every target through `t.me`
before storing it. Accepted input: a JSON file or JSON lines, a plain list of
`@name` values separated by commas, semicolons, or newlines, `t.me` and
`telegram.me` links, and `tg://resolve?domain=` links. Arbitrary hosts are
rejected. Files are capped at 1 MB, markdown fences around the JSON are
stripped, and the number of username targets is capped by `agent_import_max`
(default 50, reported as `limited`); explicit invite links are not capped.

Imported channels get `source="agent_import"`; the agent's `why` and `url`
are appended to `meta.sources.agent_import`, and an existing status, notes, or
draft is preserved. A channel with `preview_disabled` is still stored as a
channel carrying the `preview_disabled` flag and
`contact_status="manual_needed"`. The command exits `0` when at
least one target was confirmed and `1` when none were; the JSON result still
lists every rejection reason, and no invented username is stored.

Recommended order:

1. Run the profile step: `tg-collab init @user_channel --json`.
2. Run discovery: `tg-collab find --json`. Read `warnings`, `providers`, and
   `source_status`; a failing optional source is a status, not a failure. The
   agent already ran as `agent_search` unless it is turned off.
3. Read the shortlist: `tg-collab list --tab suitable --json`, sorted by
   `score`, then check `similarity`, `size_fit`, and `matched_topics`.
4. If a channel seems missing, run `tg-collab explain @channel --json` before
   re-running discovery: `absent` means it was never stored, `stored_low` means
   a stronger match outranks it, `out_of_range` waits under "Grow into it",
   `inactive` is hidden by the 14-day activity window (`--activity all` shows
   it), and `hidden_language` is outside the owner-language-plus-English set
   (`--lang all` shows it).
5. If the owner wants an independent second pass, run
   `tg-collab agent-prompt --json`, do the research, then feed the result to
   `tg-collab import`.
6. For each promising channel, fetch the draft context:
   `tg-collab list @channel --json`.
7. Write a personal draft with `tg-collab draft @channel "..." --json`.
8. Track progress: `tg-collab status @channel sent --json` after the human
   sends the message.

## Peer research playbook

This is the manual path for an external agent, the one `agent-prompt` feeds. It
is separate from the automatic `agent_search` source above: here the agent
returns a list, and nothing is stored until the owner runs `tg-collab import`.

### The chat recipe

The owner opens with one message in the agent chat:

> Here is my channel profile and my target size: 300-3000 subscribers. Find
> 10-30 peer channels and groups for my niche. Use the query families below,
> check every size on `t.me/s/<username>`, and return only the JSON array.

The agent researches and answers with the array. One chat turn, one array; a
follow-up turn is only for channels the owner asks to re-check.

### Inputs the agent gets

`tg-collab agent-prompt --json` prints everything the brief needs:

- `niche_terms`: up to 12 top terms from the owner's posts.
- `hashtags`: up to 5 profile hashtags.
- `link_domains`: up to 5 domains the owner links to.
- `range`: the target size range (`low`, `high`, `min_mult`, `max_mult`).
- `language`: the profile language; the search region arrives inside `prompt`.
- `posts_sample`: up to 8 recent post texts, capped at 400 characters each.
- `schema`: the required output shape.

The same values sit inside `prompt`, so an agent that reads only the prompt
gets the same brief.

### Five query families

Work through them in order and stop once 10-30 verified channels exist:

1. Topic terms: 2-3 `niche_terms` plus "telegram", "telegram channel",
   "telegram chat", and the same words in the profile language.
2. Hashtags and link domains: a shared domain often marks one community.
3. GitHub: `topic:telegram`, `awesome telegram`, and `in:readme` for the top
   terms; read repository homepages, descriptions, and READMEs for real `t.me`
   links.
4. Hacker News: `hn.algolia.com` for the top terms plus "telegram"; project
   threads often name their community chat.
5. Size and activity check: open `https://t.me/s/<username>` for every
   candidate and read the subscriber count and the latest posts. A dead or
   login-only page is not a candidate.

Do not use login-walled catalogs, paid directories, or anything that needs an
account: those channels cannot be verified and are out of scope.

### Evidence rule

Every `why` cites something the agent actually saw: a post topic, the channel
description, a matching hashtag or link domain, a repository README, a forum
comment, or the subscriber count from `t.me/s`. A generic compliment is not
evidence. A channel that could not be opened stays out.

Only a JSON array comes back: 10-30 items shaped
`{"username": "...", "url": "https://t.me/<username>", "why": "..."}`, no prose
and no markdown fences. `username` is a real public username without `@`;
never invent one. If fewer than 10 real peers exist, return what was verified
instead of padding the list. The saved answer holds only that array:

```json
[{"username": "example", "url": "https://t.me/example", "why": "one sentence citing what was seen"}]
```

### Import loop

1. `tg-collab agent-prompt --json` prints the brief.
2. Run the local agent with it and save the answer to a file, for example
   `peers.json`.
3. `tg-collab import peers.json --json` verifies every username through `t.me`
   and stores the real ones as `source="agent_import"`. The existence gate
   rejects an invented channel; the JSON `results` list shows each acceptance
   or rejection with its reason.

This loop stores nothing by itself. Import stays manual here, exactly as in the
agent search loop above.

### Several agents in one pass

The automatic `agent_search` source that `find` runs takes more than one agent.
Put one command per line into `agent_commands` (`{prompt}` or `{prompt_file}`
is substituted), set `agent_search_concurrency` (1-3, default 2), and let one
overall deadline (`agent_search_timeout_sec`, default 600 s) bound the batch.
Every agent returns its own array; the source merges them, dedupes by username,
and caps the merged list with `agent_search_max` (30). A failed agent is a
warning, not a failed pass.

That path is automatic and separate from the manual loop above. On the manual
side you run the brief with each local agent yourself, then run
`tg-collab import` on each array (or on one combined file). Import verifies
every username through `t.me`, dedupes, and caps the list with
`agent_import_max` (50).

### Invite-only channels

An invite link `t.me/+hash` has no public username, and `t.me/s` cannot
verify or enrich it without joining. The tool collects such links from the
owner's own posts at `init` and from explicit `import` targets, stores each as
a manual lead, and fills title and size from the public landing page when
available; the owner must check it by hand before any outreach. The agent
never opens, joins, or messages it.

The agent never sends a message, never joins a chat, and never posts anything.
See Human-in-the-Loop: the last step is always the owner.

## Offer the Web UI

After a search or a batch of drafts, tell the user the local Web UI shows the
same data with filters, cards, one export menu, and one-click copy. If they
agree, run `tg-collab ui`. It starts the server at `127.0.0.1` in the
background and opens the browser.

## Human-in-the-Loop

The tool never sends, schedules, or auto-posts messages. Do not look for a
send command: there is none. The last step is always the human copying the
text into Telegram. Do not offer mass mailing.

## Network

If `init` or `find` times out, the user's network blocks Telegram. Ask the
user to set a proxy in the Web UI (Settings, Network) and press "Test Telegram
connection", or to use a VPN. Do not try to change their system network
yourself. The product has no MTProto client and never logs in to Telegram.

Yandex, DuckDuckGo, Mojeek, Bing, Yahoo, and Startpage may answer with a CAPTCHA
from some networks. The tool records the `captcha` status and disables that
engine for the run; do not try to solve or bypass the challenge, and do not
retry it in a loop. A free engine can also return a normal page with no `t.me`
links; that is `empty`, not a failure. Google commonly returns an empty page,
GitHub enforces rate limits, Brave
without a key is `no_key`, and lyzem is off unless the owner turns it on; all
of these are statuses rather than failures. The automatic agent child gets the
configured proxy in `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` and then uses its
own tools.

## Errors

On exit code `1`, read stderr for one actionable line: a missing profile, a
disabled public preview, an invalid status, an unknown channel, an import
where no target was confirmed, or a find where every enabled source failed.
On `2`, the invocation is wrong. Do not retry blindly; fix the argument, the
profile, or the network.

## Data location

The database is `.tg-collab/data.db` (or `TGCOLLAB_DATA_DIR`). It is internal:
its schema follows `PRAGMA user_version` and may change between releases. Read
data through the JSON commands, never by querying SQLite from the agent.
