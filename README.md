# Why This Sucks – Auto-Editor

A free tool that runs on your own computer and turns **a folder of pictures + a voiceover + a caption file** into a finished YouTube video (1920×1080, 30 fps, MP4). There are no subscriptions or paid services; it only uses the free programs **ffmpeg** and **Python**.

What it does automatically:

1. **Slow Ken Burns moves**: every picture slowly zooms or pans, and the direction changes from shot to shot.
2. **Punch-in cuts**: if a picture stays on screen longer than about 3.5 s, the tool cuts to a tighter crop of the same picture (centre, left, right or top), so something changes on screen every 2–4 s.
3. **Bold animated captions**: 2–5 words at a time in big white text with a black outline. Each chunk pops in and the word being spoken turns yellow.
4. **Background music** sits well below the voice and dips further while you talk, with a fade in and fade out. If you don't supply music, the tool makes a quiet ambient pad for you, so you can never get a copyright claim from it.
5. **Sound hits**: a soft whoosh on some picture changes (not every cut), plus effects triggered by words in the captions (e.g. "fire" → crackle, "score" → stamp). If you don't supply effects, the tool generates them.
6. **Loudness** is normalised to about −14 LUFS, which is YouTube's level.

---

## 1. Install (one time)

**ffmpeg**
- Windows: open PowerShell and run `winget install Gyan.FFmpeg`, then close and reopen PowerShell.
- Mac: install [Homebrew](https://brew.sh), then run `brew install ffmpeg`.
- Linux: `sudo apt install ffmpeg`

**Python 3.9+**: get it from [python.org](https://www.python.org/downloads/). On Windows, tick **"Add Python to PATH"** during setup.

**This tool**: download it (green **Code** button → *Download ZIP*) and unzip it. Then open a terminal in the folder and run:

```
pip install -r requirements.txt
```

To check that everything is installed, run `ffmpeg -version` and `python --version`. Both should print a version number.

## 2. Prepare your files

```
my_video/
  images/          01.png, 02.png, 03.png ...   (shown in filename order)
  voiceover.mp3
  captions.srt     (or .vtt; the timestamped export from your app)
```

The tool reads timing from **captions with word-level timestamps** (such as YouTube-style VTT) when they exist. Otherwise it spreads each caption's words evenly across that caption's time.

**Optional: control when each picture appears.** Without this file, the pictures are spaced evenly across the voiceover. To set the times yourself, make a `scenes.csv` file:

```
image,start
01.png,0
02.png,4.1
03.png,0:14.2
```

(A JSON file `[{"image":"01.png","start":0}, ...]` also works.)

## 3. Run it

```
python autoedit.py --images my_video/images --audio my_video/voiceover.mp3 --captions my_video/captions.srt --out final.mp4
```

Optional extras:

| option | what it does |
|---|---|
| `--scenes my_video/scenes.csv` | sets when each picture starts |
| `--music song.mp3` | uses your own music (it loops if it's too short) |
| `--sfx my_sfx_folder` | uses your own sound effects: name the files `whoosh.wav`, `crackle.wav`, `stamp.wav`, `boom.wav`, `ding.wav`, `pop.wav`, or any name you use in the keyword list |
| `--config my_config.json` | changes settings (see below) |

A 3-minute video takes roughly 1–3 minutes to render on a normal laptop.

## 4. Settings (config file)

Copy `config.example.json` to `my_config.json`, keep only the lines you want to change, and pass it with `--config my_config.json`. Useful settings:

- `font`: the caption font. The default is `"Arial Black"` (on Windows and Mac). Any installed font works, e.g. `"Impact"`, `"Anton"` or `"Poppins Black"`. To use a font file without installing it, put the file in a folder and set `fonts_dir`.
- `font_size`, `highlight_color`, `uppercase`, `max_words`, `caption_margin_bottom`: caption look.
- `kenburns_zoom`: how strong the zoom and pan are (0.05 is subtle, 0.25 is strong).
- `punchin_after` / `segment_target`: when to add punch-in cuts and how often.
- `music_volume_db`: how far the music sits below the voice (−16 by default; lower numbers make it quieter). Set `duck` to turn ducking on or off.
- `whoosh`, `whoosh_volume_db`, `whoosh_min_gap`: whoosh sounds.
- `keyword_sfx`: list of word → sound name, e.g. `{"fire": "crackle", "score": "stamp", "lava": "boom"}`.
- `target_lufs`: final loudness (−14 by default).

## Troubleshooting

- **"ffmpeg not found"**: ffmpeg isn't installed or isn't on your PATH. Reopen your terminal after installing it.
- **Captions use a plain font**: the font named in `font` isn't installed. Pick one that is, or set `fonts_dir`.
- **Blurry pictures**: use images that are at least 1920×1080.

## License

MIT, see [LICENSE](LICENSE).
