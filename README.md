# bdh-app-catalog

The app catalog for Black Diamond Hub: every released version of every hub app
(`blackdiamondlodge/bdh-app-*`), with the wheel URL, sha256 and container
manifest a hub needs to show the app in its store and install it.

`catalog.json` is the only data file. Its format is defined by
[`schema/catalog.schema.json`](schema/catalog.schema.json) (JSON Schema 2020-12)
plus the semantic rules enforced by [`scripts/validate.py`](scripts/validate.py).

## Format

```jsonc
{
  "schema": 1,                       // bumped only for incompatible changes
  "apps": {
    "weather": {                     // PluginSpec.id; keys sorted
      "name": "Weather",             // first tile title of the newest version
      "description": "Weather for any location …",
      "icon": "https://raw.githubusercontent.com/blackdiamondlodge/bdh-app-weather/v1.6.1/weather/static/weather/hub/icon.svg",  // optional
      "repo": "blackdiamondlodge/bdh-app-weather",
      "versions": [                  // newest first, versions unique
        {
          "version": "1.6.1",
          "wheel_url": "https://github.com/blackdiamondlodge/bdh-app-weather/releases/download/v1.6.1/bdh_app_weather-1.6.1-py3-none-any.whl",
          "sha256": "074a34ba…",     // 64 lowercase hex chars of the wheel
          "hub_api": 1,              // PluginSpec.api_version the app targets
          "requires_sdk": "<1,>=0.16",
          "requires": ["requests"],  // other runtime requirements
          "permissions": [],         // PluginSpec.permissions, sorted
          "network": "outbound",     // none | outbound | lan
          "lifecycle": "on_demand",  // always | on_demand
          "memory_mb": 96,           // 48..256
          "companions": [],          // e.g. ["go2rtc"]
          "provides": ["stats"],     // stats | backgrounds | season
          "manifest_declared": false,
          "changelog": "Point to Settings > General for the hub location (1.6.1) (#7)",
          "released_at": "2026-11-14T04:10:46Z",
          "attestation": {           // optional, see Trust model
            "url": "https://github.com/blackdiamondlodge/bdh-app-weather/attestations/123",
            "signer_workflow": "blackdiamondlodge/bdh-sdk/.github/workflows/app-release.yml"
          },
          "yanked": "reason"         // optional; hubs must not offer it
        }
      ]
    }
  }
}
```

Field notes:

- **Manifest fields** (`hub_api`, `permissions`, `network`, `lifecycle`,
  `memory_mb`, `companions`, `provides`) are read from the wheel's `PluginSpec`
  by parsing its source (the wheel is never imported). Fields the app doesn't
  set get the SDK defaults: `network=outbound`, `lifecycle=on_demand`,
  `memory_mb=96`, no permissions or companions. `manifest_declared` is `true`
  only when the app sets at least one manifest field itself; `false` means the
  values are defaults and the hub should treat them as unreviewed.
- **`hub_api`** is the literal `api_version=` if the spec sets one, otherwise 2
  when the wheel requires `bdh-sdk>=0.20` (the SDK that introduced API 2), else 1.
- **`name`, `description`, `icon`** follow the newest version. The icon is the
  first tile's icon, pinned to the release tag.
- **One app per repo, one repo per app.** An app id can't move to another repo.
- **Published versions are immutable.** Only `changelog`, `yanked` and a newly
  added `attestation` may change; versions and apps are never removed. Retire a
  version (or a whole app, like `scenes`) by setting `yanked`.
- The file is in canonical form (`json.dumps(indent=2)`, apps sorted, versions
  newest first, LF). Always edit it with the scripts.

## How versions get added

Each app repo releases through the reusable
[`bdh-sdk` `app-release.yml`](https://github.com/blackdiamondlodge/bdh-sdk/blob/main/.github/workflows/app-release.yml)
workflow. After it creates the GitHub Release it:

1. attests the wheel with `actions/attest-build-provenance` (needs the caller to
   grant `id-token: write` and `attestations: write`), and
2. checks out this repo with the `CATALOG_TOKEN` secret, runs
   `scripts/add_version.py --wheel dist/*.whl …`, and opens an auto-merging PR
   `add/<app_id>-<version>`.

The `validate` check must pass before the PR merges.

If `CATALOG_TOKEN` isn't available the release still succeeds and logs a
warning. Catch the catalog up by hand:

```sh
python scripts/sync_releases.py                  # every non-archived bdh-app-* repo
python scripts/sync_releases.py bdh-app-weather  # or just some
```

`sync_releases.py` adds the latest GitHub Release of each repo that isn't in the
catalog yet (it needs an authenticated `gh`). Open a PR with the result.

## Validation

```sh
pip install "jsonschema>=4.18"
python -m unittest discover -s tests
python scripts/validate.py                                       # schema + rules
python scripts/validate.py --base old.json --network             # what CI runs
```

CI (`.github/workflows/validate.yml`, job `validate`, required on `main`) runs
on every PR and push against the previous catalog, so published versions stay
immutable and only new or changed versions are downloaded. A weekly scheduled
run re-downloads every wheel. `--network`:

- downloads the wheel and checks its sha256,
- checks that the wheel's `PluginSpec` id and version match the entry, and
- if there is an `attestation`, runs
  `gh attestation verify <wheel> --repo <repo> --signer-workflow <signer_workflow>`.

## Trust model

- **The catalog is the allowlist.** Hubs install only what is listed here, and
  this repo only accepts changes through PRs that pass `validate`.
- **sha256 pins the bytes.** A wheel is accepted only if its hash matches the
  catalog, so a release asset replaced later is rejected by the hub (and flagged
  by the weekly CI run).
- **Wheels come only from the app's own GitHub releases.** `wheel_url` must be
  `https://github.com/<repo>/releases/download/v<version>/…` for that app's repo.
- **Build provenance.** Wheels released by `app-release.yml` carry a GitHub
  OIDC/Sigstore build attestation from `actions/attest-build-provenance`. It
  proves the wheel was built by the shared SDK release workflow running in that
  app repo, from a specific commit. Hubs and CI verify it with
  `--signer-workflow`, so a wheel built anywhere else fails even with a matching
  hash. Versions released before attestation was added have no `attestation`.
- **Permissions are declared, not trusted.** `permissions`, `network` and
  `companions` are what the hub shows the admin for approval and what the
  supervisor enforces on the app's container. `manifest_declared: false` means
  the app hasn't declared them.

## Consuming the catalog (hubs)

- Fetch `https://raw.githubusercontent.com/blackdiamondlodge/bdh-app-catalog/main/catalog.json`
  with `If-None-Match` and cache it on disk. Refresh about hourly and when the
  store is opened. If the fetch fails, keep using the last good copy. A GitHub
  outage must never break installed apps.
- Reject a catalog whose `schema` is not one you know.
- Don't offer versions that are `yanked`, whose `hub_api` is newer than the
  hub's, or whose `requires_sdk` the hub's SDK doesn't meet.
- Download `wheel_url`, check `sha256` before installing (or use pip
  `--require-hashes`), and verify the attestation when present.
- Show `permissions`, `network` and `companions` for approval, and re-ask when
  an update widens them.
