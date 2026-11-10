# setup-lusine

Sets up **L'Usine**, the Devoteam Snowflake Partner hub. This repository holds only the setup script: L'Usine itself, its registry and the assets stay in private `DevoteamSP` repositories, and you need a GitHub account in the DevoteamSP organization to download them.

## Visitor: "I just want to see it"

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv run https://raw.githubusercontent.com/DevoteamSP/setup-lusine/main/setup_lusine.py
```

Choose *Visitor*. A code appears: type it on the GitHub page that opens, click **Continue** then **Authorize**. L'Usine downloads as plain files and starts in your browser. Nothing else to install: `uv` brings Python.

## Developer: "I need to clone and have everything"

Install `uv`, `git` and the GitHub CLI (`gh`), then run the same command and choose *Developer*. L'Usine is cloned with `gh`, so you can change it and push.

## Options

`--visitor`, `--developer`, `--dir <folder>` (default `~/dev/snow-builders`), `--yes`, `--dry-run`, `--no-launch`, `--shortcut` / `--no-shortcut`.

At the end, on macOS and Windows, the setup offers a shortcut on your Desktop (`L'Usine.command` or `L'Usine.cmd`): a double-click opens a terminal in the L'Usine folder and runs `uv run lusine`. Delete the file to remove it.

The script is one standard-library Python file with no secret in it (the GitHub App client id is public by design).
