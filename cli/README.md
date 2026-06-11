# loomhost

**Deploy a small app with one command — instantly routed at its own URL,
health-checked, discoverable, agent-callable, and able to consume platform
services. No per-app plumbing.**

Loom is an agent-native, additive app-hosting platform. Drop a four-line
`fleet.app.yaml` next to your app, run `loom deploy`, and it is live behind a
shared reverse proxy at a stable HTTPS URL — indexed into an LLM-addressable
Library (`loom find` / `loom describe --json`) and callable by agents via
`loom mcp` (MCP + OpenAPI + REST). `loom admin` opens a local fleet console
with a directory scanner and one-click deploys.

```yaml
# fleet.app.yaml
name: hello-web
runtime: node        # node | python | static | docker
port: 3000
access: public       # public | gated | private
```

```bash
pipx install loomhost
loom proxy up
loom deploy ./hello-web    # → https://hello-web.loom.localhost:8443
loom admin                 # the fleet console
```

Requires Docker (or OrbStack) and Python 3.9+. Full documentation, examples
(shared services, a no-key LLM gateway, data federation), and source:
**https://github.com/jhammant/loom-oss**.

Licensed under the Business Source License 1.1 (converts to Apache 2.0 on
2030-06-10): self-host freely in production; the one restriction is offering
Loom itself as a competing hosted service.
