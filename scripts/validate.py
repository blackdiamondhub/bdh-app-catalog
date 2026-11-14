#!/usr/bin/env python3
"""Validate catalog.json.

Offline checks: the JSON Schema, plus what a schema can't say -- versions are
unique and newest first, wheel URLs point at the app's own repo and the tag
for that version, and each repo holds one app id.

``--base OLD.json`` also enforces immutability: a published version keeps its
wheel_url and sha256 and is never removed (set ``yanked`` instead).

``--network`` downloads the wheels of new/changed versions (all of them
without ``--base``), checks their sha256, that the wheel's PluginSpec id and
version match the entry, and runs ``gh attestation verify`` for versions
that carry an attestation.
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
            url = v.get("wheel_url", "")
            prefix = f"{bc.release_url_prefix(repo)}v{version}/"
            if not url.startswith(prefix):
                errors.append(f"{where}: wheel_url must start with {prefix}")
            else:
                parts = unquote(url[len(prefix):]).split("-")
                if len(parts) < 2 or parts[1] != version:
                    errors.append(f"{where}: wheel filename is not for version {version}")
            att = v.get("attestation")
            if att and not att.get("url", "").startswith(f"https://github.com/{repo}/attestations/"):
                errors.append(f"{where}: attestation is not from {repo}")
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
            for field in set(old) | set(new):
                if field in MUTABLE:
                    continue
                if old.get(field) != new.get(field):
                    errors.append(f"{where}: {field} changed; published versions are immutable")
            if "attestation" in old and old["attestation"] != new.get("attestation"):
                errors.append(f"{where}: attestation changed or removed")
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


def check_network(catalog, base):
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        for app_id, app, v in changed_versions(catalog, base):
            where = f"{app_id} {v['version']}"
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
            if att:
                cmd = ["gh", "attestation", "verify", str(path), "--repo", app["repo"],
                       "--signer-workflow", att["signer_workflow"]]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    errors.append(f"{where}: attestation did not verify: "
                                  f"{(result.stderr or result.stdout).strip()[-500:]}")
                else:
                    print(f"{where}: attestation verified")
            print(f"{where}: sha256 ok")
    return errors


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("catalog", nargs="?", default=str(bc.CATALOG))
    p.add_argument("--base", help="the catalog.json this change is based on")
    p.add_argument("--network", action="store_true", help="download and verify changed wheels")
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
