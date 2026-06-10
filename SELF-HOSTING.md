# Self-Hosting Loom

This guide covers running Loom on your own infrastructure — from a laptop with
nothing but Docker, all the way to a public deployment behind your own reverse
proxy with TLS.

Loom is intentionally **bring-your-own-edge**. The open core gives you the local
substrate (a shared Traefik reverse proxy + one container per app), a native
relay that punches Loom out beyond the local machine, and a generator that emits
ready-to-deploy config for *your* public reverse proxy. It does **not** ship a
managed cloud, a hosted SSO, or a domain provisioner — you wire Loom into the
edge you already run (or the one this guide helps you stand up).

There are three deployment shapes, each a strict superset of the previous one:

| Shape | What you need | What you get |
|-------|---------------|--------------|
| **Local-only** | Docker | Every app at `https://<name>.loom.localhost:8443`, health-checked, discoverable, agent-callable. |
| **Public** | Local-only **+** your own reverse proxy with wildcard TLS | Public apps at `https://<name>.<your-domain>`. |
| **Gated / Private** | Public **+** forward-auth (SSO) and/or Tailscale | The `gated` and `private` access tiers. |

No private IPs, hostnames, or domains appear anywhere in this guide — substitute
your own values for `example.com` / `<your-domain>` and friends.

---

## 1. Prerequisites

- **Docker** (or a Docker-compatible engine such as OrbStack). The reverse proxy
  and every app run as containers.
- **Python 3.9+** and [`pipx`](https://pipx.pypa.io/) to install the CLI.
- **[`mkcert`](https://github.com/FiloSottile/mkcert)** (optional, recommended for
  local use) — issues a locally-trusted wildcard cert so `https://*.loom.localhost`
  has no browser warning. Without it, Traefik falls back to a self-signed cert.
- For going public you will additionally need a **reverse proxy you control**
  (Traefik, Caddy, or nginx) and a **wildcard DNS record** + **wildcard TLS** for
  `*.<your-domain>`.

---

## 2. Install the CLI

From the repo root:

```bash
pipx install -e ./cli
```

This installs the `loom` console script. Verify:

```bash
loom --help
```

Loom locates its working tree (which holds `proxy/`, `fleet/`, and `examples/`)
from the install path. To run the CLI against a tree somewhere else, set
`LOOM_HOME`:

```bash
export LOOM_HOME=/path/to/loom
```

---

## 3. Local-only use (just Docker)

This is the zero-config path. Bring up the shared reverse proxy and deploy an app:

```bash
loom proxy up
loom deploy examples/hello-web
```

`loom proxy up` creates the `loom` Docker network, generates a local TLS cert for
`*.loom.localhost` (via mkcert if present), and starts the shared Traefik
container. `loom deploy` builds the app into an image, runs it as a container on
the `loom` network, and writes a per-app route file that Traefik picks up live.

The app is now reachable:

```bash
loom list
```

```text
NAME       STATUS   HEALTH  URL                                  EXTERNAL  RUNTIME  ACCESS
hello-web  running  ok      https://hello-web.loom.localhost:8443  -        node     public
```

`*.localhost` resolves to `127.0.0.1` with no DNS setup. The proxy is bound to
**loopback only** (`127.0.0.1`), so nothing is exposed off this machine yet —
that is what Section 4 adds.

Useful commands:

```bash
loom list                 # apps, status, health, URLs
loom logs <app> -f        # follow an app's logs
loom health               # re-probe app health
loom stop|start|remove <app>
loom find <query>         # search the Library (add --json for agents)
loom describe <app>       # an app's capabilities as callable handles
loom mcp                  # serve the Library as MCP + OpenAPI for agents
loom proxy status         # proxy state + dashboard URL (http://127.0.0.1:8080/dashboard/)
```

### How local routing & TLS work

- Routes are **not** discovered from Docker labels. Loom writes one Traefik
  dynamic-config file per app into `proxy/dynamic/`, and Traefik's file provider
  watches that directory. Loom owns the route table.
- HTTPS listens on `https_port` (default **8443**, since some local stacks reserve
  443). HTTP on `http_port` (80) redirects to HTTPS.
- Local TLS material lives in `proxy/certs/` and is git-ignored. If `mkcert` is
  installed, Loom generates and trusts a `*.loom.localhost` wildcard cert; if not,
  Traefik serves its built-in self-signed cert (browsers warn — fine for local
  dev).

---

## 4. Going public: your own reverse proxy + TLS

Loom keeps its proxy on loopback by design. To serve apps on the public internet
you put **your own reverse proxy** in front and point it at the **Loom relay** —
a small native (non-Docker) TCP listener on the Loom host. The relay exists
because some container engines (e.g. OrbStack) only forward Docker-published ports
on loopback; the relay forwards off-loopback traffic into Loom's HTTPS port.

```text
                  *.example.com  (wildcard DNS → your edge)
                          │
              ┌───────────▼────────────┐
              │  YOUR reverse proxy     │   terminates TLS for *.example.com
              │  (Traefik / Caddy /     │   (Let's Encrypt, etc.)
              │   nginx) — the "edge"   │
              └───────────┬────────────┘
                          │  forwards to https://<loom-host-LAN-IP>:<relay_port>
              ┌───────────▼────────────┐
              │  Loom relay (native)    │   :8444  →  Loom Traefik :8443 (loopback)
              └───────────┬────────────┘
                          │  routes <name>.example.com → app container
              ┌───────────▼────────────┐
              │  Loom Traefik + apps    │
              └─────────────────────────┘
```

### Step 4.1 — Set the public domain

Edit `fleet/config.json` (created on first run; see Section 7 for every key) and
set `public_domain`:

```json
{
  "public_domain": "example.com"
}
```

With a public domain set, every **public** (and **gated**) app gets a second
Traefik router for `<name>.<public_domain>` in addition to its `*.loom.localhost`
router. Private apps are never given a public router.

### Step 4.2 — Bring up the relay

```bash
loom gateway up
```

This starts the native relay listening on `gateway.relay_port` (default **8444**)
and forwarding to Loom's HTTPS port. It is supervised **cross-platform** and
survives restarts: a **launchd** agent on macOS, a **systemd `--user`** unit on
Linux, or a plain background process if neither is available (no reboot
persistence in that case — on Linux, `loginctl enable-linger $USER` makes the
systemd unit persist). Force a backend with `gateway.relay_supervisor`
(`launchd | systemd | process`). `socat` is required. Check it:

```bash
loom gateway status
```

### Step 4.3 — Generate the edge config and point your proxy at the relay

```bash
loom gateway sync
```

`sync` auto-detects the Loom host's LAN IP (so a DHCP change doesn't silently
break the public path — just re-run `sync`), regenerates
`proxy/gateway/edge-loom.yml`, and — **if** you've configured an edge push target
(see Step 4.4) — pushes it to your edge. If you have **not** configured a push
target, it just writes the files and tells you to deploy them yourself.

Print the generated config to deploy by hand:

```bash
loom gateway edge-config
```

The generated `edge-loom.yml` is a Traefik dynamic-config file. It declares a
wildcard router for `*.<your-domain>` on the `websecure` entrypoint, a service
pointing at `https://<loom-host-LAN-IP>:<relay_port>`, and a `serversTransport`
with `insecureSkipVerify` (the relay re-presents Loom's internal cert). It assumes
your edge Traefik has a cert resolver named `letsencrypt` and a `websecure`
entrypoint — rename to match your setup if needed.

#### Using Traefik as your edge

Drop the generated file into your edge Traefik's dynamic-config directory, e.g.
`/etc/traefik/dynamic/loom.yml`. Ensure your static config defines the
`websecure` entrypoint and a `letsencrypt` (or equivalently named) cert resolver
configured for the `*.<your-domain>` wildcard (DNS-01 challenge).

#### Using Caddy as your edge

The generated file is Traefik-flavored; with Caddy you express the same routing
in a `Caddyfile`. The essentials are a wildcard site that reverse-proxies to the
relay over TLS without verifying the upstream cert:

```caddyfile
*.example.com {
    tls {
        dns <your-dns-provider> {env.DNS_API_TOKEN}
    }
    reverse_proxy https://<loom-host-LAN-IP>:8444 {
        transport http {
            tls_insecure_skip_verify
        }
        header_up Host {host}
    }
}
```

#### Using nginx as your edge

Equivalent nginx: a wildcard `server` block with a wildcard cert that proxies to
the relay, passing the original `Host` header and not verifying the upstream cert:

```nginx
server {
    listen 443 ssl;
    server_name *.example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    location / {
        proxy_pass              https://<loom-host-LAN-IP>:8444;
        proxy_ssl_verify        off;
        proxy_set_header        Host $host;
    }
}
```

> Forwarding the original `Host` header is required for all three: Loom routes
> `<name>.<your-domain>` to the right app by Host, so the edge must preserve it.

### Step 4.4 — (Optional) Automate the push to your edge

If you want `loom gateway sync` to deploy the regenerated config for you, set a
push target in `fleet/config.json`:

```json
{
  "gateway": {
    "edge_host": "user@edge.example.com",
    "edge_vmid": 0
  }
}
```

> **Note:** the built-in push uses `scp` + Proxmox `pct push` (it copies the file
> to the host, then into an LXC container by `edge_vmid`). **That is one example
> of the edge step, not a requirement.** If your edge isn't a Proxmox LXC, leave
> `edge_host`/`edge_vmid` unset and deploy `proxy/gateway/edge-loom.yml` to your
> reverse proxy with whatever you already use (Ansible, `rsync`, a Git-backed
> config repo, etc.). The generic instruction is always: *deploy
> `proxy/gateway/edge-loom.yml` to your reverse proxy's dynamic-config directory.*

After any IP change, or after adding/removing gated apps, re-run `loom gateway
sync` (or regenerate + redeploy by hand).

---

## 5. Access tiers

Each app declares its tier with `access:` in `fleet.app.yaml`:

```yaml
access: public   # public | gated | private
```

| Tier | Local | Public | Auth |
|------|-------|--------|------|
| `public` | routed | routed at `<name>.<your-domain>` | none |
| `gated` | routed | routed at `<name>.<your-domain>` | SSO at the edge |
| `private` | loopback only | **not** routed publicly | n/a (optional tailnet URL) |

### 5.1 The `gated` tier — bring your own forward-auth

A `gated` app is routed publicly but sits behind a **forward-auth** (SSO)
middleware applied **at your edge**. The auth check happens at the edge — not in
the Loom container — because the Loom proxy can't reach your auth server's
network. You bring the SSO; Authelia and oauth2-proxy are the common choices, but
any forward-auth-capable provider works.

Configure the auth endpoint in `fleet/config.json`:

```json
{
  "gateway": {
    "auth_upstream": "auth.example.com:9091",
    "auth_rd": "https://auth.example.com"
  }
}
```

- `auth_upstream` — `host:port` of your auth server's verify endpoint, reachable
  from the edge.
- `auth_rd` — the SSO login URL unauthenticated users are redirected to.

Both must be set, or deploying a `gated` app fails fast with a clear error.

When set, Loom generates `proxy/gateway/edge-loom-gated.yml` — higher-priority
per-gated-app routers on your edge that attach a `forwardAuth` middleware pointed
at `auth_upstream`, with the standard `Remote-User` / `Remote-Groups` /
`Remote-Name` / `Remote-Email` response headers forwarded to the app. Deploy it
alongside the wildcard file:

```bash
loom gateway sync          # regenerates + (optionally) pushes both files
loom gateway edge-config   # or print them to deploy by hand
```

Deploy the gated file to your edge as e.g. `/etc/traefik/dynamic/loom-gated.yml`.
The generated middleware targets an Authelia-style verify path
(`/api/verify?rd=...`); if your provider differs (e.g. oauth2-proxy), adapt the
emitted `forwardAuth` block to your provider's verify endpoint and headers.

> The generated gated routers are Traefik-shaped. On a Caddy or nginx edge,
> translate the same idea: a wildcard route for the public apps, plus a
> higher-precedence route per gated host that runs your forward-auth filter
> (e.g. Caddy's `forward_auth`, or nginx `auth_request`) before proxying to the
> relay.

The Loom-side route for a gated app is identical to a public one — the gating is
purely an edge concern, so on the LAN/tailnet a gated app is reachable without
SSO.

### 5.2 The `private` tier — optional Tailscale

A `private` app is **never** routed publicly. It's published on loopback
(`http://127.0.0.1:<random-port>`, shown by `loom list`) for local access only.

Optionally, give private apps a stable **tailnet-only** URL via
[Tailscale](https://tailscale.com/) `serve`. Install Tailscale on the Loom host,
then set your node name in `fleet/config.json`:

```json
{
  "gateway": {
    "tailnet_host": "loom-host.tailXXXX.ts.net",
    "tailnet_base_port": 7100
  }
}
```

With `tailnet_host` set (and the `tailscale` CLI present), each private app gets a
`https://<tailnet_host>:<port>` URL on (re)deploy, assigned from
`tailnet_base_port` upward. Removing the app tears the serve mapping down. This
exposes private apps to your tailnet **only** — never to the public internet.

---

## 6. Verifying a public deployment

After wiring the edge:

```bash
loom deploy examples/hello-web      # public app
loom list                            # EXTERNAL column shows the public URL
curl -fsS https://hello-web.example.com/healthz
```

`loom gateway status` summarizes what's enabled: relay state, the public domain
and where its edge route file lives, whether the gated tier is configured, and
whether tailnet serve is on.

---

## 7. `fleet/config.json` reference

`fleet/config.json` is created with defaults on first run and is **git-ignored**
(it holds machine-local values). You only need to set the keys you use; anything
omitted falls back to the default below.

### Top-level keys

| Key | Default | What it does |
|-----|---------|--------------|
| `base_domain` | `loom.localhost` | Local wildcard domain. Apps are routed at `<name>.<base_domain>`. `*.localhost` resolves to `127.0.0.1` with no DNS setup. |
| `network` | `loom` | Name of the Docker network the proxy and all app containers share. |
| `http_port` | `80` | Host port for plain HTTP (redirected to HTTPS). Bound to loopback. |
| `https_port` | `8443` | Host port for HTTPS. `443` is ideal but often reserved locally; `8443` needs no system changes. Bound to loopback. |
| `default_target` | `local` | Deploy target adapter. Only `local` (Docker) ships in the open core. |
| `public_domain` | `""` | Public wildcard domain for externally-reachable apps. When set, public/gated apps get a `<name>.<public_domain>` router and the edge generators activate. Empty = local-only. |

### `gateway` block

| Key | Default | What it does |
|-----|---------|--------------|
| `gateway.relay_port` | `8444` | Port the native relay listens on; your edge proxy forwards here. The relay bridges off-loopback traffic to Loom's HTTPS port. |
| `gateway.auth_upstream` | `""` | `host:port` of your forward-auth (SSO) verify endpoint, reachable from the edge. Required (with `auth_rd`) to enable the `gated` tier. |
| `gateway.auth_rd` | `""` | SSO login redirect URL for unauthenticated users, e.g. `https://auth.example.com`. |
| `gateway.auth_relay_port` | `19091` | Local port for the optional auth-bridge relay (used when the proxy can't reach the auth host directly). |
| `gateway.edge_host` | `""` | SSH target for `loom gateway sync` to push the edge config to. Leave empty to deploy the generated files yourself. |
| `gateway.edge_vmid` | `0` | Proxmox LXC vmid for the built-in `pct push` (one example edge). `0`/empty disables the automated push. |
| `gateway.tailnet_host` | `""` | Tailscale node name. When set (and `tailscale` is installed), private apps get a stable tailnet-only `serve` URL. |
| `gateway.tailnet_base_port` | `7100` | First port used for private apps' tailnet URLs; subsequent apps increment from here. |

### Generated, git-ignored artifacts

Loom regenerates these; never edit by hand and never commit them:

- `proxy/dynamic/app-*.yml` — per-app local route files (Traefik file provider).
- `proxy/dynamic/tls.yml` + `proxy/certs/*.pem` — local TLS config and material.
- `proxy/gateway/edge-loom.yml` — the wildcard route to deploy to **your** edge.
- `proxy/gateway/edge-loom-gated.yml` — per-gated-app forward-auth routers for
  your edge (only when the gated tier is configured).
- `fleet/registry.json`, `fleet/library.json` — fleet state and the harvested,
  LLM-addressable Library.

---

## 8. Security notes

- The Loom proxy binds to **loopback only**. Nothing is exposed off-host until
  *you* put a reverse proxy in front of the relay — exposure is an explicit,
  opt-in step.
- **TLS is yours to own at the edge.** Terminate `*.<your-domain>` TLS on your
  reverse proxy (Let's Encrypt or your CA). The relay link inside your trust
  boundary uses Loom's internal cert, which the edge does not verify
  (`insecureSkipVerify`) — keep that hop on a network you control.
- The `gated` tier's SSO runs at the **edge**, so it does **not** protect a gated
  app reached directly over the LAN or tailnet. Treat the LAN/tailnet path as
  trusted, or front sensitive apps with `private` + Tailscale instead.
- `fleet/config.json` may hold environment-specific endpoints and is git-ignored
  for that reason — keep it out of version control.

---

## 9. Where hosted Loom differs

The open core ships the **mechanisms** — the relay, the edge-config generators,
the `consumes:` service-wiring and data-federation hooks — and self-host examples
for each. It deliberately does **not** include multi-tenancy, accounts/SSO/RBAC, a
web dashboard, billing/quotas, managed consumables (a hosted LLM/image gateway,
managed Postgres/storage/domains), or a managed cloud deploy target. Those are the
commercial, hosted product. Everything in this guide is yours to run, end to end.
