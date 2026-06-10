# Loom Architecture

Loom is an **additive, agent-native app-hosting platform**. You deploy a small app
with one command; it is instantly routed at its own URL, health-checked,
discoverable, agent-callable, and able to consume platform services — with **no
per-app plumbing**. Apps are often vibecoded or deployed *by* agents (e.g. Claude
Code), so every surface is machine-readable and the contract is additive: a
four-field v1 manifest deploys byte-identically to a fully-specified v2 one.

This document explains how the open core works end to end:

1. [The shape of the system](#1-the-shape-of-the-system)
2. [Deploy: containers behind a Traefik file-provider proxy](#2-deploy-containers-behind-a-traefik-file-provider-proxy)
3. [Local routing + TLS (`*.localhost` / mkcert)](#3-local-routing--tls-localhost--mkcert)
4. [The discovery pipeline: contract → registry → harvester → Library → MCP](#4-the-discovery-pipeline-contract--registry--harvester--library--mcp)
5. [Shared services: `consumes` / provisioning + the HMAC token model](#5-shared-services-consumes--provisioning--the-hmac-token-model)
6. [Data federation + the grant-checked gateway](#6-data-federation--the-grant-checked-gateway)
7. [External exposure: the native relay + the edge](#7-external-exposure-the-native-relay--the-edge)
8. [Access tiers, end to end](#8-access-tiers-end-to-end)
9. [The target-adapter seam](#9-the-target-adapter-seam)
10. [State files & reference map](#10-state-files--reference-map)

Everything below references real files in this repo. The CLI lives in
[`cli/loom/`](cli/loom); the shared reverse proxy in [`proxy/`](proxy); the SDK in
[`sdk/python/loom_sdk.py`](sdk/python/loom_sdk.py); worked examples in
[`examples/`](examples); the contract reference in
[`docs/app-contract.md`](docs/app-contract.md).

---

## 1. The shape of the system

```text
                                          loom CLI  (cli/loom/*.py)
                                    deploy · list · health · find · describe
                                  proxy · gateway · mcp · data · logs/stop/start
                                                   │
            ┌──────────────────────────────────────┼──────────────────────────────────────┐
            │ writes route files                    │ records state                         │ projects
            ▼                                        ▼                                        ▼
   ┌──────────────────┐   FILE provider     ┌──────────────────┐  harvest   ┌──────────────────────┐
   │  Traefik (proxy) │◀── proxy/dynamic ───│ fleet/registry   │──────────▶ │ fleet/library.json   │
   │   "loom-proxy"   │     app-<n>.yml      │     .json        │            │ (LLM-addressable)    │
   └────────┬─────────┘                      └──────────────────┘            └──────────┬───────────┘
            │ routes by Host(), over the                                                │ loom mcp
            │ shared `loom` docker network                                              ▼
            ▼                                                              ┌──────────────────────────┐
   ┌──────────────────────────────────────────┐                          │ MCP (POST /mcp) ·         │
   │ app containers  loom-<name>               │   consumes:  LOOM_*_URL  │ OpenAPI 3.1 · REST        │
   │  node | python | static | docker          │◀── + HMAC LOOM_*_TOKEN   │ search / describe / invoke│
   │  each binds $PORT; reads LOOM_* env        │   injected at deploy     └──────────────────────────┘
   └──────────────────────────────────────────┘
```

Three planes, all driven by the CLI:

- **Routing plane** — a single shared Traefik instance ([`proxy/docker-compose.yml`](proxy/docker-compose.yml))
  whose route table is a directory of files Loom writes ([`proxy/dynamic/`](proxy)).
- **State plane** — [`fleet/registry.json`](cli/loom/registry.py) is the record of
  every deployed app; it is *harvested* into [`fleet/library.json`](cli/loom/library.py),
  the searchable, LLM-addressable index.
- **Service plane** — apps declare what they **provide** and **consume** in their
  manifest; Loom resolves those against the live fleet and injects URLs + HMAC
  tokens at deploy time. No service mesh, no per-app config.

There is no central daemon beyond Traefik (and optional native relays). All Loom
state is plain JSON and all routing state is plain YAML on disk — inspectable,
diffable, regenerable.

---

## 2. Deploy: containers behind a Traefik file-provider proxy

The whole deploy flow is `cmd_deploy` in [`cli/loom/cli.py`](cli/loom/cli.py):

```python
manifest = load_manifest(app_dir)            # validate fleet.app.yaml
target   = get_target(cfg["default_target"]) # "local" today
entry    = target.deploy(cfg, app_dir, manifest)
registry.upsert(entry)                        # → fleet/registry.json
_sync_edge_gated(cfg)                          # keep the edge gated-router file fresh
library.upsert(harvester.harvest_app(cfg, entry))  # → fleet/library.json
```

The **local** target ([`cli/loom/targets/local.py`](cli/loom/targets/local.py))
does the real work:

1. **Bring up the proxy** (`proxy.ensure`) and, if a public domain is set and the
   app is `public`/`gated`, the gateway relay (`gateway.ensure`). Both idempotent.
2. **Build an image** `loom/<name>:latest`:
   - `node` / `python` / `static` get a **generated Dockerfile** from
     [`cli/loom/dockerfiles.py`](cli/loom/dockerfiles.py) (Node 22 / Python 3.12 /
     nginx). The app needs no Dockerfile.
   - `runtime: docker` uses the app's **own `Dockerfile`** verbatim.
3. **Run the container** `loom-<name>` on the shared `loom` Docker network, with
   the Loom environment injected (see below).
4. **Wire routing by tier:**
   - `public` / `gated` → write a per-app route file (`proxy.write_route`); the app
     is reachable only on the `loom` network, fronted by Traefik.
   - `private` → **no** route file; instead publish to `127.0.0.1:<free-port>` and
     optionally expose a tailnet-only URL. A private app is *structurally*
     unreachable on any public hostname because no route file is ever written for it.
5. **Return a registry entry** (url, public_url, tailnet_url, container, image,
   `contract` snapshot, resolved `grants` / `data_grants`, status) which the CLI
   upserts into the registry.

Every container receives a small, stable env contract (from `local.py`):

```text
PORT                 the port the app binds (static → 80, served by nginx)
LOOM_APP             the app's own name  (used as its identity in HMAC tokens)
LOOM_HEALTH_PATH     manifest health.path (default /health)
LOOM_CAPABILITIES    comma-joined capability ids
# plus, when applicable, the service/data wiring from §5 and §6
```

### Why a FILE provider, not Docker labels

Traefik is configured **entirely by flags** and reads its routes from a watched
directory ([`proxy/docker-compose.yml`](proxy/docker-compose.yml)):

```yaml
- "--providers.file.directory=/dynamic"
- "--providers.file.watch=true"
```

Loom writes exactly one file per routed app — `proxy/dynamic/app-<name>.yml`
([`proxy.write_route`](cli/loom/proxy.py)) — and Traefik hot-reloads it. This is a
deliberate design choice:

- **Loom owns the route table.** The set of route files *is* the public surface of
  the fleet — the seed of the Library. Nothing is inferred from container labels.
- **No Docker socket.** Traefik never needs `/var/run/docker.sock`, which sidesteps
  the Traefik-Docker-provider API-version incompatibility with newer daemons and
  removes a privileged mount.
- **Tier enforcement is structural.** The route file only exists for `public`/`gated`
  apps, so `private` apps cannot be matched on a public hostname.

A generated route file looks like this (one router + one service, matched by host,
TLS on, pointing at the container over the `loom` network by name):

```yaml
# Generated by Loom for app 'hello-web'. Do not edit by hand.
http:
  routers:
    hello-web:
      rule: "Host(`hello-web.loom.localhost`)"   # || Host(`hello-web.<public_domain>`) when set
      entryPoints: [websecure]
      service: hello-web
      tls: {}
  services:
    hello-web:
      loadBalancer:
        servers:
          - url: "http://loom-hello-web:3000"
```

The app container and Traefik join the **same external `loom` network** (created
and owned by the CLI, `networks: { loom: { external: true } }`), so Traefik
reaches the app by container name (`loom-hello-web:3000`) with no port publishing.

---

## 3. Local routing + TLS (`*.localhost` / mkcert)

The default base domain is `loom.localhost` ([`cli/loom/config.py`](cli/loom/config.py)).
`*.localhost` resolves to `127.0.0.1` on every modern OS, so **no `/etc/hosts` edits
and no DNS setup** are required — `hello-web.loom.localhost` just works.

```text
   browser                         loom-proxy (Traefik)               app container
─────────────                  ───────────────────────             ─────────────────
https://hello-web.loom.localhost:8443
        │  (resolves to 127.0.0.1)
        ▼
   127.0.0.1:8443  ──────────▶  match Host(`hello-web.loom.localhost`)
                                 terminate TLS (mkcert wildcard)
                                 route to service "hello-web"  ────▶  http://loom-hello-web:3000
                                          (over the `loom` docker network, by name)
```

**Ports.** Traefik publishes on **loopback only** — this is a local fleet host, not
a LAN service:

```yaml
ports:
  - "127.0.0.1:${LOOM_HTTP_PORT:-80}:80"
  - "127.0.0.1:${LOOM_HTTPS_PORT:-8443}:${LOOM_HTTPS_PORT:-8443}"
  - "127.0.0.1:8080:8080"   # dashboard
```

HTTPS defaults to **8443**, not 443 — some local stacks (e.g. OrbStack's domain
proxy) reserve 443, and 8443 needs no system changes. `web` (:80) issues a
permanent redirect to `websecure`; because `websecure` listens on the *same* port
number Loom publishes, the redirect emits the correct external port automatically.
HTTP/HTTPS ports are parameterised via `LOOM_HTTP_PORT` / `LOOM_HTTPS_PORT`, which
the CLI exports from `fleet/config.json` (`proxy.compose_env`).

**TLS via mkcert.** On `loom proxy up`, `ensure_cert` ([`cli/loom/proxy.py`](cli/loom/proxy.py)):

- runs `mkcert` to mint a wildcard cert for `*.<base_domain>` + `<base_domain>` into
  [`proxy/certs/`](proxy/certs) (gitignored), then writes `proxy/dynamic/tls.yml`
  pointing Traefik at it as the default certificate;
- if `mkcert` isn't installed, it writes an empty `tls.yml` so Traefik falls back to
  its built-in self-signed cert (browsers warn) — deploy never blocks on TLS.

Because the cert is a local mkcert/self-signed one, the CLI's own probes
(harvester, health, MCP `invoke`) connect with TLS verification disabled when they
reach apps through Traefik on loopback (see `verify_mode = ssl.CERT_NONE` in
[`harvester.py`](cli/loom/harvester.py), [`local.py`](cli/loom/targets/local.py),
[`mcp_server.py`](cli/loom/mcp_server.py)).

---

## 4. The discovery pipeline: contract → registry → harvester → Library → MCP

This is the spine of Loom's agent-nativeness: a single machine-readable contract
flows into a searchable, callable surface with no extra work from the app author.

```text
fleet.app.yaml                 deploy            harvest               loom mcp
(the contract)  ──parse──▶  registry.json  ──flatten──▶  library.json  ──project──▶  MCP / OpenAPI / REST
 §contract.py    snapshot   §registry.py    +live probe   §library.py    serve       §mcp_server.py
                            contract block               operations[]    search()
```

### 4.1 The contract (`fleet.app.yaml`)

Parsed and validated by [`cli/loom/manifest.py`](cli/loom/manifest.py) (v1 fields)
and [`cli/loom/contract.py`](cli/loom/contract.py) (v2 fields). Full reference:
[`docs/app-contract.md`](docs/app-contract.md).

- **v1 (required):** `name` (DNS-safe subdomain), `runtime`
  (`node|python|static|docker`), `port`, `access` (`public|gated|private`).
- **v2 (all optional, defaulted):** `metadata` (description/tags/owner), `health.path`,
  `capabilities[]` (`id` + `kind` of `http|openapi|mcp` + `path` + JSON-Schema
  `input_schema`/`output_schema`), `consumes[]`, `data.{provides,consumes}`,
  `provides_service`. `manifest_version` defaults to 1; a *newer* version is accepted
  with a warning (best-effort), and unknown top-level keys are ignored — a newer
  manifest never hard-fails an older CLI.

`semantics:` on capabilities/datasets is **accepted and stored but not yet acted
on** — forward-compat for taxilang/Orbital semantic types.

### 4.2 The registry

`contract.snapshot(manifest)` produces the `contract` block stored on each registry
entry, kept **separate from operational fields** (url, container, status). On
redeploy, [`registry.upsert`](cli/loom/registry.py) **carries forward**
`harvested_at` / `health_status` / `capability_index` so a redeploy doesn't blank a
later harvest, and preserves `created_at`.

### 4.3 The harvester

[`cli/loom/harvester.py`](cli/loom/harvester.py) turns one registry entry into a
flattened, searchable **Library record**. It is target-agnostic — it reads the
entry and reaches the app at its recorded URL (loopback-reachable for both routed
apps via Traefik and private apps via `127.0.0.1`):

- every app gets a default `web` operation;
- each declared capability becomes an `operation`; a `kind: openapi` capability is
  **fetched and expanded** — `_flatten_openapi` walks the spec's `paths` and emits
  one operation per `operationId`;
- `data.provides` datasets are attached as `datasets`;
- operations are **deduped by `(method, path)`** so a declared `http` capability and
  the same op from a flattened OpenAPI spec collapse to one (the declared one, with
  the richer schema, wins).

Probing is best-effort: an app that isn't ready yet still yields a record from its
declared contract.

### 4.4 The Library

[`fleet/library.json`](cli/loom/library.py) is fully regenerable from the registry
(`loom reindex` → `library.reindex_from_registry`). `library.search()` is a single
seam — a lexical ranked search today (tokenised over name/description/tags/operation
ids+summaries, with name/tag hits weighted higher) — behind which a vector store can
later swap in with **no change to callers or the record shape**.

CLI front doors: `loom find <query>` (`cmd_find`) and `loom describe <app>`
(`cmd_describe`), both with `--json` for agents. `describe` falls back to harvesting
on the fly if the app isn't in the Library yet.

### 4.5 The MCP / OpenAPI / REST projection

`loom mcp` ([`cli/loom/mcp_server.py`](cli/loom/mcp_server.py), default
`127.0.0.1:7878`) projects the Library as a callable surface for agents, three ways
off the *same* live Library + registry:

- **MCP** (Streamable HTTP / JSON-RPC 2.0) at `POST /mcp`, protocol `2025-06-18`.
- **OpenAPI 3.1** at `GET /openapi.json` (for non-MCP HTTP agents).
- **Plain REST** (`GET /apps`, `/apps/{name}`, `/search`, `POST /invoke`).

It exposes **three stable meta-tools** over the whole fleet (progressive disclosure
— *not* one tool per operation, which would explode an agent's context):

| tool | does |
|------|------|
| `loom_search_apps` | ranked search over the Library |
| `loom_describe_app` | an app's callable operations (method, path, schemas) |
| `loom_invoke` | call an operation on a fleet app |

It also serves `loom://app/{name}` **resources** (`resources/list` + `resources/read`).

`loom_invoke` is the security boundary: it **only proxies registered apps** (never
arbitrary URLs — the SSRF guard) and **refuses `private` apps** (`"… is private and
cannot be invoked through the Library"`), so the tier model holds even for agents.

---

## 5. Shared services: `consumes` / provisioning + the HMAC token model

Loom ships the *mechanism* for app-to-app services (the managed backends are the
commercial product's job). An app declares a service it **provides** or **consumes**;
Loom resolves consumers against deployed providers at deploy time and injects the
wiring. Logic lives in [`cli/loom/services.py`](cli/loom/services.py); the reference
backend is [`examples/loom-wallet`](examples/loom-wallet) (a credit ledger) and the
proof of the chain is [`examples/wallet-consumer`](examples/wallet-consumer).

```text
 ┌─ provides_service: wallet ─────────────┐        ┌─ consumes: [{service: wallet, scope: charge}] ─┐
 │  loom-wallet  (private backend)         │        │  wallet-consumer  (public app)                 │
 │                                         │        │                                                │
 │  env injected by Loom:                  │        │  env injected by Loom (provider resolved):     │
 │   LOOM_SERVICE=wallet                   │        │   LOOM_WALLET_URL=http://loom-wallet:8080      │
 │   LOOM_SERVICE_SECRET=<platform secret> │        │   LOOM_WALLET_TOKEN=HMAC(secret,"consumer:wallet")
 └───────────────▲─────────────────────────┘        └───────────────────────┬────────────────────────┘
                 │  verify caller                                            │  loom_sdk.wallet().charge(...)
                 │  Authorization: Bearer <token>                            │
                 │  X-Loom-App: wallet-consumer                              │
                 └────────────────────  over the `loom` network  ◀──────────┘
```

**Provisioning at deploy.** In `local.deploy`:

```python
env.update(services.provider_env(cfg, manifest))          # provider side
provisioned_env, grants = services.provision_env(cfg, manifest)   # consumer side
env.update(provisioned_env)
```

- **Provider side** (`provider_env`): if the manifest sets `provides_service: <svc>`,
  inject `LOOM_SERVICE=<svc>` and `LOOM_SERVICE_SECRET=<platform secret>`.
- **Consumer side** (`provision_env`): for each `consumes[]` entry, find the deployed
  provider (`find_provider` — the app whose `contract.provides_service` matches) and
  inject:

  ```text
  LOOM_<SVC>_URL    = http://loom-<provider>:<service_port>   # over the loom network
  LOOM_<SVC>_TOKEN  = HMAC-SHA256(secret, "<consumer-app>:<svc>")
  ```

  and record a `grant` (`{service, provider, scope}`) on the registry entry.

**The token model** (`mint_token` / `verify_token`):

```python
mint_token(secret, app, service) = HMAC_SHA256(secret, f"{app}:{service}")  # hex
```

- **`secret`** is a single platform secret (`service_secret`): generated once with
  `secrets.token_hex(32)` and persisted to `fleet/config.json` (gitignored). It signs
  *all* app-to-app tokens.
- The token is an **app identity assertion**, not a bearer capability to a URL: it
  binds the **calling app name** to the **service name**. A provider verifies by
  recomputing the HMAC from the `X-Loom-App` header and comparing in constant time
  (`hmac.compare_digest`) — see `caller()` in
  [`examples/loom-wallet/app.py`](examples/loom-wallet/app.py).

**The SDK** ([`sdk/python/loom_sdk.py`](sdk/python/loom_sdk.py)) reads *exactly* these
env vars — zero config:

```python
from loom_sdk import wallet
wallet().charge("alice", 100, idempotency_key="order-42")
```

It sends `Authorization: Bearer $LOOM_WALLET_TOKEN` + `X-Loom-App: $LOOM_APP`, and
maps provider responses to typed errors: **401 → `Unauthorized`**, **402 →
`InsufficientCredits`**. The wallet backend demonstrates the full contract: 401 on a
bad/missing token, 402 on insufficient balance, and **idempotency** keyed on
`idempotency_key`.

**Resolution is best-effort.** A `consumes` with no deployed provider **warns and
injects nothing** (never blocks deploy) — redeploy once the provider exists, and the
grant resolves. An unknown service name is allowed with a warning (the contract is
extensible; `auth|email|billing|wallet` are the recognised ones today).

---

## 6. Data federation + the grant-checked gateway

Datasets are federated, not shared directly. An app declares `data.provides` /
`data.consumes`; consumers always read **through a federation gateway** that enforces
a **deny-by-default, live-grant** policy. The gateway is an ordinary fleet app that
declares `provides_service: federation` — [`examples/loom-fed`](examples/loom-fed) —
with [`examples/data-provider`](examples/data-provider) +
[`examples/data-consumer`](examples/data-consumer) proving the chain.

```text
 data-consumer                    loom-fed  (federation gateway, private)            data-provider
 consumes: [items]                provides_service: federation                       provides: [items @ /items]
──────────────                  ─────────────────────────────────────             ──────────────────
 GET $LOOM_DATA_ITEMS_URL
   Authorization: Bearer <tok>          (1) verify HMAC token
   X-Loom-Consumer: data-consumer  ───▶     tok == HMAC(secret,"data:<consumer>:items") ?
                                       (2) re-check LIVE grant in /registry.json (bind-mounted :ro)
                                            "items" in registry[consumer].data_grants ?   ─── no ─▶ 403
                                       (3) resolve provider of "items", proxy the read
                                            GET http://loom-data-provider:8100/items  ───────────────▶ {items:[…]}
                                  ◀──────  body + X-Loom-Federated-From: data-provider  ◀──────────────
```

**Consumer provisioning** (`services.provision_data_env`, called from `local.deploy`):
for each `data.consumes` entry, point the consumer at the gateway with a scoped token
and record a grant:

```text
LOOM_DATA_<DS>_URL    = http://loom-loom-fed:8090/fed/<dataset>
LOOM_DATA_<DS>_TOKEN  = HMAC-SHA256(secret, "data:<consumer-app>:<dataset>")
data_grants[]         = {dataset, provider}   # provider may be null until it deploys
```

(A `data.consumes` with no federation gateway deployed warns and injects nothing.)

**The gateway** ([`examples/loom-fed/app.py`](examples/loom-fed/app.py)) serves
`GET /fed/<dataset>` and enforces **three checks** before proxying:

1. **Token** — verify `Authorization: Bearer …` equals
   `HMAC(secret, "data:<X-Loom-Consumer>:<dataset>")` (constant-time). → 401 on
   failure.
2. **Live grant** — Loom **bind-mounts the registry read-only** into the gateway
   (`volumes[registry_file] = "/registry.json:ro"` in `local.deploy`, gated on
   `provides_service == "federation"`). The gateway reads `data_grants` for the
   consumer **on every request**; if `<dataset>` isn't granted → **403**. Because
   it reads the *live* registry, **revoking a grant takes effect immediately** —
   deny-by-default plus prompt revocation, with no gateway restart.
3. **Provider resolution** — find the app whose `data.provides` contains the dataset,
   proxy the read to it over the `loom` network at its declared `path`, and stamp the
   response with `X-Loom-Federated-From: <provider>`.

The provider itself ([`examples/data-provider/app.py`](examples/data-provider/app.py))
never authenticates the consumer — it only sees the gateway and the forwarded
`X-Loom-Consumer` header. **All trust decisions are centralised in the gateway**, and
the consumer needs only the injected `LOOM_DATA_*_URL`/`_TOKEN` — it never learns the
provider's address.

Inspect the fabric with `loom data ls` (datasets and who provides them) and
`loom data grants` (resolved consumer→dataset→provider grants) — `cmd_data` in
[`cli/loom/cli.py`](cli/loom/cli.py).

---

## 7. External exposure: the native relay + the edge

By default Loom is loopback-only. To reach the fleet from outside this machine, Loom
splits the job between a small **native relay** on the host and **your own reverse
proxy** at the edge. Logic: [`cli/loom/gateway.py`](cli/loom/gateway.py), config under
the `gateway` block of [`fleet/config.json`](cli/loom/config.py).

### Why a native relay

Some hosts (notably **OrbStack**) only forward Docker-published ports on **loopback**,
so the LAN can't reach Traefik directly. Loom therefore runs a **native (non-Docker)**
TCP listener — `socat` under a **launchd** agent (so it survives reboots) — that
forwards an external port to Traefik on loopback:

```text
relay_port (:8444)  ──socat (launchd)──▶  127.0.0.1:<https_port> (Traefik)
```

`loom gateway up` (`gateway.ensure` → `relay_up`) loads this agent;
`loom gateway status` reports it.

### The edge route

Your own reverse proxy (Traefik, nginx, Caddy, …) at the network edge terminates
public TLS and forwards `*.<your-domain>` to `<this-machine-LAN-IP>:<relay_port>`.
Loom **generates** that edge config for you — it does not run your edge.

```text
  Internet                  YOUR edge proxy                 this machine
─────────────            ────────────────────            ─────────────────────────────
https://app.example.com
       │  public DNS  *.example.com → edge
       ▼
   edge :443  ── match HostRegexp(*.example.com) ──▶  relay  https://<LAN-IP>:8444
   (Let's Encrypt, insecureSkipVerify upstream)               │  (insecure: local mkcert cert)
                                                               ▼
                                                          Traefik :8443  ──▶  loom-app:port
```

`loom gateway sync` (`gateway.sync`) is the **one-command** path:

1. **Auto-detect the LAN IP** (`gateway.lan_ip`) so a DHCP change doesn't silently
   break the public path.
2. **Regenerate** [`proxy/gateway/edge-loom.yml`](cli/loom/gateway.py) — a wildcard
   `HostRegexp(^[a-z0-9-]+\.<domain>$)` router → `https://<LAN-IP>:<relay_port>`
   (with `insecureSkipVerify` because the upstream presents the local mkcert cert),
   plus `passHostHeader: true` so the Host survives to Loom's Traefik for routing.
3. **Push it** to the edge and reload.

The push uses an `scp` + `pct push` recipe that is a **Proxmox LXC example**
(`edge_host` + `edge_vmid`). The **generic** contract is simply: *deploy
`proxy/gateway/edge-loom.yml` to your reverse proxy's dynamic config directory.* If
`edge_host`/`edge_vmid` aren't set, `sync` generates the files locally and tells you
to push them yourself. `loom gateway edge-config` prints the files to drop on the edge.

### Gating at the edge (the `gated` tier)

The Loom Traefik container can't reach the SSO server's LAN IP, so **gated apps are
gated at the edge**, not in Loom. Loom generates
[`proxy/gateway/edge-loom-gated.yml`](cli/loom/gateway.py) (`write_edge_gated`,
re-synced on every deploy/remove via `_sync_edge_gated`): a **higher-priority**
(`priority: 1000`) per-gated-app router carrying a `forwardAuth` middleware pointing
at your auth server (e.g. Authelia: `…/api/verify?rd=<login-url>`, forwarding
`Remote-User`/`Remote-Groups`/`Remote-Name`/`Remote-Email`). Deploy that file to the
edge alongside `edge-loom.yml`. On the LAN/tailnet the app is reachable without SSO;
only the edge enforces auth. Enabling it requires `gateway.auth_upstream` +
`gateway.auth_rd`; deploying a `gated` app without them is a hard error.

### Tailnet (the `private` tier, optionally)

For `private` apps, Loom can publish a **stable tailnet-only URL** via
`tailscale serve` (`tailnet_serve`) — each app gets a deterministic tailnet port
(`next_tailnet_port`, carried across redeploys) — without exposing the app publicly.
Enabled by `gateway.tailnet_host` when the `tailscale` CLI is present.

---

## 8. Access tiers, end to end

The four-field v1 `access` is the one knob that ties §2, §3, and §7 together. Tier is
enforced **structurally** — a tier that shouldn't be public simply never gets a route
file or a public hostname.

| tier | route file? | local URL | external | invokable via MCP? | gating |
|------|-------------|-----------|----------|--------------------|--------|
| `public` | yes | `https://<n>.loom.localhost:8443` | `<n>.<public_domain>` via relay→edge | yes | none |
| `gated`  | yes (same as public) | same | `<n>.<public_domain>` | yes | **forwardAuth at the edge**; bypassed on LAN/tailnet |
| `private`| **no** | `http://127.0.0.1:<port>` | tailnet URL only (optional) | **refused** | n/a — not routed |

So `loom-wallet` and `loom-fed` (both `private` backends) are reachable **only over
the `loom` network** by their consumers, never publicly and never through
`loom_invoke` — exactly what a shared backend service wants.

---

## 9. The target-adapter seam

The CLI is **target-agnostic**. It loads a manifest, hands it to a `Target`, records
the returned entry, and operates all lifecycle verbs against that entry. The contract
is the small abstract base in [`cli/loom/targets/base.py`](cli/loom/targets/base.py):

```python
class Target(ABC):
    name: str = "base"
    def deploy(self, cfg, app_dir, manifest) -> dict: ...   # build+run → registry entry
    def start(self, cfg, entry) -> None: ...
    def stop(self, cfg, entry) -> None: ...
    def remove(self, cfg, entry) -> None: ...
    def logs(self, cfg, entry, follow, tail) -> int: ...
    def reconcile(self, cfg, entries) -> dict: ...          # {app_name: live_status}
    def probe_health(self, cfg, entry) -> str: ...          # concrete; defaults "unknown"
```

`get_target(name)` ([`cli/loom/targets/__init__.py`](cli/loom/targets)) resolves the
name from `default_target` in `fleet/config.json`. **Only `local`**
([`cli/loom/targets/local.py`](cli/loom/targets/local.py), Docker) is implemented in
the open core.

What makes this a clean seam:

- The **registry entry is the contract** between the CLI and a target. As long as a
  target returns an entry with `name`/`url`/`status`/`contract` (+ whatever it needs
  to manage the app), every downstream stage — registry, harvester, Library, MCP,
  services, federation — works unchanged. Each entry records its own `target`, and the
  CLI dispatches per-entry (`get_target(e.get("target", "local"))`), so **mixed-target
  fleets** are already supported.
- `reconcile` returns live status without the CLI knowing how (Docker container state
  for `local`); `probe_health` is concrete and defaults to `"unknown"` so a target
  opts into health probing and **never blocks** on a failed probe.

A cloud / Coolify / Kubernetes target drops in here **without touching the CLI** —
this is the documented extension point of the open core (a managed cloud target is
reserved for the commercial product).

---

## 10. State files & reference map

All state is plain files under `fleet/` and `proxy/` (both gitignored where they hold
machine-specific data). Nothing is hidden in a database.

| path | what | written by |
|------|------|-----------|
| `fleet/config.json` | base domain, ports, network, target, `gateway` block, `service_secret` | [`config.py`](cli/loom/config.py) / [`services.py`](cli/loom/services.py) |
| `fleet/registry.json` | every deployed app: operational fields + `contract` block + grants | [`registry.py`](cli/loom/registry.py) |
| `fleet/library.json` | the LLM-addressable index (regenerable from the registry) | [`library.py`](cli/loom/library.py) |
| `proxy/docker-compose.yml` | the shared Traefik proxy (flag-configured, file provider) | committed |
| `proxy/dynamic/app-<name>.yml` | one route file per routed app | [`proxy.py`](cli/loom/proxy.py) |
| `proxy/dynamic/tls.yml` | local TLS config (mkcert or self-signed fallback) | [`proxy.py`](cli/loom/proxy.py) |
| `proxy/certs/` | mkcert wildcard cert + key | `mkcert` via [`proxy.py`](cli/loom/proxy.py) |
| `proxy/gateway/edge-loom.yml` | wildcard edge route → relay (deploy to your edge) | [`gateway.py`](cli/loom/gateway.py) |
| `proxy/gateway/edge-loom-gated.yml` | per-gated-app forwardAuth routers for the edge | [`gateway.py`](cli/loom/gateway.py) |

### Code map

| concern | file |
|---------|------|
| CLI commands & arg parsing | [`cli/loom/cli.py`](cli/loom/cli.py) |
| manifest v1 load/validate | [`cli/loom/manifest.py`](cli/loom/manifest.py) |
| contract v2 parse + snapshot | [`cli/loom/contract.py`](cli/loom/contract.py) |
| reverse proxy + route files + TLS | [`cli/loom/proxy.py`](cli/loom/proxy.py) |
| generated Dockerfiles | [`cli/loom/dockerfiles.py`](cli/loom/dockerfiles.py) |
| registry (state) | [`cli/loom/registry.py`](cli/loom/registry.py) |
| harvester (contract → record) | [`cli/loom/harvester.py`](cli/loom/harvester.py) |
| Library + search | [`cli/loom/library.py`](cli/loom/library.py) |
| MCP / OpenAPI / REST server | [`cli/loom/mcp_server.py`](cli/loom/mcp_server.py) |
| shared services + HMAC + data provisioning | [`cli/loom/services.py`](cli/loom/services.py) |
| external exposure (relay/auth/tailnet/edge) | [`cli/loom/gateway.py`](cli/loom/gateway.py) |
| target interface | [`cli/loom/targets/base.py`](cli/loom/targets/base.py) |
| local Docker target | [`cli/loom/targets/local.py`](cli/loom/targets/local.py) |
| Python SDK (consumes) | [`sdk/python/loom_sdk.py`](sdk/python/loom_sdk.py) |
| contract reference | [`docs/app-contract.md`](docs/app-contract.md) |
| worked examples | [`examples/`](examples) (`loom-wallet`, `wallet-consumer`, `loom-fed`, `data-provider`, `data-consumer`, `hello-*`, `capability-demo`) |

---

**In one breath:** the CLI builds each app into a container on a shared `loom`
network, writes a Traefik file-provider route so it is instantly reachable at
`https://<name>.loom.localhost` over locally-trusted TLS, snapshots its contract into
the registry, harvests that into the searchable Library, and projects the Library over
MCP/OpenAPI/REST so agents can discover *and* call it — while resolving the app's
declared `consumes`/`data` against the live fleet and injecting URLs + HMAC tokens so
apps reach shared services and grant-checked datasets with zero per-app plumbing, and
a native relay + your own edge proxy carry the chosen tiers beyond the machine. New
deploy targets plug in behind one small `Target` interface.
