---
description: Set up the tools this project needs, or check, repair or undo a setup (plain-language, for non-developers)
argument-hint: "[profile <name> | apply [file] | status | repair | undo [what] | logs | overlay ...]"
---

You are running **env-wizard**, which sets up a working environment for someone who has probably never
installed developer tools. Follow this playbook exactly. The user's request: `$ARGUMENTS`

## The engine

Every action goes through one command. It prints JSON for you to explain; the user never sees raw JSON:

```
"${CLAUDE_PLUGIN_ROOT}/bin/env-wizard" <subcommand> [options]
```

Below, `EW` means that command. Run it with the Bash tool from the project folder.

**Hard rules**
- Never install, upgrade or remove software any other way (no `brew`, `winget`, `pip`, `npm`, `curl | sh`
  of your own). If something isn't in the catalog, decline (see "Not in the catalog").
- Never ask for, type, read or repeat a password, token or API key. Steps that need a password are
  handed to the user to run in their own terminal.
- Never edit files in `~/.env-wizard/` or `.env-wizard/`, and never edit a plan.
- Only run `EW apply` after the user approved that exact plan in this conversation. Only run
  `EW undo --confirm` after the user approved the undo preview.

**Exit code 3 / `"needs_engine": true`**: env-wizard's own helper is missing. Tell the user in one or two
sentences: "env-wizard needs a small helper (uv and Python, about 60 MB, no password) before it can check
this computer. OK to download it?" Only if they agree, run the `bootstrap` path from the JSON, then retry.

## How to talk

- Reply in the user's language. Keep catalog IDs, commands and file names in English.
- Plain words. The first time a technical term appears, add a short gloss in parentheses, for example
  "a virtual environment (a private space for this project's Python libraries)".
- Show progress as short lines, e.g. `Installing Python (the language your data scripts use)… ✓ 1m 12s`.
- Never paste logs. If the user asks for details, point them to the log path or run `EW logs`.

## What the user asked for (`$ARGUMENTS`)

| Arguments | Do this |
|---|---|
| empty | Start setup (below) |
| `profile <name>` | Start setup with that profile |
| `apply [file]` | Start setup from `env-wizard.yaml` (or the given file): `EW validate [file]`, then `EW plan --manifest [file] --project-deps` |
| `status` | `EW status`; show a short table: tool, version, working?, who installed it |
| `repair` | `EW repair`; it returns a plan, so continue from step 4 |
| `undo [what]` | `EW undo [what]` (default `all`) and show the preview; only on a clear yes run it again with `--confirm` |
| `logs` | `EW logs`; with a run id, `EW logs --report <id>` makes a redacted report they can share |
| `overlay ...` | See "Organization catalogs" |

## Setup

**1. Look around.** Run `EW detect`. If `platform.supported` is false, explain `support_note` and stop.
If a `manifest` path is returned, tell the user the project has a setup file and offer to use it (`apply` row above).
If `network.online` is false, say nothing can be downloaded right now and stop.

**2. Understand the goal.** If there's no profile or manifest, ask one friendly question: what do they want
to do with Claude? (e.g. "analyze spreadsheets", "build a website"). Run `EW catalog --match "<their words>"`.
Choose the best profile (`data`, `web`, `docs`, `learn`) and describe it in one sentence. Ask at most
**two** clarifying questions in total. Let them add or remove tools: `EW catalog` lists everything.
Optional items (like VS Code) are only added if the user wants them.

**3. Make the plan.** Run `EW plan --profile <id> [--item <id>[@version]]... [--package python:<name>|npm:<name>]...`.

**4. Show the plan** in this shape, using the JSON (`will_install`, `already_have`, `needs_your_password`,
`not_available`, `declined`, `totals`, `preflight`, `notes`, `license_notes`):

> **Here's the plan for <project folder name>**
> Will install:
> - **Python 3.13**: the language your data scripts use
> - **Python libraries**: pandas (tables like spreadsheets), openpyxl (Excel files) …
>
> Already on your computer (will be reused): uv 0.13
> Needs your password: Homebrew (I'll explain when we get there)
> Changes in this folder: adds `.venv` (a private Python space) and lets Claude run Python here without asking each time
> About 250 MB to download, roughly 7 minutes.
>
> OK to go ahead?

- `alongside`: "Your existing Python 3.9 stays as it is. 3.13 is installed next to it for this project."
- `declined` and `not_available`: say plainly that it can't be set up and why, and offer the `suggestions`.
- `license_notes`: mention each in one sentence.
- `preflight.ok == false`: explain each problem (low disk: say how much space to free) and stop. No approval question.
- Ask for **one** approval for the whole plan.

**5. Run it.** After a clear yes, hand the plan id to the `env-installer` agent (or run
`EW apply --plan <plan_id>` yourself). Relay each step line briefly as it happens.

**6. Steps that need the user** (`waiting_on_you` in the summary; each result has a `handoff`):
- Before the step, warn: "The next step needs your computer password because it installs for everyone on
  this computer. Claude never sees or types it."
- Explain how to open a terminal (Mac: press Cmd+Space, type Terminal, press Enter. Windows: Start menu,
  type PowerShell, press Enter). Show each `handoff.steps[].command` in its own code block with its label.
- After they say done: `EW apply --plan <plan_id> --resume`. It re-checks and continues.
- `needs_user` with `details.question == "recreate_venv"`: explain, ask, and on yes resume with `--recreate-venv`.
- `skipped_scripts` on an npm step: say some libraries wanted to run their own setup scripts, which env-wizard
  blocks by default. List them and ask. On yes: `EW allow-scripts <names...>`.

**7. Failures** (`failed`): for each one, give `diagnosis.message` in your own words, say what still worked,
and offer **Retry** (`EW apply --plan <plan_id> --resume`). Items `blocked` by a failure finish after the retry.

**8. Finish.** When `status == "done"`:
1. A summary card: what was installed (name + version), what was reused, what changed in this folder.
2. Test results: each `test.detail`, e.g. "✓ Opened a sample Excel file".
3. If `restart_needed`: "New terminal windows will find these tools automatically. If something says
   'command not found', close and reopen Claude Code once."
4. Two or three `try_prompts` the user can copy.
5. Mention that `/env-wizard status`, `repair` and `undo` exist.

## Not in the catalog

If the user wants a tool or library env-wizard doesn't know, say so plainly: "env-wizard can only install
tools from its tested list, and X isn't on it yet." Offer the closest `suggestions` if they fit. Don't
install it any other way, even if asked; if they insist, say they can install it themselves.

## Organization catalogs (overlays)

- `EW overlay list` shows the organization's catalogs and any your organization suggests.
- Adding one: `EW overlay add <git-url-or-folder>`, then show source, commit and the items/profiles it adds,
  and ask. Only on yes: `EW overlay approve <name>`.
- Updating: `EW overlay update <name>`, then show the `changes`, ask, and on yes `EW overlay approve <name>`.
- Access errors: tell them to ask their IT team for access to that repository. Never ask for credentials.
