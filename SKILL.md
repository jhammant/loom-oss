---
name: deploy-to-loom
description: Deploy, manage, and interconnect apps on Loom — the agent-native, additive app-hosting platform. Use this whenever you need to ship a small web app, API, static site, or service so it is instantly routed at its own URL, health-checked, and discoverable + callable by other agents. Covers the one-command deploy workflow, the `fleet.app.yaml` manifest (v1 + v2 capabilities/consumes/data), the full `loom` command reference, access tiers (public/gated/private), local DNS/TLS, making an app discoverable, finding and invoking other fleet apps via the Library and MCP, shared services, and failure handling. Supersedes any prior "deploy" skill.
---

# Deploying to Loom

Loom is an **additive** app-hosting platform: you deploy a small app with one command and it is
instantly routed at its own URL, TLS-terminated, health-checked, indexed into an LLM-addressable
**Library**, and able to consume **shared services** — with **no per-app plumbing**. You write one
manifest (`fleet.app.yaml`) and run `loom deploy <dir>`. Loom builds the image, runs the container
behind a shared reverse proxy, writes the route, wires any services the app declares, and harvests
its contract into the Library so other agents can find and call it.

This skill lets you (an agent) drive Loom end to end. Read it top to bottom before deploying; the
manifest and tier sections are the parts that most affect correctness.

---

## 1. The deploy workflow (happy path)

```bash
# 1. (once) install the CLI editable
pipx install -e ./cli            # provides the `loom` command

# 2. (once per machine) bring up the shared reverse proxy
loom proxy up                    # Traefik on the `loom` Docker network; generates local TLS

# 3. write fleet.app.yaml in your app dir (see §2), then deploy
loom deploy ./path/to/app        # builds image, runs container, writes route, indexes Library

# 4. verify
loom list                        # NAME STATUS HEALTH URL EXTERNAL RUNTIME ACCESS
loom health <name>               # probe /health for one app (or all)
loom logs <name> -f              # follow logs if something's wrong
```

A successful `loom deploy` prints the live URL, e.g.
`hello-web is live → https://hello-web.loom.localhost:8443`, plus any capabilities, public/tailnet
URLs, and a note if it is gated. **Redeploys replace in place** — run `loom deploy` again after any
code or manifest change; operational state (`harvested_at`, `health_status`) is preserved.

Requirements: a running Docker/OrbStack daemon. `loom proxy up` fails fast with a clear message if
the daemon is unreachable. Local TLS uses **mkcert** if installed (trusted certs); without it,
Traefik serves a self-signed cert and browsers warn — functionally fine, agents ignore it.

---

## 2. The manifest: `fleet.app.yaml`

Every app declares itself in one `fleet.app.yaml` at the app-directory root. The contract is
**versioned and additive**: the four v1 fields are all you need, and **every v2 field is optional and
defaulted** — a v1 manifest deploys byte-identically. The manifest is the machine-readable surface
the harvester reads to build the Library and that shared services + data federation key off.

### v1 — required

```yaml
name: hello-web      # subdomain; DNS-safe: lowercase letters, digits, hyphens; no leading/trailing hyphen; <=63 chars
runtime: node        # node | python | static | docker
port: 3000           # the port the app binds; the app MUST read $PORT. Omit for static (served by nginx on 80)
access: public       # public | gated | private
```

- `runtime: node` → generated Dockerfile (`node:22-alpine`, `npm ci`/`npm install`, `npm start`).
  Provide a `package.json` with a `start` script.
- `runtime: python` → generated Dockerfile (`python:3.12-slim`, installs `requirements.txt` if
  present, runs `python app.py`). Your entrypoint must be `app.py` and bind `$PORT`.
- `runtime: static` → `nginx:alpine` serving the directory on port 80. `port` is optional.
- `runtime: docker` → **uses the app's own `Dockerfile`** (must exist in the dir). Use this for
  anything the generated images don't cover. The container must listen on `$PORT`.

**Always read `$PORT`** in non-static apps — Loom injects it and routes to it. Hardcoding a port
that differs from `port:` will break routing/health.

### v2 — optional, defaulted (capabilities, consumes, data)

```yaml
manifest_version: 2

metadata:
  description: One line describing what the app does.   # default ""  — feeds Library search
  tags: [search, demo]                                  # default []  — lowercased, deduped
  owner: loom                                           # default ""

health:
  path: /health        # default "/health"; must start with "/". Injected as $LOOM_HEALTH_PATH

capabilities:          # default [] — declared, discoverable, callable handles
  - id: search                 # DNS-safe, unique within the app (required)
    kind: http                 # http | openapi | mcp (required)
    path: /search              # required for EVERY kind
    description: Search the corpus.
    input_schema:  { type: object, properties: { q: { type: string } }, required: [q] }
    output_schema: { type: object }

consumes:              # default [] — shared services this app needs (see §6)
  - service: wallet            # auth | email | billing | wallet (others warn but are allowed)
    scope: charge

data:                  # default {provides: [], consumes: []} — data federation (see §7)
  provides:
    - name: orders
      api: rest                # rest | graphql | event
      path: /api/orders
  consumes:
    - name: customers
      api: rest

provides_service: wallet   # top-level: this app is the BACKEND for a named service (see §6)
```

**Capabilities**: `kind` tells the harvester how the capability is reached — `http` (a plain
endpoint), `openapi` (an OpenAPI 3.x spec at `path` that Loom expands into individual operations),
or `mcp` (an MCP endpoint). Declared `http` capabilities and the same op discovered from an OpenAPI
spec collapse to one (the declared one wins; it carries the richer schema). `input_schema`/
`output_schema` are JSON Schema and feed the future MCP/OpenAPI tool surface.

**`semantics:`** (on a capability or dataset) is **accepted and stored but not yet acted on** —
forward-compat for taxilang/Orbital semantic types. Safe to include today.

**Versioning**: `manifest_version` defaults to 1. A version newer than the CLI supports is accepted
with a warning (best-effort) — a newer manifest never hard-fails an older CLI. Unknown top-level
keys are ignored (forward-compatible).

On deploy Loom validates the manifest (clear `fleet.app.yaml: …` errors), injects
`LOOM_APP`, `LOOM_HEALTH_PATH`, and `LOOM_CAPABILITIES` (comma-separated ids) into the container,
and records a `contract` block on the registry entry.

---

## 3. Command reference

| Command | What it does |
| --- | --- |
| `loom deploy <dir>` | Build + deploy the app in `<dir>` (reads its `fleet.app.yaml`). Redeploy = replace in place. |
| `loom list` (`loom ls`) | List apps: `NAME STATUS HEALTH URL EXTERNAL RUNTIME ACCESS`. Reconciles live container state. |
| `loom health [app]` | Probe `health.path` for one app, or all. Refreshes stored health. |
| `loom logs <app> [-f] [--tail N]` | Show (or follow) an app's logs. Default tail 200. |
| `loom stop <app>` | Stop the running container (keeps the app in the fleet). |
| `loom start <app>` | Start a stopped app. |
| `loom remove <app>` (`loom rm`) | Remove the app: container, route, image, Library + registry entry. |
| `loom find <query> [--json] [--limit N]` | Search the Library by name/description/tags/capabilities. `--json` for agents. |
| `loom describe <app> [--json]` | Show an app's callable operations (method, path, schemas) + URLs. |
| `loom reindex` | Rebuild the Library from the registry (after manual edits / drift). |
| `loom mcp [--host H] [--port P]` | Serve the Library as MCP + OpenAPI + REST (default `127.0.0.1:7878`). See §5. |
| `loom data ls` | List datasets the fleet provides and who provides them. |
| `loom data grants` | Show resolved data-federation grants (consumer → dataset from provider). |
| `loom proxy up\|down\|status` | Manage the shared Traefik reverse proxy (network, TLS, route table). |
| `loom gateway up\|down\|status\|sync\|edge-config` | Manage external exposure (native relay, gated-tier auth, tailnet). See §8. |

`loom <command> -h` prints usage for any command.

---

## 4. Access tiers, DNS & TLS

`access:` controls reachability. Pick deliberately — it is the security boundary.

| Tier | Reachable | URL shape | Notes |
| --- | --- | --- | --- |
| `public` | anyone (local + public domain if configured) | `https://<name>.<base-domain>` | Open. Routed by the shared proxy. |
| `gated` | behind SSO (forward-auth) at the edge; bypassed on LAN/tailnet | same as public | Needs gateway auth configured (see §8). SSO is applied at the **edge**, not by Loom's container. |
| `private` | this machine only (+ optional tailnet URL) | `http://127.0.0.1:<random-port>` | **Not routed publicly.** Used for backend services. **Cannot be invoked through the Library/MCP.** |

**DNS**: `*.localhost` resolves to `127.0.0.1` automatically on modern OSes — **no `/etc/hosts` or DNS
setup**. The default base domain is `loom.localhost`. So `hello-web` is reachable at
`https://hello-web.loom.localhost:<https_port>`.

**TLS/ports**: the proxy terminates HTTPS and redirects HTTP→HTTPS. The default HTTPS port is
**8443** (443 is ideal but some local stacks, e.g. OrbStack's domain proxy, reserve it; 8443 needs no
system changes). Always include the port in local URLs unless it's 443. mkcert gives trusted local
certs; otherwise expect a self-signed warning.

Choose **private** for anything other apps call but the public shouldn't (databases, ledgers,
gateways). Choose **gated** for human-facing apps that need SSO. Choose **public** for open
endpoints and demos.

---

## 5. Making your app discoverable — and finding/invoking others

### Make yours discoverable
Just deploy it. On every `loom deploy`, Loom **harvests** the app's contract (and live-probes
OpenAPI capabilities) into `fleet/library.json` — the LLM-addressable Library. To rank well in
search, give it a `metadata.description`, useful `tags`, and declared `capabilities`. That's the
whole job: no separate registration step.

### Find and describe others
```bash
loom find "search"          --json     # search name/description/tags/capabilities
loom describe capability-demo --json    # operations as callable handles (method, path, schemas)
```
`--json` is the agent-facing form. `describe` lists each operation's `method`, `path`, `id`, and
schemas — everything you need to construct a call.

### Invoke others (the agent path): `loom mcp`
Run `loom mcp` to project the whole fleet as a small, stable tool surface for agents — **progressive
disclosure**: a few meta-tools over the fleet, not one tool per operation (which explodes context).
It serves three faces on `http://127.0.0.1:7878`:

- **MCP** (Streamable HTTP / JSON-RPC 2.0) at `POST /mcp` — tools:
  - `loom_search_apps {query, limit?}` → matching apps (compact).
  - `loom_describe_app {name}` → the app's operations + schemas.
  - `loom_invoke {app, path, method?, body?}` → calls an operation on a fleet app and returns
    `{app, path, status, body}`.
  - Resources: `loom://app/{name}` (read an app's full Library record).
- **OpenAPI 3.1** at `GET /openapi.json` — for non-MCP HTTP agents.
- **REST**: `GET /apps`, `GET /apps/{name}`, `GET /search?q=`, `POST /invoke`.

`loom_invoke` **only proxies REGISTERED apps** (never arbitrary URLs) and **refuses PRIVATE apps** —
that's the SSRF / tier-leak guard. If you need to call a private backend, do it from another deployed
app over the `loom` network (see §6), not through the Library.

Example invoke over REST:
```bash
curl -s localhost:7878/invoke -d '{"app":"capability-demo","path":"/search?q=loom"}'
```

---

## 6. Shared services (`consumes:` / `provides_service:`)

An app declares a service it needs; Loom resolves it against a deployed **provider** and injects
credentials — **zero plumbing in the app**.

**Provider** (the backend) sets `provides_service: <name>` and is typically `access: private`. Loom
injects `LOOM_SERVICE=<name>` and `LOOM_SERVICE_SECRET` so it can verify callers.

**Consumer** lists the service under `consumes:`. For each resolved service Loom injects into the
consumer container:
- `LOOM_<SERVICE>_URL` — the provider's in-network address (`http://loom-<provider>:<port>`).
- `LOOM_<SERVICE>_TOKEN` — an HMAC the provider verifies (app-to-app identity), signed over
  `"<consumer>:<service>"`.

The app reads exactly those env vars. The **Python SDK** (`sdk/python/loom_sdk.py`) wraps this — vendor
the file into the app:
```python
import loom_sdk
loom_sdk.wallet().charge("alice", 100, idempotency_key="order-42")  # 401→Unauthorized, 402→InsufficientCredits
reply = loom_sdk.llm().chat("Summarise this in one line: ...", model="fast")  # no API key in your app
```
Known services: `auth | email | billing | wallet | llm`. Others are allowed but warn (and stay
unresolved). Reference backends: `examples/loom-wallet` (a credit ledger doing 401/402/idempotency)
and `examples/loom-llm` (the LLM gateway below); `examples/wallet-consumer` and
`examples/llm-consumer` prove the chains end to end.

### No-key LLM (`consumes: [llm]`)
Declare `consumes: [llm]` and call `loom_sdk.llm().chat(prompt, model="fast"|"smart"|"frontier")` —
**your app never holds a provider key**. The gateway (`examples/loom-llm`) HMAC-verifies the caller,
maps the model alias, meters tokens against a **per-app cap**, and proxies to Anthropic. It runs in
**stub mode** until a key is present, so the wiring works before you add one. To go live, the
*operator* drops the key in `fleet/secrets.json` (gitignored) — the gateway declares
`secrets: [ANTHROPIC_API_KEY]` and Loom injects it host-side. Apps stay key-free.

### Caller identity (`identity()`)
For **gated** apps, `loom_sdk.identity(headers)` returns the SSO-authenticated caller
(`who.user`, `who.email`, `who.groups`, `who.is_authenticated`) from the edge's forward-auth headers —
zero config. Public requests resolve to an empty, unauthenticated identity.

**Resolution is best-effort and order-independent**: if you deploy a consumer before its provider,
Loom warns (`consumes 'wallet' … no provider … redeploy after deploying the provider`) and injects
nothing — it never blocks the deploy. **Deploy the provider, then redeploy the consumer.**

---

## 7. Data federation (`data:`)

Apps can share datasets through a **grant-checked gateway** with deny-by-default semantics.

- A provider sets `data.provides: [{name, api, path}]` (usually `access: private`).
- A consumer sets `data.consumes: [{name}]`. For each, Loom injects
  `LOOM_DATA_<NAME>_URL` (pointing at the federation gateway's `/fed/<name>`) and a scoped
  `LOOM_DATA_<NAME>_TOKEN`.
- A **federation gateway** app (`provides_service: federation`) must be deployed; Loom mounts the
  live registry into it and it re-checks the grant on every request. Without it, data-consumes warn
  and stay unresolved (deploy never blocks).

Inspect the fabric:
```bash
loom data ls       # datasets and their providers
loom data grants   # consumer → dataset from provider (resolved grants)
```
Reference: `examples/loom-fed` (gateway), `examples/data-provider`, `examples/data-consumer`.

---

## 8. External exposure (`loom gateway`)

By default Loom is local-only. To reach apps from beyond the machine you front Loom with **your own
reverse proxy** (the "edge") and Loom provides a **native relay** the edge forwards to (a non-Docker
listener — some hosts like OrbStack only forward Docker-published ports on loopback).

- Set `public_domain` (and, for `gated`, `gateway.auth_upstream` + `auth_rd`) in `fleet/config.json`.
- `loom gateway up` — start the native relay (+ auth bridge if configured).
- `loom gateway sync` — auto-detect this machine's LAN IP, regenerate `proxy/gateway/edge-loom.yml`
  (and the gated router file), and push them to the edge. **Re-run after a DHCP/IP change** or it
  silently breaks the public path. The push uses a Proxmox `pct` step as an **example**; the generic
  path is "deploy `proxy/gateway/edge-loom.yml` to your reverse proxy."
- `loom gateway edge-config` — print the edge dynamic-config files to drop on your proxy.
- `loom gateway status` — relay / public-domain / gated / tailnet state.

`gated` apps are gated **at the edge** (Loom emits a higher-priority forward-auth router for them);
deploying a gated app reminds you to push `proxy/gateway/edge-loom-gated.yml`. For `private` apps,
if `gateway.tailnet_host` is set and `tailscale` is installed, Loom can serve a stable tailnet-only
URL per app.

Use generic placeholders in anything you emit (`example.com`, `<your-domain>`) — never bake in real
IPs/hostnames.

---

## 9. Failure handling & troubleshooting

- **Manifest errors** surface as `fleet.app.yaml: …` and **fail the deploy** — fix the manifest and
  redeploy. Common: non-DNS-safe `name`; missing `port` on a non-static runtime; a capability
  missing `path`; duplicate capability `id`; bad `runtime`/`access` value.
- **`runtime: docker` but no Dockerfile** → deploy errors; add a `Dockerfile` or switch runtime.
- **`access: gated` without gateway auth** → deploy errors asking for `gateway.auth_upstream` +
  `auth_rd`. Configure them or use `public`/`private`.
- **Docker daemon unreachable** → `loom proxy up`/deploy fail fast: start Docker/OrbStack.
- **App shows `unknown`/`unready` health** → `loom health <app>` then `loom logs <app> -f`. Health is
  best-effort: a 2xx/3xx (or even 404, meaning "up but no `/health` route") counts as `ok`; a
  connection failure is `unknown` and never blocks. Confirm the app binds `$PORT` and that
  `health.path` exists.
- **`consumes`/`data.consumes` warns "no provider/gateway"** → expected when the backend isn't
  deployed yet. Deploy the provider (or the `federation` gateway), then **redeploy the consumer** so
  the env wiring is injected.
- **`loom_invoke` refuses an app** → it's `private` (by design) or unregistered. Call private
  backends from another deployed app over the `loom` network, not via the Library.
- **Library looks stale / app missing from `find`** → `loom reindex` to rebuild from the registry.
- **STATUS `missing`/`dead` in `loom list`** → the container is gone or crashed; check `loom logs`,
  then `loom start <app>` or `loom deploy <dir>` to recreate.
- **Public URL stopped working after a network change** → re-run `loom gateway sync` (LAN IP drift).

When in doubt: `loom list` for the fleet snapshot, `loom logs <app> -f` for the failing app,
`loom describe <app>` to confirm the operations you're calling.
