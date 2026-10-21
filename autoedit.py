#!/usr/bin/env python3
"""Why This Sucks auto-editor: stills + voiceover + captions -> finished 1080p MP4.

Free and local: uses only ffmpeg and Python (numpy). See README.md.
"""
import argparse, concurrent.futures as cf, csv, json, math, os, random, re
import shutil, subprocess, sys, tempfile, time, wave

import numpy as np

SR = 44100
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
AUDIO_EXT = (".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac")

DEFAULT_CONFIG = {
    "aspect": "16:9",                  # 16:9 (YouTube), 9:16 (Shorts/Reels/TikTok), 1:1, 4:5
    "fit": "pan",                      # pan = fill the frame and pan across; blur = fit whole image on a blurred background
    "max_duration": 0,                 # seconds; 0 = full length. Trims with a clean fade (e.g. 60 for Shorts)
    "fps": 30,
    "crf": 20, "preset": "medium",
    "workers": 0,                      # 0 = auto
    "seed": 7,
    # Ken Burns / punch-ins
    "kenburns_zoom": 0.12,             # how much each shot zooms/pans (0.05 subtle - 0.25 strong)
    "punchin_after": 3.5,              # split any image shown longer than this (seconds)
    "segment_target": 3.0,             # aim for a visual change roughly this often
    "punchin_tightness": [0.55, 0.72], # punch-in crop size as fraction of the image
    # captions
    "captions": True,
    "font": "Arial Black", "fonts_dir": "",
    "font_size": 110, "outline": 6, "shadow": 3,   # font_size is for 16:9 1080p; other ratios scale automatically
    "text_color": "#FFFFFF", "highlight_color": "#FFE135", "outline_color": "#000000",
    "uppercase": True, "max_words": 4, "min_words": 2,
    "caption_y": 0,                    # 0 = automatic; else caption position as fraction of height (0.65 = 65% down)
    "highlight_current_word": True, "pop_in": True,
    # music
    "music_volume_db": -16,            # music loudness relative to the voice (LUFS difference) before ducking
    "duck": True,                      # sidechain-duck music under the voice
    "music_fade_in": 2.0, "music_fade_out": 4.0,
    # sound effects
    "whoosh": True, "whoosh_volume_db": -16, "whoosh_min_gap": 6.0,
    "whoosh_on_punchins": False,
    "sfx_volume_db": -12, "sfx_cooldown": 6.0,
    "keyword_sfx": {
        "fire": "crackle", "flame": "crackle", "flames": "crackle", "burn": "crackle",
        "burning": "crackle", "burns": "crackle",
        "score": "stamp", "rating": "stamp", "verdict": "stamp", "rated": "stamp",
        "roar": "boom", "explode": "boom", "explosion": "boom", "crash": "boom", "boom": "boom",
        "money": "ding", "gold": "ding", "cost": "ding", "costs": "ding", "dollars": "ding",
        "pop": "pop",
    },
    # loudness
    "target_lufs": -14.0, "true_peak": -1.0,
}


ASPECTS = {  # size, caption font multiplier, caption y (fraction of height), side margin, max words
    "16:9": ((1920, 1080), 1.00, 0.88, 120, 4),
    "9:16": ((1080, 1920), 0.92, 0.65, 80, 3),
    "1:1": ((1080, 1080), 0.80, 0.82, 70, 3),
    "4:5": ((1080, 1350), 0.82, 0.76, 70, 3),
}


def log(*a):
    print("[autoedit]", *a, flush=True)


def run(cmd, **kw):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit("ffmpeg/ffprobe failed: " + " ".join(cmd[:6]) + " ...")
    return r


def probe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    return float(r.stdout.strip())


# ---------------------------------------------------------------- scenes
def parse_time(s):
    s = str(s).strip().replace(",", ".")
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        t = 0.0
        for p in parts:
            t = t * 60 + p
        return t
    return float(s)


def load_scenes(images_dir, scenes_file, total):
    imgs = sorted(f for f in os.listdir(images_dir) if f.lower().endswith(IMG_EXT))
    if not imgs:
        raise SystemExit("No images found in " + images_dir)
    scenes = []
    if scenes_file:
        if scenes_file.lower().endswith(".json"):
            data = json.load(open(scenes_file, encoding="utf-8"))
            if isinstance(data, dict):
                data = [{"image": k, "start": v} for k, v in data.items()]
            for d in data:
                scenes.append((d["image"], parse_time(d["start"])))
        else:
            with open(scenes_file, newline="", encoding="utf-8-sig") as f:
                for row in csv.reader(f):
                    if len(row) < 2 or row[0].strip().lower() in ("image", "file", "filename"):
                        continue
                    scenes.append((row[0].strip(), parse_time(row[1])))
        for name, _ in scenes:
            if not os.path.exists(os.path.join(images_dir, name)):
                raise SystemExit("Scenes file mentions missing image: " + name)
        scenes.sort(key=lambda s: s[1])
        scenes[0] = (scenes[0][0], 0.0)
    else:
        step = total / len(imgs)
        scenes = [(n, i * step) for i, n in enumerate(imgs)]
    out = []
    for i, (n, st) in enumerate(scenes):
        en = scenes[i + 1][1] if i + 1 < len(scenes) else total
        if en > st and st < total:
            out.append({"image": os.path.join(images_dir, n), "start": st, "end": min(en, total)})
    return out


# ---------------------------------------------------------------- shot plan
def kb_move(i, rng, s):
    moves = [
        (1.0, 1 + s, 0.5, 0.5, 0.5, 0.5),          # zoom in centre
        (1 + s, 1.0, 0.5, 0.5, 0.5, 0.5),          # zoom out
        (1 + s, 1 + s, 0.0, 1.0, 0.5, 0.5),        # pan left -> right
        (1 + s, 1 + s, 1.0, 0.0, 0.5, 0.5),        # pan right -> left
        (1.0, 1 + s, 0.5, 0.2, 0.5, 0.3),          # zoom in to upper-left
        (1.0, 1 + s, 0.5, 0.8, 0.5, 0.7),          # zoom in to lower-right
        (1 + s, 1 + s, 0.5, 0.5, 1.0, 0.0),        # tilt up
        (1 + s, 1.0, 0.8, 0.5, 0.3, 0.5),          # zoom out from upper-right
    ]
    return moves[(i * 5 + rng.randrange(3)) % len(moves)]


def plan_shots(scenes, cfg, rng):
    fps = cfg["fps"]
    lo, hi = cfg["punchin_tightness"]
    crops = [("center", 0.5, 0.45), ("left", 0.22, 0.45), ("right", 0.78, 0.45),
             ("top", 0.5, 0.25), ("center", 0.5, 0.55)]
    shots, k = [], 0
    for si, sc in enumerate(scenes):
        d = sc["end"] - sc["start"]
        n = 1
        if d > cfg["punchin_after"]:
            n = max(2, round(d / cfg["segment_target"]))
        prev_crop = None
        for j in range(n):
            st = sc["start"] + d * j / n
            en = sc["start"] + d * (j + 1) / n
            if j == 0:
                crop = None
            else:
                choices = [c for c in crops if c != prev_crop]
                c = rng.choice(choices)
                crop = (c[1], c[2], rng.uniform(lo, hi))
                prev_crop = c
            shots.append({"image": sc["image"], "start": st, "end": en, "crop": crop,
                          "move": kb_move(k, rng, cfg["kenburns_zoom"]),
                          "new_scene": j == 0, "index": k})
            k += 1
    # exact frame boundaries so the total frame count matches the audio
    for s in shots:
        s["f0"] = int(round(s["start"] * fps))
        s["f1"] = int(round(s["end"] * fps))
    for a, b in zip(shots, shots[1:]):
        a["f1"] = b["f0"]
    shots = [s for s in shots if s["f1"] > s["f0"]]
    return shots


def ease(den):
    return "(0.5-0.5*cos(PI*{v}/%d))" % den


def kb_filters(move, N, W, H, fps):
    """zoompan Ken Burns for an input whose aspect already equals W:H (input at 2x size)."""
    z0, z1, x0, x1, y0, y1 = move
    p = ease(max(N - 1, 1)).replace("{v}", "on")
    z = "{a}+({b}-{a})*{p}".format(a=z0, b=z1, p=p)
    x = "(iw-iw/zoom)*({a}+({b}-{a})*{p})".format(a=x0, b=x1, p=p)
    y = "(ih-ih/zoom)*({a}+({b}-{a})*{p})".format(a=y0, b=y1, p=p)
    return "zoompan=z='{z}':x='{x}':y='{y}':d={N}:s={W}x{H}:fps={fps}".format(z=z, x=x, y=y, N=N, W=W, H=H, fps=fps)


def punch_crop(shot):
    if not shot["crop"]:
        return []
    cx, cy, t = shot["crop"]
    return ["crop=iw*{t}:ih*{t}:(iw-iw*{t})*{cx}:(ih-ih*{t})*{cy}".format(t=t, cx=cx, cy=cy)]


def image_aspect(path, cache={}):
    if path not in cache:
        r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", path])
        w, h = [int(v) for v in r.stdout.strip().split(",")[:2]]
        cache[path] = w / h
    return cache[path]


def render_shot(shot, cfg, tmp):
    W, H, fps = cfg["width"], cfg["height"], cfg["fps"]
    N = shot["f1"] - shot["f0"]
    out = os.path.join(tmp, "shot_%04d.mp4" % shot["index"])
    src_ar = image_aspect(shot["image"])
    if shot["crop"]:
        src_ar = src_ar  # punch crop keeps aspect
    mismatch = abs(src_ar - W / H) > 0.05
    enc = ["-frames:v", str(N), "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", out]
    if not mismatch:
        vf = ",".join(punch_crop(shot) + [
            "scale={0}:{1}:force_original_aspect_ratio=increase:flags=lanczos,crop={0}:{1}".format(W * 2, H * 2),
            "setsar=1", kb_filters(shot["move"], N, W, H, fps), "format=yuv420p"])
        run(["ffmpeg", "-y", "-v", "error", "-i", shot["image"], "-vf", vf] + enc)
        return out
    if cfg["fit"] == "blur":
        # foreground: whole image fitted to width (or height), Ken Burns inside; background: blurred fill
        if src_ar > W / H:
            fw = W; fh = int(round(W / src_ar / 2)) * 2
        else:
            fh = H; fw = int(round(H * src_ar / 2)) * 2
        fy = int(max(0, min(H - fh, H * cfg["_fg_center"] - fh / 2)))
        fx = (W - fw) // 2
        fc = ("[0:v]split=2[a][b];"
              "[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:3,"
              "eq=brightness=-0.18:saturation=0.8,setsar=1,loop=loop={N}:size=1:start=0,fps={fps}[bg];"
              "[b]{pc}scale={fw2}:{fh2}:force_original_aspect_ratio=increase:flags=lanczos,crop={fw2}:{fh2},setsar=1,"
              "{kb}[fg];[bg][fg]overlay={fx}:{fy}:shortest=1,format=yuv420p[v]").format(
            W=W, H=H, N=N, fps=fps, fw2=fw * 2, fh2=fh * 2, fx=fx, fy=fy,
            pc="".join(c + "," for c in punch_crop(shot)), kb=kb_filters(shot["move"], N, fw, fh, fps))
        run(["ffmpeg", "-y", "-v", "error", "-i", shot["image"], "-filter_complex", fc, "-map", "[v]"] + enc)
        return out
    # pan: fill the frame; animate the crop window across the image's longer side,
    # centre-weighted so the middle (where the subject usually is) is always covered.
    z0, z1, x0, x1, y0, y1 = shot["move"]
    wide = src_ar > W / H
    a0, a1 = (0.25, 0.75) if shot["index"] % 2 == 0 else (0.75, 0.25)
    if shot["crop"]:
        a0, a1 = 0.5 + (a0 - 0.5) * 0.5, 0.5 + (a1 - 0.5) * 0.5
    p = ease(max(N - 1, 1)).replace("{v}", "n")
    pos = "({a}+({b}-{a})*{p})".format(a=a0, b=a1, p=p)
    cx = "(iw-ow)*" + pos if wide else "(iw-ow)/2"
    cy = "(ih-oh)/2" if wide else "(ih-oh)*" + pos
    zs = 1 + (max(z0, z1) - 1) * 0.5          # gentle zoom on top of the pan
    zin = z1 >= z0
    zz = "{a}+({b}-{a})*{p}".format(a=1.0 if zin else zs, b=zs if zin else 1.0, p=ease(max(N - 1, 1)).replace("{v}", "in"))
    vf = ",".join(punch_crop(shot) + [
        "scale={0}:{1}:force_original_aspect_ratio=increase:flags=lanczos".format(W * 2, H * 2),
        "setsar=1", "loop=loop={N}:size=1:start=0".format(N=N), "fps=%d" % fps,
        "crop={0}:{1}:x='{x}':y='{y}'".format(W * 2, H * 2, x=cx, y=cy),
        "zoompan=z='{z}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={W}x{H}:fps={fps}".format(z=zz, W=W, H=H, fps=fps),
        "format=yuv420p"])
    run(["ffmpeg", "-y", "-v", "error", "-i", shot["image"], "-vf", vf] + enc)
    return out


# ---------------------------------------------------------------- captions
TS = r"(\d+:)?\d+:\d+[.,]\d+"


def parse_captions(path):
    txt = open(path, encoding="utf-8-sig", errors="replace").read().replace("\r", "")
    cues = []
    for l in txt.split("\n"):
        m = re.match(r"\s*(" + TS + r")\s*-->\s*(" + TS + r")", l)
        if m:
            cues.append((parse_time(m.group(1)), parse_time(m.group(3)), []))
        elif cues:
            cues[-1][2].append(l)
    for c in cues:  # drop SRT index numbers that precede the next cue
        while c[2] and re.fullmatch(r"\s*\d*\s*", c[2][-1]):
            c[2].pop()
    words = []
    if any("<c>" in "".join(c[2]) or re.search(r"<" + TS + ">", "".join(c[2])) for c in cues):
        # YouTube-style word-timed VTT: only the lines carrying inline timestamps are new words
        prev_line = None
        for st, en, lines in cues:
            lines = [l for l in lines if l.strip()]
            if not lines or en - st < 0.05:
                continue
            l = lines[-1]          # the newest line of a rolling cue
            plain = re.sub(r"<[^>]+>", "", l).strip()
            if plain == prev_line:
                continue
            prev_line = plain
            parts = re.split(r"<(" + TS + r")>", l)
            seq = [(st, parts[0])]
            i = 1
            while i < len(parts):
                seq.append((parse_time(parts[i]), parts[i + 2] if i + 2 < len(parts) else ""))
                i += 3
            for t, chunk in seq:
                for w in re.sub(r"<[^>]+>", "", chunk).split():
                    words.append([t, None, w])
            if words:
                words[-1][1] = en
    else:
        prev = []
        for st, en, lines in cues:
            all_lines = [re.sub(r"<[^>]+>", "", l).strip() for l in lines]
            lines = [l for l in all_lines if l and l not in prev]  # drop rolling repeats
            prev = all_lines
            ws = " ".join(lines).split()
            if not ws:
                continue
            lens = [len(w) + 1 for w in ws]
            tot = sum(lens)
            t = st
            for w, L in zip(ws, lens):
                d = (en - st) * L / tot
                words.append([t, t + d, w])
                t += d
    words.sort(key=lambda w: w[0])
    for i, w in enumerate(words):
        nxt = words[i + 1][0] if i + 1 < len(words) else None
        if w[1] is None or (nxt is not None and w[1] > nxt):
            w[1] = nxt if nxt is not None else w[0] + 0.6
        if nxt is not None and w[1] - w[0] > 1.2 and nxt - w[0] > 1.2:
            w[1] = w[0] + 1.0   # long silence after the word: don't hold it forever
        w[1] = max(w[1], w[0] + 0.05)
    return [tuple(w) for w in words]


def chunk_words(words, cfg):
    chunks, cur = [], []
    mx, mn = cfg["max_words"], cfg["min_words"]
    for i, w in enumerate(words):
        if cur and (w[0] - cur[-1][1] > 0.5):
            chunks.append(cur); cur = []
        cur.append(w)
        punct = re.search(r"[.,!?;:]$", w[2])
        if len(cur) >= mx or (punct and len(cur) >= mn) or (punct and re.search(r"[.!?]$", w[2])):
            chunks.append(cur); cur = []
    if cur:
        chunks.append(cur)
    return chunks


def ass_color(hexc):
    h = hexc.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return "&H00%s%s%s" % (b, g, r)


def ass_time(t):
    t = max(t, 0)
    cs = int(round(t * 100))
    return "%d:%02d:%02d.%02d" % (cs // 360000, cs // 6000 % 60, cs // 100 % 60, cs % 100)


def clean_word(w, cfg):
    w = w.replace("{", "(").replace("}", ")").replace("\\", "/")
    return w.upper() if cfg["uppercase"] else w


def write_ass(chunks, cfg, path):
    W, H = cfg["width"], cfg["height"]
    hdr = """[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{fs},{pc},{pc},{oc},&H80000000,-1,0,0,0,100,100,1,0,1,{ol},{sh},2,{ml},{ml},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""".format(W=W, H=H, font=cfg["font"], fs=cfg["_font_px"], ml=cfg["_margin_lr"], pc=ass_color(cfg["text_color"]),
           oc=ass_color(cfg["outline_color"]), ol=round(cfg["outline"] * cfg["_font_px"] / 110, 1), sh=round(cfg["shadow"] * cfg["_font_px"] / 110, 1),
           mv=int(H * (1 - cfg["_caption_y"])))
    hl = ass_color(cfg["highlight_color"])
    lines = []
    for ci, ch in enumerate(chunks):
        c_start = ch[0][0]
        c_end = ch[-1][1]
        if ci + 1 < len(chunks):
            c_end = min(max(c_end, c_start + 0.3), chunks[ci + 1][0][0])
        ws = [clean_word(w[2], cfg) for w in ch]
        pop = r"{\fscx70\fscy70\t(0,90,\fscx108\fscy108)\t(90,160,\fscx100\fscy100)}" if cfg["pop_in"] else ""
        if not cfg["highlight_current_word"]:
            lines.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s%s" % (ass_time(c_start), ass_time(c_end), pop, " ".join(ws)))
            continue
        for k, w in enumerate(ch):
            s = c_start if k == 0 else w[0]
            e = ch[k + 1][0] if k + 1 < len(ch) else c_end
            if e <= s:
                continue
            txt = " ".join((r"{\c%s}%s{\c%s}" % (hl, x, ass_color(cfg["text_color"]))) if j == k else x
                           for j, x in enumerate(ws))
            lines.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s%s" % (ass_time(s), ass_time(e), pop if k == 0 else "", txt))
    with open(path, "w", encoding="utf-8") as f:
        f.write(hdr + "\n".join(lines) + "\n")


# ---------------------------------------------------------------- audio synthesis
def write_wav(path, x):
    x = np.clip(x, -1, 1)
    if x.ndim == 1:
        x = np.stack([x, x], 1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def onepole(x, a):
    """Simple low-pass; a may be scalar or per-sample array (0..1, higher = brighter)."""
    y = np.empty_like(x)
    a = np.broadcast_to(np.asarray(a, dtype=float), x.shape)
    acc = 0.0
    for i in range(len(x)):
        acc += a[i] * (x[i] - acc)
        y[i] = acc
    return y


def gen_pad(dur, rng):
    """Quiet, slow ambient pad (A minor-ish progression), procedurally generated, royalty-free."""
    n = int(dur * SR) + SR
    t = np.arange(n) / SR
    chords = [[57, 60, 64, 69], [53, 57, 60, 65], [48, 55, 60, 64], [55, 59, 62, 67]]  # Am F C G
    clen = 8.0
    L = np.zeros(n); R = np.zeros(n)
    for ci in range(int(math.ceil(n / SR / clen)) + 1):
        notes = chords[ci % 4]
        s0 = int(max(ci * clen - 2.0, 0) * SR); s1 = min(int((ci * clen + clen + 2.0) * SR), n)
        if s0 >= n:
            break
        tt = t[s0:s1]
        loc = tt - ci * clen
        env = np.clip(np.minimum((loc + 2.0) / 3.0, (clen + 2.0 - loc) / 3.0), 0, 1) ** 1.5
        for k, m in enumerate(notes):
            f = 440 * 2 ** ((m - 69) / 12)
            for det, pan in ((-0.12, 0.2), (0.12, 0.8)):
                ff = f * 2 ** (det / 12)
                ph = rng.uniform(0, 6.28)
                v = np.sin(2 * np.pi * ff * tt + ph) + 0.25 * np.sin(4 * np.pi * ff * tt + ph)
                v *= env * (0.6 if k else 0.9)
                L[s0:s1] += v * (1 - pan); R[s0:s1] += v * pan
        f0 = 440 * 2 ** ((notes[0] - 12 - 69) / 12)  # soft bass
        b = np.sin(2 * np.pi * f0 * tt) * env * 0.8
        L[s0:s1] += b; R[s0:s1] += b
    trem = 1 + 0.15 * np.sin(2 * np.pi * 0.11 * t)
    x = np.stack([L * trem, R * trem], 1)
    x /= np.max(np.abs(x)) + 1e-9
    return x[: int(dur * SR)] * 0.5


def gen_sfx(name, rng):
    def env_exp(n, k):
        return np.exp(-np.arange(n) / SR * k)
    if name == "whoosh":
        n = int(0.6 * SR); t = np.arange(n) / n
        noise = rng_noise(rng, n)
        env = np.sin(np.pi * t) ** 2
        bright = 0.02 + 0.25 * np.sin(np.pi * t) ** 2
        x = onepole(noise, bright) * env
        x = x - onepole(x, 0.01)
        pan = t
        x = np.stack([x * (1 - pan * 0.6), x * (0.4 + pan * 0.6)], 1)
    elif name == "crackle":
        n = int(1.4 * SR)
        x = onepole(rng_noise(rng, n), 0.05) * 0.4
        pops = np.zeros(n)
        idx = rng.integers(0, n - 200, 90)
        for i in idx:
            pops[i:i + 60] += rng.uniform(-1, 1) * env_exp(60, 3000)
        x = (x + pops) * np.minimum(1, np.linspace(3, 0, n))
    elif name == "stamp":
        n = int(0.5 * SR); t = np.arange(n) / SR
        x = np.sin(2 * np.pi * (90 - 40 * t) * t) * env_exp(n, 14)
        x += onepole(rng_noise(rng, n), 0.3) * env_exp(n, 60) * 0.8
    elif name == "boom":
        n = int(1.5 * SR); t = np.arange(n) / SR
        x = np.sin(2 * np.pi * (60 - 25 * t) * t) * env_exp(n, 3)
        x += onepole(rng_noise(rng, n), 0.03) * env_exp(n, 4) * 0.7
    elif name == "ding":
        n = int(1.2 * SR); t = np.arange(n) / SR
        x = (np.sin(2 * np.pi * 1318 * t) + 0.4 * np.sin(2 * np.pi * 2637 * t)) * env_exp(n, 4)
    elif name == "pop":
        n = int(0.15 * SR); t = np.arange(n) / SR
        x = np.sin(2 * np.pi * (600 - 3000 * t) * t) * env_exp(n, 40)
    else:
        return None
    x = np.asarray(x)
    return x / (np.max(np.abs(x)) + 1e-9) * 0.9


def rng_noise(rng, n):
    return rng.uniform(-1, 1, n)


def load_audio(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode:
        raise SystemExit("Could not read audio " + path)
    return np.frombuffer(r.stdout, dtype="<f4").reshape(-1, 2)


def get_sfx(name, sfx_dir, cache, nrng):
    if name in cache:
        return cache[name]
    x = None
    if sfx_dir and os.path.isdir(sfx_dir):
        for f in os.listdir(sfx_dir):
            if os.path.splitext(f)[0].lower() == name.lower() and f.lower().endswith(AUDIO_EXT):
                x = load_audio(os.path.join(sfx_dir, f)); break
    if x is None:
        x = gen_sfx(name, nrng)
        if x is not None and x.ndim == 1:
            x = np.stack([x, x], 1)
    cache[name] = x
    return x


def build_sfx_track(shots, words, total, cfg, sfx_dir, nrng, path):
    n = int(total * SR) + SR
    track = np.zeros((n, 2))
    cache, hits = {}, []

    def place(x, t, db):
        if x is None:
            return
        s = int(max(t, 0) * SR); e = min(s + len(x), n)
        if e > s:
            track[s:e] += x[: e - s] * 10 ** (db / 20)

    if cfg["whoosh"]:
        last = -1e9
        for s in shots[1:]:
            if (s["new_scene"] or cfg["whoosh_on_punchins"]) and s["start"] - last >= cfg["whoosh_min_gap"]:
                place(get_sfx("whoosh", sfx_dir, cache, nrng), s["start"] - 0.3, cfg["whoosh_volume_db"])
                hits.append(("whoosh", round(s["start"], 2)))
                last = s["start"]
    lastk = {}
    kmap = {k.lower(): v for k, v in cfg["keyword_sfx"].items()}
    for st, en, w in words:
        key = re.sub(r"[^a-z0-9']", "", w.lower())
        if key in kmap:
            name = kmap[key]
            if st - lastk.get(name, -1e9) >= cfg["sfx_cooldown"]:
                x = get_sfx(name, sfx_dir, cache, nrng)
                if x is not None:
                    place(x, st, cfg["sfx_volume_db"]); hits.append((name, round(st, 2), w)); lastk[name] = st
    write_wav(path, track[: int(total * SR)])
    return hits


def measure_lufs(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "ebur128", "-f", "null", "-"],
                       stderr=subprocess.PIPE, text=True)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else -23.0


def mix_audio(vo, music, sfx, total, cfg, tmp, fade=0):
    mixed = os.path.join(tmp, "mix.wav")
    gain = measure_lufs(vo) + cfg["music_volume_db"] - measure_lufs(music)
    fi, fo = cfg["music_fade_in"], cfg["music_fade_out"]
    mus = ("[1:a]aformat=sample_rates=44100:channel_layouts=stereo,atrim=0:{T},asetpts=N/SR/TB,"
           "volume={db}dB,afade=t=in:d={fi},afade=t=out:st={fos}:d={fo}[m0];").format(
        T=total, db="%.2f" % gain, fi=fi, fo=fo, fos=max(total - fo, 0))
    voc = "[0:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,atrim=0:{T}[v0];".format(T=total)
    if cfg["duck"]:
        duck = ("[v0]asplit=2[v1][vsc];[m0][vsc]sidechaincompress=threshold=0.05:ratio=3:attack=50:release=700[m1];")
    else:
        duck = "[v0]acopy[v1];[m0]acopy[m1];"
    fc = voc + mus + duck + "[2:a]aformat=sample_rates=44100:channel_layouts=stereo[s0];" \
        "[v1][m1][s0]amix=inputs=3:normalize=0:duration=first[out]"
    run(["ffmpeg", "-y", "-v", "error", "-i", vo, "-stream_loop", "-1", "-i", music, "-i", sfx,
         "-filter_complex", fc, "-map", "[out]", "-t", str(total), "-c:a", "pcm_s16le", mixed])
    # two-pass loudnorm to target LUFS
    I, TP = cfg["target_lufs"], cfg["true_peak"]
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", mixed, "-af",
                        "loudnorm=I=%s:TP=%s:LRA=11:print_format=json" % (I, TP), "-f", "null", "-"],
                       stderr=subprocess.PIPE, text=True)
    m = json.loads(r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1])
    final = os.path.join(tmp, "final_audio.wav")
    af = ("loudnorm=I={I}:TP={TP}:LRA=11:measured_I={mi}:measured_TP={mtp}:measured_LRA={ml}:"
          "measured_thresh={mt}:offset={off}:linear=true,aresample=48000").format(
        I=I, TP=TP, mi=m["input_i"], mtp=m["input_tp"], ml=m["input_lra"], mt=m["input_thresh"],
        off=m["target_offset"])
    if fade:
        af += ",afade=t=out:st=%.3f:d=%.3f" % (total - fade, fade)
    run(["ffmpeg", "-y", "-v", "error", "-i", mixed, "-af", af, "-c:a", "pcm_s16le", final])
    return final


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Auto-edit a 'Why This Sucks' style video.")
    ap.add_argument("--images", required=True, help="folder of images (sorted by filename)")
    ap.add_argument("--audio", required=True, help="voiceover audio file")
    ap.add_argument("--captions", help="timestamped captions (.srt or .vtt)")
    ap.add_argument("--scenes", help="optional CSV/JSON mapping image -> start time")
    ap.add_argument("--music", help="optional music file (otherwise an ambient pad is generated)")
    ap.add_argument("--sfx", help="optional folder of sound effects (whoosh.wav, crackle.wav, ...)")
    ap.add_argument("--config", help="optional JSON config file (see config.example.json)")
    ap.add_argument("--aspect", choices=list(ASPECTS), help="16:9 (default), 9:16, 1:1 or 4:5")
    ap.add_argument("--fit", choices=["pan", "blur"], help="how to fit images that don't match the aspect ratio")
    ap.add_argument("--max-duration", type=float, help="trim the video to this many seconds (e.g. 60 for Shorts)")
    ap.add_argument("--out", default="final.mp4")
    ap.add_argument("--keep-temp", action="store_true")
    a = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(tool + " not found. Install ffmpeg first (see README).")
    cfg = dict(DEFAULT_CONFIG)
    if a.config:
        user = json.load(open(a.config, encoding="utf-8"))
        cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    if a.aspect: cfg["aspect"] = a.aspect
    if a.fit: cfg["fit"] = a.fit
    if a.max_duration is not None: cfg["max_duration"] = a.max_duration
    if cfg["aspect"] not in ASPECTS:
        raise SystemExit("aspect must be one of " + ", ".join(ASPECTS))
    (W, H), fmul, cy, mlr, mw = ASPECTS[cfg["aspect"]]
    cfg["width"], cfg["height"] = W, H
    cfg["_font_px"] = int(round(cfg["font_size"] * fmul))
    cfg["_caption_y"] = cfg["caption_y"] or cy
    cfg["_margin_lr"] = mlr
    cfg["_fg_center"] = 0.40 if H > W else 0.5
    if "max_words" not in (user if a.config else {}):
        cfg["max_words"] = mw
    rng = random.Random(cfg["seed"]); nrng = np.random.default_rng(cfg["seed"])
    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="autoedit_")
    try:
        total = vo_len = probe_duration(a.audio)
        trimmed = bool(cfg["max_duration"]) and cfg["max_duration"] < total
        if trimmed:
            total = float(cfg["max_duration"])
        log("voiceover %.2fs%s, output %dx%d (%s, fit=%s)" % (vo_len, " -> trimmed to %.2fs" % total if trimmed else "",
                                                               cfg["width"], cfg["height"], cfg["aspect"], cfg["fit"]))
        scenes = load_scenes(a.images, a.scenes, total)
        shots = plan_shots(scenes, cfg, rng)
        log("%d images -> %d shots (incl. punch-ins)" % (len(scenes), len(shots)))

        words = parse_captions(a.captions) if a.captions else []
        if a.captions:
            log("%d caption words" % len(words))
        ass = os.path.join(tmp, "captions.ass")
        if words and cfg["captions"]:
            chunks = chunk_words(words, cfg)
            write_ass(chunks, cfg, ass)
            log("%d caption chunks" % len(chunks))

        music = a.music
        if not music:
            music = os.path.join(tmp, "pad.wav")
            write_wav(music, gen_pad(total + 1, nrng))
            log("generated ambient pad")
        sfx = os.path.join(tmp, "sfx.wav")
        hits = build_sfx_track(shots, words, total, cfg, a.sfx, nrng, sfx)
        log("sound hits: %s" % hits)
        words = [w for w in words if w[0] < total - 0.3]
        audio = mix_audio(a.audio, music, sfx, total, cfg, tmp, fade=1.0 if trimmed else 0)
        log("audio mixed + normalised")

        workers = cfg["workers"] or max(1, (os.cpu_count() or 2) // 2)
        with cf.ThreadPoolExecutor(workers) as ex:
            futs = [ex.submit(render_shot, s, cfg, tmp) for s in shots]
            for i, f in enumerate(cf.as_completed(futs)):
                f.result()
                if (i + 1) % 10 == 0 or i + 1 == len(futs):
                    log("rendered shots %d/%d" % (i + 1, len(futs)))
        with open(os.path.join(tmp, "list.txt"), "w") as f:
            for s in shots:
                f.write("file 'shot_%04d.mp4'\n" % s["index"])
        vf = "fps=%d" % cfg["fps"]
        if trimmed:
            vf += ",fade=t=out:st=%.3f:d=0.8" % (total - 0.8)
        if os.path.exists(ass):
            vf += ",ass=captions.ass" + (":fontsdir='%s'" % cfg["fonts_dir"].replace("\\", "/").replace(":", "\\:") if cfg["fonts_dir"] else "")
        out = os.path.abspath(a.out)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        log("final encode ...")
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-i", audio,
             "-vf", vf, "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", cfg["preset"],
             "-crf", str(cfg["crf"]), "-pix_fmt", "yuv420p", "-r", str(cfg["fps"]), "-c:a", "aac",
             "-b:a", "192k", "-t", "%.3f" % total, "-movflags", "+faststart", out], cwd=tmp)
        log("done: %s (%.1fs)" % (out, time.time() - t0))
    finally:
        if a.keep_temp:
            log("temp files kept in " + tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
