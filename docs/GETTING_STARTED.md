# Getting started (for complete beginners)

This guide takes you from nothing to your first clip. No coding needed. Allow about 1 hour the first time, most of it waiting for downloads.

You will need a Windows PC, an internet connection, about 15 GB of free disk space, and a Claude paid plan (Pro or higher). A YouTube channel and an Instagram account are needed only for posting.

Tip: when a step says "open a terminal", press the Windows key, type `cmd`, press Enter. A black window opens. Copy each command, right-click in the window to paste, press Enter.

## Step 1. Install the 4 programs

Run these one at a time in the terminal. If Windows asks permission, say yes. Close and reopen the terminal after the last one.

```
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install OpenJS.NodeJS.LTS
winget install Git.Git
```

Check it worked:

```
python --version
ffmpeg -version
node --version
git --version
```

All four should print a version number. If `python` opens the Microsoft Store instead, install Python 3.12 from python.org and tick "Add python.exe to PATH" on the first screen.

## Step 2. Get Clipdesk

Easiest way:
1. Go to https://github.com/Abhishekdzns/clipdesk
2. Click the green **Code** button, then **Download ZIP**.
3. Right-click the ZIP, **Extract All**, and put the folder somewhere simple, for example `C:\clipdesk`.

(If you know Git: `git clone https://github.com/Abhishekdzns/clipdesk.git`.)

## Step 3. Install Clipdesk

Open the `clipdesk` folder and double-click **install.bat**. It downloads everything Clipdesk needs. This takes 10 to 20 minutes. Wait until it says "Next steps". If a red error appears, copy the last lines and see Troubleshooting below.

## Step 4. Sign in to Claude

Clipdesk uses your Claude plan to pick the best moments and write titles. In the terminal:

```
npm install -g @anthropic-ai/claude-code
claude
```

A browser opens. Sign in with your Claude account. When it says you are logged in, close the terminal. You only do this once. No API key is needed.

## Step 5. Start the app

Double-click **run_clipdesk.bat**. Keep the black window open. Your browser opens Clipdesk at http://127.0.0.1:8765. If it does not, open that address yourself.

The first clip downloads speech and vision models (a few hundred MB). This is normal.

## Step 6. Add your first streamer

1. In Clipdesk click **Add streamer**.
2. Enter the streamer's name and their official channel link.
3. Confirm you have permission to clip them. Clipdesk only clips and posts channels you approve. Official channels only.
4. Paste a stream or VOD link in **Add link** and press go.

Clipdesk now downloads the stream, finds the best moments, and makes vertical clips. It takes a while for long streams. Leave the PC on.

## Step 7. Review your clips

Open the **Clips** page. Click a clip to watch it. Approve the good ones, reject the bad ones. Nothing is ever posted without your approval. Read `CLIP_RULES.md` once to see the standards every clip must meet.

## Step 8. Connect YouTube (optional, for posting)

Follow `docs/youtube.md`. You create a free Google Cloud project, download one file, and run **connect_youtube.bat**. Important: a brand new Google project can only upload as private. Set each video to public in YouTube Studio, or ask Google for an API audit.

## Step 9. Connect Instagram (optional, for posting)

Follow `docs/instagram.md`. Your Instagram must be a Business or Creator account linked to a Facebook Page. Run **ig_login.bat** and sign in yourself in the window that opens.

## Your accounts are private

These files appear on your PC only and are never uploaded to GitHub: `client_secret.json`, `yt_token.json`, the `ig_profile` folder, `creators.json`. Never send them to anyone.

## Troubleshooting

- **"python is not recognized"**: close and reopen the terminal. Still failing: reinstall Python 3.12 and tick "Add python.exe to PATH".
- **install.bat fails on mediapipe or onnx**: you probably have a newer Python. Install Python 3.12, delete the `.venv` folder if one exists, run install.bat again.
- **ffmpeg not found**: run `winget install Gyan.FFmpeg`, close and reopen the terminal.
- **Page does not load**: make sure the black Clipdesk window is still open. Run **run_clipdesk.bat** again.
- **Clips have no title font**: add your own title font, see `fonts/README.txt`.
- **Claude errors or "not logged in"**: run `claude` in a terminal and sign in again.
- **Port in use**: close other Clipdesk windows, run **run_clipdesk.bat** again.
- **Nothing here fixes it**: open an Issue on the GitHub page and paste the last 30 lines of `clipdesk_log.txt`.

## Updating later

Download the new ZIP, copy your `creators.json`, `client_secret.json`, `yt_token.json` and `ig_profile` folder into it, and run install.bat again.
