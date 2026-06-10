"""The `loom` command-line interface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import gateway, harvester, library, proxy, registry
from .config import load_config, paths
from .manifest import load_manifest
from .targets import get_target
from .util import LoomError, bold, dim, err, green, info, ok, red, yellow


# --- helpers -------------------------------------------------------------------

def _require_entry(name: str) -> dict:
    entry = registry.get(name)
    if entry is None:
        known = [a["name"] for a in registry.all_apps()]
        hint = f" Known apps: {', '.join(known)}" if known else ""
        raise LoomError(f"no app named '{name}' in the fleet.{hint}")
    return entry


def _sync_edge_gated(cfg: dict):
    """Keep the edge gated-router file in sync with the fleet's gated apps."""
    gated = [a["name"] for a in registry.all_apps() if a.get("access") == "gated"]
    return gateway.write_edge_gated(cfg, gated)


def _status_color(status: str):
    if status == "running":
        return green
    if status in ("missing", "dead"):
        return red
    return yellow


def _health_color(h: str):
    if h == "ok":
        return green
    if h == "down":
        return red
    return yellow  # unknown / unready / —


# --- commands ------------------------------------------------------------------

def cmd_deploy(args) -> int:
    cfg = load_config()
    app_dir = Path(args.path).resolve()
    if not app_dir.is_dir():
        raise LoomError(f"not a directory: {app_dir}")
    manifest = load_manifest(app_dir)
    target = get_target(cfg["default_target"])

    info(f"deploying {bold(manifest['name'])} ({manifest['runtime']}, {manifest['access']}) "
         f"via target '{target.name}'")
    entry = target.deploy(cfg, app_dir, manifest)
    registry.upsert(entry)
    _sync_edge_gated(cfg)
    library.upsert(harvester.harvest_app(cfg, entry))  # index into the Library

    ok(f"{bold(manifest['name'])} is live → {bold(entry['url'])}")
    caps = (entry.get("contract") or {}).get("capabilities") or []
    if caps:
        print(dim(f"  capabilities: {', '.join(c['id'] for c in caps)}"))
    if entry.get("public_url"):
        suffix = dim(" (SSO required)") if entry["access"] == "gated" else ""
        print(f"  public → {bold(entry['public_url'])}{suffix}")
    if entry["access"] == "gated":
        print(dim("  gated: push proxy/gateway/edge-loom-gated.yml to the edge "
                  "(loom gateway edge-config)"))
    if entry.get("tailnet_url"):
        print(f"  tailnet → {bold(entry['tailnet_url'])}")
    if entry["access"] in ("public", "gated"):
        print(dim("  (first request may take a moment while the app starts)"))
    return 0


def cmd_list(args) -> int:
    cfg = load_config()
    entries = registry.all_apps()
    if not entries:
        print("No apps deployed. Try: loom deploy examples/hello-web")
        return 0

    # Reconcile live status per target, and persist it back to the registry.
    by_target: dict[str, list[dict]] = {}
    for e in entries:
        by_target.setdefault(e.get("target", "local"), []).append(e)
    live: dict[str, str] = {}
    for tname, items in by_target.items():
        live.update(get_target(tname).reconcile(cfg, items))
    for e in entries:
        s = live.get(e["name"], "unknown")
        if e.get("status") != s:
            registry.set_status(e["name"], s)
        e["status"] = s

    rows = [("NAME", "STATUS", "HEALTH", "URL", "EXTERNAL", "RUNTIME", "ACCESS")]
    for e in sorted(entries, key=lambda x: x["name"]):
        external = e.get("public_url") or e.get("tailnet_url") or "-"
        health = (e.get("contract") or {}).get("health_status", "unknown")
        rows.append((e["name"], e["status"], health, e["url"], external,
                     e["runtime"], e["access"]))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]

    for idx, r in enumerate(rows):
        cells = []
        for i, val in enumerate(r):
            text = str(val).ljust(widths[i])  # pad on plain text, then style
            if idx == 0:
                text = bold(text)
            elif i == 1:  # status column
                text = _status_color(str(val))(text)
            elif i == 2:  # health column
                text = _health_color(str(val))(text)
            cells.append(text)
        print("  ".join(cells))
    return 0


def cmd_health(args) -> int:
    cfg = load_config()
    entries = [_require_entry(args.app)] if args.app else registry.all_apps()
    if not entries:
        print("No apps deployed.")
        return 0
    by_target: dict[str, list[dict]] = {}
    for e in entries:
        by_target.setdefault(e.get("target", "local"), []).append(e)
    states: dict[str, str] = {}
    for tname, items in by_target.items():
        states.update(get_target(tname).reconcile(cfg, items))
    for e in sorted(entries, key=lambda x: x["name"]):
        running = states.get(e["name"]) == "running"
        h = get_target(e.get("target", "local")).probe_health(cfg, e) if running else "—"
        if h != "—":
            registry.set_health(e["name"], h)
        print(f"  {_health_color(h)(h.ljust(8))} {e['name']}")
    return 0


def cmd_find(args) -> int:
    load_config()
    results = library.search(args.query, limit=args.limit)
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    if not results:
        print(f"No apps match '{args.query}'.")
        return 0
    for r in results:
        print(f"{bold(r['name'])}  {dim(r.get('public_url') or r.get('url') or '')}")
        if r.get("description"):
            print(f"  {r['description']}")
        ops = [o["id"] for o in r.get("operations", []) if o.get("id") != "web"]
        if ops:
            print(dim(f"  capabilities: {', '.join(ops)}"))
    return 0


def cmd_describe(args) -> int:
    cfg = load_config()
    rec = library.get(args.app)
    if rec is None:  # not harvested yet — derive on the fly from the registry
        rec = harvester.harvest_app(cfg, _require_entry(args.app))
    if args.json:
        print(json.dumps(rec, indent=2))
        return 0
    print(bold(rec["name"]) + (f" — {rec['description']}" if rec.get("description") else ""))
    for k in ("url", "public_url", "tailnet_url"):
        if rec.get(k):
            print(f"  {k}: {rec[k]}")
    print(f"  access: {rec.get('access')}   health: {rec.get('health_status')}")
    print("  operations:")
    for o in rec.get("operations", []):
        print(f"    {o.get('method', 'GET'):6} {o.get('path', '/'):26} "
              f"{bold(o.get('id', ''))} {dim(o.get('summary', ''))}")
    return 0


def cmd_reindex(args) -> int:
    cfg = load_config()
    n = library.reindex_from_registry(cfg)
    ok(f"reindexed {n} app(s) into the Library")
    return 0


def cmd_mcp(args) -> int:
    from . import mcp_server
    mcp_server.serve(load_config(), host=args.host, port=args.port)
    return 0


def cmd_data(args) -> int:
    load_config()
    apps = sorted(registry.all_apps(), key=lambda x: x["name"])
    if args.what == "ls":
        shown = False
        for e in apps:
            for ds in ((e.get("contract") or {}).get("data") or {}).get("provides", []):
                shown = True
                print(f"  {ds['name']:16} {dim('provided by')} {bold(e['name'])} {dim(ds.get('path', ''))}")
        if not shown:
            print("  no datasets provided")
    else:  # grants
        shown = False
        for e in apps:
            for g in e.get("data_grants") or []:
                shown = True
                prov = g.get("provider") or yellow("(no provider yet)")
                print(f"  {bold(e['name'])} {dim('→')} {g['dataset']} {dim('from')} {prov}")
        if not shown:
            print("  no data grants")
    return 0


def cmd_logs(args) -> int:
    cfg = load_config()
    entry = _require_entry(args.app)
    target = get_target(entry.get("target", "local"))
    return target.logs(cfg, entry, follow=args.follow, tail=args.tail)


def cmd_stop(args) -> int:
    cfg = load_config()
    entry = _require_entry(args.app)
    get_target(entry.get("target", "local")).stop(cfg, entry)
    registry.set_status(entry["name"], "exited")
    ok(f"stopped {bold(entry['name'])}")
    return 0


def cmd_start(args) -> int:
    cfg = load_config()
    entry = _require_entry(args.app)
    get_target(entry.get("target", "local")).start(cfg, entry)
    registry.set_status(entry["name"], "running")
    ok(f"started {bold(entry['name'])} → {bold(entry['url'])}")
    return 0


def cmd_remove(args) -> int:
    cfg = load_config()
    entry = _require_entry(args.app)
    get_target(entry.get("target", "local")).remove(cfg, entry)
    registry.remove(entry["name"])
    _sync_edge_gated(cfg)
    library.drop(entry["name"])
    ok(f"removed {bold(entry['name'])} from the fleet")
    return 0


def cmd_proxy(args) -> int:
    cfg = load_config()
    if args.action == "up":
        proxy.up(cfg)
    elif args.action == "down":
        proxy.down(cfg)
    else:
        proxy.status(cfg)
    return 0


def cmd_gateway(args) -> int:
    cfg = load_config()
    if args.action == "up":
        gateway.ensure(cfg)
        ok("gateway up")
    elif args.action == "down":
        gateway.relay_down(cfg)
    elif args.action == "sync":
        gateway.sync(cfg)
    elif args.action == "edge-config":
        # Print the dynamic-config files to drop on the external edge proxy.
        gw_dir = paths().proxy / "gateway"
        for fn, dest in (("edge-loom.yml", "loom.yml"), ("edge-loom-gated.yml", "loom-gated.yml")):
            f = gw_dir / fn
            if f.exists():
                print(f"# --- deploy to edge as /etc/traefik/dynamic/{dest} ---")
                print(f.read_text())
    else:
        gateway.status(cfg)
    return 0


# --- argument parser -----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loom",
        description="Loom — a local fleet host. Deploy many small apps side by side.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("deploy", help="build and deploy an app from a directory")
    d.add_argument("path", help="path to the app directory (containing fleet.app.yaml)")
    d.set_defaults(func=cmd_deploy)

    ls = sub.add_parser("list", aliases=["ls"], help="list deployed apps and their URLs")
    ls.set_defaults(func=cmd_list)

    hl = sub.add_parser("health", help="probe and refresh app health")
    hl.add_argument("app", nargs="?", help="app to probe (default: all)")
    hl.set_defaults(func=cmd_health)

    fd = sub.add_parser("find", help="search the Library for apps/capabilities")
    fd.add_argument("query")
    fd.add_argument("--json", action="store_true", help="machine-readable (for agents)")
    fd.add_argument("--limit", type=int, default=10)
    fd.set_defaults(func=cmd_find)

    ds = sub.add_parser("describe", help="show an app's capabilities as callable handles")
    ds.add_argument("app")
    ds.add_argument("--json", action="store_true")
    ds.set_defaults(func=cmd_describe)

    rx = sub.add_parser("reindex", help="rebuild the Library from the registry")
    rx.set_defaults(func=cmd_reindex)

    mc = sub.add_parser("mcp", help="serve the Library as an MCP + OpenAPI endpoint")
    mc.add_argument("--host", default="127.0.0.1")
    mc.add_argument("--port", type=int, default=7878)
    mc.set_defaults(func=cmd_mcp)

    dt = sub.add_parser("data", help="inspect the data-federation fabric")
    dt.add_argument("what", choices=["ls", "grants"], nargs="?", default="ls")
    dt.set_defaults(func=cmd_data)

    lg = sub.add_parser("logs", help="show an app's logs")
    lg.add_argument("app")
    lg.add_argument("-f", "--follow", action="store_true", help="follow log output")
    lg.add_argument("--tail", default="200", help="lines to show from the end (default 200)")
    lg.set_defaults(func=cmd_logs)

    st = sub.add_parser("stop", help="stop a running app")
    st.add_argument("app")
    st.set_defaults(func=cmd_stop)

    sr = sub.add_parser("start", help="start a stopped app")
    sr.add_argument("app")
    sr.set_defaults(func=cmd_start)

    rm = sub.add_parser("remove", aliases=["rm"], help="remove an app from the fleet")
    rm.add_argument("app")
    rm.set_defaults(func=cmd_remove)

    px = sub.add_parser("proxy", help="manage the shared reverse proxy")
    px.add_argument("action", choices=["up", "down", "status"])
    px.set_defaults(func=cmd_proxy)

    gw = sub.add_parser("gateway", help="manage external exposure (relay, auth, tailnet)")
    gw.add_argument("action", choices=["up", "down", "status", "sync", "edge-config"])
    gw.set_defaults(func=cmd_gateway)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return args.func(args)
    except LoomError as e:
        err(str(e))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
