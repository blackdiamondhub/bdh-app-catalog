import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import add_version  # noqa: E402
import bdhcatalog as bc  # noqa: E402
import validate  # noqa: E402

REPO = "blackdiamondlodge/bdh-app-demo"

PLUGIN = '''\
from bdh_sdk import PluginSpec, Tile
from bdh_sdk.plugins import NETWORK_LAN
from bdh_sdk.provides import STATS

spec = PluginSpec(
    id="demo",
    app="demo",
    tiles=(Tile(title="Demo", url_name="demo", icon="demo/hub/icon.svg"),),
    provides={STATS: "demo.provides.stats", "season": "demo.provides.season"},
    permissions=("ha.read:sensor.*", "alerts", "alerts"),
    network=NETWORK_LAN,
    memory_mb=128,
    companions=("go2rtc",),
)
'''

PLAIN_PLUGIN = 'from bdh_sdk import PluginSpec\nspec = PluginSpec(id="demo", app="demo")\n'


def make_wheel(directory, version="1.0.0", plugin=PLUGIN, sdk=">=0.16,<1"):
    path = Path(directory) / f"bdh_app_demo-{version}-py3-none-any.whl"
    di = f"bdh_app_demo-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{di}/METADATA", (
            "Metadata-Version: 2.1\nName: bdh-app-demo\n"
            f"Version: {version}\nSummary: Black Diamond Hub app: a demo\n"
            f"Requires-Dist: bdh-sdk{sdk}\nRequires-Dist: requests\n"
            'Requires-Dist: pytest; extra == "test"\n'))
        zf.writestr(f"{di}/entry_points.txt", "[blackdiamondhub.plugins]\ndemo = demo.plugin:spec\n")
        zf.writestr("demo/plugin.py", plugin)
        zf.writestr("demo/static/demo/hub/icon.svg", "<svg/>")
    return path


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = Path(self.tmp.name) / "catalog.json"

    def add(self, wheel, *extra):
        with redirect_stdout(io.StringIO()):
            add_version.main(["--wheel", str(wheel), "--repo", REPO, "--catalog", str(self.catalog),
                              "--released-at", "2026-01-01T00:00:00Z", *extra])
        return json.loads(self.catalog.read_text())

    def test_reads_manifest_without_importing(self):
        catalog = self.add(make_wheel(self.tmp.name))
        app = catalog["apps"]["demo"]
        self.assertEqual(app["name"], "Demo")
        self.assertEqual(app["description"], "A demo")
        self.assertTrue(app["icon"].endswith(f"{REPO}/v1.0.0/demo/static/demo/hub/icon.svg"))
        v = app["versions"][0]
        self.assertEqual(v["permissions"], ["alerts", "ha.read:sensor.*"])
        self.assertEqual((v["network"], v["lifecycle"], v["memory_mb"]), ("lan", "on_demand", 128))
        self.assertEqual(v["companions"], ["go2rtc"])
        self.assertEqual(v["provides"], ["season", "stats"])
        self.assertEqual(v["requires"], ["requests"])
        self.assertEqual(v["hub_api"], 1)
        self.assertTrue(v["manifest_declared"])
        self.assertEqual(validate.check_schema(catalog) + validate.check_semantics(catalog), [])

    def test_every_capability_is_readable_and_valid(self):
        # Each SDK provides constant must parse from a wheel and pass the schema.
        names = ", ".join(bc.CAPABILITIES)
        plugin = (f"from bdh_sdk import PluginSpec\nfrom bdh_sdk.provides import {names}\n"
                  "spec = PluginSpec(id=\"demo\", app=\"demo\", provides={"
                  + ", ".join(f'{k}: "demo.provides.{v}"' for k, v in bc.CAPABILITIES.items()) + "})\n")
        catalog = self.add(make_wheel(self.tmp.name, plugin=plugin))
        self.assertEqual(catalog["apps"]["demo"]["versions"][0]["provides"], sorted(bc.CAPABILITIES.values()))
        self.assertEqual(validate.check_schema(catalog), [])

    def test_every_fixed_permission_is_valid(self):
        # Each SDK fixed-name grant (apps can't install without it) must pass the schema.
        # Mirrors the fixed names in bdh_sdk/grants.py (CI doesn't install the SDK).
        perms = ["alerts", "speakers", "media", "displays.read", "ha.ws"]
        plugin = ("from bdh_sdk import PluginSpec\n"
                  f"spec = PluginSpec(id=\"demo\", app=\"demo\", permissions={perms!r})\n")
        catalog = self.add(make_wheel(self.tmp.name, plugin=plugin))
        self.assertIn("displays.read", catalog["apps"]["demo"]["versions"][0]["permissions"])
        self.assertEqual(validate.check_schema(catalog), [])

    @staticmethod
    def schema_enums(field):
        schema = json.loads((Path(__file__).resolve().parents[1] / "schema" / "catalog.schema.json").read_text())
        enums = []

        def walk(node):
            if isinstance(node, dict):
                if field in node and isinstance(node[field], dict):
                    enums.append(node[field]["items"]["enum"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(schema)
        return enums

    def test_schema_provides_enums_match_capabilities(self):
        enums = self.schema_enums("provides")
        self.assertTrue(enums)
        for enum in enums:
            self.assertEqual(set(enum), set(bc.CAPABILITIES.values()))

    def test_schema_companion_enums_match_sdk(self):
        enums = self.schema_enums("companions")
        self.assertTrue(enums)
        for enum in enums:
            self.assertEqual(set(enum), set(bc.COMPANIONS))

    def test_every_companion_is_readable_and_valid(self):
        plugin = PLUGIN.replace('companions=("go2rtc",)', f"companions={bc.COMPANIONS!r}")
        catalog = self.add(make_wheel(self.tmp.name, plugin=plugin))
        self.assertEqual(catalog["apps"]["demo"]["versions"][0]["companions"], list(bc.COMPANIONS))
        self.assertEqual(validate.check_schema(catalog), [])

    def test_defaults_and_old_sdk_mean_hub_api_1(self):
        v = self.add(make_wheel(self.tmp.name, plugin=PLAIN_PLUGIN))["apps"]["demo"]["versions"][0]
        self.assertEqual(v["hub_api"], 1)
        self.assertFalse(v["manifest_declared"])
        self.assertEqual((v["network"], v["lifecycle"], v["memory_mb"]), ("outbound", "on_demand", 96))

    def test_hub_api_2_wheels_are_refused(self):
        with self.assertRaises(SystemExit) as cm:
            self.add(make_wheel(self.tmp.name, sdk=">=0.20,<1"))
        self.assertIn("images", str(cm.exception))

    def test_versions_sorted_newest_first_and_immutable(self):
        a = Path(self.tmp.name) / "a"
        b = Path(self.tmp.name) / "b"
        a.mkdir()
        b.mkdir()
        self.add(make_wheel(a, "1.10.0"))
        catalog = self.add(make_wheel(a, "1.9.0"))
        self.assertEqual([v["version"] for v in catalog["apps"]["demo"]["versions"]], ["1.10.0", "1.9.0"])
        self.assertEqual(catalog["apps"]["demo"]["icon"].split("/")[5], "v1.10.0")
        with self.assertRaises(SystemExit):
            self.add(make_wheel(b, "1.9.0", plugin=PLAIN_PLUGIN))

    def test_attestation_is_recorded(self):
        url = f"https://github.com/{REPO}/attestations/123"
        v = self.add(make_wheel(self.tmp.name), "--attestation-url", url)["apps"]["demo"]["versions"][0]
        self.assertEqual(v["attestation"], {"url": url, "signer_workflow": bc.SIGNER_WORKFLOW})

    def test_semantic_checks(self):
        catalog = self.add(make_wheel(self.tmp.name))
        versions = catalog["apps"]["demo"]["versions"]
        older = dict(versions[0], version="0.9.0")
        versions.insert(0, older)
        errors = validate.check_semantics(catalog)
        self.assertTrue(any("newest first" in e for e in errors))
        self.assertTrue(any("wheel_url must start with" in e for e in errors))

        catalog["apps"]["other"] = dict(catalog["apps"]["demo"], versions=versions[1:])
        self.assertTrue(any("already holds app" in e for e in validate.check_semantics(catalog)))

    def test_published_versions_are_immutable(self):
        base = self.add(make_wheel(self.tmp.name))
        changed = json.loads(json.dumps(base))
        v = changed["apps"]["demo"]["versions"][0]
        v["changelog"] = "edited"
        v["yanked"] = "broken"
        self.assertEqual(validate.check_immutable(changed, base), [])
        v["sha256"] = "0" * 64
        self.assertTrue(any("sha256 changed" in e for e in validate.check_immutable(changed, base)))
        changed["apps"]["demo"]["versions"] = []
        self.assertTrue(any("removed" in e for e in validate.check_immutable(changed, base)))

    def test_schema_rejects_foreign_wheel_url(self):
        catalog = self.add(make_wheel(self.tmp.name))
        catalog["apps"]["demo"]["versions"][0]["wheel_url"] = "http://example.com/x-1.0.0-py3-none-any.whl"
        self.assertTrue(validate.check_schema(catalog))


if __name__ == "__main__":
    unittest.main()
