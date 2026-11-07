---
name: env-setup
description: Use when a command fails because a tool, language or library is missing (for example "command not found", "python is not recognized", "No module named", "ModuleNotFoundError", "Cannot find module", "npm: not found", "pandoc: command not found"), or when the user asks to install or set up Python, Node.js, Git, pandoc, VS Code or a library. Offers a small, approved setup through env-wizard instead of installing anything ad hoc.
---

# Fix a missing tool with env-wizard

A missing tool or library interrupted the task. Set up only what is missing, with the user's OK, then
carry on with the original task.

The engine is `"${CLAUDE_PLUGIN_ROOT}/bin/env-wizard"` (called `EW` below). Its output is JSON; explain it
in plain words. The hard rules in the `/env-wizard` command apply here too: no installing by other means,
no passwords or keys, and apply only an approved plan.

1. Work out what's missing and map it to the catalog:
   - Command missing (`python`, `node`, `git`, `gh`, `pandoc`, `code`): `--item python|node|git|gh|pandoc|vscode`
   - Python library (`No module named 'pandas'`): `--package python:pandas`. Use the package name, not
     the import name: `docx` is `python-docx`.
   - JavaScript library (`Cannot find module 'serve'`): `--package npm:serve`
   - The project's own requirements (requirements.txt, pyproject.toml, package.json): `--project-deps`
   - Not sure: `EW catalog --match "<tool>"`
2. Run `EW plan ...` for just that.
3. Ask in one or two sentences, e.g. "To do this I need **pandas** (a tool for reading spreadsheets).
   Set it up now? About 30 seconds, no password." Mention anything in `declined`, `needs_your_password` or
   `preflight.problems`.
4. On yes: `EW apply --plan <plan_id>`. On anything other than `done`, follow steps 6 and 7 of the
   `/env-wizard` playbook.
5. Re-run the command that failed and continue the original task. If you need the project's Python, use
   `uv run python ...` or `.venv/bin/python` (Windows: `.venv\Scripts\python.exe`).

If the item isn't in the catalog (`declined`), say env-wizard can't install it, and don't install it
another way.
