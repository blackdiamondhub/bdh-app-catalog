#!/usr/bin/env python3
"""Add (or update) one hub API 1 app version in catalog.json from a built wheel.

Hub API 2+ apps ship as images built by .github/workflows/build.yml (see
scripts/builder.py); this script refuses them.

    python scripts/add_version.py --wheel dist/bdh_app_x-1.2.0-py3-none-any.whl \
        --repo blackdiamondhub/bdh-app-x --tag v1.2.0 \
        --changelog "Fix the thing" --attestation-url https://github.com/...

The plugin id, hub_api and container manifest are read from the wheel's
PluginSpec without importing it. ``--manifest file.json`` overrides them with
``PluginSpec.manifest()`` output for specs that aren't plain literals.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bdhcatalog as bc  # noqa: E402


DESCRIPTION_PREFIX = "Black Diamond Hub app:"


def _description(summary):
    summary = summary.strip()
    if summary.startswith(DESCRIPTION_PREFIX):
        summary = summary[len(DESCRIPTION_PREFIX):].strip()
        summary = summary[:1].upper() + summary[1:]
    return summary


def build_entry(info, args, sha256, url):
    manifest = dict(info["manifest"])
    declared = bool(info["manifest_declared"])
    hub_api = info["hub_api"]
    if args.manifest:
        override = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if override.get("id", info["id"]) != info["id"]:
            raise SystemExit(f"--manifest id {override['id']!r} != wheel id {info['id']!r}")
        for field in (*bc.MANIFEST_FIELDS, "provides"):
            if field in override:
                manifest[field] = override[field]
        if "api_version" in override:
            hub_api = override["api_version"]
        declared = True
    if hub_api >= 2:
        raise SystemExit(f"{info['id']} {info['version']} is hub API {hub_api}: hub API 2+ apps are images; "
                         "run the builder (gh workflow run build.yml -f apps=<id>)")
    manifest["permissions"] = sorted(set(manifest["permissions"]))
    manifest["provides"] = sorted(set(manifest["provides"]))

    entry = {
        "version": info["version"],
        "channel": bc.channel_for(info["version"]),
        "wheel_url": url,
        "sha256": sha256,
        "hub_api": hub_api,
        "requires_sdk": info["requires_sdk"],
        "requires": info["requires"],
        **{k: manifest[k] for k in (*bc.MANIFEST_FIELDS, "provides")},
        "manifest_declared": declared,
        "changelog": args.changelog or "",
        "released_at": args.released_at,
    }
    if args.attestation_url:
        entry["attestation"] = {
            "url": args.attestation_url,
            "signer_workflow": args.signer_workflow,
        }
    return entry


def add_version(args):
    wheel = Path(args.wheel)
    info = bc.read_wheel(wheel)
    version = info["version"]
    tag = args.tag or f"v{version}"
    if tag != f"v{version}":
        raise SystemExit(f"tag {tag!r} does not match wheel version {version!r}")
    owner = args.repo.split("/", 1)[0]
    if owner not in bc.ORGS:
        raise SystemExit(f"repo must be under github.com/{' or '.join(bc.ORGS)}")
    url = args.url or f"{bc.release_url_prefix(args.repo)}{tag}/{wheel.name}"
    sha256 = bc.sha256_file(wheel)

    catalog = bc.load_catalog(args.catalog) if Path(args.catalog).exists() else {
        "schema": bc.SCHEMA_VERSION, "apps": {}}
    apps = catalog["apps"]
    app = apps.get(info["id"])
    if app is None:
        app = apps[info["id"]] = {"name": "", "description": "", "repo": args.repo, "versions": []}
    elif not bc.same_repo(app["repo"], args.repo):
        raise SystemExit(f"app id {info['id']!r} belongs to {app['repo']}, not {args.repo}")

    entry = build_entry(info, args, sha256, url)
    existing = next((v for v in app["versions"] if v["version"] == version), None)
    if existing is not None:
        if existing["sha256"] != sha256 and not args.replace:
            raise SystemExit(
                f"{info['id']} {version} is already in the catalog with a different sha256; "
                "versions are immutable (bump the version, or pass --replace)")
        if not args.attestation_url and "attestation" in existing:
            entry["attestation"] = existing["attestation"]
        entry["channel"] = bc.channel(existing)
        app["versions"].remove(existing)
    app["versions"].append(entry)

    newest = max(app["versions"], key=lambda v: bc.version_key(v["version"]))
    if newest is entry:
        app["name"] = args.name or info["title"] or app["name"] or info["id"].replace("_", " ").title()
        app["description"] = args.description or _description(info["summary"]) or app["description"]
        if info["icon_path"]:
            app["icon"] = f"https://raw.githubusercontent.com/{args.repo}/{tag}/{info['icon_path']}"
        # Keep keys in a stable, readable order.
        apps[info["id"]] = {k: app[k] for k in ("name", "description", "icon", "repo", "versions") if k in app}

    bc.dump_catalog(catalog, args.catalog)
    action = "updated" if existing is not None else "added"
    print(f"{action} {info['id']} {version} sha256={sha256}")
    return info["id"], version


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wheel", required=True)
    p.add_argument("--repo", required=True, help="owner/name of the app repo")
    p.add_argument("--tag", help="release tag (default v<version>)")
    p.add_argument("--url", help="wheel URL (default: the release asset URL)")
    p.add_argument("--released-at", default=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    p.add_argument("--changelog", default="")
    p.add_argument("--name", help="display name (default: first tile title)")
    p.add_argument("--description", help="description (default: wheel Summary)")
    p.add_argument("--attestation-url")
    p.add_argument("--signer-workflow", default=bc.SIGNER_WORKFLOW)
    p.add_argument("--manifest", help="JSON file with PluginSpec.manifest() output")
    p.add_argument("--replace", action="store_true", help="allow replacing a version's wheel")
    p.add_argument("--catalog", default=str(bc.CATALOG))
    p.add_argument("--github-output", help="append app_id/version lines to this file")
    args = p.parse_args(argv)
    try:
        app_id, version = add_version(args)
    except bc.WheelError as exc:
        raise SystemExit(str(exc))
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"app_id={app_id}\nversion={version}\n")


if __name__ == "__main__":
    main()
