# heygen-dub

This tool turns a Slovak social-media video that has no captions into one captioned video per target language. Each output has a lip-synced dub made by HeyGen and burned-in captions.

There are two ways to use it:

- **Without paid APIs (recommended):** HeyGen's app or website does the lip-synced dubbing, using your normal subscription. This tool then adds animated word-by-word captions locally, for free. See [Animated captions](#animated-captions-no-paid-apis).
- **Fully through the HeyGen API:** this needs an API plan. See the sections that follow.

## Animated captions (no paid APIs)

```bash
pip install faster-whisper numpy        # one-time; the ~1.6 GB speech model downloads on first use
python -m heygen_dub caption dubbed_en.mp4 dubbed_de.mp4 -o output/captioned
```

For each video, the tool:
1. Transcribes the speech with Whisper (`large-v3-turbo`, running locally) and records when each word is spoken.
2. Writes `output/captioned/<video>/transcript.txt`, one sentence per line.
3. Shows a few words at a time, highlights the word being spoken, and burns the captions in. The result is `output/captioned/<video>.captioned.mp4`, plus `captions.srt` as a sidecar file.

**Fixing wording:** edit `transcript.txt` (brand names, numbers, misheard words) and run the same command again. It doesn't transcribe again: your corrected text is fitted onto the original word timings. You can also pass the correct translation up front with `--script translation.txt`.

The look is set by `[captions]` in `config.toml`: font, size, colours, `highlight_color`, `max_words`, `uppercase`, `pop`, `box`, and position.

## HeyGen API pipeline

```
source.mp4 (Slovak, no captions)
   │  upload (or public URL)
   ▼
HeyGen video translation, mode "precision"   ← one request for all languages
   │  voice cloned into each language, mouth re-rendered to match
   ▼
per language: dubbed.mp4  +  translated SRT from HeyGen
   │  SRT re-split into short 1–2 line social captions
   ▼
ffmpeg/libass burns styled captions in  →  output/<video>/<video>.<language>.mp4
```

## Setup

Requirements:
- Python 3.11 or newer. No other Python packages are needed.
- `ffmpeg`, built with libass. The standard builds from apt, brew and choco include it.
- A HeyGen plan with API access to video translation.

```bash
cp config.example.toml config.toml        # pick languages + caption style
export HEYGEN_API_KEY=...                 # HeyGen → Settings → API
python -m heygen_dub languages            # exact language names your account accepts
```

## Usage

```bash
# Translate one or more videos (local files or public URLs)
python -m heygen_dub run reel_01.mp4 reel_02.mp4

# One-off language override / cheaper fast mode
python -m heygen_dub run reel_01.mp4 -l "English (United States)" "German (Germany)" --mode speed

# Check what would be sent, without spending credits
python -m heygen_dub run reel_01.mp4 --dry-run
```

Output:

```
output/reel-01/
  reel-01.english-united-states.mp4   ← ready to post
  reel-01.german-germany.mp4
  english-united-states/
    dubbed.mp4              HeyGen output, no captions
    captions.heygen.srt     HeyGen's translated transcript
    captions.srt            re-chunked captions (edit me)
    captions.ass            styled subtitles that get burned in
  state.json                translation ids + progress
```

### Resuming and fixing

- **Re-running is safe.** Translation ids are saved in `state.json`. Running the same command again resumes polling or downloading and does not submit a paid translation twice. If you interrupt the run with Ctrl-C, or it times out, run the same command again.
- **Failed languages** are reported in the summary, and the process exits with code 1. Use `--retry-failed` to resubmit only those languages.
- **Proofreading captions:** edit `output/<video>/<language>/captions.srt`, then run
  `python -m heygen_dub burn output/<video>`. Your edits are never overwritten.
- **Changing caption style:** edit `[captions]` in `config.toml`, then run `burn` again. HeyGen is not called.

## Caption style

All sizes are fractions of the video height, so one style works for 9:16, 1:1 and 16:9 videos. The defaults put white bold text with a black outline 18% up from the bottom, which keeps it clear of the TikTok/Reels/Shorts buttons, with at most 2 lines of about 22 characters. Set `box = true` for a semi-transparent background box instead of an outline, and `uppercase = true` for all-caps captions. For Japanese, Arabic, Hindi and other non-Latin scripts, set a font that has those glyphs under `[captions.fonts]`. Chinese, Japanese and Thai captions are wrapped by character, because those languages don't separate words with spaces.

## Notes / caveats

- **Lip-sync quality:** `mode = "precision"` is HeyGen's lip-sync mode. It works best with one clearly visible face, little head turning and clean audio. Set `speaker_num` in `[heygen.request_options]` if more than one person speaks.
- **Source language:** HeyGen detects the source language from the audio. Before starting a batch, run one short Slovak clip into one language to confirm that detection works.
- **Upload limit:** direct uploads are limited to 32 MB. For larger files, pass a public or presigned URL (S3, GCS and similar) that stays valid for at least 2 hours.
- **API field names:** the HeyGen request and response details are all in `heygen_dub/heygen.py`, so changes to HeyGen's API only need fixing there. Fields under `[heygen.request_options]` are passed to HeyGen unchanged. Use them to switch on extra options such as `enable_speech_enhancement` or `callback_url` without editing code.

## Tests

```bash
pip install pytest
python -m pytest
```

The tests run the whole pipeline against a fake HeyGen server: upload, batch submit, polling, a failed language, `--retry-failed`, resuming, and caption burn-in checked on real video frames. They make no network calls and use no credits.
