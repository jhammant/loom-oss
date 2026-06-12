# Changelog

All notable changes to Loom are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may
include breaking changes, though the app contract itself is additive by
design).

## [0.2.0] — 2026-06-12

### Added

- **`loom admin` grew into a real console** — app detail drawer (contract,
  operations, grants, secrets-names, live usage), "the weave" shared-services
  view (providers, consumers, resolved/unresolved grants, data federation),
  CPU/MEM columns from `docker stats`, a config drawer, and **import from
  git** (URL or `user/repo` shorthand → clone into the scan root → deploy).
- **Admin authentication** — `loom admin --set-password` stores a
  PBKDF2-SHA256 credential in the gitignored `fleet/secrets.json`; the console
  then enforces HTTP Basic auth (timing-safe, throttled), and `--host` may
  bind beyond loopback. Without credentials it remains loopback-only.
- **Per-app authorization on the gated tier** — `allow.users` /
  `allow.groups` in the manifest; Loom generates header-matcher routers
  checking the SSO-injected `Remote-User`/`Remote-Groups`, answering 403 to
  authenticated-but-not-allowed users. Local hostname stays open.
- **Analytics** — a new `analytics` shared service
  (`examples/loom-analytics`, HMAC-verified `/track` + `/stats`) with
  `loom_sdk.analytics().track()` (fire-and-forget) and
  `examples/analytics-consumer`; plus **zero-instrumentation traffic stats**:
  the proxy writes a JSON access log and the console shows per-app
  requests / avg latency / errors / denied for the last hour.
- **Platform admin over MCP** — `loom mcp --admin` adds `loom_fleet`,
  `loom_deploy`, `loom_stop`, `loom_start`, `loom_remove`, `loom_services`,
  `loom_traffic` so agents can operate the fleet, not just call it.
  Loopback-only; tools refused without the flag.

### Changed

- Published package renamed `loom-cli` → **`loomhost`** (PyPI name-similarity
  rule); the installed command is unchanged (`loom`).
- Test suite: 66 tests.

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
