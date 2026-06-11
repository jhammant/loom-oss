# Changelog

All notable changes to Loom are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may
include breaking changes, though the app contract itself is additive by
design).

## [0.1.0] — 2026-06-11

First public release of the Loom open core.

### Added

- **`loom` CLI** — `deploy`, `list`, `logs`, `stop`, `start`, `remove`,
  `proxy up|down|status`, `gateway up|down|status|sync|edge-config`,
  `health`, `find`, `describe`, `reindex`, `mcp`, `data ls|grants`.
- **Local Docker substrate** behind a shared Traefik reverse proxy (file
  provider — one generated route file per app, no per-app proxy config).
  Generated Dockerfiles for `node`, `python`, and `static` runtimes;
  `docker` runtime uses the app's own Dockerfile. Local TLS via mkcert on
  `*.loom.localhost`.
- **App contract** (`fleet.app.yaml`) — versioned and additive. v1 is four
  fields (`name`, `runtime`, `port`, `access`); v2 adds optional, defaulted
  `metadata`, `health`, `capabilities`, `consumes`, `provides_service`,
  `secrets`, and `data`. Full reference in `docs/app-contract.md`.
- **Access tiers** — `public`, `gated` (forward-auth SSO at the edge),
  `private` (never publicly routed).
- **Library** — deployed apps are harvested into an LLM-addressable index
  (`fleet/library.json`) searchable via `loom find` / `loom describe`
  (both with `--json`).
- **`loom mcp`** — MCP (Streamable HTTP/JSON-RPC) + OpenAPI 3.1 + REST
  surface with `loom_search_apps`, `loom_describe_app`, `loom_invoke` and
  `loom://app/{name}` resources. Invoke proxies registered apps only and
  refuses `private` apps.
- **Shared services** — `consumes:` / `provides_service:` resolution with
  injected `LOOM_<SVC>_URL` + HMAC `LOOM_<SVC>_TOKEN`; Python SDK
  (`sdk/python/loom_sdk.py`) with `wallet()`, `llm()`, and `identity()`.
  Dogfooded backends: `examples/loom-wallet` (credit ledger) and
  `examples/loom-llm` (bring-your-own-key LLM gateway with per-app token
  caps and stub mode).
- **Server-side secrets** — `secrets:` in the manifest injects values from
  the gitignored `fleet/secrets.json` at deploy time.
- **Data federation** — `data.provides` / `data.consumes` wired through a
  grant-checked, deny-by-default federation gateway (`examples/loom-fed`).
- **External exposure** — native relay + `loom gateway sync` /
  `edge-config` for fronting the fleet with your own edge proxy.
- **Target-adapter seam** — `cli/loom/targets/base.py`; `local` (Docker)
  implemented.
- **`loom admin`** — localhost-only fleet console: live fleet table with
  one-click stop/start/remove and logs, plus a directory scanner (default
  `~/dev`) that infers runtimes and deploys candidates, writing a minimal
  `fleet.app.yaml` next to apps that lack one.
- Test suite: 46 tests across contract, Library, MCP, services, federation,
  SDK, and the admin console.
