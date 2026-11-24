# Clipdesk agents map

Clipdesk is a pipeline, not free-roaming agents. Each step is a fixed script. Claude steps are one-shot calls through `claude -p --model sonnet` with a JSON schema. They have no memory and no tools, except the judge reading frame images.

## Local, free (no Claude)
| # | Agent | Tech | Job |
|---|-------|------|-----|
| 1 | English transcriber | Parakeet TDT 0.6B v2 (onnx_asr) | Words with timings, about 45x real time |
| 2 | Hindi transcriber | faster-whisper (large-v3-turbo on GPU) | Hindi/Hinglish words. Slow. |
| 3 | Sound listener | loudness per second + YAMNet (MediaPipe) | LOUD, LAUGHING, SCREAMING, GUNFIRE tags |
| 4 | Face watcher | MediaPipe face detector + landmarker | SHOCKED FACE, MOUTH WIDE OPEN, SUDDEN MOVE, LEFT CAMERA |
| 5 | Facecam finder | facecam.py (MediaPipe + borders) | Finds the facecam box for the layout |
| 6 | Signal scorer | clipper.py | Blends tags + keywords into candidate peaks |
| 7 | Live watcher | live.py | Buffers a live stream, fires when 2+ signals agree |
| 8 | Cutter | brain.cut_points | Snaps start/end to sentence ends |
| 9 | Renderer + QA | ffmpeg, layouts.py, qa_rules.py | 1080x1920 clip, sharpen, rule checks |
| 10 | Posters | yt_poster.py, ig_poster.py, server post_tick | Upload approved clips only |
| 11 | Trending keywords | trending.py | Keyword list, no AI |

## Claude (uses your limit)
| # | Agent | Where | Cost driver |
|---|-------|-------|-------------|
| A | Scout | brain.scout | Reads transcript in 10 min parts, names up to 8 moments per part |
| B | Judge | brain.judge | One call per moment: rules + transcript + tags + 6 frames. Biggest cost. Cap 60 per stream |
| C | Ranker | brain.rank | One call when more clips are kept than wanted |
| D | Title writer | brain.hook_title | One short call per clip |
| E | Caption fixer | brain.fix_captions | English fix-ups |
| F | Hinglish captioner | brain.hinglish_captions | Hindi streamers (set per creator) |

## Local model hooks (dormant)
- `CLIPPER_JUDGE_MODEL`: local Ollama vision model replaces B.
- `CLIPPER_SCOUT_MODEL`: local Ollama text model replaces A, falls back to Claude on failure.
- Tested 22 Nov: qwen3-vl:8b-instruct as judge worked (13/13 answered, ~6 s per moment, 7.9 GB VRAM, sensible score spread). qwen2.5:14b as scout was fast but inflated scores. All models were deleted in the 22 Nov cleanup. Ollama app is still installed.
