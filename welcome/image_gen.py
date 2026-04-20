from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import Optional, Tuple

import aiohttp

log = logging.getLogger("red.enigma.welcome")

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    log.info("Pillow not installed — welcome image cards disabled. Run: pip install Pillow")


async def _fetch(url: str) -> Optional[bytes]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as exc:
        log.debug("Failed to fetch %s: %s", url, exc)
    return None


def _circle_crop(img: "Image.Image", size: int) -> "Image.Image":
    img = img.resize((size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask)
    return result


def _load_font(size: int) -> "ImageFont.FreeTypeFont":
    candidates = [
        r"C:\Windows\Fonts\calibrib.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _gradient_background(
    width: int,
    height: int,
    top: Tuple[int, int, int],
    bottom: Tuple[int, int, int],
) -> "Image.Image":
    img = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))
    return img


async def generate_welcome_card(
    avatar_url: str,
    username: str,
    display_name: str,
    server_name: str,
    member_count: int,
    bg_url: Optional[str] = None,
    accent_color: Tuple[int, int, int] = (88, 101, 242),
) -> Optional[BytesIO]:
    if not PIL_AVAILABLE:
        return None

    W, H = 900, 280
    AV_SIZE = 160
    AV_X = 55
    AV_Y = (H - AV_SIZE) // 2

    # ── Background ───────────────────────────────────────────────────────────
    card: Image.Image
    if bg_url:
        bg_data = await _fetch(bg_url)
        if bg_data:
            try:
                bg = Image.open(BytesIO(bg_data)).convert("RGBA").resize((W, H), Image.LANCZOS)
                bg = bg.filter(ImageFilter.GaussianBlur(radius=5))
                overlay = Image.new("RGBA", (W, H), (0, 0, 0, 150))
                card = Image.alpha_composite(bg, overlay)
            except Exception:
                card = _gradient_background(W, H, (18, 18, 35), (30, 28, 60))
        else:
            card = _gradient_background(W, H, (18, 18, 35), (30, 28, 60))
    else:
        card = _gradient_background(W, H, (18, 18, 35), (30, 28, 60))

    draw = ImageDraw.Draw(card)

    # Accent bar on left edge
    draw.rectangle([(0, 0), (5, H)], fill=accent_color + (255,))

    # Vertical divider after avatar area
    div_x = AV_X + AV_SIZE + 30
    draw.line([(div_x, 30), (div_x, H - 30)], fill=(255, 255, 255, 45), width=2)

    # Subtle horizontal lines for depth
    for y_off in (0, H - 1):
        draw.line([(0, y_off), (W, y_off)], fill=accent_color + (180,), width=2)

    # ── Avatar ───────────────────────────────────────────────────────────────
    avatar_data = await _fetch(avatar_url)
    if avatar_data:
        try:
            av_img = Image.open(BytesIO(avatar_data)).convert("RGBA")
            av_circle = _circle_crop(av_img, AV_SIZE)

            ring_pad = 5
            ring_size = AV_SIZE + ring_pad * 2
            ring = Image.new("RGBA", (ring_size, ring_size), (0, 0, 0, 0))
            ring_draw = ImageDraw.Draw(ring)
            ring_draw.ellipse((0, 0, ring_size, ring_size), fill=accent_color + (230,))
            # Cut out inner circle to make it a ring
            inner_pad = ring_pad
            ring_draw.ellipse(
                (inner_pad, inner_pad, ring_size - inner_pad, ring_size - inner_pad),
                fill=(0, 0, 0, 0),
            )
            card.paste(ring, (AV_X - ring_pad, AV_Y - ring_pad), ring)
            card.paste(av_circle, (AV_X, AV_Y), av_circle)
        except Exception as exc:
            log.debug("Avatar rendering failed: %s", exc)

    # ── Text ─────────────────────────────────────────────────────────────────
    tx = div_x + 32

    font_label = _load_font(14)
    font_server = _load_font(32)
    font_user = _load_font(24)
    font_count = _load_font(18)

    # "WELCOME TO" label
    draw.text((tx, 52), "WELCOME TO", fill=(180, 180, 210, 170), font=font_label)

    # Server name (truncate to avoid overflow)
    server_display = server_name if len(server_name) <= 28 else server_name[:26] + "…"
    draw.text((tx, 73), server_display, fill=(255, 255, 255, 255), font=font_server)

    # Separator line under server name
    draw.line([(tx, 118), (tx + 380, 118)], fill=accent_color + (120,), width=1)

    # Display name
    name_display = display_name if len(display_name) <= 30 else display_name[:28] + "…"
    draw.text((tx, 128), name_display, fill=accent_color + (255,), font=font_user)

    # Member count
    draw.text(
        (tx, 165),
        f"You are member #{member_count:,}",
        fill=(190, 190, 215, 200),
        font=font_count,
    )

    # ── Export ───────────────────────────────────────────────────────────────
    buf = BytesIO()
    card.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
