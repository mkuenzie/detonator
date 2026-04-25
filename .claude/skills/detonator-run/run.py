#!/usr/bin/env python3
"""Detonator troubleshooting helper.

Usage:
    run.py summary       <run_id>
    run.py artifacts     <run_id> [--type T] [--url-substr S]
    run.py fetch         <run_id> <artifact_name> [--out PATH] [--raw]
    run.py chain         <run_id> [--limit N]
    run.py navigations   <run_id>
    run.py console       <run_id>
    run.py dom           <run_id> [--out PATH]
    run.py screenshots   <run_id>
    run.py errors        <run_id>
    run.py list          [--status S] [--domain D] [--limit N]

Env: DETONATOR_BASE (default http://bon-clay:8080)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any

BASE = os.environ.get("DETONATOR_BASE", "http://bon-clay:8080")


def _get(path: str, params: dict | None = None) -> bytes:
    url = BASE.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def _get_json(path: str, params: dict | None = None) -> Any:
    return json.loads(_get(path, params))


def _run(run_id: str) -> dict:
    return _get_json(f"/runs/{run_id}")


def _artifact(run_id: str, name: str) -> bytes:
    return _get(f"/runs/{run_id}/artifacts/{name}")


def _artifact_json(run_id: str, name: str) -> Any:
    return json.loads(_artifact(run_id, name))


def _by_type(run: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for a in run.get("artifacts", []):
        out.setdefault(a["type"], []).append(a)
    return out


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


# ---------- commands ----------

def cmd_summary(args):
    run = _run(args.run_id)
    types = _by_type(run)
    print(f"run_id      : {run['id']}")
    print(f"status      : {run['status']}")
    print(f"seed_url    : {run['seed_url']}")
    print(f"egress      : {run['egress_type']}")
    print(f"created_at  : {run['created_at']}")
    print(f"completed   : {run.get('completed_at')}")
    if run.get("error"):
        print(f"error       : {run['error']}")
    print(f"artifacts   : {len(run.get('artifacts', []))}")
    for t, items in sorted(types.items()):
        total = sum(a["size"] for a in items)
        print(f"  {t:<16} {len(items):>4}  {total:>12} bytes")

    # Navigations are first-class evidence — show inline.
    if "navigations" in types:
        try:
            navs = _artifact_json(args.run_id, "navigations.json")
            print(f"\nnavigations ({len(navs)}):")
            for n in navs:
                print(f"  {n.get('timestamp','?')}  {n.get('frame','?'):<6}  {n.get('url','')}")
        except Exception as e:
            print(f"\nnavigations: <failed to fetch: {e}>")

    # Filter result summary if present.
    if "filter_result" in types:
        try:
            fr = _artifact_json(args.run_id, "filter_result.json")
            print(f"\nchain: total={fr.get('total_requests')} scope={fr.get('scope_requests')} noise={fr.get('noise_requests')}")
        except Exception:
            pass

    # Manifest summary (enrichment / chain).
    if "manifest" in types:
        try:
            m = _artifact_json(args.run_id, "manifest.json")
            enr = m.get("enrichment") or {}
            ch = m.get("chain") or {}
            print(
                f"enrichment: enrichers={enr.get('enricher_count')} "
                f"observables={enr.get('observable_count')}"
            )
            print(
                f"chain summary: requests={ch.get('chain_requests')} "
                f"noise={ch.get('noise_requests')} technique_hits={ch.get('technique_hit_count')}"
            )
        except Exception:
            pass


def cmd_artifacts(args):
    run = _run(args.run_id)
    rows = run.get("artifacts", [])
    if args.type:
        rows = [a for a in rows if a["type"] == args.type]
    if args.url_substr:
        s = args.url_substr.lower()
        rows = [a for a in rows if s in (a.get("source_url") or "").lower()]
    rows.sort(key=lambda a: a.get("captured_at") or "")
    for a in rows:
        name = _basename(a["path"])
        url = a.get("source_url") or ""
        print(f"{a['type']:<14} {a['size']:>10}  {name:<48}  {url}")
    print(f"\n{len(rows)} artifact(s)")


def cmd_fetch(args):
    data = _artifact(args.run_id, args.artifact_name)
    if args.out:
        with open(args.out, "wb") as f:
            f.write(data)
        print(f"wrote {len(data)} bytes to {args.out}")
        return
    if args.raw:
        sys.stdout.buffer.write(data)
        return
    # Pretty-print json if applicable.
    if args.artifact_name.endswith((".json", ".har")):
        try:
            print(json.dumps(json.loads(data), indent=2))
            return
        except Exception:
            pass
    if args.artifact_name.endswith(".jsonl"):
        for line in data.splitlines():
            try:
                print(json.dumps(json.loads(line)))
            except Exception:
                sys.stdout.buffer.write(line + b"\n")
        return
    sys.stdout.buffer.write(data)


def cmd_chain(args):
    fr = _artifact_json(args.run_id, "filter_result.json")
    print(f"seed_url        : {fr.get('seed_url')}")
    print(f"total_requests  : {fr.get('total_requests')}")
    print(f"scope_requests  : {fr.get('scope_requests')}")
    print(f"noise_requests  : {fr.get('noise_requests')}")
    # filter_result.entries carries scope/noise flags only.
    # The real request/response data lives in har_navigation.log.entries
    # (also embedded under filter_result.har_navigation).
    har = (fr.get("har_navigation") or {}).get("log") or {}
    entries = har.get("entries") or []
    statuses = Counter()
    methods = Counter()
    hosts = Counter()
    redirects = []
    failures = []
    for e in entries:
        req = e.get("request") or {}
        resp = e.get("response") or {}
        url = req.get("url", "")
        host = urllib.parse.urlparse(url).netloc
        status = resp.get("status", 0)
        statuses[status] += 1
        methods[req.get("method", "?")] += 1
        hosts[host] += 1
        if 300 <= status < 400:
            loc = ""
            for h in resp.get("headers") or []:
                if h.get("name", "").lower() == "location":
                    loc = h.get("value", "")
                    break
            redirects.append((status, url, loc))
        if status == 0 or status >= 400:
            failures.append((status, req.get("method", "?"), url))
    print(f"\nchain entries (har_navigation): {len(entries)}")
    print(f"methods : {dict(methods)}")
    print(f"statuses: {dict(statuses)}")
    print(f"\ntop hosts:")
    for h, c in hosts.most_common(args.limit):
        print(f"  {c:>4}  {h}")
    if redirects:
        print(f"\nredirects ({len(redirects)}):")
        for st, u, loc in redirects[: args.limit]:
            print(f"  {st}  {u}\n       -> {loc}")
    if failures:
        print(f"\nfailures / 4xx-5xx ({len(failures)}):")
        for st, m, u in failures[: args.limit]:
            print(f"  {st}  {m}  {u}")


def cmd_navigations(args):
    navs = _artifact_json(args.run_id, "navigations.json")
    for n in navs:
        print(f"{n.get('timestamp','?')}  {n.get('frame','?'):<6}  {n.get('url','')}")
    print(f"\n{len(navs)} navigation(s)")


def cmd_console(args):
    msgs = _artifact_json(args.run_id, "console.json")
    if not msgs:
        print("(no console messages)")
        return
    for m in msgs:
        ts = m.get("timestamp") or m.get("time") or ""
        lvl = m.get("type") or m.get("level") or "?"
        text = m.get("text") or m.get("message") or json.dumps(m)
        print(f"[{lvl}] {ts}  {text}")


def cmd_dom(args):
    data = _artifact(args.run_id, "dom.html")
    if args.out:
        with open(args.out, "wb") as f:
            f.write(data)
        print(f"wrote {len(data)} bytes to {args.out}")
    else:
        sys.stdout.buffer.write(data)


def cmd_screenshots(args):
    run = _run(args.run_id)
    shots = [a for a in run.get("artifacts", []) if a["type"] == "screenshot"]
    shots.sort(key=lambda a: a.get("captured_at") or "")
    for a in shots:
        name = _basename(a["path"])
        url = f"{BASE}/runs/{args.run_id}/artifacts/{name}"
        print(f"{a.get('captured_at','?')}  {a['size']:>9}  {name}  {url}")
    print(f"\n{len(shots)} screenshot(s)")


def cmd_errors(args):
    """Hunt for trouble signals across all the easy sources."""
    run = _run(args.run_id)
    print(f"status: {run['status']}   error: {run.get('error')}")
    types = _by_type(run)

    if "console" in types:
        try:
            msgs = _artifact_json(args.run_id, "console.json") or []
            errs = [m for m in msgs if (m.get("type") or m.get("level") or "").lower() in {"error", "warning"}]
            print(f"\nconsole error/warning entries: {len(errs)}/{len(msgs)}")
            for m in errs[:20]:
                lvl = m.get("type") or m.get("level")
                print(f"  [{lvl}] {m.get('text') or m.get('message') or m}")
        except Exception as e:
            print(f"console: <{e}>")

    if "har_navigation" in types or "filter_result" in types:
        try:
            har = _artifact_json(args.run_id, "har_navigation.json")
            entries = (har.get("log") or {}).get("entries") or []
            bad = []
            for e in entries:
                resp = e.get("response") or {}
                st = resp.get("status", 0)
                if st == 0 or st >= 400:
                    req = e.get("request") or {}
                    bad.append((st, req.get("method", "?"), req.get("url", "")))
            print(f"\nchain failed/zero-status requests: {len(bad)} of {len(entries)}")
            for st, m, u in bad[:20]:
                print(f"  {st}  {m}  {u}")
        except Exception as e:
            print(f"har_navigation: <{e}>")

    if "manifest" in types:
        try:
            m = _artifact_json(args.run_id, "manifest.json")
            enr_modules = (m.get("enrichment") or {}).get("modules") or []
            mod_errs = []
            for mod in enr_modules if isinstance(enr_modules, list) else []:
                if isinstance(mod, dict) and (mod.get("error") or mod.get("status") in {"error", "failed"}):
                    mod_errs.append(mod)
            print(f"\nenrichment module errors: {len(mod_errs)}")
            for me in mod_errs[:20]:
                print(f"  {me}")
        except Exception as e:
            print(f"manifest: <{e}>")


def cmd_list(args):
    rows = _get_json("/runs", {"status": args.status, "domain": args.domain, "limit": args.limit})
    items = rows if isinstance(rows, list) else rows.get("items") or rows.get("runs") or []
    for r in items:
        print(f"{r.get('id')}  {r.get('status'):<12}  {r.get('created_at','')}  {r.get('seed_url','')[:120]}")
    print(f"\n{len(items)} run(s)")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("summary"); s.add_argument("run_id"); s.set_defaults(fn=cmd_summary)
    s = sub.add_parser("artifacts"); s.add_argument("run_id"); s.add_argument("--type"); s.add_argument("--url-substr"); s.set_defaults(fn=cmd_artifacts)
    s = sub.add_parser("fetch"); s.add_argument("run_id"); s.add_argument("artifact_name"); s.add_argument("--out"); s.add_argument("--raw", action="store_true"); s.set_defaults(fn=cmd_fetch)
    s = sub.add_parser("chain"); s.add_argument("run_id"); s.add_argument("--limit", type=int, default=15); s.set_defaults(fn=cmd_chain)
    s = sub.add_parser("navigations"); s.add_argument("run_id"); s.set_defaults(fn=cmd_navigations)
    s = sub.add_parser("console"); s.add_argument("run_id"); s.set_defaults(fn=cmd_console)
    s = sub.add_parser("dom"); s.add_argument("run_id"); s.add_argument("--out"); s.set_defaults(fn=cmd_dom)
    s = sub.add_parser("screenshots"); s.add_argument("run_id"); s.set_defaults(fn=cmd_screenshots)
    s = sub.add_parser("errors"); s.add_argument("run_id"); s.set_defaults(fn=cmd_errors)
    s = sub.add_parser("list"); s.add_argument("--status"); s.add_argument("--domain"); s.add_argument("--limit", type=int, default=25); s.set_defaults(fn=cmd_list)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
