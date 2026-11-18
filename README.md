# postflight

**postflight** burns the flight-controller OSD recorded by Walksnail Avatar goggles
(`.osd`) into an MP4. The script keeps the original video frame timestamps,
including the long gaps/freezes that Walksnail DVRs sometimes contain.

`uv sync --locked` installs the `postflight` command; everything in this directory
is the postflight project.

## Requirements

- [uv](https://docs.astral.sh/uv/) (tested with `uv 0.8.5`)
- Python **3.11.13**. It is pinned in `.python-version` and restricted by
  `requires-python`; `uv` downloads it automatically if it is not installed.
- **No system `ffmpeg` or `ffprobe` is required.** PyAV ships its own FFmpeg
  build, including libx264.
- The video and its matching `.osd` file (same basename by default).
- **No network access is needed** for the normal 720p/1080p case: the stock
  Walksnail BFx4 fonts are bundled in `fonts/`. The script falls back to
  downloading the official font only for other resolutions or if the bundled
  files are removed.

Dependencies are pinned in `pyproject.toml` and fully resolved in `uv.lock`:

| Package  | Version  |
|----------|----------|
| `av`     | `17.1.0` |
| `pillow` | `12.3.0` |

## Quick start

Clone the repository and run postflight from the `recording/` directory:

```bash
cd recording
uv sync --locked
uv run --locked postflight AvatarG0005.mp4
```

This writes `AvatarG0005_osd.mp4`.

`uv sync --locked` installs the project into `recording/.venv/` and puts the
`postflight` command in that environment. `--locked` makes uv fail rather than
modify `uv.lock`, so you always get the tested dependency set.

Equivalent ways to run the same command:

```bash
# activation-based (then plain `postflight …` works)
source .venv/bin/activate
postflight AvatarG0005.mp4

# without activating, or without `uv sync` (creates/updates .venv on demand)
uv run --locked postflight AvatarG0005.mp4

# run the module directly
uv run --locked python postflight.py AvatarG0005.mp4
```

## Preview a frame

Render one composited frame without encoding the whole video:

```bash
uv run --locked postflight AvatarG0005.mp4 --preview 30
```

This writes `AvatarG0005_preview_30.000.png`. The 30-second frame shows the
yellow `RUNAWAY` warning from the colour-font page.

## Options

```text
--osd PATH            OSD file (default: <video>.osd)
--font PATH           Walksnail BFx4 font PNG or userfont.zip
-o, --output PATH     output MP4 (default: <video>_osd.mp4)
--codec libx264       video encoder (libx264 or libx265)
--crf 18              quality; lower is better/larger
--preset medium       x264/x265 preset
--osd-offset-ms MS    shift OSD timing in milliseconds
--osd-speed FACTOR    OSD playback speed (e.g. 0.995)
--preview SECONDS     write a PNG at this time and exit
--overwrite           replace an existing output file
--quiet               suppress progress output
```

Examples:

```bash
# Replace an existing render with higher quality
uv run --locked postflight AvatarG0005.mp4 --overwrite --crf 14

# Use a custom font (PNG or the official userfont.zip)
uv run --locked postflight AvatarG0005.mp4 \
    --font /path/to/WS_BFx4_Europa_24.png

# Render faster at lower quality
uv run --locked postflight AvatarG0005.mp4 \
    --preset ultrafast --crf 24 -o test.mp4
```

## How it works

- The `.osd` file is a `BTFL` container: a 40-byte header with the grid size
  (53×20 here), followed by frames of `uint32` milliseconds + 53×20
  little-endian `uint16` glyph codes.
- Glyph codes index the Walksnail **BFx4** four-page font:
  `page = code // 256`, `glyph = code % 256`. Page 0 is white; later pages
  carry warning colours such as the yellow `RUNAWAY` text.
- Each character cell is 24×36 px at 720p. The 53×20 grid is 1272×720 and is
  centred at `x=4, y=0`; 1080p uses 36×54 px cells.
- OSD frames are selected by the video frame's **PTS**, not by frame index.
  This matters because the video and OSD both contain a ~4 second gap, and
  index-based timing would drift.
- Frames are re-encoded as H.264 (`libx264`, `yuv420p`) with the original PTS.
  The output has the same frame count, duration, and freeze/gap as the input.

## Notes and limitations

- Only the `.osd` glyph layer is burned. The `Signal:…` telemetry line in the
  `.srt` file is **not** burned in.
- Walksnail DVR recordings normally have no audio. If the input does contain
  audio, the script prints a warning and writes a video-only output.
- The stock fonts are bundled in `fonts/`:
  - `WS_BFx4_Europa_24.png` for 720p (24×36 cells)
  - `WS_BFx4_Europa_36.png` for 1080p (36×54 cells)

  Search order:

  1. `<video-dir>/fonts/<font>`
  2. `<video-dir>/<font>`
  3. `<cwd>/fonts/<font>`
  4. `<cwd>/<font>`
  5. `<script-dir>/fonts/<font>` ← the bundled fonts
  6. `<script-dir>/<font>`
  7. `~/.cache/walksnail-osd/<font>`
  8. download the official `userfont.zip` and cache it

  The bundled fonts are found from any working directory thanks to steps 5–6.
  Duplicate paths (for example when `<cwd>` is the script directory) are
  skipped. Use `--font` to select a custom font or an offline copy.
- Font provenance and the original download URL are documented in
  `fonts/SOURCE.md`. If you do not want to redistribute the fonts, delete
  `fonts/` and pass `--font` explicitly.
- If a font's cell size does not match the video, the script scales the glyphs
  and prints a warning. For best quality use 24 px for 720p and 36 px for 1080p.
- The output is written to a `.part.mp4` temp file and renamed atomically on
  success. Existing output requires `--overwrite`.
- Performance on the sample (1280×720, 60 fps, 31.7 s): about 41 s at
  `--preset medium` and about 17 s at `--preset ultrafast`. The CRF 18 output is
  86 MB; the input is 134 MB.

## Troubleshooting

**`Error: output already exists`** — add `--overwrite`.

**`PyAV is required`** — run through uv (`uv run --locked postflight …`), not the
system `python3`; `uv sync --locked` installs the locked environment.

**`could not download the OSD font`** — this should only happen if the bundled
fonts are missing and you are offline. Restore `fonts/` from the repository, or
download the official `userfont.zip`, extract `WS_BFx4_Europa_24.png` (720p) or
`WS_BFx4_Europa_36.png` (1080p), and pass it with `--font`.

**uv picks the wrong Python** — an explicit `UV_PYTHON` environment variable
overrides `.python-version`. Unset it, or set `UV_PYTHON=3.11.13`.
