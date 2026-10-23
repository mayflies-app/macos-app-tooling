# Agent Instructions

- Maintain shared scripts, workflows, design and localization helpers in `templates/macos-app`. App configuration, repository-relative shared destinations, translations, product behavior and native checks belong to the app repositories. See [Shared Maintenance](templates/macos-app/README.md#shared-maintenance).
- Run synchronization from this repository with explicit app checkout arguments: `python3 -B scripts/sync_apps.py --check APP_CHECKOUT [APP_CHECKOUT ...]` is read-only; `--apply` explicitly copies shared files. Review local and staged changes before applying, and resulting consumer diffs before committing. Do not commit host-specific paths or assume a workspace layout.
- Match validation to the change. For documentation only, check links, paths and `git diff --check`; no product build or test is needed. For executable template or tooling changes, use `bash scripts/check.sh` and explicitly compare affected consumer checkouts. Validate only affected product behavior in consumers.
- Keep `./scripts/app.py build|run|test|package` and `release --dry-run` as the common app entrypoints. Do not add alternate builders, compatibility wrappers, historical receipts, or governance layers.
- Use relevant Build macOS Apps skills and verify affected behavior. Protect unrelated changes and staged content.
- Published casks belong in [mayflies-app/homebrew-tap](https://github.com/mayflies-app/homebrew-tap). Do not synchronize shared tools into a tap checkout or rewrite its published history.
- Local validation, packaging, and commits do not authorize remote writes. Release requires an explicitly authorized manual CI run. See [Releasing](docs/releasing.md).
