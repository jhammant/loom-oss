# Loom

**Deploy a small app with one command — instantly routed at its own URL, health-checked, discoverable, agent-callable, and able to consume platform services. No per-app plumbing.**

Loom is an **agent-native, additive app-hosting platform**. Drop a `fleet.app.yaml` next to your app, run `loom deploy`, and the app is live behind a shared reverse proxy at a stable URL — TLS, health checks, service discovery, and an LLM-addressable index come for free. It is built for the apps that get *vibecoded* and deployed **by agents** (e.g. Claude Code): each one is a small unit that shows up in the fleet without you wiring up routing, certs, or a control plane by hand.

## Why "additive"?

Most platforms are *substitutive*: adopting them means rebuilding your app to fit their model. Loom is **additive** — every app is a self-contained directory plus one manifest. Adding the Nth app costs the same as the first:

- **No per-app plumbing.** Loom writes one route file per app for Traefik's file provider; no DNS setup, no Docker-label wrangling, no hand-edited proxy config.
- **Backward-compatible contract.** A four-field v1 manifest deploys today. Every richer capability (health, capabilities, shared services, data federation) is an *optional, defaulted* v2 field — old manifests deploy byte-identically.
- **Discoverable by default.** The moment an app is live it is indexed into the **Library** and reachable by humans (`loom find` / `loom describe`) and by agents (`loom mcp`).

## Quickstart

### Prerequisites

- **Docker** (or OrbStack) — each app runs as a container behind a shared Traefik proxy.
- **Python 3.9+** and **pipx** — to install the `loom` CLI.
- **mkcert** — for trusted local TLS on `*.loom.localhost`. Without it Loom falls back to a self-signed cert (browser warning); install it for clean HTTPS.

`*.localhost` resolves to `127.0.0.1` automatically — there is **no DNS to configure**.

### Install

```bash
pipx install -e ./cli       # installs the `loom` command
mkcert -install             # one-time: trust mkcert's local CA
loom proxy up               # start the shared reverse proxy (generates the local cert + network)
```

### Deploy your first apps

```bash
loom deploy examples/hello-web      # a minimal Node app  → https://hello-web.loom.localhost:8443
loom deploy examples/hello-static   # a static site (nginx) → https://hello-static.loom.localhost:8443
loom list
```

```text
NAME          STATUS   HEALTH  URL                                       EXTERNAL  RUNTIME  ACCESS
hello-static  running  ok      https://hello-static.loom.localhost:8443  -         static   public
hello-web     running  ok      https://hello-web.loom.localhost:8443     -         node     public
```

> HTTPS defaults to port **8443** (443 is often reserved by local stacks like OrbStack's domain proxy). Open the printed URL in a browser.

Everyday commands:

```bash
loom logs hello-web -f      # stream logs
loom health                 # probe + refresh health for the fleet
loom stop hello-web         # stop / start without removing
loom start hello-web
loom remove hello-web       # tear down and de-index
loom proxy status           # proxy state + dashboard URL
```

## The manifest

Every app declares itself in one `fleet.app.yaml`. The four v1 fields are all you need:

```yaml
name: hello-web      # becomes the subdomain; DNS-safe (lowercase, digits, hyphens)
runtime: node        # node | python | static | docker
port: 3000           # the port the app binds; the app reads $PORT (omit for static)
access: public       # public | gated | private
```

For `node` / `python` / `static`, Loom generates the Dockerfile; with `runtime: docker` it builds the app's **own** Dockerfile.

The optional, defaulted **v2 contract** layers on metadata, health, capabilities, and the hooks for shared services and data federation:

```yaml
manifest_version: 2

metadata:
  description: One line describing what the app does.
  tags: [search, demo]

health:
  path: /health                 # default "/health"

capabilities:                   # what agents can call
  - id: search
    kind: http                  # http | openapi | mcp
    path: /search
    input_schema:  { type: object, properties: { q: { type: string } }, required: [q] }
    output_schema: { type: object }

consumes:                       # shared platform services this app wants
  - service: wallet
    scope: charge

provides_service: wallet        # (backend apps) the service this app provides

data:                           # cross-app dataset federation
  provides: [{ name: orders, api: rest, path: /api/orders }]
  consumes: [{ name: customers, api: rest }]
```

Full reference: [`docs/app-contract.md`](docs/app-contract.md).

## Access tiers

`access:` controls how an app is exposed:

| Tier | Reachability |
| --- | --- |
| **public** | Routed and open — reachable by anyone who can reach the proxy. |
| **gated** | Routed, but behind **forward-auth SSO** at the proxy edge; bypassed on LAN/tailnet. |
| **private** | Not publicly routed — local-only, plus an optional **Tailscale serve** URL. |

Private apps never get a public route file, so the tier is enforced at the proxy itself (and `loom mcp` refuses to invoke them).

## Agent-native: a Library agents can find *and* call

When an app deploys, Loom **harvests** its contract from the registry into `fleet/library.json` — the LLM-addressable **Library**.

```bash
loom find search                 # search apps + capabilities (add --json for agents)
loom describe capability-demo     # show an app's callable operations (method, path, schemas)
```

`loom mcp` then serves that Library as a live, callable surface so coding agents can both **discover** and **invoke** fleet apps:

```bash
loom mcp                          # Streamable HTTP on 127.0.0.1:7878
```

It exposes:

- an **MCP** endpoint (JSON-RPC) with stable meta-tools — `loom_search_apps`, `loom_describe_app`, `loom_invoke` — plus `loom://app/{name}` resources. A few tools over the whole fleet, not one tool per operation (which would blow up context).
- **OpenAPI 3.1** at `/openapi.json` for non-MCP HTTP agents.
- a plain **REST** projection (`/apps`, `/search`, `/invoke`).

`loom_invoke` only proxies **registered** apps (never arbitrary URLs) and **refuses private apps** — the SSRF / tier-leak guard.

## Shared services & data federation

**Shared services** — an app declares a service it needs with `consumes:`; Loom resolves it against a deployed `provides_service:` app and injects `LOOM_<SVC>_URL` plus an HMAC `LOOM_<SVC>_TOKEN`, which the provider verifies (app-to-app identity). See `sdk/python/loom_sdk.py`; the dogfooded backends are `examples/loom-wallet` (a credit ledger with 401/402/idempotency) and `examples/loom-llm` (a **no-key LLM gateway** — declare `consumes: [llm]` and call `loom_sdk.llm().chat(...)` with no provider key in your app; per-app token cap, bring-your-own `ANTHROPIC_API_KEY` via the gitignored `fleet/secrets.json`, stub mode until then). `examples/wallet-consumer` and `examples/llm-consumer` prove the chains.

**Data federation** — `data.provides` / `data.consumes` wire consumers to a grant-checked **federation gateway** (`examples/loom-fed`, deny-by-default with a live-grant check); inspect the fabric with `loom data ls` and `loom data grants`. See `examples/data-provider` and `examples/data-consumer`.

## External exposure

For reachability beyond the host, Loom ships a **native relay** (a non-Docker listener that works around Docker port-publishing quirks on hosts like OrbStack, which only forward published ports on loopback) that **your own** reverse proxy forwards to:

```bash
loom gateway sync         # detect LAN IP, regenerate proxy/gateway/edge-loom.yml
loom gateway edge-config   # print the dynamic-config to drop on your edge proxy
loom gateway status
```

Deploy `proxy/gateway/edge-loom.yml` to your reverse proxy. (`loom gateway sync`'s pct-based push is a **Proxmox example**; the generic path is just "deploy that file to your edge.")

## Repo layout

```text
cli/                    The `loom` CLI (Python)
  loom/                 deploy/list/logs/proxy/gateway/find/describe/mcp/data + the contract, harvester, Library
  loom/targets/         target-adapter seam (base.py); only `local` (Docker) is implemented today
  tests/                38 tests (manifest/contract, Library, MCP, services, federation, SDK)
proxy/                  Shared Traefik reverse proxy (docker-compose.yml), per-app route files, local certs
sdk/python/             loom_sdk.py — call shared services from an app
examples/               hello-web, hello-static, hello-clock, hello-gated, capability-demo,
                        loom-wallet + wallet-consumer, loom-llm + llm-consumer,
                        loom-fed + data-provider/data-consumer
docs/                   app-contract.md (the manifest reference)
```

The **target-adapter seam** (`cli/loom/targets/base.py`) is where future deploy targets plug in — `local` is the only one in the open core.

## Open core, BSL — and a hosted version

This repository is the **open core** of Loom, released under the **Business Source License 1.1** (it converts to **Apache 2.0** on **2030-06-10**). You may run it in production and self-host the whole thing; the one limit is offering Loom *itself* to third parties as a competing hosted service. See [`LICENSE`](LICENSE) for exact terms and [`OPEN-CORE.md`](OPEN-CORE.md) for what is in the open core versus the commercial product.

There is also a **hosted version** of Loom. The open core ships the *mechanisms* — the `consumes:` wiring, the federation engine, self-host examples — while multi-tenancy, accounts/SSO/RBAC, a web dashboard, billing/quotas, managed consumables (a hosted LLM/image gateway, managed Postgres/storage/domains), and a managed cloud deploy target are reserved for the hosted product.

## Docs

- [`docs/app-contract.md`](docs/app-contract.md) — the full `fleet.app.yaml` reference (v1 + v2).
- [`OPEN-CORE.md`](OPEN-CORE.md) — open core vs. hosted boundary.
- [`LICENSE`](LICENSE) — BSL 1.1 terms.
- `examples/` — a working app for every feature above.
