Refactor preview
================

This folder contains a safe preview of small, non-invasive style and safety
changes to the project. Review these files and tell me which changes you want
applied to the main tree.

Files changed in preview:

- `config.py` — read token from `DISCORD_TOKEN` env var (no secrets in repo)
- `observable_set.py` — added type hints and docstrings, spacing fixes
- `requirements.txt` — minimal runtime deps
- `pyproject.toml` — basic Black/isort/flake8 configs

To run the preview copy you'll need to set `DISCORD_TOKEN` in environment and
run the main script from the original location or adapt imports.

Example:

```bash
export DISCORD_TOKEN="your_token_here"
python main.py
```
