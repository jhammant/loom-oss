# The Loom App Contract (`fleet.app.yaml`)

Every app declares itself in one `fleet.app.yaml`. The contract is **versioned and
additive**: the four v1 fields are all you need, and every v2 field is optional and
defaulted — a v1 manifest deploys byte-identically. The contract is the
machine-readable surface the **harvester** reads to build the LLM-addressable
**Library**, and that **shared services** and **data federation** key off later.

## v1 (required, unchanged)

```yaml
name: hello-web      # subdomain; DNS-safe (lowercase, digits, hyphens)
runtime: node        # node | python | static | docker
port: 3000           # the port the app binds; the app reads $PORT (omit for static)
access: public       # public | gated | private
```

- **public** — routed, reachable by anyone.
- **gated** — routed, behind SSO (Authelia) at the edge; bypassed on LAN/tailnet.
- **private** — not routed publicly; local + an optional tailnet-only URL.

## v2 (optional, defaulted)

```yaml
manifest_version: 2

metadata:
  description: One line describing what the app does.   # default ""
  tags: [search, demo]                                  # default []
  owner: loom                                           # default ""

health:
  path: /health        # default "/health"; must start with "/"

capabilities:          # default []
  - id: search                 # DNS-safe, unique within the app (required)
    kind: http                 # http | openapi | mcp (required)
    path: /search              # required for every kind
    description: Search the corpus.
    input_schema:  { type: object, properties: { q: { type: string } }, required: [q] }
    output_schema: { type: object }
    # semantics: <reserved for taxilang/Orbital semantic types — accepted, not yet used>

consumes:              # default []  (shared services the app wants)
  - service: wallet            # auth | email | billing | wallet | llm (others warn, still allowed)
    scope: charge

provides_service: ""   # default ""; the service name this app BACKS (e.g. wallet, llm)

secrets: []            # default []; env-var names injected from fleet/secrets.json
  # - ANTHROPIC_API_KEY        #   (gitignored host-side key store; missing ones warn + skip)

data:                  # default {provides: [], consumes: []}
  provides:
    - name: orders
      api: rest                # rest | graphql | event
      path: /api/orders
      # semantics: <reserved for taxilang>
  consumes:
    - name: customers
      api: rest
```

### Capabilities
The `kind` tells the harvester how the capability is reached: `http` (a plain
endpoint), `openapi` (an OpenAPI 3.x spec the harvester can expand into
operations), or `mcp` (an MCP endpoint). Every kind needs a `path`. `input_schema`
/`output_schema` are JSON Schema and feed the future MCP/OpenAPI tool surface.

### Shared services: `provides_service` and `secrets`
`provides_service` marks an app as a **backend** for a named service; apps that
`consumes:` that service are wired to it automatically (Loom injects
`LOOM_<SVC>_URL` + an HMAC `LOOM_<SVC>_TOKEN`, and the provider gets
`LOOM_SERVICE` + `LOOM_SERVICE_SECRET` to verify callers). `secrets` lists
env-var names to inject at deploy from **`fleet/secrets.json`** (gitignored, so
keys live on the host — never in the image or repo); a declared-but-missing
secret warns and is skipped rather than blocking the deploy. See
[`examples/loom-llm`](../examples/loom-llm) — a bring-your-own-key LLM backend
that declares `provides_service: llm` and `secrets: [ANTHROPIC_API_KEY]`.

### Reserved: `semantics`
`semantics` on a capability or dataset is **accepted and stored but not yet acted
on** — forward-compat for taxilang/Orbital semantic types, so apps can declare
*meaning* today. The data-federation engine that uses it arrives in a later
milestone.

## What Loom does with it
On deploy, Loom:
- validates the manifest (clear `fleet.app.yaml: …` errors),
- injects `LOOM_HEALTH_PATH` and `LOOM_CAPABILITIES` into the container,
- records a `contract` block on the registry entry (kept separate from
  operational fields), preserving `harvested_at`/`health_status` across redeploys.

## Versioning
`manifest_version` defaults to 1. A version newer than the CLI supports is
accepted with a warning (best-effort), so a newer manifest never hard-fails an
older CLI. Unknown top-level keys are ignored (forward-compatible).
