"""Render assets/demo.gif from the REAL output of examples/run_demo.py.

    pip install pillow          # dev-only; the hook itself needs nothing
    python tools/make_demo_gif.py

The demo is run, its output captured, and replayed as a terminal animation (Pillow draws
the frames, ffmpeg encodes the GIF with a generated palette). The GIF can never show
anything the hook does not actually print. Regenerate it whenever the demo changes.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "demo.gif")
COMMAND = "python examples/run_demo.py"

W, H, PAD, SIZE, COLS = 1100, 540, 22, 15, 118
BG, FG, DIM = (13, 17, 23), (201, 209, 217), (110, 118, 129)
RED, GREEN, YELLOW, BLUE = (255, 123, 114), (126, 231, 135), (255, 212, 121), (121, 192, 255)
FONTS = ["C:/Windows/Fonts/consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
         "/System/Library/Fonts/Menlo.ttc"]


def font():
    for p in FONTS:
        if os.path.exists(p):
            return ImageFont.truetype(p, SIZE)
    return ImageFont.load_default()


def colour(line):
    s = line.lstrip()
    if s.startswith("$"):
        return BLUE
    if s.startswith("User"):
        return FG
    if s.startswith("Claude"):
        return YELLOW
    if "BLOCKED" in s or s.startswith("honest-done"):
        return RED
    if "allowed" in s:
        return GREEN
    return DIM


def wrap(lines):
    out = []
    for line in lines:
        if line.startswith("Stop   : BLOCKED -> "):
            head, body = "Stop   : BLOCKED", line[len("Stop   : BLOCKED -> "):]
            body = body.split(" If this is intentional")[0]
            out.append((head, RED))
            for chunk in textwrap.wrap(body, COLS - 9):
                out.append(("         " + chunk, RED))
            continue
        for i, chunk in enumerate(textwrap.wrap(line, COLS) or [""]):
            out.append((("         " if i else "") + chunk, colour(line)))
    return out


def render(lines, f, cursor=False):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 30], fill=(30, 35, 44))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([14 + i * 20, 10, 26 + i * 20, 22], fill=c)
    d.text((W // 2 - 120, 8), "honest-done  |  real hook output", font=f, fill=DIM)
    lh = SIZE + 6
    visible = lines[-((H - 50 - PAD) // lh):]
    for i, (text, col) in enumerate(visible):
        d.text((PAD, 44 + i * lh), text, font=f, fill=col)
    if cursor and visible:
        x = PAD + d.textlength(visible[-1][0], font=f)
        y = 44 + (len(visible) - 1) * lh
        d.rectangle([x + 2, y, x + 10, y + SIZE + 2], fill=FG)
    return img


def main():
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    out = subprocess.run([sys.executable, os.path.join(ROOT, "examples", "run_demo.py")], cwd=ROOT,
                         stdout=subprocess.PIPE, check=True).stdout.decode("utf-8", "replace")
    # Timings vary per machine; the GIF shows the zero-token claim, not a ms figure.
    lines = [l for l in out.splitlines() if not l.strip().endswith("0 tokens") and not set(l) <= {"-"}]
    f = font()
    frames = []      # (image, seconds)
    shown = []
    prompt = "$ " + COMMAND
    for i in range(2, len(prompt) + 1, 2):
        frames.append((render([(prompt[:i], BLUE)], f, cursor=True), 0.05))
    frames.append((render([(prompt, BLUE)], f, cursor=True), 0.6))
    shown.append((prompt, BLUE))
    for line in lines:
        for item in wrap([line]):
            shown.append(item)
            frames.append((render(shown, f), 0.05))
        pause = 1.8 if line.startswith("Stop") else 0.7 if line.startswith("Claude") else 0.35
        frames.append((render(shown, f), pause))
        if line.startswith("Stop"):
            shown.append(("", FG))
    shown.append(("3 'All done!' turns blocked, 1 honest turn allowed. 0 LLM calls, 0 extra tokens.", GREEN))
    frames.append((render(shown, f), 4.0))
    tmp = tempfile.mkdtemp(prefix="hd-gif-")
    try:
        concat = os.path.join(tmp, "list.txt")
        with open(concat, "w") as fh:
            for i, (img, dur) in enumerate(frames):
                p = os.path.join(tmp, "f%04d.png" % i)
                img.save(p)
                fh.write("file '%s'\nduration %.2f\n" % (p.replace("\\", "/"), dur))
            fh.write("file '%s'\n" % os.path.join(tmp, "f%04d.png" % (len(frames) - 1)).replace("\\", "/"))
        vf = "fps=20,split[a][b];[a]palettegen=max_colors=32[p];[b][p]paletteuse=dither=none"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", concat,
                        "-vf", vf, "-loop", "0", OUT], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("wrote %s (%d frames, %.0f KB)" % (OUT, len(frames), os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    main()
