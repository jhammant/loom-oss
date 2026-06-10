# Contributing to Loom

Thanks for hacking on Loom. It is an agent-native, **additive** app-hosting
platform: deploy a small app with one command and it is instantly routed,
health-checked, discoverable, agent-callable, and able to consume platform
services — with no per-app plumbing. This guide covers the dev loop, the code
layout, and the three extension seams you are most likely to touch: a **runtime**,
a **target adapter**, and a **shared service**.

By contributing you agree that your contributions are licensed under the
project's [Business Source License 1.1](./LICENSE) (Change Date `2030-06-10`,
Change License Apache 2.0). Don't add code under an incompatible license.

## Dev setup

Requirements: Python 3.9+, Docker (the only deploy target today is local
Docker), and [`mkcert`](https://github.com/FiloSottile/mkcert) if you want local
TLS. `*.localhost` resolves to `127.0.0.1` with no DNS setup.

```bash
# install the CLI editable, on PATH, in its own venv
pipx install -e ./cli

# run the test suite (26 tests; no Docker required — they're pure-Python)
pytest cli/tests
```

`pipx install -e` means edits to `cli/loom/*.py` take effect immediately. The
entry point is `loom = "loom.cli:main"` (see `cli/pyproject.toml`). The only
runtime dependency is PyYAML; `pytest` is the lone dev dependency
(`pip install -e ./cli[dev]` if you aren't using pipx).

Smoke-test a real deploy against your Docker:

```bash
loom deploy examples/hello-web     # node, public  -> https://hello-web.loom.localhost
loom list
loom find clock
loom remove hello-web
```

## Code layout

The CLI is target-agnostic: it loads + validates a manifest, hands it to a
`Target`, and records the returned entry in the registry. Everything else
(Library, services, federation, MCP) keys off the contract stored on that entry.

```text
cli/loom/
  cli.py            # argparse surface + command handlers (the `loom` verbs)
  manifest.py       # load + validate fleet.app.yaml (v1 fields), merge v2 contract
  contract.py       # v2 contract: parse/normalize/defaults + snapshot() for the registry
  dockerfiles.py    # generated Dockerfiles for node | python | static runtimes
  dockercmd.py      # thin docker CLI wrappers (build/run/rm/logs/...)
  proxy.py          # the shared Traefik reverse proxy (FILE provider; one route file per app)
  gateway.py        # external exposure: native relay, forward-auth, tailnet serve
  services.py       # shared-services + data-federation provisioning (provides/consumes)
  registry.py       # fleet/registry.json — the record of every deployed app
  harvester.py      # turn a deployed app's contract into a searchable Library record
  library.py        # fleet/library.json — the LLM-addressable Library (find/describe)
  mcp_server.py     # MCP (Streamable HTTP / JSON-RPC) + OpenAPI 3.1 + REST over the Library
  config.py         # locate the Loom home, read fleet/config.json
  util.py           # LoomError + colored output helpers
  targets/
    base.py         # the Target ABC every deploy adapter implements
    local.py        # the only implemented target: build an image, run a container
    __init__.py     # target registry (_REGISTRY) + get_target()
cli/tests/          # pytest suite (manifest/contract, services, federation, library, mcp)
examples/           # deployable reference apps (each a fleet.app.yaml)
sdk/python/         # loom_sdk.py — apps read injected service env vars
proxy/              # docker-compose.yml + generated gateway/edge config
docs/app-contract.md
```

The contract surface is documented in [`docs/app-contract.md`](./docs/app-contract.md).
Read it before changing `contract.py` — it is the spec.

## Adding a runtime

Runtimes (`node | python | static | docker`) let simple apps deploy with no
Dockerfile of their own. `docker` is the escape hatch: it builds the app's own
`Dockerfile`. To add a generated runtime (say `go`):

1. Add a template + a branch in `cli/loom/dockerfiles.py::generate()`. The app
   binds `$PORT` (static is the exception: nginx on 80).
2. Add the name to `RUNTIMES` in `cli/loom/manifest.py` so the manifest validator
   accepts it.
3. If the served port differs from the declared `port` (as `static` does),
   handle it in `LocalDockerTarget._service_port` in `cli/loom/targets/local.py`.
4. Add an example under `examples/` and a parse test.

Keep generated Dockerfiles minimal and dependency-driven (install only if a lock/
manifest file is present), matching the existing node/python templates.

## Adding a target adapter

The target seam is how a future deploy backend (a cloud host, Coolify on a home
box, scale-to-zero) drops in without touching the CLI. Implement the ABC in
`cli/loom/targets/base.py`:

```python
class Target(ABC):
    name: str = "base"
    def deploy(self, cfg, app_dir, manifest) -> dict: ...   # build+run; return a registry entry
    def start(self, cfg, entry) -> None: ...
    def stop(self, cfg, entry) -> None: ...
    def remove(self, cfg, entry) -> None: ...               # tear down completely
    def logs(self, cfg, entry, follow, tail) -> int: ...
    def reconcile(self, cfg, entries) -> dict: ...          # {app_name: live_status}
    def probe_health(self, cfg, entry) -> str: ...          # optional; defaults to "unknown"
```

Contract:

- `deploy()` returns the **registry entry dict** the CLI persists. Mirror the
  shape `LocalDockerTarget.deploy` returns — at minimum `name`, `target`,
  `runtime`, `access`, `url`, `status`, and `contract: contract.snapshot(manifest)`.
  That `contract` block is what the harvester, Library, and services read, so it
  must be present.
- Honor the three access tiers (`public | gated | private`) however your backend
  expresses them.
- `reconcile()` must be cheap and side-effect-free; it's called on every
  `loom list`.
- `probe_health()` is concrete and defaults to `"unknown"` — opt in, and never
  raise (a failed probe must not block).

Register the class in `cli/loom/targets/__init__.py::_REGISTRY` under its `name`.
Users select it via `default_target` in `fleet/config.json`. Add tests that
exercise your `deploy()` return shape and `reconcile()` mapping (mock the backend;
don't require live infra).

## Adding a shared service

Apps consume platform services with **zero per-app plumbing**. A backend app
declares the service it backs; a consumer declares what it wants; Loom resolves
the two and injects credentials. The mechanism lives in `cli/loom/services.py`.

Provider side — set `provides_service:` in the backend's `fleet.app.yaml`:

```yaml
# examples/loom-wallet/fleet.app.yaml
provides_service: wallet
```

At deploy, `services.provider_env` injects `LOOM_SERVICE` and
`LOOM_SERVICE_SECRET` so the backend can **verify** callers with
`services.verify_token` (an HMAC over `caller_app:service`).

Consumer side — declare `consumes:` (validated in `contract.py`):

```yaml
# examples/wallet-consumer/fleet.app.yaml
consumes:
  - service: wallet
    scope: charge
```

`services.provision_env` finds the deployed provider and injects, into the
consumer container:

```text
LOOM_<SERVICE>_URL     # the provider's in-network address (loom Docker network)
LOOM_<SERVICE>_TOKEN   # an HMAC the provider verifies (app-to-app identity)
```

Resolution is **best-effort and deny-by-default**: a consume with no deployed
provider warns and injects nothing — it never blocks deploy; redeploy once the
provider exists. The Python SDK (`sdk/python/loom_sdk.py`) reads exactly these
env vars, so apps need no config.

To add a new well-known service:

1. Add its name to `KNOWN_SERVICES` in `cli/loom/contract.py` (unknown services
   are still *allowed* — they just warn — so this only suppresses the warning).
2. Build the backend as a normal app that sets `provides_service:` and verifies
   the token via `services.verify_token` (mirror `examples/loom-wallet/app.py`).
3. Optionally add a typed client to the SDK (mirror the `Wallet` client).
4. Add a `wallet-consumer`-style example proving the chain end to end.

Data federation works the same way through `data.provides` / `data.consumes`,
the federation gateway (`provides_service: federation`), and
`services.provision_data_env`. See `examples/loom-fed`, `examples/data-provider`,
and `examples/data-consumer`.

## Test expectations

Tests are not optional for these areas:

- **Every contract change ships tests.** New manifest fields, validation rules,
  or defaults need cases in `cli/tests/test_manifest_contract.py` — including a
  check that a **v1 manifest still normalizes unchanged** (backward compatibility
  is a hard requirement; the contract is additive).
- **Every registry / Library / harvester change ships tests** in the relevant
  `cli/tests/test_*.py`.
- New service or federation resolution logic needs tests in
  `test_services.py` / `test_federation.py`.

Conventions to follow (see existing tests):

- Make `loom` importable without an install via the standard prelude:
  `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))`.
- Use `tmp_path` for files and `monkeypatch` to stub the registry (e.g.
  `monkeypatch.setattr(registry, "all_apps", lambda: [...])`) — tests must not
  require Docker or a live fleet.
- Follow Arrange / Act / Assert.

`pytest cli/tests` must be green before you open a PR. CI runs the same command.

## Commit & PR norms

- **Conventional Commits**: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`,
  `test:`. Branch names match (`feat/…`, `fix/…`).
- Keep PRs **atomic and reviewable** — one concern per PR.
- Reference issues in the **PR description** (`Closes #123`), not in commit
  messages.
- Update docs alongside behavior: a contract change touches both
  `docs/app-contract.md` and tests.
- Never commit secrets or `fleet/` state (`registry.json`, `config.json`,
  `service_secret`, certs are gitignored — keep it that way).
- PR checklist: `pytest cli/tests` green, an example/test for any
  contract/registry/service change, and docs updated.

## Scope

The open core is the self-hostable platform and the **mechanisms** for shared
consumables (`consumes:` / `provides_service:`) plus self-host examples. Hosted
concerns — multi-tenancy, accounts/SSO/RBAC, billing/metering/quotas, *managed*
consumable backends (a hosted LLM/image gateway, managed Postgres/storage/domain
provisioning), and a managed cloud deploy target — are intentionally **out of
scope** here and reserved for the commercial offering (the reason for the BSL).
PRs that add managed-service backends will be redirected; PRs that improve the
mechanism, the local target, runtimes, the contract, discovery, or examples are
very welcome.
