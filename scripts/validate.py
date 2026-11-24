#!/usr/bin/env python3
"""Validate catalog.json.

Offline checks: the JSON Schema, plus what a schema can't say -- versions are
unique and newest first, wheel URLs point at the app's own repo and the tag
for that version, each repo holds one app id, image entries name
``ghcr.io/<org>/bdh-app-<their id>`` (org: blackdiamondlodge or its new name
blackdiamondhub), and a hub API 2+ version is
an image (a hub API 2+ wheel may only remain as a yanked entry), and a
pre-release version (aN/bN/rcN) declares its ``channel`` while a final
version is never beta.

``--base OLD.json`` also enforces immutability: a published version keeps its
wheel_url, sha256 and manifest and is never removed (set ``yanked``
instead). An image entry may be rebuilt: image, base, build, built_at and
attestation change together. A version is added in the channel it implies
(a pre-release is beta) and its channel may only change from beta to stable
(scripts/promote.py).

``--pr`` adds the rule for pull requests (against ``--base``): a PR changes
one app, unless it is the builder's base bump (every change is a rebuild of
an existing version onto one new base). New or widened permissions are
approved by the hub admin at install/update time, not here.

``--network`` downloads the wheels of new/changed versions (all of them
without ``--base``), checks their sha256, that the wheel's PluginSpec id and
version match the entry, and runs ``gh attestation verify`` for versions
that carry an attestation. For image entries it also verifies the image's
attestation (signer: the builder workflow), its app/version labels, and that
its layers start with its base's layers.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bdhcatalog as bc  # noqa: E402
import registry  # noqa: E402

# Fields of a published version that may still change.
MUTABLE = {"changelog", "yanked", "attestation"}


def check_schema(catalog):
    import jsonschema

    schema = json.loads(bc.SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"schema: {'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(catalog), key=lambda e: list(e.absolute_path))
    ]


def check_semantics(catalog):
    errors = []
    repos = {}
    for app_id, app in catalog.get("apps", {}).items():
        repo = app.get("repo", "")
        if repo in repos:
            errors.append(f"{app_id}: repo {repo} already holds app {repos[repo]!r}")
        repos[repo] = app_id
        icon = app.get("icon")
        if icon and not icon.startswith(f"https://raw.githubusercontent.com/{repo}/"):
            errors.append(f"{app_id}: icon is not from {repo}")

        seen = set()
        keys = []
        for v in app.get("versions", []):
            version = v.get("version", "")
            where = f"{app_id} {version}"
            if version in seen:
                errors.append(f"{where}: duplicate version")
            seen.add(version)
            try:
                keys.append(bc.version_key(version))
            except ValueError as exc:
                errors.append(f"{where}: {exc}")
                continue
            if bc.is_prerelease(version):
                if "channel" not in v:
                    errors.append(f"{where}: a pre-release version must declare its channel")
            elif bc.channel(v) == bc.BETA:
                errors.append(f"{where}: only pre-release versions (aN/bN/rcN) can be beta")
            url = v.get("wheel_url", "")
            prefix = f"{bc.release_url_prefix(repo)}v{version}/"
            if not url.startswith(prefix):
                errors.append(f"{where}: wheel_url must start with {prefix}")
            else:
                parts = unquote(url[len(prefix):]).split("-")
                if len(parts) < 2 or parts[1] != version:
                    errors.append(f"{where}: wheel filename is not for version {version}")
            if bc.is_image(v):
                name = v.get("image", "").split("@", 1)[0]
                if name not in bc.image_repos(app_id):
                    errors.append(f"{where}: image must be {bc.image_repo(app_id)}@sha256:<digest>, not {name}")
            else:
                att = v.get("attestation")
                if att and not att.get("url", "").startswith(tuple(
                        f"https://github.com/{bc.repo_with_org(repo, o)}/attestations/" for o in bc.ORGS)):
                    errors.append(f"{where}: attestation is not from {repo}")
                if v.get("hub_api", 1) >= 2 and not v.get("yanked"):
                    errors.append(f"{where}: hub API {v['hub_api']} versions must be kind image "
                                  "(built by .github/workflows/build.yml); yank this wheel entry")
        if keys != sorted(keys, reverse=True):
            errors.append(f"{app_id}: versions must be ordered newest first")
    return errors


def check_immutable(catalog, base):
    errors = []
    for app_id, old_app in base.get("apps", {}).items():
        app = catalog.get("apps", {}).get(app_id)
        if app is None:
            errors.append(f"{app_id}: app removed (yank its versions instead)")
            continue
        if app.get("repo") != old_app.get("repo"):
            errors.append(f"{app_id}: repo changed from {old_app.get('repo')}")
        new_versions = {v["version"]: v for v in app.get("versions", [])}
        for old in old_app.get("versions", []):
            new = new_versions.get(old["version"])
            where = f"{app_id} {old['version']}"
            if new is None:
                errors.append(f"{where}: version removed (set yanked instead)")
                continue
            if bc.is_image(old) != bc.is_image(new):
                errors.append(f"{where}: kind changed; published versions are immutable")
                continue
            mutable = MUTABLE | (bc.REBUILD_FIELDS if bc.is_image(old) else set())
            if bc.channel(old) != bc.channel(new) and \
                    (bc.channel(old), bc.channel(new)) != (bc.BETA, bc.STABLE):
                errors.append(f"{where}: channel changed from {bc.channel(old)} to {bc.channel(new)}; "
                              "a version may only be promoted from beta to stable")
            for field in set(old) | set(new):
                if field in mutable or field == "channel":
                    continue
                if old.get(field) != new.get(field):
                    errors.append(f"{where}: {field} changed; published versions are immutable")
            if bc.is_image(old):
                rebuilt = {f for f in bc.REBUILD_FIELDS if old.get(f) != new.get(f)}
                if rebuilt and "image" not in rebuilt:
                    errors.append(f"{where}: {', '.join(sorted(rebuilt))} changed without a new image")
            elif "attestation" in old and old["attestation"] != new.get("attestation"):
                errors.append(f"{where}: attestation changed or removed")
    for app_id, app in catalog.get("apps", {}).items():
        old_app = base.get("apps", {}).get(app_id, {})
        old_versions = {v["version"] for v in old_app.get("versions", [])}
        for v in app.get("versions", []):
            if v["version"] in old_versions:
                continue
            try:
                want = bc.channel_for(v["version"])
            except ValueError:
                continue
            if bc.channel(v) != want:
                errors.append(f"{app_id} {v['version']}: a new version is added as {want} "
                              "(promote it later with scripts/promote.py)")
    return errors


def is_base_bump(catalog, base):
    """Every change is a rebuild of an existing image version onto one base."""
    bases = set()
    for app_id, app in catalog.get("apps", {}).items():
        old_app = base.get("apps", {}).get(app_id)
        if old_app is None:
            return False
        if {k: v for k, v in app.items() if k != "versions"} != \
                {k: v for k, v in old_app.items() if k != "versions"}:
            return False
        old = {v["version"]: v for v in old_app.get("versions", [])}
        if set(old) != {v["version"] for v in app.get("versions", [])}:
            return False
        for v in app.get("versions", []):
            o = old[v["version"]]
            if o == v:
                continue
            if not bc.is_image(v) or {f for f in set(o) | set(v) if o.get(f) != v.get(f)} - bc.REBUILD_FIELDS:
                return False
            bases.add(v.get("base"))
    return len(bases) == 1


def check_pr(catalog, base):
    """Rules for a pull request against ``base`` (see the module docs)."""
    errors = []
    base = base or {"apps": {}}
    changed = sorted(app_id for app_id in set(catalog.get("apps", {})) | set(base.get("apps", {}))
                     if catalog["apps"].get(app_id) != base["apps"].get(app_id))
    if len(changed) > 1 and not is_base_bump(catalog, base):
        errors.append(f"this PR changes {len(changed)} apps ({', '.join(changed)}); "
                      "change one app per PR (only the builder's base bump may change several)")
    return errors


def check_image(app_id, v, reg):
    """The registry side of an image entry: labels and the base layer prefix."""
    try:
        config = reg.config(v["image"])
        base = reg.config(v["base"])
        registry.check_built_on(config, base)
    except registry.RegistryError as exc:
        return [f"{app_id} {v['version']}: {exc}"]
    errors = []
    lab = registry.labels(config)
    for key, want in (("org.blackdiamondlodge.bdh.app", app_id),
                      ("org.blackdiamondlodge.bdh.version", v["version"])):
        if lab.get(key) != want:
            errors.append(f"{app_id} {v['version']}: image label {key} is {lab.get(key)!r}, not {want!r}")
    return errors


def changed_versions(catalog, base):
    old = {
        (app_id, v["version"]): v
        for app_id, app in (base or {}).get("apps", {}).items()
        for v in app.get("versions", [])
    }
    for app_id, app in catalog.get("apps", {}).items():
        for v in app.get("versions", []):
            if base is None or old.get((app_id, v["version"])) != v:
                yield app_id, app, v


def gh_verify(subject, repo, signer):
    cmd = ["gh", "attestation", "verify", subject, "--repo", repo, "--signer-workflow", signer]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip()[-500:]
    return None


def attested_repo(repo, signer):
    """``repo`` under the org named by ``signer``.

    The signing certificate records the org at signing time, so an entry
    signed before the org rename verifies against the old org's repo name.
    """
    return bc.repo_with_org(repo, signer.split("/", 1)[0])


def check_network(catalog, base, reg=None):
    errors = []
    reg = reg or registry.Registry()
    with tempfile.TemporaryDirectory() as tmp:
        for app_id, app, v in changed_versions(catalog, base):
            where = f"{app_id} {v['version']}"
            if bc.is_image(v) and not v.get("yanked"):
                errors += check_image(app_id, v, reg)
                signer = v.get("attestation", {}).get("signer_workflow", bc.BUILDER_WORKFLOW)
                problem = gh_verify(f"oci://{v['image']}", attested_repo(bc.CATALOG_REPO, signer), signer)
                if problem:
                    errors.append(f"{where}: image attestation did not verify: {problem}")
                else:
                    print(f"{where}: image attestation verified")
            path = Path(tmp) / Path(unquote(urlparse(v["wheel_url"]).path)).name
            try:
                with urllib.request.urlopen(v["wheel_url"], timeout=60) as resp, open(path, "wb") as f:
                    while chunk := resp.read(1 << 20):
                        f.write(chunk)
            except OSError as exc:
                errors.append(f"{where}: download failed: {exc}")
                continue
            digest = bc.sha256_file(path)
            if digest != v["sha256"]:
                errors.append(f"{where}: wheel sha256 is {digest}, catalog says {v['sha256']}")
                continue
            try:
                info = bc.read_wheel(path)
            except bc.WheelError as exc:
                errors.append(f"{where}: {exc}")
                continue
            if info["id"] != app_id or info["version"] != v["version"]:
                errors.append(f"{where}: wheel is {info['id']} {info['version']}")
            att = v.get("attestation")
            if att and not bc.is_image(v):
                signer = att["signer_workflow"]
                problem = gh_verify(str(path), attested_repo(app["repo"], signer), signer)
                if problem:
                    errors.append(f"{where}: attestation did not verify: {problem}")
                else:
                    print(f"{where}: attestation verified")
            print(f"{where}: sha256 ok")
    return errors


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("catalog", nargs="?", default=str(bc.CATALOG))
    p.add_argument("--base", help="the catalog.json this change is based on")
    p.add_argument("--network", action="store_true", help="download and verify changed wheels and images")
    p.add_argument("--pr", action="store_true", help="apply the pull request rules against --base")
    args = p.parse_args(argv)

    catalog = bc.load_catalog(args.catalog)
    base = None
    if args.base and Path(args.base).exists() and Path(args.base).stat().st_size:
        base = bc.load_catalog(args.base)

    errors = check_schema(catalog)
    if not errors:
        errors += check_semantics(catalog)
    if not errors:
        text = Path(args.catalog).read_text(encoding="utf-8")
        if text != bc.render_catalog(json.loads(text)):
            errors.append("not in canonical form; run scripts/add_version.py or "
                          "python -c \"import sys; sys.path.insert(0, 'scripts'); import bdhcatalog as b; "
                          "b.dump_catalog(b.load_catalog())\"")
    if not errors and base is not None:
        errors += check_immutable(catalog, base)
    if not errors and args.pr:
        errors += check_pr(catalog, base)
    if not errors and args.network:
        errors += check_network(catalog, base)

    for err in errors:
        print(f"::error file=catalog.json::{err}" if "GITHUB_ACTIONS" in os.environ else err)
    if errors:
        print(f"{len(errors)} problem(s) found", file=sys.stderr)
        return 1
    apps = catalog["apps"]
    print(f"catalog ok: {len(apps)} apps, {sum(len(a['versions']) for a in apps.values())} versions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
