# Notes for Claude

- **Always deliver to `main`:** after committing and pushing a change on the working branch, open a
  pull request into `main` and merge it, unless the user says otherwise. The owner downloads the bot
  from `main`.
- The project is the Rust Console Edition Discord bot in `rce-discord-bot/` (Python, discord.py,
  aiohttp web interface). Run `python -m pytest -q` inside that folder before pushing.
- The bot UI, code comments and README are in English.
