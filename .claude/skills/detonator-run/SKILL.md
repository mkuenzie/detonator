---
name: detonator-run
description: Pull Detonator run information from the orchestrator API for troubleshooting. Use when the user asks to investigate, troubleshoot, debug, summarize, or inspect a detonator run — given a run UUID or a /ui/runs/<id> URL. Covers run summary, artifact listing/fetching, navigation chain, filtered HAR (initiator chain), DOM, console, screenshots, and error hunting.
---

# detonator-run

Helper for pulling and parsing run data from the Detonator orchestrator
(default `http://bon-clay:8080`, override with `DETONATOR_BASE`).

A run UUID may arrive as a bare ID or embedded in a URL like
`http://bon-clay:8080/ui/runs/<uuid>` — extract the trailing UUID.

## When to use

Trigger on any request to investigate a run: "troubleshoot run X", "what
happened in run X", "show me the chain for X", "why did X fail", "pull
the DOM for X", etc.

Always start with `summary` — it's cheap, shows status/error/timings,
artifact-type counts, the navigation timeline (first-class evidence per
the project spec), and chain/enrichment summary. Decide where to drill
in from there.

## Commands

Run via `python3 .claude/skills/detonator-run/run.py <subcommand> ...`
(stdlib only, no install needed).

| Command | Purpose |
|---|---|
| `summary <run_id>` | Status, error, timings, artifact-type counts, navigations, chain + enrichment summary. **Start here.** |
| `errors <run_id>` | Hunt for trouble across console, HAR (failed/zero-status requests), enrichment module errors. |
| `chain <run_id> [--limit N]` | Filtered HAR analysis: methods, statuses, top hosts, redirects, failures. Reads `har_navigation.json`. |
| `navigations <run_id>` | Full navigation timeline (frame, timestamp, URL). |
| `console <run_id>` | Browser console messages. |
| `dom <run_id> [--out PATH]` | Final DOM snapshot. |
| `screenshots <run_id>` | List screenshots (timestamp, size, fetch URL). |
| `artifacts <run_id> [--type T] [--url-substr S]` | List artifacts; filter by type or by substring of source_url. |
| `fetch <run_id> <artifact_name> [--out PATH] [--raw]` | Fetch any artifact by basename. JSON / JSONL is auto-pretty-printed; binary uses `--raw` or `--out`. |
| `list [--status S] [--domain D] [--limit N]` | List runs (find a run when only the URL/domain is known). |

## Primary artifacts (what each is for)

- **navigations.json** — top-level page transitions, including frame ("main" vs "sub"). The clearest evidence of where the browser actually went.
- **har_navigation.json** — chain-filtered HAR (the initiator-chain subset). Use for request/response inspection. The `_initiator` field is what drives chain extraction.
- **filter_result.json** — scope/noise classification per URL plus an embedded `har_navigation`. The flat `entries[]` list only carries `{url, in_scope, is_noise, reasons}`; for status/method/headers, read entries from `har_navigation.log.entries`.
- **har_full.har** — unfiltered HAR (large; only fetch when chain-filtered isn't enough).
- **manifest.json** — run-level rollup: config, artifact list, enrichment module results, chain summary, technique hit count. Good source for enrichment failures.
- **manifest.jsonl** (under `bodies/`) — per-response-body metadata captured during the run.
- **dom.html** — final rendered DOM.
- **console.json** — captured console messages.
- **screenshot_*.png** — UI snapshots taken during detonation.
- **site_resource / request_body** — captured response/request bodies, addressable by content hash. Use `artifacts --url-substr` to find one for a specific URL.

## Notes

- Artifacts are fetched by basename: `GET /runs/{run_id}/artifacts/{basename}`.
- For the full bundle: `GET /runs/{run_id}/artifacts.zip` (use `curl`, not this tool).
- The orchestrator has no auth (single-operator home lab); no headers needed.
- Other useful endpoints not wrapped here: `/observables`, `/techniques`,
  `/techniques/{id}/matches`, `/domain/{domain}/runs`, `/graph/search`,
  `/graph/nodes/{type}/{id}/neighbors`. Hit them with `curl` when graph or
  cross-run correlation is needed.
