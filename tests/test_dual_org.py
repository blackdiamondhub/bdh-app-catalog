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
import validate  # noqa: E402
from test_images import catalog_with, image_entry, valid  # noqa: E402

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
        catalog = renamed(catalog_with(demo=[image_entry()]))
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
        self.assertNotIn(NEW + "/", text, "catalog.json should not name the new org yet")
        catalog = json.loads(text)
        self.assertEqual(valid(catalog), [])
        moved = renamed(catalog)
        self.assertNotIn(f"{OLD}/bdh-app-", json.dumps({k: [
            {f: x for f, x in v.items() if f != "attestation"} for v in a["versions"]] + [a["repo"]]
            for k, a in moved["apps"].items()}))
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


class OrgOverrideTest(unittest.TestCase):
    def _org(self, env):
        code = "import bdhcatalog as bc; print(bc.ORG, bc.IMAGE_PREFIX, bc.BUILDER_WORKFLOW)"
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False,
                              cwd=Path(bc.__file__).parent, env={**os.environ, **env})

    def test_default_is_unchanged_and_bdh_org_overrides(self):
        self.assertEqual(self._org({"BDH_ORG": ""}).stdout.split()[0], OLD)
        out = self._org({"BDH_ORG": NEW})
        self.assertEqual(out.stdout.split(), [NEW, f"ghcr.io/{NEW}/",
                                              f"{NEW}/bdh-app-catalog/.github/workflows/build.yml"])
        self.assertNotEqual(self._org({"BDH_ORG": "evil"}).returncode, 0)


if __name__ == "__main__":
    unittest.main()
