"""The `local` target: build each app as a Docker image and run it as a
container on the shared `loom` network. Public apps are routed by Traefik at
https://<name>.<base-domain>; private apps are published to 127.0.0.1 only.
"""
from __future__ import annotations

import socket
from pathlib import Path

from .. import contract, dockercmd, dockerfiles, gateway, proxy, registry, services
from ..config import app_url, paths, public_app_url
from ..util import LoomError, info
from .base import Target


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LocalDockerTarget(Target):
    name = "local"

    @staticmethod
    def _container(name: str) -> str:
        return f"loom-{name}"

    @staticmethod
    def _image(name: str) -> str:
        return f"loom/{name}:latest"

    @staticmethod
    def _service_port(manifest: dict) -> int:
        # static sites are served by nginx on 80; everyone else binds $PORT.
        return 80 if manifest["runtime"] == "static" else int(manifest["port"])

    def deploy(self, cfg: dict, app_dir: Path, manifest: dict) -> dict:
        name = manifest["name"]
        runtime = manifest["runtime"]
        access = manifest["access"]
        service_port = self._service_port(manifest)
        image = self._image(name)
        container = self._container(name)

        proxy.ensure(cfg)
        # If apps are exposed publicly, make sure the gateway (native relay +
        # auth middleware) is up so public/gated apps are actually reachable.
        if cfg.get("public_domain") and access in ("public", "gated"):
            gateway.ensure(cfg)

        if runtime == "docker":
            dockerfile = app_dir / "Dockerfile"
            if not dockerfile.exists():
                raise LoomError(
                    f"runtime is 'docker' but no Dockerfile found in {app_dir}"
                )
            info(f"building {image} from the app's Dockerfile")
            dockercmd.build(image, app_dir, dockerfile=dockerfile)
        else:
            text = dockerfiles.generate(runtime, manifest["port"])
            info(f"building {image} ({runtime}, generated Dockerfile)")
            dockercmd.build(image, app_dir, dockerfile_text=text)

        # Redeploy = replace in place.
        dockercmd.rm(container, force=True)

        env = {
            "PORT": service_port,
            "LOOM_APP": name,
            "LOOM_HEALTH_PATH": manifest.get("health", {}).get("path", "/health"),
            "LOOM_CAPABILITIES": ",".join(c["id"] for c in manifest.get("capabilities", [])),
        }
        # Shared-services wiring: this app may PROVIDE a service and/or CONSUME
        # others. Provider gets the verify secret; consumer gets scoped URLs+tokens.
        env.update(services.provider_env(cfg, manifest))
        env.update(services.secret_env(manifest))
        provisioned_env, grants = services.provision_env(cfg, manifest)
        env.update(provisioned_env)
        data_env, data_grants = services.provision_data_env(cfg, manifest)
        env.update(data_env)
        # The federation gateway reads the live registry (for grant checks +
        # prompt revocation); mount it read-only.
        volumes = {}
        if manifest.get("provides_service") == "federation":
            volumes[str(paths().registry_file)] = "/registry.json:ro"
        labels = {"loom.managed": "true", "loom.app": name, "loom.access": access}
        host_port = None
        public_url = None
        tailnet_url = None
        tailnet_port = None

        if access in ("public", "gated"):
            dockercmd.run_container(
                name=container, image=image, network=cfg["network"],
                env=env, labels=labels, volumes=volumes,
            )
            if access == "gated" and not gateway.auth_enabled(cfg):
                raise LoomError(
                    "access 'gated' needs gateway.auth_upstream + auth_rd in "
                    "fleet/config.json (SSO is applied at the edge)"
                )
            # The Loom-side route is identical to a public app; SSO gating is
            # applied at the EDGE via gateway.write_edge_gated (the Loom
            # container can't reach the SSO server's LAN IP).
            proxy.write_route(cfg, name, container, service_port)
            url = app_url(cfg, name, "public")
            public_url = public_app_url(cfg, name)
        else:  # private
            host_port = _free_port()
            dockercmd.run_container(
                name=container, image=image, network=cfg["network"],
                env=env, labels=labels, volumes=volumes,
                ports={f"127.0.0.1:{host_port}": service_port},
            )
            proxy.remove_route(name)  # never routed publicly
            url = app_url(cfg, name, "private", host_port)
            # Optional: a stable tailnet-only URL via `tailscale serve`.
            if gateway.tailnet_enabled(cfg):
                prev = registry.get(name) or {}
                tailnet_port = prev.get("tailnet_port") or gateway.next_tailnet_port(
                    cfg, {e.get("tailnet_port") for e in registry.all_apps()
                          if e.get("tailnet_port") and e["name"] != name}
                )
                tailnet_url = gateway.tailnet_serve(cfg, tailnet_port, host_port)

        return {
            "name": name,
            "target": self.name,
            "runtime": runtime,
            "access": access,
            "port": manifest["port"],
            "service_port": service_port,
            "host_port": host_port,
            "url": url,
            "public_url": public_url,
            "tailnet_port": tailnet_port,
            "tailnet_url": tailnet_url,
            "contract": contract.snapshot(manifest),
            "grants": grants,
            "data_grants": data_grants,
            "source_path": str(app_dir),
            "image": image,
            "container": container,
            "status": "running",
        }

    def start(self, cfg: dict, entry: dict) -> None:
        proxy.ensure(cfg)
        dockercmd.start(entry["container"])

    def stop(self, cfg: dict, entry: dict) -> None:
        dockercmd.stop(entry["container"])

    def remove(self, cfg: dict, entry: dict) -> None:
        dockercmd.rm(entry["container"], force=True)
        proxy.remove_route(entry["name"])
        if entry.get("tailnet_port"):
            gateway.tailnet_unserve(entry["tailnet_port"])
        if entry.get("image"):
            dockercmd.rmi(entry["image"])

    def logs(self, cfg: dict, entry: dict, follow: bool, tail: str) -> int:
        return dockercmd.logs(entry["container"], follow=follow, tail=tail)

    def reconcile(self, cfg: dict, entries: list[dict]) -> dict:
        states = dockercmd.managed_states()
        return {e["name"]: states.get(e["name"], "missing") for e in entries}

    def probe_health(self, cfg: dict, entry: dict) -> str:
        path = ((entry.get("contract") or {}).get("health") or {}).get("path") or ""
        if not path:
            return "unknown"
        import ssl
        import urllib.error
        import urllib.request
        if entry.get("host_port"):  # private: direct loopback
            url, ctx = f"http://127.0.0.1:{entry['host_port']}{path}", None
        else:  # routed: via Traefik on loopback (Host header routes; TLS unverified)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            url = f"https://127.0.0.1:{cfg['https_port']}{path}"
        req = urllib.request.Request(url, headers={"Host": f"{entry['name']}.{cfg['base_domain']}"})
        try:
            with urllib.request.urlopen(req, timeout=4, context=ctx) as r:
                code = r.getcode()
        except urllib.error.HTTPError as e:
            code = e.code  # the server DID respond (e.g. 404 = up, no /health route)
        except Exception:
            return "unknown"  # connection failure; never block on a failed probe
        if 200 <= code < 400 or code == 404:
            return "ok"
        return "unready"
