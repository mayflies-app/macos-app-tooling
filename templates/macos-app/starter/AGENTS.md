# Agent Instructions

- Use `./scripts/app.py build|run|test|package` and `release --dry-run`.
- Shared scripts, workflows, and design and localization helpers are maintained in `macos-app-tooling/templates/macos-app` and copied here. This app owns the repository-relative `shared` destinations in `macos-app.json`. Update shared implementations in macos-app-tooling and synchronize with an explicitly supplied app checkout path. Do not commit host-specific paths or assume a workspace layout. See [Shared Maintenance](https://github.com/mayflies-app/macos-app-tooling/tree/main/templates/macos-app#shared-maintenance). Do not add alternate builders or compatibility wrappers.
- Keep app configuration, native projects, Codex Run, translations, product behavior and targeted checks here. Preserve windows, focus, input methods, permissions and storage.
- Match validation to the change. Documentation-only changes need link, path and diff checks; executable changes need affected tests. Use isolated tests and protect real preferences, databases and credentials. Retain inspectable results for native checks.
- Review local and staged changes before synchronizing or committing. Local packaging and commits do not authorize uploads, notarization, publication, or tap updates.
