#!/usr/bin/env python3
"""postfly: burn a Walksnail Avatar .osd overlay onto an MP4 video.

The goggles record the flight-controller OSD as a ``.osd`` file next to the
video.  This script renders that OSD and burns it into a new MP4, keeping the
original frame timestamps (Walksnail DVRs can contain long gaps/freezes).

Requirements:
    uv sync --locked          # preferred (see README.md)
    # or: pip install av pillow

Usage:
    uv run --locked postfly AvatarG0005.mp4
    uv run --locked postfly AvatarG0005.mp4 \
        --font WS_BFx4_Europa_24.png -o out.mp4
    # equivalently: uv run --locked python postfly.py ...
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import struct
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

try:
    import av
except ImportError:  # pragma: no cover - friendly error for missing deps
    sys.exit("PyAV is required. Run: uv sync --locked  (or: pip install av)")

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required. Run: uv sync --locked  (or: pip install pillow)")


MAGIC = b"BTFL"
HEADER_SIZE = 40
MIN_GRID = 1
MAX_GRID_W = 120
MAX_GRID_H = 60

# Official Walksnail "userfont" pack (BFx4 = four colour pages).
FONT_ZIP_URL = (
    "https://download.walksnail.app/"
    "3bdabaf3-113d-42d9-b5dd-925b0c225cc0/userfont.zip?download"
)
FONT_ZIP_NAMES = {
    24: "WS_BFx4_Europa_24.png",
    36: "WS_BFx4_Europa_36.png",
}

# Known character cell sizes used by Walksnail / Betaflight OSD fonts.
CELL_CANDIDATES = (
    (24, 36),   # 720p
    (36, 54),   # 1080p
    (48, 72),   # 2.7K
    (72, 108),  # 4K
    (18, 27),   # 540p / race
)


# ---------------------------------------------------------------------------
# OSD file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OsdFrame:
    time_ms: int
    glyphs: tuple[int, ...]


@dataclass
class OsdFile:
    width: int
    height: int
    frames: list[OsdFrame]

    @property
    def cell_count(self) -> int:
        return self.width * self.height

    @classmethod
    def load(cls, path: Path) -> "OsdFile":
        data = path.read_bytes()
        if len(data) < HEADER_SIZE:
            raise ValueError(f"{path}: file is too small to be an OSD file")
        if data[:4] != MAGIC:
            raise ValueError(
                f"{path}: not a Walksnail OSD file (expected magic {MAGIC!r}, "
                f"got {data[:4]!r})"
            )

        width, height = struct.unpack_from("<HH", data, 36)
        if not (MIN_GRID <= width <= MAX_GRID_W and MIN_GRID <= height <= MAX_GRID_H):
            raise ValueError(f"{path}: implausible OSD grid {width}x{height}")

        cell_count = width * height
        frame_size = 4 + cell_count * 2
        body_size = len(data) - HEADER_SIZE
        if body_size == 0 or body_size % frame_size != 0:
            raise ValueError(
                f"{path}: body size {body_size} is not a whole number of "
                f"{frame_size}-byte OSD frames"
            )

        frames: list[OsdFrame] = []
        for offset in range(HEADER_SIZE, len(data), frame_size):
            time_ms = struct.unpack_from("<I", data, offset)[0]
            glyphs = struct.unpack_from(f"<{cell_count}H", data, offset + 4)
            frames.append(OsdFrame(time_ms, glyphs))

        if any(
            frames[i].time_ms < frames[i - 1].time_ms
            for i in range(1, len(frames))
        ):
            raise ValueError(f"{path}: OSD timestamps are not monotonic")

        return cls(width, height, frames)


# ---------------------------------------------------------------------------
# Font
# ---------------------------------------------------------------------------


def _detect_native_cell(width: int, height: int) -> tuple[int, int, int, int]:
    """Return (cell_w, cell_h, pages, glyphs_per_page) for a font sprite sheet."""
    candidates: list[tuple[tuple[int, int, int], int, int, int, int]] = []
    for cell_w, cell_h in CELL_CANDIDATES:
        if width % cell_w or height % cell_h:
            continue
        pages = width // cell_w
        count = height // cell_h
        if pages not in (1, 2, 3, 4) or count < 32:
            continue
        # Betaflight fonts almost always use 256 glyphs per page.  Prefer that,
        # then 512, then whatever is closest.
        score = (
            0 if count == 256 else 1 if count == 512 else 2,
            abs(count - 256),
            pages,
        )
        candidates.append((score, cell_w, cell_h, pages, count))

    if not candidates:
        raise ValueError(
            f"unsupported font image size {width}x{height}; expected a Walksnail "
            "BF/INAV/ARDU PNG sprite sheet"
        )

    _, cell_w, cell_h, pages, count = min(candidates)
    return cell_w, cell_h, pages, count


def _target_cell(
    video_w: int, video_h: int, grid_w: int, grid_h: int
) -> tuple[int, int]:
    """Pick the largest 2:3 character cell that fits the video."""
    cell_h = max(1, min(video_h // grid_h, int(video_w / (grid_w * 2 / 3))))
    cell_w = max(1, round(cell_h * 2 / 3))
    return cell_w, cell_h


class OsdFont:
    def __init__(self, image: Image.Image, target_cell: tuple[int, int]):
        self.image = image.convert("RGBA")
        self.image.load()
        (
            self.cell_w,
            self.cell_h,
            self.pages,
            self.glyphs_per_page,
        ) = _detect_native_cell(*self.image.size)

        self.target_w, self.target_h = target_cell
        self._cache: dict[int, Image.Image | None] = {}

        if (self.cell_w, self.cell_h) != (self.target_w, self.target_h):
            print(
                f"Font cell {self.cell_w}x{self.cell_h} differs from video OSD "
                f"cell {self.target_w}x{self.target_h}; scaling glyphs.",
                file=sys.stderr,
            )

    def glyph(self, code: int) -> Image.Image | None:
        if code in self._cache:
            return self._cache[code]

        page, index = divmod(code, self.glyphs_per_page)
        if page >= self.pages:
            # Single-page fallback for a code from a colour page that the font
            # does not contain.
            page = 0
            index = code % self.glyphs_per_page

        if index == 0 or index == 0x20:
            self._cache[code] = None
            return None

        box = (
            page * self.cell_w,
            index * self.cell_h,
            (page + 1) * self.cell_w,
            (index + 1) * self.cell_h,
        )
        glyph = self.image.crop(box)
        if (self.target_w, self.target_h) != (self.cell_w, self.cell_h):
            glyph = glyph.resize(
                (self.target_w, self.target_h), Image.Resampling.LANCZOS
            )

        if glyph.getbbox() is None:
            self._cache[code] = None
            return None

        self._cache[code] = glyph
        return glyph


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "walksnail-osd"
    return Path.home() / ".cache" / "walksnail-osd"


def _font_size_for_video(video_h: int) -> int:
    return 24 if video_h <= 800 else 36


def _download_font(size: int) -> Path:
    """Download the stock Walksnail BFx4 font and cache it locally."""
    name = FONT_ZIP_NAMES[size]
    dest = _cache_dir() / name
    dest.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading stock Walksnail BF font ({name})...", file=sys.stderr)
    request = urllib.request.Request(
        FONT_ZIP_URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://walksnail.app/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            archive = response.read()
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            with zf.open(name) as src:
                with tempfile.NamedTemporaryFile(
                    dir=dest.parent, delete=False, suffix=".png"
                ) as tmp:
                    shutil.copyfileobj(src, tmp)
                    tmp_path = Path(tmp.name)
        os.replace(tmp_path, dest)
    except Exception as exc:
        raise RuntimeError(
            f"could not download the OSD font ({exc}).\n"
            f"Download {FONT_ZIP_URL} manually, extract {name}, and pass it "
            "with --font."
        ) from exc

    return dest


def _font_from_zip(path: Path, size: int) -> Image.Image:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".png")]
        wanted = FONT_ZIP_NAMES[size]
        if wanted in names:
            name = wanted
        else:
            bf = [n for n in names if "bfx4" in n.lower()]
            pool = bf or names
            if not pool:
                raise ValueError(f"{path}: no PNG font found in archive")
            # Pick the closest resolution by cell width in the filename.
            name = min(
                pool,
                key=lambda n: abs(
                    int(next((s for s in (18, 24, 36, 48, 72) if str(s) in n), 24))
                    - size
                ),
            )
        with zf.open(name) as src:
            return Image.open(io.BytesIO(src.read()))


def _find_font(path: Path | None, video_h: int, input_dir: Path) -> Image.Image:
    size = _font_size_for_video(video_h)

    if path is not None:
        if not path.exists():
            raise FileNotFoundError(f"font file not found: {path}")
        if path.suffix.lower() == ".zip":
            return _font_from_zip(path, size)
        with Image.open(path) as image:
            return image.convert("RGBA")

    # Prefer fonts shipped with this project.  The cache and the network
    # fallback only matter for resolutions/fonts not bundled here.
    wanted = FONT_ZIP_NAMES[size]
    script_dir = Path(__file__).resolve().parent
    candidates = [
        input_dir / "fonts" / wanted,      # 1. <video-dir>/fonts/
        input_dir / wanted,                # 2. <video-dir>/
        Path.cwd() / "fonts" / wanted,     # 3. <cwd>/fonts/
        Path.cwd() / wanted,               # 4. <cwd>/
        script_dir / "fonts" / wanted,     # 5. <script-dir>/fonts/
        script_dir / wanted,               # 6. <script-dir>/
        _cache_dir() / wanted,             # 7. cache
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        key = candidate.resolve()
        if key in seen:  # e.g. <cwd> is the script dir
            continue
        seen.add(key)
        if candidate.exists():
            with Image.open(candidate) as image:
                return image.convert("RGBA")

    downloaded = _download_font(size)
    with Image.open(downloaded) as image:
        return image.convert("RGBA")


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------


class OsdRenderer:
    def __init__(
        self,
        osd: OsdFile,
        font: OsdFont,
        video_w: int,
        video_h: int,
    ):
        self.osd = osd
        self.font = font
        self.video_w = video_w
        self.video_h = video_h

        self.overlay_w = osd.width * font.target_w
        self.overlay_h = osd.height * font.target_h
        self.x = (video_w - self.overlay_w) // 2
        self.y = (video_h - self.overlay_h) // 2

        if self.x < 0 or self.y < 0:
            raise ValueError(
                f"OSD grid {osd.width}x{osd.height} at "
                f"{font.target_w}x{font.target_h} px does not fit "
                f"{video_w}x{video_h}"
            )

        self._current_index = -1
        self._current_overlay: Image.Image | None = None

    def overlay(self, index: int) -> Image.Image:
        if index != self._current_index:
            frame = self.osd.frames[index]
            overlay = Image.new(
                "RGBA",
                (self.overlay_w, self.overlay_h),
                (0, 0, 0, 0),
            )
            for cell, code in enumerate(frame.glyphs):
                if code == 0 or code == 0x20:
                    continue
                glyph = self.font.glyph(code)
                if glyph is None:
                    continue
                col = cell % self.osd.width
                row = cell // self.osd.width
                overlay.alpha_composite(
                    glyph,
                    (col * self.font.target_w, row * self.font.target_h),
                )
            self._current_index = index
            self._current_overlay = overlay
        assert self._current_overlay is not None
        return self._current_overlay

    def apply(self, image: Image.Image, index: int) -> None:
        image.paste(self.overlay(index), (self.x, self.y), self.overlay(index))


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------


def _open_video(path: Path) -> tuple[av.container.InputContainer, av.VideoStream]:
    container = av.open(str(path))
    if not container.streams.video:
        container.close()
        raise ValueError(f"{path}: no video stream found")
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"
    return container, stream


def _add_encoder(
    output: av.container.OutputContainer,
    stream: av.VideoStream,
    codec: str,
    crf: int,
    preset: str,
) -> av.VideoStream:
    rate = stream.guessed_rate or stream.average_rate or 60
    try:
        encoder = output.add_stream(codec, rate=rate)
    except Exception as exc:
        if codec != "libx264":
            raise
        print(
            f"{codec} is not available ({exc}); falling back to libx265.",
            file=sys.stderr,
        )
        encoder = output.add_stream("libx265", rate=rate)

    encoder.width = stream.codec_context.width
    encoder.height = stream.codec_context.height
    encoder.pix_fmt = "yuv420p"
    # Keep the input time base so original PTS values can be reused directly.
    encoder.codec_context.time_base = stream.time_base
    encoder.options = {"crf": str(crf), "preset": preset}
    return encoder


def _choose_osd_index(
    frames: list[OsdFrame],
    current: int,
    next_index: int,
    target_ms: float,
) -> tuple[int, int]:
    while next_index < len(frames) and frames[next_index].time_ms <= target_ms:
        current = next_index
        next_index += 1
    return current, next_index


def _output_path(video: Path, requested: Path | None) -> Path:
    if requested is not None:
        return requested
    return video.with_name(f"{video.stem}_osd{video.suffix}")


def _render_preview(
    video: Path,
    osd: OsdFile,
    font: OsdFont,
    seconds: float,
    output: Path,
) -> None:
    container, stream = _open_video(video)
    renderer = OsdRenderer(
        osd,
        font,
        stream.codec_context.width,
        stream.codec_context.height,
    )
    osd_index = 0
    next_osd = 1
    first_time: float | None = None
    selected = None
    try:
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            timestamp = float(frame.pts * frame.time_base)
            if first_time is None:
                first_time = timestamp
            relative_ms = (timestamp - first_time) * 1000.0
            osd_index, next_osd = _choose_osd_index(
                osd.frames, osd_index, next_osd, relative_ms
            )
            if timestamp - first_time >= seconds:
                selected = (frame.to_image().convert("RGB"), osd_index)
                break
    finally:
        container.close()

    if selected is None:
        raise RuntimeError(f"could not find a frame at {seconds:.3f}s")

    image, index = selected
    renderer.apply(image, index)
    image.save(output)
    print(f"Wrote preview: {output}")


def _encode(
    video: Path,
    osd: OsdFile,
    font: OsdFont,
    output: Path,
    *,
    codec: str,
    crf: int,
    preset: str,
    osd_offset_ms: float,
    osd_speed: float,
    quiet: bool,
) -> None:
    container, stream = _open_video(video)
    renderer = OsdRenderer(
        osd,
        font,
        stream.codec_context.width,
        stream.codec_context.height,
    )

    if container.streams.audio:
        print(
            "Warning: input has audio; this script writes a video-only output.",
            file=sys.stderr,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output.with_name(f"{output.stem}.part{output.suffix}")
    out: av.container.OutputContainer | None = None
    try:
        out = av.open(
            str(temp_path),
            mode="w",
            format="mp4",
            options={"movflags": "+faststart"},
        )
        encoder = _add_encoder(out, stream, codec, crf, preset)

        osd_index = 0
        next_osd = 1
        first_time: float | None = None
        last_time = 0.0
        frame_count = 0
        total = stream.frames or 0
        fallback_dt = 1.0 / float(stream.guessed_rate or 60)

        for frame in container.decode(stream):
            if frame.pts is None:
                timestamp = last_time + fallback_dt
                pts = round(timestamp / stream.time_base)
            else:
                timestamp = float(frame.pts * frame.time_base)
                pts = frame.pts

            if first_time is None:
                first_time = timestamp
            last_time = timestamp

            relative_ms = (timestamp - first_time) * 1000.0
            target_ms = relative_ms * osd_speed + osd_offset_ms
            osd_index, next_osd = _choose_osd_index(
                osd.frames, osd_index, next_osd, target_ms
            )

            image = frame.to_image().convert("RGB")
            renderer.apply(image, osd_index)

            new_frame = av.VideoFrame.from_image(image)
            new_frame.pts = pts
            new_frame.time_base = stream.time_base
            for packet in encoder.encode(new_frame):
                out.mux(packet)

            frame_count += 1
            if not quiet and (frame_count % 60 == 0 or frame_count == total):
                if total:
                    percent = 100.0 * frame_count / total
                    print(
                        f"\rFrame {frame_count}/{total} ({percent:5.1f}%) "
                        f"t={timestamp:6.2f}s",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(
                        f"\rFrame {frame_count} t={timestamp:6.2f}s",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )

        for packet in encoder.encode():
            out.mux(packet)
        out.close()
        out = None
        os.replace(temp_path, output)

        if not quiet:
            print(file=sys.stderr)
            print(
                f"Wrote {output} ({frame_count} frames, "
                f"{stream.codec_context.width}x{stream.codec_context.height})",
                file=sys.stderr,
            )
    finally:
        if out is not None:
            out.close()
        container.close()
        if temp_path.exists():
            temp_path.unlink()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="postfly: burn a Walksnail Avatar .osd overlay onto an MP4 video.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("video", type=Path, help="input MP4 video")
    parser.add_argument(
        "--osd",
        type=Path,
        help="OSD file (default: same name as the video, .osd extension)",
    )
    parser.add_argument(
        "--font",
        type=Path,
        help="Walksnail BFx4 font PNG or userfont.zip "
        "(default: stock font, downloaded/cached automatically)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output MP4 (default: <video>_osd.mp4)",
    )
    parser.add_argument(
        "--codec",
        default="libx264",
        choices=("libx264", "libx265"),
        help="video encoder",
    )
    parser.add_argument("--crf", type=int, default=25, help="quality (lower is better)")
    parser.add_argument(
        "--preset",
        default="medium",
        choices=(
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
        ),
        help="encoder preset",
    )
    parser.add_argument(
        "--osd-offset-ms",
        type=float,
        default=0.0,
        help="shift OSD timing by this many milliseconds",
    )
    parser.add_argument(
        "--osd-speed",
        type=float,
        default=1.0,
        help="OSD playback speed multiplier (e.g. 0.995 to slow it down)",
    )
    parser.add_argument(
        "--preview",
        type=float,
        metavar="SECONDS",
        help="write a PNG preview at this timestamp and exit",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite the output file if it already exists",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress progress output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    video: Path = args.video
    if not video.exists():
        raise FileNotFoundError(f"video not found: {video}")

    osd_path: Path = args.osd or video.with_suffix(".osd")
    if not osd_path.exists():
        raise FileNotFoundError(
            f"OSD file not found: {osd_path} (use --osd to specify it)"
        )

    osd = OsdFile.load(osd_path)

    # Read the video dimensions before choosing a font size.
    container, stream = _open_video(video)
    video_w = stream.codec_context.width
    video_h = stream.codec_context.height
    container.close()

    font_image = _find_font(args.font, video_h, video.parent)
    cell = _target_cell(video_w, video_h, osd.width, osd.height)
    font = OsdFont(font_image, cell)

    if args.preview is not None:
        preview_path = video.with_name(
            f"{video.stem}_preview_{args.preview:.3f}.png"
        )
        _render_preview(video, osd, font, args.preview, preview_path)
        return 0

    output = _output_path(video, args.output)
    if output.exists() and not args.overwrite:
        raise FileExistsError(
            f"output already exists: {output} (use --overwrite to replace it)"
        )

    print(
        f"OSD: {osd.width}x{osd.height} grid, {len(osd.frames)} frames; "
        f"video: {video_w}x{video_h}",
        file=sys.stderr,
    )
    _encode(
        video,
        osd,
        font,
        output,
        codec=args.codec,
        crf=args.crf,
        preset=args.preset,
        osd_offset_ms=args.osd_offset_ms,
        osd_speed=args.osd_speed,
        quiet=args.quiet,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
