# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities privately to **security@example.com**. Do **not** open a public GitHub issue for security reports.

Include where you can:

- a description of the issue and its impact,
- the affected component (CLI, proxy, gateway, MCP server, SDK, an example), and
- steps to reproduce or a proof of concept.

We aim to acknowledge reports within a few business days. Please give us reasonable time to investigate and ship a fix before any public disclosure.

## Supported Versions

Loom is pre-1.0 open-core software. Security fixes target the latest commit on the default branch; there is no long-term backport guarantee for older tags.

## Trust & Security Model

Loom is an **additive, self-hosted** platform. You run the substrate (Docker, the Traefik reverse proxy, the optional edge gateway) on infrastructure you control, and you own the trust boundary at your own edge. The points below describe the controls Loom ships; they are building blocks, not a turnkey hardened deployment.

### Access tiers

Every app declares an `access` tier in `fleet.app.yaml`, and routing enforces it:

- **public** — routed and reachable by anyone who can reach your edge.
- **gated** — routed, but behind forward-auth SSO (Authelia) at the reverse-proxy edge. The proxy challenges the request before it reaches the app. Note: gating is applied at the public edge and is **bypassed on the LAN/tailnet** by design, so treat any host with direct network access to the substrate as trusted.
- **private** — not routed publicly at all; reachable only on the local host and, optionally, over a Tailscale `serve` URL on your tailnet.

Choose the tier deliberately: an app is only as protected as the tier you give it. Apps remain responsible for their own authentication and authorization for anything beyond what the tier provides.

### Agent invocation guard (`loom_invoke`)

The MCP server projects the Library so agents can discover and call apps. `loom_invoke` is deliberately constrained to limit SSRF and tier-leak risk:

- it only proxies **registered fleet apps** — never arbitrary URLs supplied by the caller, and
- it **refuses `private` apps** (returns an error rather than proxying), so the agent surface cannot be used to reach apps that are not meant to be publicly invokable.

Because invocation is restricted to the known registry rather than free-form URLs, the agent tool surface is not a general-purpose outbound HTTP proxy.

### App-to-app identity (HMAC tokens)

Shared services use the contract's `consumes:`/`provides_service:` blocks. When Loom wires a consumer to a provider it injects `LOOM_<SERVICE>_URL` plus a per-app **HMAC-SHA256** token (`LOOM_<SERVICE>_TOKEN`) derived from a platform secret over the caller's `<app>:<service>` identity. The provider receives the platform secret (`LOOM_SERVICE_SECRET`) and verifies callers with a constant-time comparison (`hmac.compare_digest`). The same mechanism backs data tokens (`LOOM_DATA_<DATASET>_TOKEN`).

These tokens establish **identity between co-located apps on a shared substrate**; they are not a substitute for end-to-end authentication across an untrusted network. Protect the platform secret accordingly (see below).

### Data federation (deny-by-default)

Cross-app dataset access goes through a grant-checked federation gateway (see `examples/loom-fed`). It is **deny-by-default**: a consumer only receives a token for datasets it explicitly declares in `consumes:`, and the gateway independently re-checks the **live grant** in the registry on every request, returning `403` when no grant exists. Revoking a grant takes effect immediately, without re-minting or rotating tokens.

## Local TLS

Local HTTPS for `*.localhost` is provisioned with [mkcert](https://github.com/FiloSottile/mkcert), which installs a locally-trusted development CA. This CA is for **local development only** — do not distribute or trust it elsewhere. If mkcert is not installed, Traefik falls back to a self-signed certificate and browsers will warn.

## Secrets & State

Machine-local state and secrets are **gitignored** and must never be committed:

- `fleet/registry.json`, `fleet/config.json`, `fleet/library.json` — fleet state, including the platform `service_secret` that signs all app-to-app and data tokens. Treat `config.json` as sensitive; anyone who can read it can mint valid tokens.
- `proxy/certs/*.pem` — local TLS certificate and private key material.
- `proxy/dynamic/app-*.yml`, `proxy/dynamic/tls.yml`, and the generated `proxy/gateway/edge-loom*.yml` — generated route/edge config (machine-specific).

Never commit secrets, certificates, or generated state. If a secret is exposed, rotate it (regenerate `service_secret`, which invalidates outstanding tokens) and re-deploy affected apps.
