# CLIPDESK CLIP RULES (MANDATORY)

These rules are IMPORTANT and MUST be followed for every clip, by every person and every Claude session.
No clip leaves the tool if it breaks one. If a rule cannot be met, STOP and ask the owner. Never fall back silently.
Defaults shipped with Clipdesk. Edit them to suit your channel, then treat them as locked.

## 1. All streamers
1. Layout: title_card. Blurred background, small rounded facecam on top, title pill in the middle, gameplay fills the bottom. The source facecam is cropped out of the gameplay.
2. Facecam: found per clip, full webcam visible, no black bars, no tiny face. If it cannot be found, the job STOPS with a visible error. Never fall back to another layout, never save a facecam box per streamer. (the owner, 24 Nov 2026)
2b. UNIVERSAL (the owner, 24 Nov 2026): facecam + gameplay is the rule for EVERY link and EVERY streamer. No layout, facecam box or other special case for one streamer in creators.json or code.
2c. GAMEPLAY ONLY mode: chosen per link with the switch in Add link ("Facecam + gameplay" or "Just gameplay"). Layout title_card_game: blurred gameplay background, gameplay centred, same title pill, same fonts (GTAArtDeco Medium), same captions, same sharpen. No facecam. This is the only way to skip the facecam, and it is never picked automatically.
3. Output: 1080x1920, 60 fps, H.264.
4. Quality: every clip is rendered from a 1080p (or the streamer's top) window of the stream. NEVER render a final clip from the 480p reading copy. H.264 source (AV1 breaks the render on this PC).
5. Sharpen: light gameplay-only sharpen (CAS 0.7 + unsharp 0.5). Facecam untouched. No AI upscaler. EVERY clip ships sharpened: new clips, live clips, re-renders, every streamer and colour theme. A clip made before sharpen existed is re-rendered before it is posted. (the owner, 19 Nov 2026)
6. Title:
   - starts with the streamer name (the streamer's display name). Never "The streamer".
   - short, ONE line. Shrinks before it wraps.
   - GTAArtDeco Medium, UPPERCASE, letter spacing -2. No condensed font.
   - pill padding 30, radius 12.
   - one Apple (iOS) emoji at the right end that matches the title.
7. File name = the title (no emoji). The .info.txt keeps where it came from: "From h:mm:ss to h:mm:ss of the stream | link".
8. Captions: GTAArtDeco Medium, white, word-by-word highlight, caption_y 380 (clears Reels and Shorts buttons), on the gameplay. Punctuated, clean, no delay, no cut-offs, no spelling mistakes. On-screen names spelled as shown.
9. Picks (updated 21 Nov 2026 by the owner): the AI looks at gameplay frames as well as the words. Wanted: relatable funny moments, awkward or spicy scenes (strip club, kissing, lap dance, flirting, rejection), epic fails, getting busted, crashes and chaos, crazy streamer reactions, the very first reaction to the game. A different kind per clip, 2 to 3 extra clips per streamer. Adult content is not filtered by the picker (the owner reviews every clip by hand before posting, 22 Nov 2026).
10. Process:
    - official streamer channels only.
    - POSTING (changed 19 Nov 2026 by the owner): clips post automatically, but ONLY clips the owner approved in Clipdesk, ONLY for streamers whose permission in creators.json is "approved", inside the posting window, with the gap and daily cap set in Clipdesk. Every post credits the streamer and links their stream. YouTube Shorts first.
    - ask the owner before any rerun.
    - ask the owner if unsure whether a streamer is English or Hindi.
    - show a before/after side by side before any quality change.
    - check a frame of every clip before sending it.

## 2. English streamers
1. Captions in English only (Parakeet, CLIPPER_LANG=en). Remove --hinglish and --language. No Hindi letters anywhere.
2. White title pill, dark text #1A0F14, orange caption highlight #FFAD36.
3. Pick one finished clip as your reference look and match every clip to it.

## 3. Hindi streamers
1. Captions ALWAYS Hinglish: Hindi in English letters plus English words (CLIPPER_LANG=hi + --hinglish, needs the Claude login).
2. Everything else exactly as section 1.

## 4. Live streams (clips made while the stream is running)
1. Every rule in sections 1 to 3 applies to live clips too: same layout, title, emoji, captions at 380, sharpen, colours.
2. Captions must be just as accurate live as on finished streams: English streamers get the English model (CLIPPER_LANG=en), Hindi streamers get Hinglish (CLIPPER_LANG=hi + --hinglish). Same clean, punctuated, no delay, no cut-off standard.
3. Every streamer in creators.json must have "language": "en" or "hi". If it is missing, live clipping does not start for that streamer and the owner is asked. Never guess.
4. The live recording is taken at the stream's top H.264 quality (1080p60), never the 480p reading copy.
5. When joining a stream mid-way, the part that already happened is read first, then live watching continues.
6. Each live clip goes through the same finishing step (remaster.py --finish): hooky short title, title_card look, file named after the title, stream time recorded.

## 5. Per streamer
Keep per-streamer exceptions to a minimum. Rule 2b stands: no layout or facecam box special cases. Theme colours may differ per creator (see layouts.py).

## 6. Post titles, captions and tags (the owner, 24 Nov 2026)
Built by seo.py from what is inside the clip. No AI call, no cost. The on-video title is not changed.
1. "GTA 6" is always in the YouTube title and in the first line of the Instagram caption. Added at the end if the title does not have it.
2. Tags come from what happens in the video: names (Dexter, Satan, Bigfoot), GTA 6 things (six stars, wanted level, jet ski, helicopter, Vice City) and the main objects of the title (train, truck, whale). No generic filler.
3. Hashtag order: #GTA6 #GTAVI, then the streamer, then up to 4 topic tags, then #shorts (YouTube) or #gaming #reels (Instagram).
4. YouTube tags field carries the same topics as plain words. Every post still credits the streamer and links the stream.
