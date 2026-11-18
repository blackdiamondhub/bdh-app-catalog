#!/usr/bin/env python3
"""Promote a beta version to the stable channel in catalog.json.

A pre-release (e.g. 2.3.0b1) is added to the catalog as ``"channel": "beta"``,
so only beta hubs see it. Once it has proven itself there, promoting it flips
the entry to ``"stable"``: the same image (digest) then reaches every hub, no
rebuild. Run it through .github/workflows/promote.yml, which opens the PR:

    gh workflow run promote.yml -f app=weather -f version=2.3.0b1
    python scripts/promote.py weather 2.3.0b1          # locally
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bdhcatalog as bc  # noqa: E402


def promote(catalog, app_id, version):
    app = catalog["apps"].get(app_id)
    if app is None:
        raise SystemExit(f"no app {app_id!r} in the catalog")
    entry = next((v for v in app["versions"] if v["version"] == version.removeprefix("v")), None)
    if entry is None:
        raise SystemExit(f"{app_id} has no version {version!r}")
    if bc.channel(entry) != bc.BETA:
        raise SystemExit(f"{app_id} {entry['version']} is already {bc.channel(entry)}")
    if entry.get("yanked"):
        raise SystemExit(f"{app_id} {entry['version']} is yanked")
    entry["channel"] = bc.STABLE
    return entry


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("app_id")
    p.add_argument("version")
    p.add_argument("--catalog", default=str(bc.CATALOG))
    args = p.parse_args(argv)
    catalog = bc.load_catalog(args.catalog)
    entry = promote(catalog, args.app_id, args.version)
    bc.dump_catalog(catalog, args.catalog)
    print(f"promoted {args.app_id} {entry['version']} to stable")


if __name__ == "__main__":
    main()
