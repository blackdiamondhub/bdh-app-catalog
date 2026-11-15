"""Minimal read-only OCI registry client for ghcr.io (stdlib only).

Anonymous pulls work for public packages. Set ``GHCR_TOKEN`` (a token with
``read:packages``, e.g. the workflow's GITHUB_TOKEN) for private ones.
"""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

REGISTRY = "ghcr.io"
MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
MAX_BYTES = 4 * 1024 * 1024


class RegistryError(Exception):
    pass


def split_ref(ref):
    """``ghcr.io/org/name@sha256:x`` or ``ghcr.io/org/name:tag`` -> (name, reference)."""
    if not ref.startswith(REGISTRY + "/"):
        raise RegistryError(f"not a {REGISTRY} reference: {ref}")
    rest = ref[len(REGISTRY) + 1:]
    if "@" in rest:
        name, reference = rest.split("@", 1)
    elif ":" in rest.rsplit("/", 1)[-1]:
        name, reference = rest.rsplit(":", 1)
    else:
        name, reference = rest, "latest"
    return name, reference


class Registry:
    def __init__(self, token=None, opener=None):
        self.secret = os.environ.get("GHCR_TOKEN", "") if token is None else token
        self.opener = opener or urllib.request.urlopen
        self._tokens = {}

    def _token(self, name):
        if name not in self._tokens:
            url = f"https://{REGISTRY}/token?" + urllib.parse.urlencode(
                {"scope": f"repository:{name}:pull", "service": REGISTRY})
            req = urllib.request.Request(url)
            if self.secret:
                basic = base64.b64encode(f"x:{self.secret}".encode()).decode()
                req.add_header("Authorization", f"Basic {basic}")
            try:
                with self.opener(req, timeout=30) as resp:
                    self._tokens[name] = json.loads(resp.read(MAX_BYTES))["token"]
            except (OSError, ValueError, KeyError) as exc:
                raise RegistryError(f"{REGISTRY}/{name}: no pull token: {exc}") from exc
        return self._tokens[name]

    def _get(self, name, path, accept=None, method="GET"):
        req = urllib.request.Request(f"https://{REGISTRY}/v2/{name}/{path}", method=method)
        req.add_header("Authorization", f"Bearer {self._token(name)}")
        if accept:
            req.add_header("Accept", accept)
        try:
            with self.opener(req, timeout=60) as resp:
                return resp.headers, (b"" if method == "HEAD" else resp.read(MAX_BYTES))
        except urllib.error.HTTPError as exc:
            raise RegistryError(f"{REGISTRY}/{name}/{path}: HTTP {exc.code}") from exc
        except OSError as exc:
            raise RegistryError(f"{REGISTRY}/{name}/{path}: {exc}") from exc

    def digest(self, ref):
        """The manifest digest a tag (or digest) points at."""
        name, reference = split_ref(ref)
        headers, _ = self._get(name, f"manifests/{reference}", MANIFEST_TYPES, method="HEAD")
        digest = headers.get("Docker-Content-Digest", "")
        if not digest.startswith("sha256:"):
            raise RegistryError(f"{ref}: registry returned no digest")
        return digest

    def config(self, ref, platform=("linux", "amd64")):
        """The image config (labels, rootfs diff_ids) of ``ref``."""
        name, reference = split_ref(ref)
        headers, body = self._get(name, f"manifests/{reference}", MANIFEST_TYPES)
        manifest = json.loads(body)
        media = manifest.get("mediaType") or headers.get("Content-Type", "")
        if media in INDEX_TYPES or "manifests" in manifest:
            match = [m for m in manifest.get("manifests", [])
                     if (m.get("platform") or {}).get("os") == platform[0]
                     and (m.get("platform") or {}).get("architecture") == platform[1]]
            if not match:
                raise RegistryError(f"{ref}: no {'/'.join(platform)} image")
            _, body = self._get(name, f"manifests/{match[0]['digest']}", MANIFEST_TYPES)
            manifest = json.loads(body)
        _, blob = self._get(name, f"blobs/{manifest['config']['digest']}")
        return json.loads(blob)


def diff_ids(config):
    return list((config.get("rootfs") or {}).get("diff_ids") or [])


def labels(config):
    return dict((config.get("config") or {}).get("Labels") or {})


def check_built_on(app_config, base_config):
    """Raise unless the app image's layers start with the base's layers."""
    app, base = diff_ids(app_config), diff_ids(base_config)
    if not base or len(app) <= len(base) or app[:len(base)] != base:
        raise RegistryError("image layers do not start with the base's layers")
