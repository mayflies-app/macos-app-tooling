# macOS App Tooling

Shared development and release tools for Mayflies macOS apps. Each app owns its
configuration, product code, translations, native projects, and targeted checks.
Shared scripts, workflows, design components, and localization helpers are copied
into app repositories so they build independently of this checkout.

## Development

Apps use `./scripts/app.py build|run|test|package` and
`./scripts/app.py release --dry-run`. Each has a native Xcode project named after
the product; Codex Run uses `./scripts/app.py run`.

Run shared-tool checks and synchronization commands from this repository:

```sh
bash scripts/check.sh
python3 -B scripts/sync_apps.py --check APP_CHECKOUT
python3 -B scripts/sync_apps.py --apply APP_CHECKOUT
```

Replace `APP_CHECKOUT` with the chosen app repository root; multiple checkout
arguments are supported. Locations are supplied only when running the command.
Each app's `macos-app.json` declares its repository-relative shared destinations.
Synchronization compares files by default. `--apply` copies only shared paths and
preserves product tools. Review local and staged changes before applying, then
inspect the resulting consumer diffs.

Edit shared code in [`templates/macos-app`](templates/macos-app) and synchronize
consumers; edit product behavior in each app repository. See
[Shared Maintenance](templates/macos-app/README.md#shared-maintenance) for ownership
and validation scope, and the [template guide](templates/macos-app/README.md) for
commands and configuration.

## Distribution

The [release guide](docs/releasing.md) covers signing, notarization, publication,
and cask promotion through each app's existing release workflow. Published casks
and Homebrew installation instructions live in
[mayflies-app/homebrew-tap](https://github.com/mayflies-app/homebrew-tap).
The tap is a distribution consumer, not a shared-tool synchronization target.

## License

[MIT](LICENSE).
