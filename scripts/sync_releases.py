#!/usr/bin/env python3
"""Add the latest GitHub releases of each app repo to catalog.json.

Both the newest stable release and the newest pre-release (a beta, e.g.
v2.3.0b1, published as a GitHub pre-release) are considered.

Hub API 1 releases (a wheel only) are added with add_version.py. Releases
that publish bdh-app.json (hub API 2+, built as images) are handed to the
builder instead: this dispatches .github/workflows/build.yml for them (pass
--no-dispatch to just list them). Needs the ``gh`` CLI.

    python scripts/sync_releases.py                      # every bdh-app-* repo
    python scripts/sync_releases.py bdh-app-weather      # just one
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_version  # noqa: E402
import bdhcatalog as bc  # noqa: E402
from builder import newest_per_channel  # noqa: E402


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def app_repos():
    out = gh("repo", "list", bc.ORG, "--limit", "200", "--no-archived", "--json", "name")
    return sorted(r["name"] for r in json.loads(out) if r["name"].startswith("bdh-app-")
                  and r["name"] != "bdh-app-catalog")


def sync(repo_name, catalog_path, dispatch=True):
    repo = f"{bc.ORG}/{repo_name}"
    try:
        rels = json.loads(gh("release", "list", "-R", repo, "--limit", "100",
                             "--json", "tagName,isDraft"))
    except subprocess.CalledProcessError:
        rels = []
    tags = newest_per_channel(r["tagName"] for r in rels if not r.get("isDraft"))
    if not tags:
        print(f"{repo}: no release, skipped")
        return
    for tag in tags:
        rel = json.loads(gh("release", "view", tag, "-R", repo, "--json", "tagName,publishedAt,assets"))
        sync_release(repo, rel, catalog_path, dispatch)


def sync_release(repo, rel, catalog_path, dispatch):
    repo_name = repo.split("/", 1)[1]
    if any(a["name"] == "bdh-app.json" for a in rel["assets"]):
        catalog = bc.load_catalog(catalog_path) if Path(catalog_path).exists() else {"apps": {}}
        version = rel["tagName"].removeprefix("v")
        if any(bc.same_repo(app["repo"], repo) and any(v["version"] == version for v in app["versions"])
               for app in catalog["apps"].values()):
            print(f"{repo} {rel['tagName']}: already in the catalog")
        elif dispatch:
            gh("workflow", "run", "build.yml", "-R", bc.CATALOG_REPO, "-f", f"apps={repo_name}")
            print(f"{repo} {rel['tagName']}: image build dispatched")
        else:
            print(f"{repo} {rel['tagName']}: needs an image build")
        return
    wheels = [a for a in rel["assets"] if a["name"].endswith(".whl")]
    if len(wheels) != 1:
        print(f"{repo} {rel['tagName']}: expected one wheel asset, found {len(wheels)}; skipped")
        return
    catalog = bc.load_catalog(catalog_path) if Path(catalog_path).exists() else {"apps": {}}
    digest = wheels[0].get("digest", "").removeprefix("sha256:")
    for app in catalog["apps"].values():
        if bc.same_repo(app["repo"], repo) and any(v["sha256"] == digest for v in app["versions"]):
            print(f"{repo} {rel['tagName']}: already in the catalog")
            return

    subject = gh("api", f"repos/{repo}/commits/{rel['tagName']}", "--jq", ".commit.message")
    changelog = subject.strip().splitlines()[0] if subject.strip() else ""
    with tempfile.TemporaryDirectory() as tmp:
        gh("release", "download", rel["tagName"], "-R", repo, "-p", "*.whl", "-D", tmp)
        wheel = next(Path(tmp).glob("*.whl"))
        add_version.main([
            "--wheel", str(wheel), "--repo", repo, "--tag", rel["tagName"],
            "--released-at", rel["publishedAt"].replace(".000", ""),
            "--changelog", changelog, "--catalog", catalog_path,
        ])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("repos", nargs="*", help="app repo names (default: all bdh-app-* repos)")
    p.add_argument("--catalog", default=str(bc.CATALOG))
    p.add_argument("--no-dispatch", action="store_true", help="don't start image builds")
    args = p.parse_args(argv)
    for name in args.repos or app_repos():
        sync(name, args.catalog, dispatch=not args.no_dispatch)


if __name__ == "__main__":
    main()
