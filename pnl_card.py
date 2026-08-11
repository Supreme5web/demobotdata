"""Generates PaperBoat PNL card images for closed trades.

Takes the existing template background (assets/pnl_card_template.png) and
overlays dynamic trade data on top of it with Pillow. Nothing here touches
trading.py's actual buy/sell logic - it only renders a picture from the
result of a trade that already happened.
"""

import io
import logging
import os
import tempfile
import time
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "assets", "pnl_card_template.png")

FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
EMOJI_FONT = os.path.join(FONT_DIR, "NotoColorEmoji.ttf")

# Rajdhani everywhere: Bold for headings/values/numbers, Medium for labels
# and secondary text (token symbol, username uses Bold to stay prominent).
FONT_BOLD = os.path.join(FONT_DIR, "Rajdhani-Bold.ttf")
FONT_REGULAR = os.path.join(FONT_DIR, "Rajdhani-Medium.ttf")

# Colors
WHITE = (255, 255, 255, 255)
LABEL_GRAY = (148, 168, 200, 255)
GREEN = (52, 211, 153, 255)
RED = (248, 113, 113, 255)
CYAN = (56, 189, 248, 255)
DIVIDER_COLOR = (60, 90, 140, 140)

# Layout is tuned for the current 1751x898 template, which already carries
# the PAPERBOAT logo/wordmark (top-right) and wave/candlestick art (right
# and bottom), so all overlaid text stays in the left/upper dark region.
CONTENT_LEFT = 70
CONTENT_RIGHT = 940
LOGO_SIZE = 130
COL_GAP = 480  # x-offset of the second stat column relative to CONTENT_LEFT


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x1F300 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or cp in (0x2705, 0xFE0F)
    )


# NotoColorEmoji is a fixed-strike bitmap font (rendered at a single native
# size and then scaled), so we load one size and let Pillow resample it.
_EMOJI_FONT_CACHE = {}


def _emoji_font(px_size: int) -> Optional[ImageFont.FreeTypeFont]:
    """NotoColorEmoji only ships strike size 109; request that and resize
    the rendered glyph to the size we actually need. Returns None (instead
    of raising) if the font can't be loaded, so a font/environment issue
    degrades to plain text rather than crashing card generation."""
    if 109 not in _EMOJI_FONT_CACHE:
        try:
            _EMOJI_FONT_CACHE[109] = ImageFont.truetype(EMOJI_FONT, 109)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load emoji font at %s: %s", EMOJI_FONT, exc)
            _EMOJI_FONT_CACHE[109] = None
    return _EMOJI_FONT_CACHE[109]


def draw_text_with_emoji(overlay: Image.Image, xy, text: str, font: ImageFont.FreeTypeFont,
                          fill, emoji_px: Optional[int] = None) -> int:
    """Draws `text` onto `overlay` (RGBA), rendering any emoji characters
    with the color emoji font instead of tofu boxes. Returns the total
    rendered width in pixels."""
    draw = ImageDraw.Draw(overlay)
    x, y = xy
    start_x = x
    emoji_px = emoji_px or font.size
    ascent, _ = font.getmetrics()

    for ch in text:
        if _is_emoji(ch):
            efont = _emoji_font(emoji_px)
            if efont is None:
                # Emoji font unavailable - skip the glyph rather than crash.
                continue
            bbox = efont.getbbox(ch)
            glyph_w = bbox[2] - bbox[0] if bbox else emoji_px
            # Render the emoji at native strike size on its own canvas, then
            # scale to the target size so it lines up with surrounding text.
            tmp = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
            ImageDraw.Draw(tmp).text((0, 0), ch, font=efont, embedded_color=True)
            tmp = tmp.crop((0, 0, 140, 140)).resize((emoji_px, emoji_px), Image.LANCZOS)
            overlay.alpha_composite(tmp, (int(x), int(y + (ascent - emoji_px))))
            x += emoji_px * 0.95
        else:
            draw.text((x, y), ch, font=font, fill=fill)
            w = draw.textlength(ch, font=font)
            x += w
    return int(x - start_x)


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _fmt_usd(value: float) -> str:
    """Standard 2-decimal USD formatting, except for sub-cent values (common
    for freshly-launched memecoins) where 2 decimals would just show $0.00.
    In that case, show enough significant digits to be meaningful."""
    value = float(value)
    if value == 0:
        return "$0.00"
    if abs(value) >= 0.01:
        return f"${value:,.2f}"
    # Sub-cent: show ~4 significant figures, e.g. $0.00004510
    text = f"{value:.10f}".rstrip("0")
    decimals = len(text.split(".")[1]) if "." in text else 2
    return f"${value:,.{min(decimals, 10)}f}"


def _fmt_compact(value) -> str:
    """Compact market-cap style formatting: $1.24M, $850.0K, etc. Mirrors
    the fmt_compact() helper already used elsewhere in the bot for MCap."""
    v = float(value or 0)
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font_path: str, max_width: int,
              start_size: int, min_size: int = 24) -> ImageFont.FreeTypeFont:
    """Shrinks font size until `text` fits within max_width, down to a floor."""
    size = start_size
    while size > min_size:
        font = _font(font_path, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return _font(font_path, min_size)


def _truncate_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                        max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and draw.textlength(trimmed + ellipsis, font=font) > max_width:
        trimmed = trimmed[:-1]
    return trimmed + ellipsis if trimmed else ellipsis


def _fetch_logo(logo_url: Optional[str], size: int) -> Optional[Image.Image]:
    """Best-effort download of a token logo, cropped to a circle. Returns
    None on any failure so the card still renders without a logo."""
    if not logo_url:
        return None
    try:
        resp = httpx.get(logo_url, timeout=6)
        resp.raise_for_status()
        logo = Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001 - logo is optional, never fail the card for it
        logger.warning("Could not fetch token logo from %s: %s", logo_url, exc)
        return None

    logo = logo.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    circular = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    circular.paste(logo, (0, 0), mask)
    return circular


def _draw_logo_placeholder(draw: ImageDraw.ImageDraw, box, symbol: str) -> None:
    """Fallback circular badge with the token's first letter when no logo
    is available."""
    x0, y0, x1, y1 = box
    draw.ellipse(box, fill=(20, 40, 80, 255), outline=CYAN, width=3)
    letter = (symbol or "?")[0].upper()
    font = _font(FONT_BOLD, int((x1 - x0) * 0.5))
    bbox = draw.textbbox((0, 0), letter, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), letter, font=font, fill=WHITE)


def _draw_accent_line(draw: ImageDraw.ImageDraw, x: int, y: int, width: int = 32, thickness: int = 3) -> None:
    """Small cyan accent line under a stat label - a wider, low-opacity line
    behind the crisp main line gives it a subtle glow without looking neon."""
    glow_color = (CYAN[0], CYAN[1], CYAN[2], 70)
    draw.line((x, y, x + width, y), fill=glow_color, width=thickness + 4)
    draw.line((x, y, x + width, y), fill=CYAN, width=thickness)


def _stat(draw, x, y, label, value, value_color=WHITE, label_font=None, value_font=None):
    draw.text((x, y), label, font=label_font, fill=LABEL_GRAY)
    _draw_accent_line(draw, x, y + label_font.size + 4)
    draw.text((x, y + 48), value, font=value_font, fill=value_color)


def generate_pnl_card(trade: dict) -> str:
    """Renders a PNL card PNG for a closed trade and returns the path to a
    temporary file. Caller is responsible for deleting the file after use.

    Expected keys in `trade`:
        token_name, token_symbol, entry_market_cap, exit_market_cap,
        invested, final_value, pnl, pnl_pct, duration_seconds,
        logo_url (optional), username (optional), status_label (optional,
        defaults to "TRADE CLOSED" - e.g. pass "CURRENT PNL" for a live,
        still-open position shared via the Send PNL Card link).
    """
    base = Image.open(TEMPLATE_PATH).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    symbol_font = _font(FONT_REGULAR, 35)
    label_font = _font(FONT_REGULAR, 32)
    value_font = _font(FONT_BOLD, 58)

    pnl = float(trade["pnl"])
    pnl_pct = float(trade["pnl_pct"])
    is_win = pnl >= 0
    accent = GREEN if is_win else RED
    sign = "+" if pnl >= 0 else ""

    # --- Token identity (logo + name/symbol) -------------------------------
    # No header text here anymore - the template background already carries
    # the PAPERBOAT logo/wordmark in its top-right corner.
    x = CONTENT_LEFT
    y = 100
    logo_box = (x, y, x + LOGO_SIZE, y + LOGO_SIZE)
    logo_img = _fetch_logo(trade.get("logo_url"), LOGO_SIZE)
    if logo_img is not None:
        overlay.paste(logo_img, (x, y), logo_img)
        draw.ellipse(logo_box, outline=CYAN, width=3)
    else:
        _draw_logo_placeholder(draw, logo_box, trade["token_symbol"])

    text_x = x + LOGO_SIZE + 24
    max_name_width = CONTENT_RIGHT - text_x
    fitted_name_font = _fit_text(draw, trade["token_name"], FONT_BOLD, max_name_width, 68, min_size=36)
    name_display = _truncate_to_width(draw, trade["token_name"], fitted_name_font, max_name_width)
    draw.text((text_x, y + 22), name_display, font=fitted_name_font, fill=WHITE)
    draw.text((text_x, y + 84), trade["token_symbol"].upper(), font=symbol_font, fill=LABEL_GRAY)

    # --- Divider ------------------------------------------------------------
    y += LOGO_SIZE + 34
    draw.line((x, y, CONTENT_RIGHT, y), fill=DIVIDER_COLOR, width=2)

    # --- Stat grid (2 columns x 4 rows) -------------------------------------
    y += 40
    row_h = 130
    col2_x = x + COL_GAP

    _stat(draw, x, y, "ENTRY MCAP", _fmt_compact(trade["entry_market_cap"]),
          label_font=label_font, value_font=value_font)
    _stat(draw, col2_x, y, "EXIT MCAP", _fmt_compact(trade["exit_market_cap"]),
          label_font=label_font, value_font=value_font)

    y += row_h
    _stat(draw, x, y, "INVESTED", _fmt_usd(float(trade["invested"])),
          label_font=label_font, value_font=value_font)
    _stat(draw, col2_x, y, "FINAL VALUE", _fmt_usd(float(trade["final_value"])),
          label_font=label_font, value_font=value_font)

    y += row_h
    _stat(draw, x, y, "PROFIT", f"{sign}{_fmt_usd(pnl)}", value_color=accent,
          label_font=label_font, value_font=value_font)
    _stat(draw, col2_x, y, "PNL", f"{sign}{pnl_pct:.2f}%", value_color=accent,
          label_font=label_font, value_font=value_font)

    y += row_h
    _stat(draw, x, y, "DURATION", _format_duration(trade.get("duration_seconds", 0)),
          label_font=label_font, value_font=value_font)

    # --- Username, bottom-right of the whole card ---------------------------
    username = trade.get("username")
    if username:
        handle = username if str(username).startswith("@") else f"@{username}"
        uname_font = _font(FONT_BOLD, 44)
        text_w = draw.textlength(handle, font=uname_font)
        margin = 50
        ux = base.width - margin - text_w
        uy = base.height - margin - uname_font.size
        draw.text((ux, uy), handle, font=uname_font, fill=WHITE)

    # --- Composite + save -----------------------------------------------------
    final_img = Image.alpha_composite(base, overlay).convert("RGB")

    fd, path = tempfile.mkstemp(prefix="pnl_card_", suffix=".png", dir=tempfile.gettempdir())
    os.close(fd)
    final_img.save(path, "PNG")
    return path