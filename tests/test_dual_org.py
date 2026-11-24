"""The blackdiamondlodge -> blackdiamondhub org rename: both orgs validate."""
import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bdhcatalog as bc  # noqa: E402
import builder  # noqa: E402
import validate  # noqa: E402
from test_images import BASE1, BASE2, catalog_with, image_entry, valid  # noqa: E402

OLD, NEW = "blackdiamondlodge", "blackdiamondhub"


def renamed(value, old=OLD, new=NEW):
    """``value`` with every image/base/repo/icon/wheel ref moved to ``new``.

    Attestation records are left alone: they name the org that signed them.
    """
    if isinstance(value, dict):
        return {k: v if k == "attestation" else renamed(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [renamed(v, old, new) for v in value]
    if isinstance(value, str):
        return value.replace(f"/{old}/", f"/{new}/").replace(f"{old}/bdh-app-", f"{new}/bdh-app-")
    return value


def old_org(value):
    """``value`` entirely under the old org, attestations included (as published before the rename)."""
    return json.loads(json.dumps(value).replace(f"{NEW}/", f"{OLD}/"))


def new_org_entry(app_id="demo", **extra):
    entry = renamed(image_entry(app_id, **extra))
    entry["attestation"] = {"url": f"https://github.com/{NEW}/bdh-app-catalog/attestations/2",
                            "signer_workflow": f"{NEW}/bdh-app-catalog/.github/workflows/build.yml"}
    return entry


class DualOrgSchemaTest(unittest.TestCase):
    def test_new_org_image_base_repo_and_signer_validate(self):
        catalog = renamed(catalog_with(demo=[new_org_entry()]))
        app = catalog["apps"]["demo"]
        self.assertEqual(app["repo"], f"{NEW}/bdh-app-demo")
        self.assertTrue(app["versions"][0]["image"].startswith(f"ghcr.io/{NEW}/"))
        self.assertTrue(app["versions"][0]["base"].startswith(f"ghcr.io/{NEW}/"))
        self.assertEqual(valid(catalog), [])

    def test_new_org_wheel_signer_validates(self):
        wheel = self.wheel()
        catalog = renamed(catalog_with(demo=[wheel]))
        self.assertEqual(valid(catalog), [])
        wheel["attestation"] = {"url": f"https://github.com/{OLD}/bdh-app-demo/attestations/1",
                                "signer_workflow": f"{OLD}/bdh-sdk/.github/workflows/app-release.yml"}
        self.assertEqual(valid(renamed(catalog_with(demo=[wheel]))), [])
        wheel["attestation"]["url"] = f"https://github.com/{OLD}/bdh-app-other/attestations/1"
        self.assertTrue(any("attestation is not from" in e for e in valid(renamed(catalog_with(demo=[wheel])))))

    @staticmethod
    def wheel():
        return {"version": "1.0.0", "hub_api": 1, "requires_sdk": ">=0.16,<1",
                "wheel_url": f"https://github.com/{NEW}/bdh-app-demo/releases/download/"
                             "v1.0.0/bdh_app_demo-1.0.0-py3-none-any.whl",
                "sha256": "f" * 64, "permissions": [], "network": "outbound",
                "lifecycle": "on_demand", "memory_mb": 96, "companions": [], "provides": [],
                "requires": [], "manifest_declared": True, "changelog": "",
                "released_at": "2026-01-01T00:00:00Z",
                "attestation": {"url": f"https://github.com/{NEW}/bdh-app-demo/attestations/3",
                                "signer_workflow": f"{NEW}/bdh-sdk/.github/workflows/app-release.yml"}}

    def test_new_org_app_keeps_old_org_attestations(self):
        catalog = renamed(old_org(catalog_with(demo=[image_entry()])))
        self.assertEqual(catalog["apps"]["demo"]["versions"][0]["attestation"]["signer_workflow"],
                         f"{OLD}/bdh-app-catalog/.github/workflows/build.yml")
        self.assertEqual(valid(catalog), [])

    def test_foreign_org_is_rejected(self):
        catalog = renamed(catalog_with(demo=[new_org_entry()]))
        for field, value in (("repo", "evil/bdh-app-demo"),):
            with self.subTest(field=field):
                bad = copy.deepcopy(catalog)
                bad["apps"]["demo"][field] = value
                self.assertTrue(validate.check_schema(bad))
        for field, value in (
                ("image", f"ghcr.io/evil/bdh-app-demo@sha256:{'a' * 64}"),
                ("base", f"ghcr.io/evil/bdh-app-runtime@sha256:{'1' * 64}"),
                ("wheel_url", ("https://github.com/evil/bdh-app-demo/releases/download/"
                               "v2.0.0/bdh_app_demo-2.0.0-py3-none-any.whl")),
                ("attestation", {"url": "https://github.com/evil/bdh-app-catalog/attestations/1",
                                 "signer_workflow": f"{NEW}/bdh-app-catalog/.github/workflows/build.yml"}),
                ("attestation", {"url": f"https://github.com/{NEW}/bdh-app-catalog/attestations/1",
                                 "signer_workflow": "evil/bdh-app-catalog/.github/workflows/build.yml"})):
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(catalog)
                bad["apps"]["demo"]["versions"][0][field] = value
                self.assertTrue(validate.check_schema(bad))

    def test_published_catalog_validates_and_after_rewrite_to_new_org(self):
        text = bc.CATALOG.read_text(encoding="utf-8")
        catalog = json.loads(text)
        self.assertEqual(valid(catalog), [])
        moved = renamed(catalog)
        self.assertNotIn(f"{OLD}/bdh-app-", json.dumps({k: [
            {f: x for f, x in v.items() if f != "attestation"} for v in a["versions"]] + [a["repo"]]
            for k, a in moved["apps"].items()}))
        if f"ghcr.io/{OLD}/" in text:
            self.assertNotEqual(moved, catalog)
        self.assertEqual(valid(moved), [])

    def test_semantics_still_bind_the_image_to_the_app(self):
        other = renamed(catalog_with(demo=[new_org_entry(image=f"ghcr.io/{NEW}/bdh-app-other@sha256:{'a' * 64}")]))
        self.assertTrue(any("image must be" in e for e in valid(other)))


class DualOrgVerifyTest(unittest.TestCase):
    def test_attestations_verify_against_the_signing_org(self):
        for signer in bc.BUILDER_WORKFLOWS + bc.SIGNER_WORKFLOWS:
            org = signer.split("/")[0]
            self.assertEqual(validate.attested_repo(f"{NEW}/bdh-app-catalog", signer),
                             f"{org}/bdh-app-catalog")
            self.assertEqual(validate.attested_repo(f"{OLD}/bdh-app-demo", signer),
                             f"{org}/bdh-app-demo")

    def test_network_check_passes_each_entrys_signer(self):
        entry = new_org_entry()
        entry["wheel_url"] = "file:///nonexistent.whl"
        calls = []
        reg = mock.Mock()
        with mock.patch.object(validate, "gh_verify", lambda s, r, w: calls.append((r, w))), \
                mock.patch.object(validate, "check_image", lambda *a: []):
            validate.check_network(catalog_with(demo=[entry]), None, reg)
        self.assertEqual(calls, [(f"{NEW}/bdh-app-catalog",
                                  f"{NEW}/bdh-app-catalog/.github/workflows/build.yml")])


class MixedOrgTest(unittest.TestCase):
    """Between the org rename and the catalog rewrite, the catalog names the
    old org while the builder and new releases use the new one."""

    def setUp(self):
        self.old = old_org(catalog_with(demo=[image_entry()], other=[image_entry("other")]))
        self.releases = {f"{NEW}/bdh-app-demo": "v2.0.0", f"{NEW}/bdh-app-other": "v2.0.0"}

    def test_plan_matches_apps_across_orgs(self):
        self.assertEqual(builder.plan(self.old, BASE1, self.releases), [])
        plan = builder.plan(self.old, BASE2, self.releases)
        self.assertEqual(sorted((i["app"], i["repo"], i["reason"]) for i in plan),
                         [("demo", f"{NEW}/bdh-app-demo", "rebuild"),
                          ("other", f"{NEW}/bdh-app-other", "rebuild")])
        new = builder.plan(self.old, BASE1, {**self.releases, f"{NEW}/bdh-app-demo": "v2.1.0"})
        self.assertEqual([(i["app"], i["reason"]) for i in new], [("demo", "new")])

    def test_new_org_version_lands_on_old_org_app(self):
        catalog = copy.deepcopy(self.old)
        app = {"name": "Demo", "description": "", "repo": f"{NEW}/bdh-app-demo"}
        entry = image_entry(version="2.1.0", digest="c")
        self.assertEqual(builder.apply(catalog, [{"app_id": "demo", "app": app, "entry": entry}]),
                         ([], ["demo"]))
        self.assertEqual(catalog["apps"]["demo"]["repo"], f"{OLD}/bdh-app-demo")
        self.assertEqual(valid(catalog) + validate.check_immutable(catalog, self.old)
                         + validate.check_pr(catalog, self.old), [])

    def test_org_rewrite_pr_may_touch_every_app(self):
        moved = renamed(self.old)
        self.assertEqual(valid(moved) + validate.check_immutable(moved, self.old)
                         + validate.check_pr(moved, self.old), [])
        sneaky = copy.deepcopy(moved)
        sneaky["apps"]["demo"]["versions"][0]["sha256"] = "e" * 64
        self.assertTrue(any("sha256 changed" in e for e in validate.check_immutable(sneaky, self.old)))
        self.assertTrue(any("changes 2 apps" in e for e in validate.check_pr(sneaky, self.old)))
        evil = json.loads(json.dumps(self.old).replace(f"{OLD}/bdh-app-demo", "evil/bdh-app-demo"))
        self.assertTrue(validate.check_immutable(evil, self.old) or validate.check_schema(evil))

    def test_org_rewrite_is_one_way_and_keeps_attestations(self):
        moved = renamed(self.old)
        # Moving back to the old (claimable) org name is a change.
        self.assertTrue(validate.check_immutable(self.old, moved))
        self.assertTrue(any("changes 2 apps" in e for e in validate.check_pr(self.old, moved)))
        # Attestations name the org that signed them; an org-only edit is a change.
        att = json.loads(json.dumps(self.old).replace(f"{OLD}/bdh-app-catalog", f"{NEW}/bdh-app-catalog"))
        self.assertNotEqual(att, self.old)
        self.assertTrue(any("attestation" in e for e in validate.check_immutable(att, self.old)))
        # Only a whole old-org path segment moves.
        self.assertFalse(bc.org_moved(f"x{OLD}/a", f"x{NEW}/a"))
        self.assertTrue(bc.org_moved(f"ghcr.io/{OLD}/a", f"ghcr.io/{NEW}/a"))

    def test_one_repo_is_one_app_under_either_org(self):
        catalog = copy.deepcopy(self.old)
        catalog["apps"]["other"]["repo"] = f"{NEW}/bdh-app-demo"
        self.assertTrue(any("already holds" in e for e in validate.check_semantics(catalog)))


class OrgOverrideTest(unittest.TestCase):
    def _org(self, env):
        code = "import bdhcatalog as bc; print(bc.ORG, bc.IMAGE_PREFIX, bc.BUILDER_WORKFLOW)"
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False,
                              cwd=Path(bc.__file__).parent, env={**os.environ, **env})

    def test_default_is_the_new_org_and_bdh_org_overrides(self):
        out = self._org({"BDH_ORG": ""})
        self.assertEqual(out.stdout.split(), [NEW, f"ghcr.io/{NEW}/",
                                              f"{NEW}/bdh-app-catalog/.github/workflows/build.yml"])
        self.assertEqual(self._org({"BDH_ORG": OLD}).stdout.split()[0], OLD)
        self.assertNotEqual(self._org({"BDH_ORG": "evil"}).returncode, 0)


if __name__ == "__main__":
    unittest.main()
