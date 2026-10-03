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

## Photo library (your inventory)
Every photo you add — uploaded in the web app, passed with `--images`, dropped in `inbox/`, or downloaded as stock — is saved
to `library/` (duplicates are detected, even if resized). Open the **Photo library** tab to browse, search, edit descriptions, tags
and **credits** (credits appear in the video description), delete, or add more photos without making a video. When creating a video,
click **Choose from my library** to pick photos by hand; unless you turn it off (`images.use_library`), the AI also picks relevant
photos from your inventory on its own, preferring ones you've used less. New photos get an AI caption + tags (`images.auto_tag`; it
describes what is visible and never names people from their faces) so they can be found later. Photos stay on your computer except
small previews sent to your AI provider for captioning and arranging.

## Photo finder (relevant, copyright-safe photos that YOU approve)
In the Create and Deep analysis tabs, click **Find relevant free photos online** *before* making the video.
1. The AI first works out what the story actually needs to show - specific places, institutions, events and named officials - instead of
   searching generic stock words.
2. It searches **Wikimedia Commons** and **Openverse** (no key needed), plus **Pexels** and **Pixabay** if you add their free keys in Settings.
3. Only licences that allow commercial reuse are kept: public domain / CC0, CC BY (with credit), and the Pexels / Pixabay licences.
   CC BY-SA is off by default (`images.scout.allow_cc_by_sa`); **NonCommercial and NoDerivatives are always rejected**.
4. Each photo gets a relevance score (the AI looks at the picture and its caption). Photos with watermarks or logos are dropped, text-heavy or
   blurry ones are marked down, and generic stock photos are labelled **stock** (they are never photos of the real event).
   **People are never identified from faces**: a photo of a named person is only offered if its caption or file name says who it is.
5. You see every candidate with its licence, creator, a link to the source page and the reason for its score. Photos rated 7/10 or better are pre-ticked;
   untick, search again with your own words, then **Use selected photos**. Only those are used.
6. Approved photos go to your library with credit and licence, and the credit is added to the video description automatically.
   If a video has no photos at all it uses plain headline cards, never unchecked images. Unattended runs (`daily`, `breaking`) use
   `images.stock_mode: auto_strict` (only photos rated 8/10 or better) or `off`. CLI: `python -m newschannel scout --headline "..." --add-top 4`.
Licences vary by photo - the tool records what each source states; for anything sensitive, open the source link and check.

**Photos from a news article or any page you choose.** Paste a link in the photo finder. Seeing a photo on a news site does not make it free to use
(most are owned by the photographer or agency, and copyright claims can block earnings or strike the channel), so each photo found on the page is labelled:
*licensed* (the page states CC BY / CC0 / public domain - usable, credit added), *official* (a Government of India site such as PIB: their standard policy
generally allows free reuse with source credit and no misleading context, except third-party material - you confirm before use), *rights not stated* or
*licence not allowed* (shown for reference only; the server refuses to import them even if asked). For those, **Ask permission** gives you a ready message
for the photographer or outlet; use the photo only after they agree in writing. Deep-analysis videos can also show a **sources card** (outlet names and
headlines behind a claim) instead of article photos. Local and private-network addresses are never fetched.

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

## Step-by-step approval (script -> photos -> video)
When you create a video in the web app, choose **how it runs**:
* **✋ Ask me at each step** (default): the run stops after each stage and waits for you.
* **⚡ Fully automatic**: no stops (you still approve the finished video before anything is uploaded).
* **⚙️ Choose which steps**: pause only after the script, only after photos & media, or both.
Paused videos wait in the **Review steps** tab and resume exactly where you left off, even after closing the page.
1. **Script** - read it, edit any headline / narration (for deep analysis, every beat, with its claim chips), ask the AI to revise
   ("make it shorter", "sharpen the analysis"), then approve. Deep-analysis edits are fact-checked again on every save: an invented number,
   an unattributed claim or a banned phrase blocks approval; the target length is only a warning because you decide how long it is.
2. **Photos & media** - see the photos the AI chose for each scene; remove, add (upload, library, or the free-photo finder), reorder, change the
   camera move, re-arrange with AI, pick the music. Nothing is spent on the voice before you approve this step and the script.
3. **Video** - voice-over and render, then review the finished video in My videos as before. A failed render returns you to the photos step with
   your work intact; "Approve & run the rest automatically" is available at the script step. Defaults: `workflow.default` in `config.yaml`.

## Deep-analysis videos (researched, fact-bound, 6-15 minutes)
Open the **Deep analysis** tab (or `python -m newschannel deep --headline "..." --url <link> --url <link>`).
1. **Research & fact-check** - give a topic and source links (PIB, ministries, courts, major outlets) or pick a story from today's news.
   The app reads every source, extracts claims, and **verifies in code** that each claim's supporting sentence appears word-for-word in
   its source, that numbers match, and that quotes are verbatim. Claims get a status: **Confirmed** (official source, or 2+ outlets),
   **Quote**, **Reported by 1 outlet**, **Alleged**, **Disputed** (sources disagree) or **Unverified** (never used).
2. **Decide the angle and length** - *Neutral*, *Critical* or *Complementary*, all fact-based; or accept the tool's recommendation, which comes
   only from the evidence balance in the ledger. Length 6/9/12/15 minutes, a custom number, or the recommendation.
3. **Create** - the script is written from the ledger only, then checked automatically: every fact cites ledger claims, numbers come from those
   claims, claims that are not independently confirmed must name their outlet or speaker, quotes must be word-for-word, mind-reading and hearsay
   wording is rejected, Critical/Complementary videos must include a counterpoint scene. Failures trigger up to 3 automatic rewrites; if problems
   remain the video is **blocked from approval** and the reasons are shown.
Extras: quote / number / timeline cards built from the ledger, an on-screen "Source: ..." tag, a mood-matched royalty-free music bed that swells at
section changes (or your own tracks in `assets/music`), chapters and a tiered source list in the description, an "As of <time> IST" stamp.
Every video keeps its ledger (My videos -> Fact ledger & sources). Source quality: official (`*.gov.in`, PIB, Sansad, ECI, RBI, courts) > major
outlets and agencies > everything else (blogs and your own notes can never be the only support for a fact). Edit the lists under `analysis:` in `config.yaml`.

## Languages
`content.language` in `config.yaml` (and a switch in the web app): **Hinglish** (default; Hindi grammar with English words in Roman script, e.g.
"Kya sarkar ne ek hi din mein apna stand badal diya?") or **Hindi** (Devanagari). It controls the script, captions, on-screen labels, thumbnails,
descriptions, playlists names and the voice instructions. If a voice pronounces Roman Hinglish oddly, try another voice or the Hindi setting.

## Study a reference video
Copy a video's transcript (YouTube -> ... -> Show transcript) into a text file and run `python -m newschannel study transcript.txt`: pacing, hook,
delivery techniques worth borrowing, and a list of things our fact rules would not allow (unnamed sources, mind-reading, loaded words).

## Growth features
* **Hook / title / thumbnail packaging** — for every video the AI proposes 5 opening hooks, 5 titles and 3 thumbnail texts, scores them,
  and uses the best ones that pass fact-safety checks (no invented numbers). All options are kept: pick another title or
  thumbnail in **My videos → Title & thumbnail options** before uploading.
* **Analytics loop** — `python -m newschannel insights --sync` (or the "Sync from YouTube" button) downloads views and
  retention per video. After 4+ videos with data, the hooks that kept people watching (and the ones that didn't) are fed
  into the next scripts automatically.
* **Breaking-news alerts** — `python -m newschannel breaking` (or the 🚨 switch on the Create tab in the web app) checks the
  feeds every 5 minutes. A story triggers only if it is reported by `breaking.min_sources` (3) different outlets, the AI rates
  its national importance `breaking.min_importance` (7/10) or more, and it contains news you haven't already alerted on. It then
  makes a short (~40 s) Short with a "ब्रेकिंग न्यूज़" strap and sends it to Telegram / **My videos** for your approval, with a
  reminder to re-check the facts. Guards: max `breaking.max_alerts_per_day`, active hours only, no AI call when the feeds
  haven't changed (so it costs almost nothing while idle). `--once --dry-run` shows what would trigger without making anything.
  The scheduler runs it automatically too (`schedule.breaking`).
* **Unattended mode** — `python -m newschannel schedule` produces videos at `schedule.produce_at`, collects your
  Approve/Reject taps from Telegram, and publishes one approved video at each `schedule.publish_slots` time
  (max `limits.max_uploads_per_day`). Videos blocked by the policy checks are never auto-published. Keep the computer awake.

## Playlists & end screens
* **Playlists (automatic)** — when you publish, the video is added to a playlist for its topic (`playlists.categories`, chosen by the
  AI) and one for its format (Shorts / full reports). Missing playlists are created for you (`python -m newschannel playlists`
  creates them all up front). The description gets "Watch next" links to your latest videos in the same topic plus the playlist link, and
  the previous video in that topic is updated with a link forward to the new one (`playlists.link_previous`).
* **End screens (semi-automatic)** — YouTube's API cannot create end screens or cards, so that step stays in Studio. Long videos end with a
  12-second end card (`endscreen.seconds`) that leaves clean boxes for YouTube's elements. In **My videos**, each published video has an
  "End screen & pinned comment" helper: Studio link, which videos to link, a pinned-comment draft (also: `python -m newschannel endscreen`).
* YouTube API quota: the default 10,000 units/day covers about 4-5 uploads a day including these playlist calls. Request more in Google Cloud if needed.

## Commands
| Command | What it does |
|---|---|
| `daily` | fetch → curate → produce the day's Shorts + long video → send to Telegram for approval |
| `make --headline "…" --text "…" --images ./myphotos --format short` | one video from your own story and photos |
| `review list / show ID / approve ID / reject ID` | approval queue (`review listen` handles Telegram buttons) |
| `publish [--publish-at 2026-10-03T07:30:00Z]` | upload approved videos |
| `insights [--sync]` | what worked on your channel; lessons feed the next scripts |
| `deep --headline ... --url ...` | researched, fact-checked analysis video (`--research-only` to stop after the ledger) |
| `study transcript.txt` | analyse a reference video's structure and techniques |
| `breaking [--once] [--dry-run]` | watch for breaking political news and prepare a Short for approval |
| `playlists` | create your topic/format playlists on YouTube |
| `endscreen` | list published long videos that still need an end screen set up in Studio |
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
