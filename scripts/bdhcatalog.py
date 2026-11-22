"""Shared helpers for reading app wheels and editing catalog.json.

Stdlib only, so the release workflow can run it without installing anything.
"""
import ast
import configparser
import email.parser
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog.json"
SCHEMA = ROOT / "schema" / "catalog.schema.json"

SCHEMA_VERSION = 1
ORG = "blackdiamondlodge"
ENTRY_POINT_GROUP = "blackdiamondhub.plugins"
SIGNER_WORKFLOW = f"{ORG}/bdh-sdk/.github/workflows/app-release.yml"
CATALOG_REPO = f"{ORG}/bdh-app-catalog"
BUILDER_WORKFLOW = f"{CATALOG_REPO}/.github/workflows/build.yml"
IMAGE_PREFIX = f"ghcr.io/{ORG}/"
BASE_REPO = f"{IMAGE_PREFIX}bdh-app-runtime"
# Fields of an image entry that a rebuild (same release, new base) changes.
REBUILD_FIELDS = {"image", "base", "build", "built_at", "attestation"}

# Mirrors bdh_sdk.plugins / bdh_sdk.provides so a wheel can be read without
# importing it (or the SDK).
NETWORKS = {"NETWORK_NONE": "none", "NETWORK_OUTBOUND": "outbound", "NETWORK_LAN": "lan"}
LIFECYCLES = {"LIFECYCLE_ALWAYS": "always", "LIFECYCLE_ON_DEMAND": "on_demand"}
CAPABILITIES = {"STATS": "stats", "BACKGROUNDS": "backgrounds", "SAVER": "saver", "SEASON": "season"}
COMPANIONS = ("go2rtc", "aec")
DEFAULT_MEMORY_MB = 96
DEFAULTS = {
    "permissions": [],
    "network": "outbound",
    "lifecycle": "on_demand",
    "memory_mb": DEFAULT_MEMORY_MB,
    "companions": [],
}
MANIFEST_FIELDS = tuple(DEFAULTS)
# bdh-sdk 0.20.0 introduced API_VERSION 2 (RPC transport, container runtime).
API2_MIN_SDK = (0, 20)

VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?$")
_PRE_ORDER = {"a": 0, "b": 1, "rc": 2, None: 3}
# Release channels. A PEP 440 pre-release (aN/bN/rcN) is published as a beta;
# promote.py may later move it to stable (same image). Absent means stable.
STABLE, BETA = "stable", "beta"
CHANNELS = (STABLE, BETA)


def version_key(version):
    """Sort key for the X.Y.Z[aN|bN|rcN] versions the catalog accepts."""
    m = VERSION_RE.match(version)
    if not m:
        raise ValueError(f"unsupported version {version!r}")
    release = tuple(int(p) for p in m.group(1).split("."))
    release += (0,) * (4 - len(release))
    return release, _PRE_ORDER[m.group(2)], int(m.group(3) or 0)


def is_prerelease(version):
    m = VERSION_RE.match(version)
    if not m:
        raise ValueError(f"unsupported version {version!r}")
    return m.group(2) is not None


def channel_for(version):
    """The channel a version is first published in."""
    return BETA if is_prerelease(version) else STABLE


def channel(entry):
    return entry.get("channel", STABLE)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_catalog(path=CATALOG):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render_catalog(catalog):
    """The canonical text of catalog.json: apps by id, versions newest first."""
    catalog["apps"] = dict(sorted(catalog["apps"].items()))
    for app in catalog["apps"].values():
        app["versions"].sort(key=lambda v: version_key(v["version"]), reverse=True)
    return json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"


def dump_catalog(catalog, path=CATALOG):
    Path(path).write_text(render_catalog(catalog), encoding="utf-8", newline="\n")


def image_repo(app_id):
    return f"{IMAGE_PREFIX}bdh-app-{app_id}"


def is_image(entry):
    return entry.get("kind") == "image"


def release_url_prefix(repo):
    return f"https://github.com/{repo}/releases/download/"


def normalize_dist(name):
    return re.sub(r"[-_.]+", "-", name).lower()


class WheelError(Exception):
    pass


def _literal(node, names=None):
    """Evaluate a literal, or a bare/attribute name found in ``names``."""
    if names is not None:
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.Attribute) and node.attr in names:
            return names[node.attr]
    return ast.literal_eval(node)


def _call_name(node):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
    return None


def _find_spec_call(source, attr, filename):
    tree = ast.parse(source, filename)
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == attr for t in targets):
            if _call_name(node.value) == "PluginSpec":
                return node.value
    raise WheelError(f"{filename}: no `{attr} = PluginSpec(...)` at module level")


def parse_requirement(req):
    """Split a Requires-Dist value into (name, specifier), ignoring markers."""
    req = req.split(";", 1)[0].strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$", req)
    if not m:
        return req, ""
    spec = m.group(3).strip()
    if spec.startswith("(") and spec.endswith(")"):
        spec = spec[1:-1].strip()
    return m.group(1), spec


def _min_version(specifier):
    for part in specifier.split(","):
        part = part.strip()
        for op in (">=", "==", "~="):
            if part.startswith(op):
                nums = re.match(r"\d+(?:\.\d+)*", part[len(op):].strip())
                if nums:
                    return tuple(int(p) for p in nums.group(0).split("."))
    return None


def read_wheel(path):
    """Return what the catalog needs from a built app wheel.

    The PluginSpec is read with ``ast`` rather than imported, so the wheel's
    code never runs. Manifest fields that are not literals (or known SDK
    constants) raise WheelError; pass ``--manifest`` to add_version.py then.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        tops = {n.split("/", 1)[0] for n in names}
        dist_info = sorted(t for t in tops if t.endswith(".dist-info"))
        if len(dist_info) != 1:
            raise WheelError(f"{path.name}: expected one .dist-info directory, found {dist_info}")
        dist_info = dist_info[0]
        meta = email.parser.Parser().parsestr(zf.read(f"{dist_info}/METADATA").decode("utf-8"))

        ep_name = f"{dist_info}/entry_points.txt"
        if ep_name not in names:
            raise WheelError(f"{path.name}: no entry_points.txt (not a hub app)")
        eps = configparser.ConfigParser(delimiters=("=",))
        eps.optionxform = str
        eps.read_string(zf.read(ep_name).decode("utf-8"))
        if not eps.has_section(ENTRY_POINT_GROUP) or len(eps.items(ENTRY_POINT_GROUP)) != 1:
            raise WheelError(f"{path.name}: needs exactly one [{ENTRY_POINT_GROUP}] entry point")
        (ep_id, target), = eps.items(ENTRY_POINT_GROUP)
        module, _, attr = target.strip().partition(":")
        module_file = module.replace(".", "/") + ".py"
        if module_file not in names:
            module_file = module.replace(".", "/") + "/__init__.py"
        if module_file not in names:
            raise WheelError(f"{path.name}: entry point module {module!r} not in wheel")
        source = zf.read(module_file).decode("utf-8")
        call = _find_spec_call(source, attr or "spec", module_file)

        kw = {k.arg: k.value for k in call.keywords if k.arg}
        try:
            spec_id = _literal(kw["id"])
        except (KeyError, ValueError) as exc:
            raise WheelError(f"{module_file}: PluginSpec id must be a string literal") from exc
        if spec_id != ep_id.strip():
            raise WheelError(f"entry point name {ep_id!r} != PluginSpec id {spec_id!r}")

        manifest, declared = dict(DEFAULTS), []
        resolvers = {"network": NETWORKS, "lifecycle": LIFECYCLES,
                     "memory_mb": {"DEFAULT_MEMORY_MB": DEFAULT_MEMORY_MB}}
        for field in MANIFEST_FIELDS:
            if field not in kw:
                continue
            try:
                value = _literal(kw[field], resolvers.get(field))
            except ValueError as exc:
                raise WheelError(f"{module_file}: PluginSpec {field} is not a literal; pass --manifest") from exc
            if field == "permissions":
                value = sorted(set(value))
            elif field == "companions":
                value = list(value)
            manifest[field] = value
            declared.append(field)

        provides = []
        if "provides" in kw:
            node = kw["provides"]
            if not isinstance(node, ast.Dict):
                raise WheelError(f"{module_file}: PluginSpec provides must be a dict literal; pass --manifest")
            try:
                provides = sorted(_literal(k, CAPABILITIES) for k in node.keys)
            except ValueError as exc:
                raise WheelError(f"{module_file}: unknown provides key; pass --manifest") from exc
        manifest["provides"] = provides

        title = icon = None
        tiles = kw.get("tiles")
        if isinstance(tiles, (ast.Tuple, ast.List)):
            for tile in tiles.elts:
                if _call_name(tile) != "Tile":
                    continue
                tkw = {k.arg: k.value for k in tile.keywords if k.arg}
                try:
                    if "title" in tkw:
                        title = _literal(tkw["title"])
                    elif tile.args:
                        title = _literal(tile.args[0])
                    if "icon" in tkw:
                        icon = _literal(tkw["icon"])
                except ValueError:
                    pass
                break
        icon_path = None
        if isinstance(icon, str) and "/" in icon:
            candidate = f"{module.split('.', 1)[0]}/static/{icon}"
            if candidate in names:
                icon_path = candidate

        requires = meta.get_all("Requires-Dist") or []
        sdk_spec = ""
        others = []
        for req in requires:
            if "extra ==" in req:
                continue
            name, spec = parse_requirement(req)
            if normalize_dist(name) == "bdh-sdk":
                sdk_spec = spec
            else:
                others.append(req.split(";", 1)[0].strip())

        hub_api = None
        if "api_version" in kw:
            try:
                hub_api = _literal(kw["api_version"], {"API_VERSION": None})
            except ValueError as exc:
                raise WheelError(f"{module_file}: PluginSpec api_version is not a literal") from exc
        if hub_api is None:
            # Unset (or API_VERSION): the app targets the API of the SDK it is
            # built against, so infer it from the minimum bdh-sdk it allows.
            min_sdk = _min_version(sdk_spec) if sdk_spec else None
            hub_api = 2 if min_sdk and min_sdk[:2] >= API2_MIN_SDK else 1

    return {
        "id": spec_id,
        "dist_name": meta["Name"],
        "version": meta["Version"],
        "summary": meta.get("Summary") or "",
        "title": title,
        "icon_path": icon_path,
        "requires_sdk": sdk_spec,
        "requires": others,
        "hub_api": hub_api,
        "manifest": manifest,
        "manifest_declared": declared,
    }
