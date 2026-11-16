# bdh-app-catalog

The app catalog for Black Diamond Hub: every released version of every hub app
(`blackdiamondlodge/bdh-app-*`), with what a hub needs to show the app in its
store and install it: for hub API 2 apps a digest-pinned container image built
by this repo's [builder](#how-versions-get-added), for hub API 1 apps a wheel.

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

Hub API 2+ versions are images (`"kind": "image"`, required for hub API 2;
a hub API 2 wheel entry may only remain yanked):

```jsonc
{
  "version": "2.1.0",
  "kind": "image",
  "hub_api": 2,
  "image": "ghcr.io/blackdiamondlodge/bdh-app-cameras@sha256:…",   // always ghcr.io/blackdiamondlodge/bdh-app-<id>, by digest
  "base": "ghcr.io/blackdiamondlodge/bdh-app-runtime@sha256:…",    // the runtime it was built FROM
  "build": "gh-123456789.1",         // builder run; changes on every rebuild
  "wheel_url": "https://github.com/blackdiamondlodge/bdh-app-cameras/releases/download/v2.1.0/bdh_app_cameras-2.1.0-py3-none-any.whl",
  "sha256": "…",                     // of that source wheel
  "permissions": [], "network": "lan", "lifecycle": "always", "memory_mb": 96,
  "companions": ["go2rtc"], "provides": [],
  "custom_base": false,              // true: the app adds its own Dockerfile steps
  "changelog": "…", "released_at": "…", "built_at": "…",
  "attestation": {                   // provenance of the image, signed by the builder
    "url": "https://github.com/blackdiamondlodge/bdh-app-catalog/attestations/…",
    "signer_workflow": "blackdiamondlodge/bdh-app-catalog/.github/workflows/build.yml"
  }
}
```

An image version may be **rebuilt** (a new runtime base, or a builder fix):
`image`, `base`, `build`, `built_at` and `attestation` change together and
everything else stays. The supervisor treats a changed digest as an upgrade.

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

### Hub API 2+: images, built here

App repos release through the reusable
[`bdh-sdk` `app-release.yml`](https://github.com/blackdiamondlodge/bdh-sdk/blob/main/.github/workflows/app-release.yml).
It publishes and attests only build inputs: the wheel, `bdh-lock.txt`
(hash-locked dependencies), `bdh-app.json` (build config + manifest, including
an optional declarative `apt` list) and, for `custom_base` apps only,
`bdh-extra.Dockerfile`. App repos never push images and need no catalog token.

[`.github/workflows/build.yml`](.github/workflows/build.yml) is the one image
builder. Every 6 hours, or on demand
(`gh workflow run build.yml -f apps=cameras`, `-f rebuild_all=true`), it:

1. plans (`scripts/builder.py plan`): the latest release of each app repo that
   publishes `bdh-app.json` and isn't in the catalog, plus a rebuild of every
   app whose newest image isn't on the current `bdh-app-runtime:hub-api-2`
   digest (so a base bump rebuilds every app);
2. per app: downloads the release artifacts and verifies their attestations
   (signer: the SDK's `app-release.yml`), builds the image from the SDK's
   template (`builder/build_app.sh`, FROM the base by digest), smoke-tests it
   (`builder/smoke.sh`: starts it hardened like the supervisor does, checks
   `/_bdh/health` and that its views import, measures RSS/PSS), pushes
   `ghcr.io/blackdiamondlodge/bdh-app-<id>:<version>` and attests it with
   `actions/attest-build-provenance` (pushed to the registry);
3. opens catalog PRs: one per new app version (`builder/<id>-<version>`) and
   one for all rebuilds (`builder/base-<digest>`). PRs that need no human
   review auto-merge once `validate` passes.

With an optional `CATALOG_PR_TOKEN` repo secret (a fine-grained PAT or App
token with contents and pull-requests write on this repo), the builder pushes
the branch and opens the PR with it, so `validate` runs and the PR
auto-merges hands-off; a failure to open the PR then fails the run. Without
the secret, the default `GITHUB_TOKEN` pushes the branch and tries to open the
PR. If the org doesn't let Actions open PRs, the run summary links the branch
and you open the PR by hand. Either way, a branch pushed with `GITHUB_TOKEN`
doesn't trigger `validate` until a human opens or updates the PR.

### Hub API 1: wheels

`scripts/add_version.py --wheel …` adds a hub API 1 wheel (it refuses hub API
2+). `scripts/sync_releases.py [bdh-app-x …]` catches the catalog up with the
latest release of each repo: wheel-only releases go through `add_version.py`,
releases with `bdh-app.json` dispatch the builder.

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
  `gh attestation verify <wheel> --repo <repo> --signer-workflow <signer_workflow>`,
- for image entries: `gh attestation verify oci://<image> --repo
  blackdiamondlodge/bdh-app-catalog --signer-workflow <build.yml>`, checks the
  image's `org.blackdiamondlodge.bdh.app`/`.version` labels, and that the
  image's rootfs `diff_ids` start with its `base`'s (a compatibility check:
  the app was built on that runtime).

On pull requests (`--pr`):

- **one app per PR.** Only the builder's base bump may touch several apps,
  and only if every change is a rebuild of an existing version onto one
  common new `base`.
- no label is needed for new or widened permissions, network or companions:
  the hub admin approves those when installing or updating the app.
- the image repo must be `ghcr.io/blackdiamondlodge/bdh-app-<same id>`
  (enforced on every run).

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
- **Images are built only by this repo.** The image attestation's signer is
  pinned to `blackdiamondlodge/bdh-app-catalog/.github/workflows/build.yml`;
  the hub supervisor verifies it (with `cosign verify-attestation` against
  that workflow identity on `refs/heads/main`) before it runs an image, and
  pulls only by the catalog digest. App repos have no push rights to GHCR.
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
- Hub API 2 hubs install only `kind: image` entries for hub API 2+ apps:
  the supervisor pulls `image` by digest, verifies its attestation and base,
  and runs it. The images are public; installs and upgrades need ghcr.io,
  running apps don't.
- For hub API 1 wheels: download `wheel_url`, check `sha256` before
  installing (or use pip `--require-hashes`), and verify the attestation
  when present.
- Show `permissions`, `network` and `companions` for approval, and re-ask when
  an update widens them.
