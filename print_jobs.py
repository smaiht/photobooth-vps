"""Telegram print-choice previews and durable local pending jobs."""

from __future__ import annotations

import io
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

log = logging.getLogger(__name__)

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception as exc:
    log.warning("HEIC/HEIF image decoder is unavailable on VPS: %s", exc)


LANDSCAPE_PRINT_SIZE = (3688, 2480)
PORTRAIT_PRINT_SIZE = (2480, 3688)
MAX_PRINT_PIXELS = 100_000_000
PENDING_ROOT = Path(__file__).resolve().parent / "print_pending_jobs"
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")

_BACKGROUND = (20, 21, 25)
_PAPER = (255, 255, 255)
_TEXT = (246, 247, 250)
_BADGE_FILL = (10, 11, 14)
_FIT_TITLE = "Как есть"
_FIT_DETAIL = "(будут белые поля)"
_FILL_TITLE = "Увеличить под размер"
_FILL_DETAIL = "(обрежутся края)"

_TILE_PADDING = 32
_TILE_GAP = 22
_LABEL_TOP_MARGIN = 28
_LABEL_LINE_GAP = 10
_LABEL_BOTTOM_MARGIN = 38
_BADGE_RADIUS = 34
_BADGE_INSET = 10
_PAPER_OUTLINE_WIDTH = 5
_BADGE_OUTLINE_WIDTH = 4


@dataclass(frozen=True)
class PrintPreview:
    payload: bytes | None
    source_size: tuple[int, int]
    target_size: tuple[int, int]
    orientation: str
    exact_ratio: bool
    overflow_axis: str | None


def _load_rgb(payload: bytes) -> Image.Image:
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("пустое изображение")
    with Image.open(io.BytesIO(payload)) as source:
        if source.width * source.height > MAX_PRINT_PIXELS:
            raise ValueError("изображение слишком большое")
        source.seek(0)
        oriented = ImageOps.exif_transpose(source)
        try:
            if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                image = Image.new("RGB", rgba.size, _PAPER)
                image.paste(rgba, mask=rgba.getchannel("A"))
                rgba.close()
            else:
                image = oriented.convert("RGB")
        finally:
            if oriented is not source:
                oriented.close()
    return image


def _font(
    size: int,
    *,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
            Path(r"C:\Windows\Fonts\arialbd.ttf"),
        )
        if bold
        else (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path(r"C:\Windows\Fonts\arial.ttf"),
        )
    )
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _paper_preview_size(target_size: tuple[int, int]) -> tuple[int, int]:
    target_w, target_h = target_size
    long_side = 960
    if target_w > target_h:
        return long_side, round(long_side * target_h / target_w)
    return round(long_side * target_w / target_h), long_side


def _contain(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    result = Image.new("RGB", size, _PAPER)
    fitted = image.copy()
    fitted.thumbnail(size, Image.Resampling.LANCZOS)
    position = (
        (size[0] - fitted.width) // 2,
        (size[1] - fitted.height) // 2,
    )
    result.paste(fitted, position)
    fitted.close()
    return result


def _cover_view(
    image: Image.Image,
    paper_size: tuple[int, int],
) -> tuple[Image.Image, str]:
    """Show the printed center normally and discarded overflow in dark red."""
    paper_w, paper_h = paper_size
    scale = max(paper_w / image.width, paper_h / image.height)
    scaled_w = image.width * scale
    scaled_h = image.height * scale
    overflow_x = max(0.0, (scaled_w - paper_w) / 2)
    overflow_y = max(0.0, (scaled_h - paper_h) / 2)

    if overflow_x > overflow_y:
        axis = "horizontal"
        visible_x = min(max(18, round(overflow_x)), round(paper_w * 0.24))
        visible_y = 0
    else:
        axis = "vertical"
        visible_x = 0
        visible_y = min(max(18, round(overflow_y)), round(paper_h * 0.24))

    view_w = paper_w + visible_x * 2
    view_h = paper_h + visible_y * 2
    source_view_w = view_w / scale
    source_view_h = view_h / scale
    source_box = (
        (image.width - source_view_w) / 2,
        (image.height - source_view_h) / 2,
        (image.width + source_view_w) / 2,
        (image.height + source_view_h) / 2,
    )
    normal = image.transform(
        (view_w, view_h),
        Image.Transform.EXTENT,
        source_box,
        resample=Image.Resampling.BICUBIC,
    )
    red = Image.new("RGB", normal.size, (105, 8, 10))
    shaded = Image.blend(normal, red, 0.58)
    red.close()
    darkened = ImageEnhance.Brightness(shaded).enhance(0.72)
    shaded.close()

    paper_box = (
        visible_x,
        visible_y,
        visible_x + paper_w,
        visible_y + paper_h,
    )
    printed = normal.crop(paper_box)
    darkened.paste(printed, (visible_x, visible_y))
    printed.close()
    normal.close()
    return darkened, axis


def _tile(
    visual: Image.Image,
    visual_area_size: tuple[int, int],
    paper_size: tuple[int, int],
    title: str,
    detail: str,
    number: str,
) -> Image.Image:
    area_width, area_height = visual_area_size
    paper_width, paper_height = paper_size
    if visual.width > area_width or visual.height > area_height:
        raise ValueError("preview visual exceeds its shared area")
    if paper_width > area_width or paper_height > area_height:
        raise ValueError("preview paper exceeds its shared area")

    title_font = _font(40, bold=True)
    detail_font = _font(38)
    title_box = title_font.getbbox(title)
    detail_box = detail_font.getbbox(detail)
    title_line_height = max(
        _font_bbox_height(title_font, value)
        for value in (_FIT_TITLE, _FILL_TITLE)
    )
    detail_line_height = max(
        _font_bbox_height(detail_font, value)
        for value in (_FIT_DETAIL, _FILL_DETAIL)
    )
    label_top = _TILE_PADDING + area_height + _LABEL_TOP_MARGIN
    detail_top = label_top + title_line_height + _LABEL_LINE_GAP
    tile_height = detail_top + detail_line_height + _LABEL_BOTTOM_MARGIN
    tile = Image.new(
        "RGB",
        (area_width + _TILE_PADDING * 2, tile_height),
        _BACKGROUND,
    )
    draw = ImageDraw.Draw(tile)
    visual_position = (
        _TILE_PADDING + (area_width - visual.width) // 2,
        _TILE_PADDING + (area_height - visual.height) // 2,
    )
    tile.paste(visual, visual_position)

    badge_font = _font(42)
    paper_position = (
        (area_width - paper_width) // 2,
        (area_height - paper_height) // 2,
    )
    paper_box = (
        _TILE_PADDING + paper_position[0],
        _TILE_PADDING + paper_position[1],
        _TILE_PADDING + paper_position[0] + paper_width - 1,
        _TILE_PADDING + paper_position[1] + paper_height - 1,
    )
    draw.rectangle(
        paper_box,
        outline=_PAPER,
        width=_PAPER_OUTLINE_WIDTH,
    )
    badge_center = (
        _TILE_PADDING + paper_position[0] + _BADGE_RADIUS + _BADGE_INSET,
        _TILE_PADDING + paper_position[1] + paper_height
        - _BADGE_RADIUS - _BADGE_INSET,
    )
    draw.ellipse(
        (
            badge_center[0] - _BADGE_RADIUS,
            badge_center[1] - _BADGE_RADIUS,
            badge_center[0] + _BADGE_RADIUS,
            badge_center[1] + _BADGE_RADIUS,
        ),
        fill=_BADGE_FILL,
        outline=_PAPER,
        width=_BADGE_OUTLINE_WIDTH,
    )
    number_box = badge_font.getbbox(number)
    number_width = number_box[2] - number_box[0]
    number_height = number_box[3] - number_box[1]
    draw.text(
        (
            badge_center[0] - number_width / 2 - number_box[0],
            badge_center[1] - number_height / 2 - number_box[1],
        ),
        number,
        fill=_TEXT,
        font=badge_font,
    )

    title_width = title_box[2] - title_box[0]
    title_height = title_box[3] - title_box[1]
    draw.text(
        (
            (tile.width - title_width) / 2 - title_box[0],
            label_top + (title_line_height - title_height) / 2 - title_box[1],
        ),
        title,
        fill=_TEXT,
        font=title_font,
    )
    detail_width = detail_box[2] - detail_box[0]
    detail_height = detail_box[3] - detail_box[1]
    draw.text(
        (
            (tile.width - detail_width) / 2 - detail_box[0],
            detail_top + (detail_line_height - detail_height) / 2 - detail_box[1],
        ),
        detail,
        fill=_TEXT,
        font=detail_font,
    )
    return tile


def _font_bbox_height(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    text: str,
) -> int:
    box = font.getbbox(text)
    return box[3] - box[1]


def build_choice_preview(payload: bytes) -> PrintPreview:
    """Return no image for an exact ratio, otherwise one two-option collage."""
    image = _load_rgb(payload)
    try:
        source_size = image.size
        landscape = image.width > image.height
        target_size = LANDSCAPE_PRINT_SIZE if landscape else PORTRAIT_PRINT_SIZE
        exact_ratio = image.width * target_size[1] == image.height * target_size[0]
        if exact_ratio:
            return PrintPreview(
                payload=None,
                source_size=source_size,
                target_size=target_size,
                orientation="landscape" if landscape else "portrait",
                exact_ratio=True,
                overflow_axis=None,
            )

        # The comparison sent to Telegram never needs the full source raster.
        preview_source = image.copy()
        preview_source.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
        paper_size = _paper_preview_size(target_size)

        cover_visual, overflow_axis = _cover_view(
            preview_source, paper_size)
        fit_paper = _contain(preview_source, paper_size)
        preview_source.close()

        visual_area_size = (
            max(fit_paper.width, cover_visual.width),
            max(fit_paper.height, cover_visual.height),
        )

        fit_tile = _tile(
            fit_paper,
            visual_area_size,
            paper_size,
            _FIT_TITLE,
            _FIT_DETAIL,
            "1",
        )
        cover_tile = _tile(
            cover_visual,
            visual_area_size,
            paper_size,
            _FILL_TITLE,
            _FILL_DETAIL,
            "2",
        )
        fit_paper.close()
        cover_visual.close()

        if overflow_axis == "vertical":
            collage = Image.new(
                "RGB",
                (fit_tile.width + cover_tile.width + _TILE_GAP,
                 max(fit_tile.height, cover_tile.height)),
                _BACKGROUND,
            )
            collage.paste(
                fit_tile,
                (0, (collage.height - fit_tile.height) // 2),
            )
            collage.paste(cover_tile, (fit_tile.width + _TILE_GAP, 0))
        else:
            collage = Image.new(
                "RGB",
                (max(fit_tile.width, cover_tile.width),
                 fit_tile.height + cover_tile.height + _TILE_GAP),
                _BACKGROUND,
            )
            collage.paste(
                fit_tile,
                ((collage.width - fit_tile.width) // 2, 0),
            )
            collage.paste(
                cover_tile,
                ((collage.width - cover_tile.width) // 2,
                 fit_tile.height + _TILE_GAP),
            )
        fit_tile.close()
        cover_tile.close()

        output = io.BytesIO()
        collage.save(output, "JPEG", quality=90, subsampling=0)
        collage.close()
        return PrintPreview(
            payload=output.getvalue(),
            source_size=source_size,
            target_size=target_size,
            orientation="landscape" if landscape else "portrait",
            exact_ratio=False,
            overflow_axis=overflow_axis,
        )
    finally:
        image.close()


def _job_dir(job_id: str) -> Path:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("invalid pending print job id")
    return PENDING_ROOT / job_id


def _write_metadata(job_dir: Path, metadata: dict) -> None:
    temporary = job_dir / "metadata.json.tmp"
    temporary.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(job_dir / "metadata.json")


def save_pending(
    job_id: str,
    suffix: str,
    payload: bytes,
    metadata: dict,
) -> None:
    if not SUFFIX_RE.fullmatch(str(suffix or "")):
        raise ValueError("invalid pending print suffix")
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("invalid pending print payload")
    if not isinstance(metadata, dict):
        raise ValueError("invalid pending print metadata")
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    source_path = job_dir / f"original{suffix}"
    source_temporary = source_path.with_name(source_path.name + ".tmp")
    source_temporary.write_bytes(payload)
    source_temporary.replace(source_path)
    pending = dict(metadata)
    pending.update({
        "job_id": job_id,
        "source_suffix": suffix,
        "pending_created_at": time.time(),
        "pending_status": "awaiting_choice",
    })
    _write_metadata(job_dir, pending)


def load_pending(job_id: str) -> tuple[bytes, dict]:
    job_dir = _job_dir(job_id)
    metadata_path = job_dir / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError("pending print job not found")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    suffix = str(metadata.get("source_suffix") or "")
    if not SUFFIX_RE.fullmatch(suffix):
        raise ValueError("invalid stored print suffix")
    source_path = job_dir / f"original{suffix}"
    payload = source_path.read_bytes()
    if not payload:
        raise ValueError("stored print image is empty")
    return payload, metadata


def update_pending(job_id: str, **changes) -> dict:
    _payload, metadata = load_pending(job_id)
    metadata.update(changes)
    _write_metadata(_job_dir(job_id), metadata)
    return metadata


def delete_pending(job_id: str) -> None:
    job_dir = _job_dir(job_id)
    if not job_dir.is_dir():
        return
    for path in job_dir.iterdir():
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
    job_dir.rmdir()
