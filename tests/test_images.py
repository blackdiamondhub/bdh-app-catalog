import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bdhcatalog as bc  # noqa: E402
import builder  # noqa: E402
import registry  # noqa: E402
import validate  # noqa: E402

BASE1 = f"{bc.BASE_REPO}@sha256:{'1' * 64}"
BASE2 = f"{bc.BASE_REPO}@sha256:{'2' * 64}"


def image_entry(app_id="demo", version="2.0.0", digest="a", base=BASE1, **extra):
    repo = f"{bc.ORG}/bdh-app-{app_id}"
    entry = {
        "version": version,
        "kind": "image",
        "hub_api": 2,
        "image": f"{bc.image_repo(app_id)}@sha256:{digest * 64}",
        "base": base,
        "build": "gh-1.1",
        "wheel_url": f"https://github.com/{repo}/releases/download/v{version}/bdh_app_{app_id}-{version}-py3-none-any.whl",
        "sha256": "f" * 64,
        "permissions": ["alerts"],
        "network": "outbound",
        "lifecycle": "on_demand",
        "memory_mb": 96,
        "companions": [],
        "provides": [],
        "custom_base": False,
        "changelog": "",
        "released_at": "2026-01-01T00:00:00Z",
        "built_at": "2026-01-01T00:00:00Z",
        "attestation": {"url": f"https://github.com/{bc.CATALOG_REPO}/attestations/1",
                        "signer_workflow": bc.BUILDER_WORKFLOW},
    }
    entry.update(extra)
    return entry


def catalog_with(**apps):
    return {"schema": 1, "apps": {
        app_id: {"name": app_id.title(), "description": "", "repo": f"{bc.ORG}/bdh-app-{app_id}",
                 "versions": versions}
        for app_id, versions in apps.items()}}


def valid(catalog):
    return validate.check_schema(catalog) + validate.check_semantics(catalog)


class ImageEntryTest(unittest.TestCase):
    def test_image_entry_is_valid(self):
        self.assertEqual(valid(catalog_with(demo=[image_entry()])), [])

    def test_image_must_be_the_apps_own_digest_pinned_repo(self):
        other = catalog_with(demo=[image_entry(image=f"{bc.image_repo('other')}@sha256:{'a' * 64}")])
        self.assertTrue(any("image must be" in e for e in valid(other)))
        tag = catalog_with(demo=[image_entry(image=f"{bc.image_repo('demo')}:2.0.0")])
        self.assertTrue(validate.check_schema(tag))
        foreign = catalog_with(demo=[image_entry(image=f"docker.io/x/bdh-app-demo@sha256:{'a' * 64}")])
        self.assertTrue(validate.check_schema(foreign))

    def test_hub_api_2_wheel_must_be_yanked(self):
        wheel = image_entry()
        for k in ("kind", "image", "base", "build", "custom_base", "built_at", "attestation"):
            del wheel[k]
        wheel.update(requires_sdk=">=0.20", requires=[], manifest_declared=True)
        catalog = catalog_with(demo=[wheel])
        self.assertTrue(any("must be kind image" in e for e in valid(catalog)))
        wheel["yanked"] = "retired"
        self.assertEqual(valid(catalog), [])

    def test_image_entry_hub_api_at_least_2(self):
        self.assertTrue(validate.check_schema(catalog_with(demo=[image_entry(hub_api=1)])))

    def test_rebuild_may_change_image_fields_only(self):
        base = catalog_with(demo=[image_entry()])
        new = copy.deepcopy(base)
        new["apps"]["demo"]["versions"][0].update(image=f"{bc.image_repo('demo')}@sha256:{'b' * 64}",
                                                   base=BASE2, build="gh-2.1")
        self.assertEqual(validate.check_immutable(new, base), [])
        new["apps"]["demo"]["versions"][0]["permissions"] = ["alerts", "ha.write"]
        self.assertTrue(any("permissions changed" in e for e in validate.check_immutable(new, base)))
        same_image = copy.deepcopy(base)
        same_image["apps"]["demo"]["versions"][0]["base"] = BASE2
        self.assertTrue(any("without a new image" in e for e in validate.check_immutable(same_image, base)))


class PullRequestRulesTest(unittest.TestCase):
    def setUp(self):
        self.base = catalog_with(demo=[image_entry()], other=[image_entry("other")])

    def test_version_bump_needs_no_label(self):
        new = copy.deepcopy(self.base)
        new["apps"]["demo"]["versions"].insert(0, image_entry(version="2.1.0", digest="c"))
        self.assertEqual(validate.check_pr(new, self.base), [])

    def test_two_apps_in_one_pr_are_rejected(self):
        new = copy.deepcopy(self.base)
        new["apps"]["demo"]["versions"].insert(0, image_entry(version="2.1.0", digest="c"))
        new["apps"]["other"]["versions"].insert(0, image_entry("other", version="2.1.0", digest="c"))
        errors = validate.check_pr(new, self.base)
        self.assertTrue(any("changes 2 apps" in e for e in errors))

    def test_base_bump_may_touch_every_app(self):
        new = copy.deepcopy(self.base)
        for app in new["apps"].values():
            app["versions"][0].update(image=app["versions"][0]["image"][:-64] + "d" * 64,
                                      base=BASE2, build="gh-9.1")
        self.assertEqual(validate.check_pr(new, self.base), [])
        new["apps"]["other"]["versions"][0]["base"] = f"{bc.BASE_REPO}@sha256:{'3' * 64}"
        self.assertTrue(validate.check_pr(new, self.base))
        new["apps"]["other"]["versions"][0]["base"] = BASE2
        new["apps"]["other"]["versions"][0]["changelog"] = "sneaky"
        self.assertTrue(validate.check_pr(new, self.base))

    def test_manifest_changes_and_new_apps_need_no_label(self):
        for field, value in (("permissions", ["alerts", "ha.write"]), ("network", "lan"),
                             ("companions", ["go2rtc"]), ("custom_base", True)):
            with self.subTest(field=field):
                new = copy.deepcopy(self.base)
                new["apps"]["demo"]["versions"].insert(
                    0, image_entry(version="2.1.0", digest="c", **{field: value}))
                self.assertEqual(validate.check_pr(new, self.base), [])
        new = copy.deepcopy(self.base)
        new["apps"].update(catalog_with(third=[image_entry("third")])["apps"])
        self.assertEqual(validate.check_pr(new, self.base), [])


class RegistryCheckTest(unittest.TestCase):
    def test_layers_must_start_with_the_base(self):
        base = {"rootfs": {"diff_ids": ["sha256:a", "sha256:b"]}}
        registry.check_built_on({"rootfs": {"diff_ids": ["sha256:a", "sha256:b", "sha256:c"]}}, base)
        for bad in (["sha256:a"], ["sha256:x", "sha256:b", "sha256:c"]):
            with self.assertRaises(registry.RegistryError):
                registry.check_built_on({"rootfs": {"diff_ids": bad}}, base)

    def test_check_image_labels(self):
        configs = {
            "app": {"rootfs": {"diff_ids": ["sha256:a", "sha256:c"]},
                    "config": {"Labels": {"org.blackdiamondlodge.bdh.app": "demo",
                                          "org.blackdiamondlodge.bdh.version": "2.0.0"}}},
            "base": {"rootfs": {"diff_ids": ["sha256:a"]}, "config": {"Labels": {}}},
        }
        reg = SimpleNamespace(config=lambda ref: configs["base" if "runtime" in ref else "app"])
        self.assertEqual(validate.check_image("demo", image_entry(), reg), [])
        self.assertTrue(validate.check_image("demo", image_entry(version="2.0.1"), reg))


class BuilderTest(unittest.TestCase):
    def test_plan_new_releases_and_stale_bases(self):
        catalog = catalog_with(demo=[image_entry()], other=[image_entry("other", base=BASE2)])
        releases = {f"{bc.ORG}/bdh-app-demo": "v2.0.0", f"{bc.ORG}/bdh-app-new": "v1.0.0"}
        plan = builder.plan(catalog, BASE2, releases)
        self.assertEqual([(i["app"], i["tag"], i["reason"]) for i in plan],
                         [("new", "v1.0.0", "new"), ("demo", "v2.0.0", "rebuild")])
        self.assertEqual(len(builder.plan(catalog, BASE2, releases, rebuild_all=True)), 3)
        self.assertEqual([i["app"] for i in builder.plan(catalog, BASE2, releases, apps=["demo"])], ["demo"])
        self.assertEqual([i["app"] for i in builder.plan(catalog, BASE2, releases, apps=["new"])], ["new"])

    def test_plan_app_filter_normalises_names_and_rejects_unknown(self):
        catalog = catalog_with(demo=[image_entry()])
        releases = {f"{bc.ORG}/bdh-app-snow-report": "v1.0.0"}
        for name in ("snow_report", "snow-report", "bdh-app-snow-report", "Snow_Report"):
            self.assertEqual([i["repo"] for i in builder.plan(catalog, BASE2, releases, apps=[name])],
                             [f"{bc.ORG}/bdh-app-snow-report"], name)
        with self.assertRaisesRegex(ValueError, "nope"):
            builder.plan(catalog, BASE2, releases, apps=["snow_report", "nope"])

    def test_apply_new_version_and_rebuild(self):
        catalog = catalog_with(demo=[image_entry()])
        app = {"name": "Demo", "description": "A demo", "repo": f"{bc.ORG}/bdh-app-demo"}
        new = {"app_id": "demo", "app": app, "entry": image_entry(version="2.1.0", digest="c")}
        before = copy.deepcopy(catalog)
        rebuilds, news = builder.apply(catalog, [new])
        self.assertEqual((rebuilds, news), ([], ["demo"]))
        self.assertEqual([v["version"] for v in catalog["apps"]["demo"]["versions"]], ["2.1.0", "2.0.0"])
        self.assertEqual(catalog["apps"]["demo"]["description"], "A demo")
        self.assertEqual(valid(catalog) + validate.check_immutable(catalog, before), [])

        before = copy.deepcopy(catalog)
        rebuilt = {"app_id": "demo", "app": app,
                   "entry": image_entry(version="2.1.0", digest="e", base=BASE2, changelog="ignored")}
        self.assertEqual(builder.apply(catalog, [rebuilt]), (["demo"], []))
        v = catalog["apps"]["demo"]["versions"][0]
        self.assertEqual((v["base"], v["changelog"]), (BASE2, ""))
        self.assertEqual(validate.check_immutable(catalog, before) + validate.check_pr(catalog, before), [])
        out = builder.outputs(before, catalog, [rebuilt], ["demo"], [])
        self.assertEqual(out["branch"], f"builder/base-{'2' * 12}")

    def test_entry_from_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "bdh_app_demo-2.0.0-py3-none-any.whl").write_bytes(b"not really a wheel")
            (d / "bdh-app.json").write_text(json.dumps({
                "id": "demo", "distribution": "bdh-app-demo", "version": "2.0.0",
                "wheel": "bdh_app_demo-2.0.0-py3-none-any.whl", "custom_base": False,
                "manifest": {"id": "demo", "api_version": 2, "permissions": ["speakers", "alerts"],
                             "network": "lan", "lifecycle": "always", "memory_mb": 128,
                             "companions": ["go2rtc"], "provides": []}}))
            args = SimpleNamespace(artifacts=tmp, repo=f"{bc.ORG}/bdh-app-demo", tag="v2.0.0",
                                   image=f"{bc.image_repo('demo')}@sha256:{'a' * 64}", base=BASE1,
                                   build="gh-5.1", attestation_url=f"https://github.com/{bc.CATALOG_REPO}/attestations/7",
                                   changelog="hi", released_at="2026-01-01T00:00:00Z")
            item = builder.make_entry(args)
            self.assertEqual(item["entry"]["permissions"], ["alerts", "speakers"])
            self.assertEqual(item["app"]["name"], "Demo")
            self.assertEqual(valid(catalog_with(demo=[item["entry"]])), [])
            args.image = f"{bc.image_repo('other')}@sha256:{'a' * 64}"
            with self.assertRaises(SystemExit):
                builder.make_entry(args)


if __name__ == "__main__":
    unittest.main()
