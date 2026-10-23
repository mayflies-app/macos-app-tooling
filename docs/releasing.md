# macOS App Releases

## Configuration

Each app's `macos-app.json` defines its product name, bundle ID, GitHub repository,
cask token, and minimum macOS version. `VERSION` supplies the release version.
Before running a release, ensure `repository` matches the GitHub repository that
hosts the workflow and that the version identifies the intended source commit.
The tap destination is `mayflies-app/homebrew-tap`.

## Local Packaging

Run these commands from the target app repository root. Shared tools are maintained
in this repository; each application builds in its own checkout. Published casks
belong to the [distribution tap](https://github.com/mayflies-app/homebrew-tap).

```sh
./scripts/app.py package --sign developer-id
./scripts/app.py release --dry-run
```

Packaging creates a Release Universal app, DMG, manifest, and checksum under
`dist/release/universal`. Developer ID packaging uses the locally installed signing
identity and does not submit notarization or upload artifacts. A release dry run
inspects the plan without building or making external requests.

## GitHub Actions Release

The manual **macOS release** workflow accepts `version`, `dry_run` (default `true`),
and `publish` (default `false`). Leaving `version` empty reads `VERSION`.

Dry-run uses a separate `preview` job on `ubuntu-latest`, without credentials or
build-cache access. Execution calls the same `./scripts/app.py release --execute`
entrypoint with a CI-only `--stage` selector:

| Job | Runner | Responsibility and credentials |
| --- | --- | --- |
| `preflight` | `ubuntu-latest` | Validate clean source, version and publication eligibility; read-only GitHub token |
| `package` | GitHub-hosted `xcode-27`, Xcode 27 | Build and Developer ID sign one Universal app and DMG; signing secrets only |
| `notarize` | GitHub-hosted `xcode-27`, Xcode 27 | Verify the signed input, submit or resume notarization, staple and verify final bytes; notarization secrets and Team ID |
| `publish` | `ubuntu-latest` | Publish and anonymously verify assets; GitHub token with Contents write |
| `cask` | `ubuntu-latest` | Verify public assets and update only the target cask; read-only app token and `HOMEBREW_TAP_TOKEN` |

The chain is `preflight → package → notarize → publish → cask`. `publish=false`
stops after notarization. A matching public version skips packaging and notarization;
publication verifies the immutable existing release before cask promotion. The CLI
`package --arch` options remain available for local single-architecture packages.

### Retry a Failed Job

Use GitHub Actions **Re-run failed jobs** or re-run the failed job itself. Successful
upstream jobs are reused. Signed and notarized artifacts have attempt-specific names;
downstream jobs consume their producer's artifact ID, including outputs from earlier
attempts. Empty or invalid upstream IDs stop before download; they never fall back
to downloading every artifact. The signed handoff is a tar archive preserving app
executable modes and symlinks. Build cache saving belongs to `package` and does not wait for publication.

- `notarize` restores this workflow run's latest retained submission checkpoint,
  verifies the original signed DMG checksum, and waits on the known Apple submission
  ID. Accepted submissions only need stapling and verification. State and Apple logs
  are uploaded even on failure; credentials and the app bundle are excluded.
- `publish` continues a draft only for the same version and source. Existing asset
  bytes must match; only missing files are uploaded, without replacement. Empty
  `starter` placeholders left by failed uploads are removed only after every completed
  asset passes verification. A release published between checks is verified without
  further writes. Publication verifies the completed draft and final public download.
- `cask` retries public verification and tap promotion without rebuilding, notarizing
  or republishing. Fix `HOMEBREW_TAP_TOKEN` before retrying an access failure.

Keep upstream artifacts and notary state until completion; they use the repository's
artifact retention policy. Expired inputs require a new workflow run. If a prior
notary execution has no retained submission ID, inspect Apple submission history
before starting a new run; an interrupted upload cannot prove whether Apple accepted
it. Re-running all jobs can produce different signed bytes and cannot resume a
checkpoint for another DMG. Changed source or configuration also requires a new run.

Release execution stays serialized and does not cancel a running release. Preview
runs have a separate concurrency group and can replace an older preview. Ordinary
CI cancels superseded runs while retaining tests, bundle builds, and release-plan checks.

Compilation caches contain only `.build/app/debug/<native architecture>` for CI or
`.build/app/release` for releases. Their keys separate configurations, OS and CPU,
actual Xcode/SDK/Swift fingerprints, dependency/configuration hashes, and source SHA.
Fallback stays within the same toolchain and dependency prefix. Pull requests restore
only; successful branch pushes may save Debug caches within GitHub's branch scope.
Release execution on the default branch may save Release caches. `dist`,
signing material, and notarization receipts are excluded. Cache hits never skip
validation, signing, notarization, or public download verification.

Tracked-input timestamps are reused only when content hashes, modes and checkout
paths match the saved cache. Asset catalog and icon directory timestamps require
an identical complete tracked tree. Changed inputs still rebuild. Xcode builds on
GitHub Actions disable the SDK file-status index; local builds retain Xcode's
default. Ordinary regression scripts share SDK compiler modules inside the
existing build cache; fixtures and test execution are created afresh.

## Credentials

Configure these secrets in each app repository. Signing and notarization require the
first seven; publication and tap updates also require the eighth. Never put private
material in source or logs.

| Secret | Purpose |
| --- | --- |
| `MACOS_CERTIFICATE` | Base64 Developer ID Application PKCS#12 including its private key |
| `MACOS_CERTIFICATE_PASSWORD` | PKCS#12 password |
| `MACOS_SIGNING_IDENTITY` | Expected full signing identity or certificate SHA-1 |
| `MACOS_TEAM_ID` | Developer Team ID matching the certificate |
| `APPLE_NOTARIZATION_KEY_ID` | App Store Connect API key ID |
| `APPLE_NOTARIZATION_ISSUER_ID` | API key issuer ID |
| `APPLE_NOTARIZATION_PRIVATE_KEY` | Corresponding `.p8` private key contents |
| `HOMEBREW_TAP_TOKEN` | Fine-grained PAT limited to `mayflies-app/homebrew-tap` with Contents write |

App releases use the workflow's `github.token`; no separate `GH_TOKEN` secret is needed.
CI creates and cleans a temporary signing keychain and notarization key. The API key
issuer UUID and signing Team ID are separate values. A dry run validates the release
plan; executing the workflow validates credentials. Publication requires a public
app repository and explicit authorization to publish.

## Release Diagnostics

Execution logs identify each stage with `stage=... status=started|completed`.
Failures include a `kind`, an external exit code when available, and a GitHub
error annotation. The stages distinguish preflight/toolchain, signing keychain,
packaging, notarization submission/resume/stapling/validation, final artifact verification,
publication, and tap clone/verification/refresh/commit/push.

| Failure kind | What to check |
| --- | --- |
| `authentication` | Invalid, revoked or expired credentials; tap operations use `HOMEBREW_TAP_TOKEN` |
| `authorization` | Repository selection, Contents write, organization approval and branch rules |
| `repository-access` | Repository spelling and access; HTTP 404 can also hide a private repository |
| `network` | DNS, connectivity, GitHub availability or rate limits |
| `concurrent-update` | A non-fast-forward branch update; only this push failure is rebased and retried, up to three attempts |
| `configuration` / `validation` | Missing configuration or an identity, version, checksum, signing or notarization contract failure |
| `tool-unavailable` / `external-command` / `operation` | Runner tools or an unclassified failure; inspect the preceding stage logs |

Raw captured command arguments and output are omitted from error annotations to
protect credentials. A generic failure is not evidence of an expired token or a
concurrent update. Tap authentication, authorization and network failures stop
without concurrency retries.

The publication stage logs the verified release URL before tap promotion starts.
A later tap failure does not roll back the published release. Preserve its immutable
assets and prepare the cask from those public bytes; do not rebuild or replace the
same version. A local recovery cask must match the public DMG, manifest, checksum,
notarization receipt, version and source commit before it is reviewed for promotion.

## Homebrew Distribution

Verify signing, notarization acceptance, stapling, and public asset contents separately.
Calculate final checksums after stapling. The release and cask must identify the same
version and actual public download bytes. Add casks only through the verified release
workflow, with a SHA-256 checksum for the final DMG.

Uninstall quits the app. Zap removes preferences and saved window state while preserving
notes, account data, and Keychain items. Apps requiring macOS 13.5 use `:ventura` with
a read-only minor-version guard; apps requiring macOS 14 use `:sonoma`.
