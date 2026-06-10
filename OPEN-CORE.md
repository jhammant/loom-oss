# Loom — Open Core

Loom is an **agent-native, additive app-hosting platform**: deploy a small app
with one command and it is instantly routed at its own URL, health-checked,
discoverable, agent-callable, and able to consume platform services — with **no
per-app plumbing**. It is built for apps that are often "vibecoded" or deployed
*by* agents (e.g. Claude Code).

This repository is the **open core**, released under the
[Business Source License 1.1](#the-license-bsl-11) (BSL). It is a complete,
self-hostable platform: everything you need to run a fleet of apps on your own
hardware is here. A separate **commercial/hosted product** builds on the same
core and is what the BSL reserves (see the boundary below).

This document is the **boundary spec** — what lives in the open core, what is
reserved for the commercial product, and why the line is drawn where it is.

---

## TL;DR

- **In the open core:** the full `loom` CLI, the local Docker substrate behind a
  shared Traefik reverse proxy, the versioned `fleet.app.yaml` contract, the
  three access tiers, the LLM-addressable Library, the `loom mcp` server
  (MCP + OpenAPI + REST), the shared-services **mechanism** (`consumes:` /
  `provides_service:`), data federation, native external exposure (relay +
  edge-config), and the target-adapter seam. Everything needed to self-host a
  fleet, end to end.
- **Reserved for the commercial/hosted product:** multi-tenancy and per-tenant
  isolation; accounts/teams/SSO/RBAC and a web dashboard; billing, usage
  metering, quotas, and spend caps; **managed** consumables (a hosted LLM/image
  gateway holding provider keys, managed Postgres/storage/domain provisioning);
  and a managed cloud deploy target with its hosted control plane.
- **The principle:** the open core ships every *mechanism*; the commercial
  product ships the *managed, multi-tenant operation* of those mechanisms.

---

## What the open core includes

Everything below is built, tested, and shipping in this repo (26 tests pass).

### The `loom` CLI

A single Python CLI (`pipx install -e ./cli`), target-agnostic by design:

| Area | Commands |
| --- | --- |
| App lifecycle | `deploy`, `list` (`ls`), `logs`, `stop`, `start`, `remove` (`rm`) |
| Reverse proxy | `proxy up` \| `down` \| `status` |
| External exposure | `gateway up` \| `down` \| `status` \| `sync` \| `edge-config` |
| Health | `health` |
| Library (discovery) | `find`, `describe` (both with `--json` for agents), `reindex` |
| Agent surface | `mcp` |
| Data federation | `data ls` \| `data grants` |

### Local substrate

- Each app runs as a **Docker container** behind a **shared Traefik reverse
  proxy** using Traefik's **file provider** — Loom writes one route file per app,
  so there is **no per-app proxy config to maintain**.
- Generated Dockerfiles for the `node`, `python`, and `static` runtimes; the
  `docker` runtime uses the app's own Dockerfile.
- `*.localhost` resolves to `127.0.0.1` with **no DNS setup**; optional local TLS
  via [`mkcert`](https://github.com/FiloSottile/mkcert).

### The app contract — `fleet.app.yaml`

A **versioned, additive** manifest (full reference:
[`docs/app-contract.md`](docs/app-contract.md)):

- **v1** — `name`, `runtime`, `port`, `access`. Four fields, and you can deploy.
- **v2** (every field optional, defaulted, and backward-compatible) —
  `metadata`, `health`, `capabilities`, `consumes`, `data`, and
  `provides_service`. A v1 manifest deploys byte-identically; a newer
  `manifest_version` is accepted with a warning rather than hard-failing.

### Access tiers

| Tier | Behaviour |
| --- | --- |
| `public` | Routed, reachable by anyone. |
| `gated` | Routed, behind forward-auth SSO at the reverse-proxy edge; bypassed on LAN/tailnet. |
| `private` | Not routed publicly; local, plus an optional Tailscale-serve URL. |

### The LLM-addressable Library

`fleet/registry.json` (operational state) is **harvested** into
`fleet/library.json` — the discovery surface that `loom find` and
`loom describe` read. With `--json`, both are made for agents to consume
directly.

### `loom mcp` — the agent surface

An **MCP server** (Streamable HTTP / JSON-RPC 2.0) **plus** an OpenAPI 3.1 spec
**plus** a plain REST projection of the Library, so agents can both **discover
and call** fleet apps:

- Tools: `loom_search_apps`, `loom_describe_app`, `loom_invoke`.
- Resources: `loom://app/{name}`.
- Progressive disclosure: a few stable meta-tools over the whole fleet rather
  than one tool per operation (which would explode an agent's context).
- Guardrails: `loom_invoke` only proxies **registered** apps (never arbitrary
  URLs) and **refuses `private` apps** — the SSRF / tier-leak guard.

### Shared services — the `consumes:` mechanism

An app declares a service it needs (`consumes:`); Loom resolves it against a
deployed provider (`provides_service:`) and injects `LOOM_<SVC>_URL` plus an
HMAC `LOOM_<SVC>_TOKEN`; the provider verifies the caller (app-to-app identity).
Resolution is best-effort — an unresolved consume warns and blocks nothing.

- SDK: [`sdk/python/loom_sdk.py`](sdk/python/loom_sdk.py).
- Dogfooded backend: [`examples/loom-wallet`](examples/loom-wallet) — a credit
  ledger with `401` / `402` / idempotency semantics.
- [`examples/wallet-consumer`](examples/wallet-consumer) proves the full chain
  end to end.

> The open core ships the **mechanism** and self-host **examples**. The
> *managed* backends those examples stand in for (a hosted LLM gateway, managed
> billing) are the commercial product — see the boundary below.

### Data federation

- `data.provides` / `data.consumes` on the contract, plus a **grant-checked
  gateway** that is **deny-by-default** with a **live-grant check** at request
  time ([`examples/loom-fed`](examples/loom-fed)).
- Inspectable with `loom data ls` and `loom data grants`.
- [`examples/data-provider`](examples/data-provider) and
  [`examples/data-consumer`](examples/data-consumer) demonstrate the fabric.

### External exposure

A **native relay** works around Docker port-publishing quirks on some hosts
(e.g. OrbStack, which only forwards published ports on loopback) that your **own
reverse proxy** then forwards to. `loom gateway sync` auto-detects the LAN IP,
regenerates `proxy/gateway/edge-loom.yml`, and can push it to the edge. The
pct-based push is a **Proxmox example**; the generic path is simply: **deploy
`proxy/gateway/edge-loom.yml` to your reverse proxy** (`loom gateway edge-config`
prints exactly what to drop on the edge).

### The target-adapter seam

[`cli/loom/targets/base.py`](cli/loom/targets/base.py) defines the small
`Target` interface every deploy adapter implements. Only **`local`** (Docker) is
implemented in the open core; this seam is the extension point where future
deploy targets plug in without touching the CLI.

---

## What is reserved for the commercial / hosted product

The boundary is deliberate: **the open core ships every mechanism; the
commercial product is the managed, multi-tenant operation of those mechanisms.**
Reserved capabilities are **not** crippled stubs in this repo — they simply are
not here.

| Capability | Open core | Commercial / hosted |
| --- | :---: | :---: |
| Local Docker substrate + shared reverse proxy | ✅ | ✅ |
| `fleet.app.yaml` contract (v1 + v2) | ✅ | ✅ |
| Access tiers (public / gated / private) | ✅ | ✅ |
| Library + `loom find` / `describe` | ✅ | ✅ |
| `loom mcp` (MCP + OpenAPI + REST) | ✅ | ✅ |
| Shared-services **mechanism** (`consumes:` / `provides_service:`) | ✅ | ✅ |
| Data federation (grant-checked gateway) | ✅ | ✅ |
| Native relay + edge-config for self-hosted exposure | ✅ | ✅ |
| Target-adapter seam (`local` implemented) | ✅ | ✅ |
| **Multi-tenancy + per-tenant isolation** | ❌ | ✅ |
| **Accounts / teams / SSO / RBAC** | ❌ | ✅ |
| **Web dashboard** | ❌ | ✅ |
| **Billing, usage metering, quotas, abuse/spend caps** | ❌ | ✅ |
| **Managed LLM / image gateway** (hosted, holds provider keys) | ❌ | ✅ |
| **Managed Postgres / storage / domain provisioning** | ❌ | ✅ |
| **Managed cloud deploy target + hosted control plane** | ❌ | ✅ |

In short, the commercial product adds the things that only make sense as a
**hosted, multi-tenant service**:

- **Multi-tenancy & isolation** — running many tenants' fleets safely on shared
  infrastructure, with per-tenant isolation.
- **Identity & administration** — accounts, teams, SSO, RBAC, and a web
  dashboard over the fleet.
- **Commercial controls** — billing, usage metering, quotas, and abuse/spend
  caps.
- **Managed consumables** — the open core ships the `consumes:` *mechanism* plus
  self-host examples; the hosted product provides the **managed backends**: an
  LLM/image gateway that holds the provider keys, and managed
  Postgres/storage/domain provisioning.
- **Managed cloud deploy** — a hosted deploy target (slotting into the same
  target-adapter seam) and the control plane behind it.

---

## The license (BSL 1.1)

The open core is licensed under the **Business Source License 1.1**.

| Parameter | Value |
| --- | --- |
| **Licensor** | Jon Hammant |
| **Licensed Work** | Loom (the version released under this license) |
| **Additional Use Grant** | You may make production use of the Licensed Work, provided you do not offer the Licensed Work to third parties as a hosted or managed service that competes with the Licensor's commercial Loom offering. |
| **Change Date** | 2030-06-10 (four years after release) |
| **Change License** | Apache License, Version 2.0 |

### Why BSL?

The BSL is chosen to balance three goals:

- **Self-host freely.** You can run Loom in production — for yourself, your team,
  or your company — on your own hardware, today, at no cost. The only thing the
  Additional Use Grant withholds is offering Loom *itself* to third parties as a
  competing hosted/managed service.
- **Trust through source availability.** The entire platform is readable,
  auditable, forkable, and self-hostable. There is no hidden runtime: what you
  deploy is what you can read. That matters especially for software that agents
  deploy and call on your behalf.
- **A sustainable funnel.** Source availability + frictionless self-hosting is
  the on-ramp; the managed, multi-tenant product funds continued development.
  The one carve-out — no competing hosted service before the Change Date — is
  what keeps that funnel viable.

On the **Change Date (2030-06-10)**, the Licensed Work automatically converts to
the **Apache License 2.0**. BSL is time-boxed by design: the restriction is
temporary, and the long-term destination is a permissive open-source license.

---

## Contributing

Contributions are welcome and accepted under the **project license** (BSL 1.1,
converting to Apache 2.0 on the Change Date). By submitting a contribution you
agree it is licensed under those terms. Good first areas:

- New deploy targets behind the `Target` seam
  ([`cli/loom/targets/base.py`](cli/loom/targets/base.py)).
- New runtimes and Dockerfile generators.
- Additional example apps that exercise the contract, shared services, or data
  federation.
- Documentation and test coverage.

Please keep contributions focused, tested, and consistent with the patterns
already in the repo.

---

> **Note:** the `LICENSE` file should be reviewed by counsel before public
> release.
