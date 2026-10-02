# ychannel — automated Hindi political-news videos

Fetches Indian political news, writes a neutral Hindi script, voices it with ElevenLabs,
picks and arranges **real photos** (yours first, licensed stock as fallback) with Claude vision,
renders a TV-style video (Shorts 9:16 and long 16:9), and uploads to YouTube after **your approval**.

```
RSS feeds → curate (≥2 outlets) → Hindi script → ElevenLabs voice → AI picks/arranges photos
        → render (Ken-Burns, strap, ticker, captions, music ducking) → policy check
        → Telegram/CLI approval → YouTube upload → YPP progress stats
```

## Which API keys do I need?
**An OpenAI key alone is enough** for everything: script writing, photo picking (vision), the Hindi voice (`gpt-4o-mini-tts`)
and clip subtitles (Whisper). Or use Anthropic (scripts/photos) and/or ElevenLabs (voice, usually the most natural Hindi).
`provider: auto` in `config.yaml` uses whichever keys are in `.env`; force one with `llm.provider` / `tts.provider`.
Pick an OpenAI voice with `OPENAI_VOICE` (try `onyx`, `ash`, `nova`) and change the anchor style in `tts.openai_instructions`.

## Easiest way: the web app
```bash
python -m newschannel ui        # opens http://127.0.0.1:8765 in your browser
```
Three tabs: **Create video** (type a story or pick today's news, drag in photos/clips, one button),
**My videos** (watch, Approve/Reject, download, upload), **Settings** (paste API keys — saved to `.env`, never shown again).
It only listens on your own computer. First time? Click "demo video" to test without any key.

## Setup
```bash
pip install -r requirements.txt          # also needs ffmpeg + libraqm (see deploy/Dockerfile)
cp .env.example .env                     # fill in keys
python -m newschannel doctor             # checks everything
```
Edit `config.yaml` (channel name, feeds, voice settings, formats). YouTube: create an OAuth
"Desktop app" client in Google Cloud Console, save it as `secrets/client_secret.json`, run
`python -m newschannel stats` once on a machine with a browser, then copy `secrets/token.json` to the server.

## Using your own images
* Drop photos in `inbox/` (used for any story) or `inbox/<topic-slug>/` (one story), or pass `--images folder`.
* Optional `captions.txt` in the folder: `filename.jpg: what the photo shows` — helps the AI match photos to lines.
* You do not arrange anything: Claude looks at every photo, assigns the best ones to each scene, sets the
  focus point so faces stay in frame, picks the motion, and fills gaps with stock (Pexels / Wikimedia, credited in the description).
* Photos under `min_side_px` are rejected (they look blurry when zoomed).

## Using speech / video clips (e.g. a politician's statement)
The tool never downloads copyrighted footage — **you supply the clip** (Sansad TV, PIB, official channels, your own recording).
```
inbox/my-story/clips/
    speech.mp4
    clips.txt      # speech.mp4: 00:12-00:38 | Sansad TV | लोकसभा में बजट पर भाषण
```
or `python -m newschannel make --headline "…" --clips ./clips`. It then: trims the excerpt (max `clips.max_seconds`),
normalises loudness, keeps the original audio, transcribes the speech (ElevenLabs Scribe), writes Hindi subtitles
(machine-translated from English etc. — you are warned to verify), shows a **"स्रोत: …"** credit and a "मूल वीडियो" strap, and lists the source
in the description. The script writer introduces the clip and analyses it afterwards; clips may be at most 40% of the video,
and a clip without a credit blocks publishing.

## Growth features
* **Hook / title / thumbnail packaging** — for every video the AI proposes 5 opening hooks, 5 titles and 3 thumbnail texts, scores them,
  and uses the best ones that pass fact-safety checks (no invented numbers). All options are kept: pick another title or
  thumbnail in **My videos → Title & thumbnail options** before uploading.
* **Analytics loop** — `python -m newschannel insights --sync` (or the "Sync from YouTube" button) downloads views and
  retention per video. After 4+ videos with data, the hooks that kept people watching (and the ones that didn't) are fed
  into the next scripts automatically.
* **Unattended mode** — `python -m newschannel schedule` produces videos at `schedule.produce_at`, collects your
  Approve/Reject taps from Telegram, and publishes one approved video at each `schedule.publish_slots` time
  (max `limits.max_uploads_per_day`). Videos blocked by the policy checks are never auto-published. Keep the computer awake.

## Commands
| Command | What it does |
|---|---|
| `daily` | fetch → curate → produce the day's Shorts + long video → send to Telegram for approval |
| `make --headline "…" --text "…" --images ./myphotos --format short` | one video from your own story and photos |
| `review list / show ID / approve ID / reject ID` | approval queue (`review listen` handles Telegram buttons) |
| `publish [--publish-at 2026-10-03T07:30:00Z]` | upload approved videos |
| `insights [--sync]` | what worked on your channel; lessons feed the next scripts |
| `schedule` | run unattended: produce, collect approvals, publish at set times |
| `ui` | the point-and-click web app |
| `stats` | subscribers, watch hours, Shorts views vs. YouTube Partner Programme thresholds |

Cron examples: `deploy/crontab.example`. Docker: `deploy/Dockerfile`.

## Making money — what this tool does and doesn't do
Income comes from the **YouTube Partner Programme (YPP)**; the tool cannot grant or guarantee it.
Eligibility (verify current terms in YouTube Studio → Earn): **1,000 subscribers + 4,000 public watch hours (12 mo)
or 10M Shorts views (90 d)**; an early tier (500 subs, 3,000 h / 3M Shorts) unlocks fan funding.
Then link AdSense. `stats` shows your progress.

YouTube demonetizes *inauthentic, mass-produced, repetitive* content, so the pipeline is built to look and be original:
* every video must contain an **analysis scene** (context + implications) — not headline reading;
* a story is only made if **≥2 outlets** report it; narration may not copy 8+ words from a source;
* title near-duplicates and more than `max_uploads_per_day` uploads are blocked;
* stock images are credited; AI-voice disclosure is set (`containsSyntheticMedia`) and stated in the description;
* **nothing uploads without your approval** (`review.require_approval`); blocked items need `--force`.

Realistic expectations: Hindi news RPM is low (roughly $0.3–1.5 per 1,000 long-form views, far less for Shorts);
political content can get limited ads around elections/sensitive events. Growth comes from consistency, your own
photos/reporting, a clear niche (e.g. "संसद अपडेट"), and good thumbnails. Add other income: memberships, sponsorships, affiliate links.

## Rules to keep your channel safe
* Never use AI-generated images or cloned voices of real politicians. This tool only uses real photos you supply or licensed stock.
* Don't use other channels' clips/images; use PIB/Sansad TV, your own, or licensed material.
* Keep claims attributed and neutral; read each video before approving. You are the publisher.

## Tests
`python -m pytest -q tests` (uses a fake LLM and mock TTS; renders real videos at low resolution).
