# env-wizard

A Claude Code plugin that sets up everything a project needs (Python, Node.js, Git, document tools, libraries and Claude Code's own project settings) for people who have never installed developer tools. Users describe what they want to do in their own words. env-wizard shows a short, plain-language plan, asks once, installs from a tested catalog, checks that each tool works, and can undo exactly what it changed.

## Using it

| Command | What it does |
|---|---|
| `/env-wizard` | Starts a setup: asks what you want to do, or uses the project's `env-wizard.yaml` |
| `/env-wizard profile data` | Sets up a ready-made bundle: `data`, `web`, `docs` or `learn` |
| `/env-wizard apply` | Sets up what `env-wizard.yaml` lists, plus the project's own requirements |
| `/env-wizard status` | Shows what's installed, whether it works, and who installed it |
| `/env-wizard repair` | Re-checks everything and offers to fix what's broken |
| `/env-wizard undo` | Shows what would be removed, and removes it only after you agree |
| `/env-wizard logs` | Shows recent runs and makes a report you can share (secrets removed) |

The plugin also checks the project quietly when Claude Code starts, and only says something if a tool is missing or broken. When a command fails because a tool or library is missing, Claude offers to set up just that piece.

### Profiles

| Profile | For | Installs |
|---|---|---|
| `data` | Spreadsheets, CSV, charts | uv, Python, project environment, pandas, openpyxl, matplotlib, JupyterLab |
| `web` | Simple websites | Node.js, Git, starter site, `serve` |
| `docs` | Word/PDF/Markdown, automation | Python, python-docx, pypdf, openpyxl, requests, pandoc |
| `learn` | Learning to code | Git, Python, Node.js, VS Code, a first program |

### Sharing a setup: `env-wizard.yaml`

```yaml
version: 1
name: Marketing analytics
profiles: [data]
items:
  - python: "3.12"
  - pandoc
  - package:python: [requests]
project_deps: true   # also install what requirements.txt / package.json list
```

A setup file can only name tools and libraries from the catalog. It can't contain commands or download
links, so it's safe to open one from an untrusted repository. The plan is always shown before anything happens.

## Safety model

- **Catalog only.** Every tool is installed by one of a small set of install methods built into the
  plugin. Catalog files, setup files and organization catalogs only choose a method and its settings.
- **Never replaces what you have.** If you already have a different version of a tool, the required
  version is installed next to it.
- **No passwords.** Steps that need your computer password are given to you to paste into your own
  terminal. Claude never sees or types passwords, tokens or keys.
- **One approval** for the whole plan. Password steps and organization catalogs ask separately.
- **Undo removes only env-wizard's own changes.** Everything is recorded in `~/.env-wizard/state.json`
  and `<project>/.env-wizard/state.json`. Tools you already had, and tools other projects still use, are kept.
- **No telemetry.** Logs stay on your computer in `~/.env-wizard/logs/`.

## Platforms

v1 supports macOS 13+ and Windows 10 22H2 / 11. Linux (Ubuntu 22.04+, Debian 12+, Fedora 39+) and WSL
work as a preview for everything that doesn't need a system package manager (Python, Node.js, libraries,
project settings). Full Linux support is planned for v1.1.

## How it works

```
commands/env-wizard.md     the playbook Claude follows (plan → approve → install → verify → finish)
skills/env-setup/          offers a small setup when a tool or library is missing
agents/env-installer.md    runs an approved plan with a minimal toolset
hooks/hooks.json           SessionStart health check (silent unless broken)
bin/env-wizard[.ps1]       launcher: finds Python ≥ 3.9 without installing anything
bin/bootstrap[.ps1]        installs uv + Python, only after the user agrees
src/env_wizard/            the engine (Python standard library only)
catalog/                   items, profiles, approved libraries, Claude Code presets
templates/                 starter files for profiles
tests/                     python3 -m unittest discover -s tests
```

The engine prints JSON, and Claude turns it into plain language. Run it directly for debugging:

```bash
bin/env-wizard plan --profile data --no-network
```

### Organization catalogs

Organizations can add their own items and profiles with an overlay: a git repository (or folder) using the
same layout as `catalog/`, with IDs namespaced like `acme/tool`. Users approve each overlay and each
update, pinned to a commit. To pre-suggest overlays in an organization's own marketplace, list them in
`config/default-overlays.yaml`.

See [SPEC.md](SPEC.md) for the full specification.
