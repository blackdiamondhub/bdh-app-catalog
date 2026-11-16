#!/usr/bin/env python3
"""The catalog side of the platform image builder (.github/workflows/build.yml).

    builder.py plan [--apps a,b] [--rebuild-all]
        Decide what to build. Prints {"base": ..., "include": [...]} for a
        job matrix: the latest release of each app repo that publishes
        bdh-app.json and isn't in the catalog yet ("new"), plus the newest
        image version of each app whose base is not the current
        bdh-app-runtime:hub-api-<N> ("rebuild"). --rebuild-all rebuilds
        every app's newest image version.

    builder.py entry --artifacts DIR --repo R --tag T --image REF@DIGEST \\
            --base REF@DIGEST --build ID --attestation-url URL -o entry.json
        The catalog entry for one built image (after build_app.sh merged the
        manifest into DIR/bdh-app.json).

    builder.py apply ENTRY.json...
        Put built entries into catalog.json. A rebuild replaces the image
        fields of its version; a new version is added (and the app is created
        if it is new). Prints the branch name, PR title and whether a human
        review is needed as GitHub outputs.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bdhcatalog as bc  # noqa: E402
import registry  # noqa: E402

HUB_API = 2
APP_CONFIG = "bdh-app.json"
ENTRY_KEYS = ("version", "kind", "hub_api", "image", "base", "build", "wheel_url", "sha256",
              "permissions", "network", "lifecycle", "memory_mb", "companions", "provides",
              "custom_base", "changelog", "released_at", "built_at", "attestation", "yanked")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def app_repos():
    out = gh("repo", "list", bc.ORG, "--limit", "200", "--no-archived", "--json", "name")
    return sorted(f"{bc.ORG}/{r['name']}" for r in json.loads(out)
                  if r["name"].startswith("bdh-app-") and r["name"] != "bdh-app-catalog")


def latest_release(repo):
    try:
        rel = json.loads(gh("release", "view", "-R", repo, "--json", "tagName,assets"))
    except subprocess.CalledProcessError:
        return None
    names = {a["name"] for a in rel["assets"]}
    if APP_CONFIG not in names:
        return None
    return rel["tagName"]


def _norm(name):
    return name.strip().lower().replace("-", "_")


def plan(catalog, base, releases, apps=None, rebuild_all=False):
    """``releases`` maps repo -> latest tag that publishes bdh-app.json."""
    include = []
    by_repo = {app["repo"]: (app_id, app) for app_id, app in catalog["apps"].items()}
    wanted = {_norm(a) for a in apps or ()}

    def names(repo, app_id):
        short = repo.split("/", 1)[1]
        return {_norm(app_id), _norm(short), _norm(short.removeprefix("bdh-app-"))}

    known = set()
    for repo in set(releases) | set(by_repo):
        known |= names(repo, by_repo.get(repo, (repo.split("bdh-app-", 1)[-1],))[0])
    unknown = sorted(wanted - known)
    if unknown:
        raise ValueError(f"unknown app(s): {', '.join(unknown)}")

    def selected(repo, app_id=None):
        return not wanted or bool(wanted & names(repo, app_id or ""))

    for repo, tag in sorted(releases.items()):
        app_id, app = by_repo.get(repo, (None, None))
        app_id = app_id or repo.split("bdh-app-", 1)[1]
        if not selected(repo, app_id):
            continue
        version = tag.removeprefix("v")
        if app and any(v["version"] == version for v in app["versions"]):
            continue
        include.append({"repo": repo, "tag": tag, "app": app_id, "reason": "new"})
    planned = {i["repo"] for i in include}
    for app_id, app in sorted(catalog["apps"].items()):
        if app["repo"] in planned or not selected(app["repo"], app_id):
            continue
        images = [v for v in app["versions"] if bc.is_image(v) and not v.get("yanked")]
        if not images:
            continue
        newest = max(images, key=lambda v: bc.version_key(v["version"]))
        if rebuild_all or newest["base"] != base:
            include.append({"repo": app["repo"], "tag": f"v{newest['version']}", "app": app_id,
                            "reason": "rebuild"})
    return include


def current_base(reg=None):
    reg = reg or registry.Registry()
    return f"{bc.BASE_REPO}@{reg.digest(f'{bc.BASE_REPO}:hub-api-{HUB_API}')}"


def app_info(wheel):
    """Display name, description and icon from the wheel (best effort)."""
    try:
        info = bc.read_wheel(wheel)
    except (bc.WheelError, zipfile.BadZipFile) as exc:
        print(f"warning: {exc}", file=sys.stderr)
        return {}
    return info


def make_entry(args):
    d = Path(args.artifacts)
    config = json.loads((d / APP_CONFIG).read_text(encoding="utf-8"))
    manifest = config.get("manifest")
    if not manifest:
        raise SystemExit(f"{APP_CONFIG} has no manifest; run build_app.sh first")
    if args.tag != f"v{config['version']}":
        raise SystemExit(f"tag {args.tag} does not match version {config['version']}")
    wheel = d / config["wheel"]
    if not re.fullmatch(re.escape(bc.image_repo(config["id"])) + r"@sha256:[0-9a-f]{64}", args.image):
        raise SystemExit(f"image {args.image} is not {bc.image_repo(config['id'])}@sha256:...")
    info = app_info(wheel)
    entry = {
        "version": config["version"],
        "kind": "image",
        "hub_api": manifest.get("api_version", HUB_API),
        "image": args.image,
        "base": args.base,
        "build": args.build,
        "wheel_url": f"{bc.release_url_prefix(args.repo)}{args.tag}/{wheel.name}",
        "sha256": bc.sha256_file(wheel),
        "permissions": sorted(set(manifest.get("permissions", []))),
        "network": manifest.get("network", bc.DEFAULTS["network"]),
        "lifecycle": manifest.get("lifecycle", bc.DEFAULTS["lifecycle"]),
        "memory_mb": manifest.get("memory_mb", bc.DEFAULT_MEMORY_MB),
        "companions": manifest.get("companions", []),
        "provides": sorted(set(manifest.get("provides", []))),
        "custom_base": bool(config.get("custom_base")),
        "changelog": args.changelog,
        "released_at": args.released_at,
        "built_at": now(),
        "attestation": {"url": args.attestation_url, "signer_workflow": bc.BUILDER_WORKFLOW},
    }
    app = {
        "name": info.get("title") or config["id"].replace("_", " ").title(),
        "description": _description(info.get("summary") or ""),
        "repo": args.repo,
    }
    if info.get("icon_path"):
        app["icon"] = f"https://raw.githubusercontent.com/{args.repo}/{args.tag}/{info['icon_path']}"
    return {"app_id": config["id"], "app": app, "entry": entry}


def _description(summary):
    prefix = "Black Diamond Hub app:"
    summary = summary.strip()
    if summary.startswith(prefix):
        summary = summary[len(prefix):].strip()
        summary = summary[:1].upper() + summary[1:]
    return summary


def apply(catalog, built):
    """Merge built entries into ``catalog``; returns (kind, changed apps)."""
    rebuilds, news = [], []
    for item in built:
        app_id, entry = item["app_id"], item["entry"]
        app = catalog["apps"].get(app_id)
        if app is None:
            app = catalog["apps"][app_id] = {**item["app"], "versions": []}
        elif app["repo"] != item["app"]["repo"]:
            raise SystemExit(f"app id {app_id!r} belongs to {app['repo']}, not {item['app']['repo']}")
        existing = next((v for v in app["versions"] if v["version"] == entry["version"]), None)
        if existing is not None:
            if not bc.is_image(existing):
                raise SystemExit(f"{app_id} {entry['version']} is a wheel entry; bump the version")
            for field in bc.REBUILD_FIELDS:
                existing[field] = entry[field]
            rebuilds.append(app_id)
        else:
            app["versions"].append(entry)
            news.append(app_id)
            newest = max(app["versions"], key=lambda v: bc.version_key(v["version"]))
            if newest is entry:
                for key in ("name", "description", "icon"):
                    if item["app"].get(key):
                        app[key] = item["app"][key]
        app["versions"].sort(key=lambda v: bc.version_key(v["version"]), reverse=True)
        app["versions"] = [{k: v[k] for k in ENTRY_KEYS if k in v} if bc.is_image(v) else v
                           for v in app["versions"]]
        catalog["apps"][app_id] = {k: app[k] for k in ("name", "description", "icon", "repo", "versions")
                                   if k in app}
    return rebuilds, news


def outputs(base_catalog, catalog, built, rebuilds, news):
    entries = {i["app_id"]: i["entry"] for i in built}
    if news:
        app_id = news[0]
        branch = f"builder/{app_id}-{entries[app_id]['version']}"
        title = f"{app_id} {entries[app_id]['version']}"
    else:
        digest = next(iter(entries.values()))["base"].rsplit(":", 1)[1][:12]
        branch = f"builder/base-{digest}"
        title = f"Rebuild {', '.join(sorted(rebuilds))} on base {digest}"
    return {"branch": branch, "title": title}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--apps", default="", help="comma-separated app ids or repo names (- and _ are interchangeable)")
    pl.add_argument("--rebuild-all", action="store_true")
    pl.add_argument("--catalog", default=str(bc.CATALOG))
    en = sub.add_parser("entry")
    en.add_argument("--artifacts", required=True)
    en.add_argument("--repo", required=True)
    en.add_argument("--tag", required=True)
    en.add_argument("--image", required=True)
    en.add_argument("--base", required=True)
    en.add_argument("--build", required=True)
    en.add_argument("--attestation-url", required=True)
    en.add_argument("--changelog", default="")
    en.add_argument("--released-at", default=now())
    en.add_argument("-o", "--output", required=True)
    ap = sub.add_parser("apply")
    ap.add_argument("entries", nargs="+")
    ap.add_argument("--catalog", default=str(bc.CATALOG))
    ap.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = p.parse_args(argv)

    if args.cmd == "plan":
        catalog = bc.load_catalog(args.catalog)
        base = current_base()
        releases = {}
        for repo in app_repos():
            tag = latest_release(repo)
            if tag:
                releases[repo] = tag
        apps = [a.strip() for a in args.apps.split(",") if a.strip()]
        include = plan(catalog, base, releases, apps, args.rebuild_all)
        print(json.dumps({"base": base, "include": include}))
    elif args.cmd == "entry":
        Path(args.output).write_text(json.dumps(make_entry(args), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        built = [json.loads(Path(f).read_text(encoding="utf-8")) for f in args.entries]
        catalog = bc.load_catalog(args.catalog)
        base_catalog = json.loads(json.dumps(catalog))
        rebuilds, news = apply(catalog, built)
        bc.dump_catalog(catalog, args.catalog)
        out = outputs(base_catalog, catalog, built, rebuilds, news)
        for k, v in out.items():
            print(f"{k}={v}")
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as f:
                f.writelines(f"{k}={v}\n" for k, v in out.items())


if __name__ == "__main__":
    main()
