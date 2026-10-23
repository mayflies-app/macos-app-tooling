# macOS App Template

The shared `scripts/app.py` and `scripts/lib` are copied into each app repository.
`ci.yml` and `release.yml` are copied into `.github/workflows`. Apps using the shared
SwiftUI design declare `shared.designDestination` in their `macos-app.json` for
`shared/MacAppDesign.swift`.
There is no runtime dependency on this checkout.

Runtime translations live in English and Simplified Chinese `.lproj/*.strings`
resources. `shared/LocalizationResources.swift` is copied to each app's
`shared.localizationDestination` in its `macos-app.json`. It selects the app's explicit language,
matches SwiftPM localization directories without case sensitivity, and falls back
to English for missing keys. SwiftPM apps prefer the resource bundle embedded in
the app before the module bundle; native Xcode apps use their main bundle.
App-owned language preferences and live UI refresh stay in the product code.
Keep translation values and formatted messages in resource tables, with matching
keys and format arguments in both languages; preserve user and service content.

## Command-Line Workflow

Run app commands from the target app repository root.

| Command | Default behavior |
| --- | --- |
| `./scripts/app.py build` | Debug, native architecture, automatic local signing; `dist/dev/<Product>.app` |
| `./scripts/app.py run` | Build successfully, quit the app at that exact output, then launch the new bundle |
| `./scripts/app.py test` | Run product unit tests and declared offline regression scripts |
| `./scripts/app.py package` | Release, Universal, ad hoc signing; app, DMG, manifest, and checksum under `dist/release/universal` |
| `./scripts/app.py release --dry-run` | Inspect the release plan without building or external requests |

Common options are `--version`, `--build`, `--arch`, `--configuration`, `--output`,
and `--sign`. `run --` passes app arguments. Use `package --arch universal --sign developer-id`
for a locally signed Universal app and DMG. Packaging does not submit notarization or publish.

`release` defaults to a dry run. `--execute` is restricted to manual CI; `--publish`
also publishes the release and updates the cask. Workflow defaults are `dry_run: true`
and `publish: false`. Release execution produces one Universal package containing
arm64 and x86_64. The workflow separates `preflight`, `package`, `notarize`, `publish`
and `cask` jobs; dry-run uses `preview`. Jobs call the same release entrypoint with
CI-only `--stage` selectors and scoped credentials. Artifact IDs carry exact inputs
across retries; a tar preserves the signed app bundle. Failed notarization resumes a
retained submission ID, draft publication uploads only missing matching assets, and
a cask retry does not rebuild or republish. See [release retries](../../docs/releasing.md#retry-a-failed-job).
Local `package`
continues to support all declared architecture options.

## Configuration

Use a PascalCase product and app filename and an `app.mayfiles.<Product>` bundle ID.
Set `repository` to the exact `owner/repository` path from the checkout's Git origin,
preserving its spelling and case. Repository names are independent of product names:
BigBang uses `mayflies-app/big-bang`, while DockAutoHide uses `mayflies-app/DockAutoHide`.
`VERSION` is the version source. Interfaces support English (`en`) and Simplified
Chinese (`zh-Hans`); documents are English.

Start with `starter/macos-app.json` and `starter/AGENTS.md`, set the actual identity,
resources and repository-relative `shared` destinations. Omit optional destinations
for helpers the app does not use. Run synchronization commands from the macos-app-tooling
repository root, replacing `APP_CHECKOUT` with the chosen app repository root:

```sh
python3 -B scripts/sync_apps.py --check APP_CHECKOUT
python3 -B scripts/sync_apps.py --apply APP_CHECKOUT
```

SwiftPM apps declare a source `Info.plist`, raw `resources`, required `resourceBundles`,
and `dependencyLicenses`. Native Xcode projects compile existing app sources and
consume local package libraries; the CLI may remain SwiftPM.

Xcode apps declare `buildSystem: "xcode"`, `paths.project`, and `xcode.scheme`.
Xcode builds the complete bundle and compiled plist; `packaging.infoPlist`
identifies the source plist for validation. Signing uses `paths.entitlements`. Do not
copy Xcode-owned resources again through CLI packaging.

## Product Checks

Product checks remain under `tools` or `Tests/Tools`. Run launches normal app behavior;
isolated fixtures use explicit documented arguments and separate user data.

Declare offline regression scripts in the top-level `testScripts` list using
repository-relative `.sh` or `.py` paths. The common `test` command first runs
SwiftPM tests when applicable, then these scripts in order, stopping on failure.
Shell scripts run with Bash; Python scripts run with Python 3 and bytecode writes
disabled. `TEST_ARCH` supplies the selected execution architecture. Xcode products
declare their regression scripts in the same list. Default tests must use isolated
data and services without opening product UI or changing production settings.

All apps expose explicit prebuilt English/Chinese UI acceptance through the same command:

```sh
./scripts/app.py test --native --case localization \
  --app /Applications/ExampleApp.app --output /private/tmp/exampleapp-localization
```

This explicit local check requires a visible, unlocked desktop. The app and output
paths must be absolute; output must be new or empty with an existing writable parent.
`nativeTests.localizationScript` declares the app's existing product test script.
It receives `--app`, `--output`, `--case localization` and `--timeout-seconds`
(default 90). It does not build or replace the selected app. Product runners reuse
`VerificationEvidence.swift` for `manifest.json`, command logs, source/bundle
fingerprints and report validation, and retain UI reports and screenshots.
Each runner owns isolated data or restoration of any user settings and running state
it touches. Ordinary `test` and CI do not open UI or change production settings.

BigBang, QuickNote and SimpleCodexBar support fixture-only fault checks through
`MACOS_LOCALIZATION_TEST_MODE=fail|hold`. They write `localization-progress.json`
after the real English UI is visible. `fail` exits unsuccessfully at that point;
`hold` waits for the requested deadline or Ctrl-C on the common command. Both
remain failed runs after successful cleanup. The existing recorder reserves the
isolated preference suite and temporary directory before launch and removes them
after its child exits, even when no UI report was completed. QuickNote also removes
its isolated SQLite database and sidecars while retaining screenshots and reports.

## Documentation

Every app has `README.md`, `CONTRIBUTING.md`, and `AGENTS.md`, plus
`docs/development.md`, `docs/localization.md`, `docs/privacy.md`, and
`docs/releasing.md`. Describe the implemented product, supported workflows, and
required configuration. Keep validation artifacts in temporary output directories
and commit history in Git.

## Shared Maintenance

This tooling repository owns `scripts/`, `.github/workflows/ci.yml`, and
`.github/workflows/release.yml` copied to each app. Each app's `macos-app.json`
owns the repository-relative `shared.designDestination` and
`shared.localizationDestination` for the optional Swift helpers. Edit shared
implementations in this template and compare explicitly supplied consumers with
`python3 -B scripts/sync_apps.py --check APP_CHECKOUT [APP_CHECKOUT ...]` from this
tooling repository root. Checkout locations are invocation arguments; repositories
do not store machine paths or assume a workspace layout. New shared scripts must be added to Git before
synchronizing; untracked local files and caches are not copied.

Review local and staged changes in the managed destinations before using `--apply`:
copying replaces unstaged edits and refuses to overwrite staged files that differ
from the template. All targets are checked before copying begins. Synchronization
preserves product tools and checks shared file contents and permissions. Review
the resulting source and consumer diffs and commit them in their respective repositories.

Each app owns `macos-app.json`, `VERSION`, its native Xcode project, any Swift package,
Codex Run configuration, translations, product code, and targeted checks. App documents
follow the organization above and retain the product's actual requirements and behavior.
The starter files are examples to configure, not synchronized consumer files.

Choose verification by the affected behavior:

- Documentation only: check links, referenced paths, and `git diff --check`.
- Configuration or workflow changes: check syntax, referenced identities and paths,
  and the affected command or workflow contract. Compare shared bytes when applicable.
- Executable shared tooling: run `bash scripts/check.sh` in macos-app-tooling and explicitly
  compare affected consumer checkouts after synchronization. Run affected app checks
  when the change affects consumer behavior.
- Product behavior: use the app's common `test` entrypoint and targeted checks from
  its development guide. Build, package, or inspect native UI only when affected.

## CI and Distribution

CI and release execution use the GitHub-hosted `xcode-27` runner, Xcode 27, and SDK 27.
Pure release previews run separately on `ubuntu-latest`. Ordinary CI cancels older
runs for the same ref; release execution remains serialized without cancellation.
Tests and app builds run in order so they can share the native Debug compilation cache.

Debug and Release caches have separate namespaces and paths. Keys include the actual
toolchain fingerprint, OS/CPU, dependency/configuration hashes, and source SHA, with
fallback restricted to the same compiler and dependency prefix. PRs only restore;
successful branch pushes can save Debug caches scoped by GitHub to that branch;
release execution can save Release caches on the default branch. Cache entries exclude
distribution artifacts and credentials, and every required check still runs.

Compilation caches retain tracked-input hashes and timestamps. Restores preserve
timestamps only when file content, mode and checkout path still match; changed or
new inputs keep their current timestamps and rebuild normally. Asset catalog and
icon directory timestamps are restored only when their complete tracked trees match.
Xcode builds on GitHub Actions disable the SDK file-status index that would be
regenerated for each runner; local builds retain Xcode's default setting.
Regression scripts share a compiler module cache within the same configuration
and architecture, while each fixture and test execution remains independent.

The minimum runtime macOS version is an app setting. See [distribution preparation](../../docs/releasing.md)
for credentials, notarization, and cask publication. The configured repository must
match the repository running the release workflow.
