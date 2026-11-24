# Clipdesk

A local tool that turns long streams into vertical Shorts and Reels, then posts the ones you approve to your own YouTube channel and Instagram account.

It runs on your PC. Nothing is hosted. You review every clip before it posts.

Default setup is GTA VI streamer clips (titles, tags and captions say "GTA 6"). The game name lives in `seo.py` and a few strings in `clipper.py` if you want another game.

## What it does

1. You add a streamer (official channel only) and paste a stream or VOD link.
2. It downloads the stream, transcribes it, listens for loud and funny moments, watches faces, and uses Claude to pick and judge the best moments.
3. It cuts 1080x1920, 60 fps clips: blurred background, facecam on top, title pill with an emoji, gameplay below, word-by-word captions.
4. You approve or reject each clip in the web app.
5. Approved clips post on a schedule: YouTube through the official API, Instagram Reels through Meta Business Suite in a browser profile you sign in to.

The pipeline is described in `docs/PIPELINE.md`. The clip rules are in `CLIP_RULES.md`, read them first. Every clip must follow them.

## You need

- Windows 10/11 (macOS and Linux are not tested)
- Python 3.10+, ffmpeg, Node.js
- A Claude subscription and the Claude CLI signed in (`claude -p` does the scouting, judging, titles and captions). No API key is used.
- A YouTube channel and a Google Cloud project (free) for the upload API, see `docs/youtube.md`
- An Instagram Business or Creator account linked to a Facebook Page, see `docs/instagram.md`
- An NVIDIA GPU helps for Hindi transcription and rendering. English works on CPU.

## Install

```
git clone <this repo>
cd clipdesk
install.bat
```

Then run `claude` once in a terminal and sign in. Start the app with `run_clipdesk.bat`. It opens at http://127.0.0.1:8765.

First run downloads the speech and vision models (a few hundred MB).

## Your accounts stay yours

These files are created on your PC and are in `.gitignore`. Never commit or share them:
`client_secret.json`, `yt_token.json`, `ig_profile/`, `ig_posted.json`, `creators.json`, `clipdesk_state.json`.

## Streamers and permission

Only add channels you have a right to clip. A streamer is clipped and posted only when `permission` in `creators.json` starts with `approved`. Every post credits the streamer and links the stream. Official channels only: no re-uploads from random sources.

## Fonts and emoji

Geist, Inter and Manrope are bundled. The title and caption font is GTA Art Deco, which is not redistributable: add your own font to `fonts/` (see `fonts/README.txt`). Title emojis are Apple style PNGs in `emoji/`, also not redistributable. Without them clips render without the emoji.

## Limits you should know

- A new Google Cloud project's YouTube uploads are forced to private until Google audits the project. Until then, set videos public by hand in YouTube Studio, or request the audit.
- Instagram posting drives the Meta Business Suite web page. If Meta changes the page, `ig_poster.py` needs a fix.
- Live clipping is experimental.

## License

MIT, see `LICENSE`. Not affiliated with any streamer, YouTube, Meta, Rockstar or Anthropic.
