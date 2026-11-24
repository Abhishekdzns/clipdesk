# Working on Clipdesk

- Read `CLIP_RULES.md`. A change must never let a clip break a rule. If a rule cannot be met the job stops with a visible error. No silent fallbacks.
- Ask before re-running finished clips.
- Never commit secrets (see `.gitignore`). Run a search for `token`, `secret` and your own handle before every push.
- After editing `server.py` or `jobs_api.py`, restart the app. UI edits only need a browser reload.
- UI: Geist for body text, no borders or strokes, blue means happening or scheduled, green means done, pink means uploading.
- Keep titles, captions and tags per `CLIP_RULES.md` section 6.
