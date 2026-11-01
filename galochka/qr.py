import io
import re
import warnings

import qrcode
import zxingcpp
from PIL import Image, ImageOps, UnidentifiedImageError
from qrcode.image.svg import SvgPathImage

from galochka.images import check_size

# Same limits as images.py: an oversized image is refused, not decoded.
Image.MAX_IMAGE_PIXELS = 50_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)

# Payload of a worker badge: "galochka:badge:" + 32 url-safe characters.
BADGE_RE = re.compile(r"^galochka:badge:([A-Za-z0-9_-]{32})$")

_MAX_SIDE = 2048
_DECODE_ERRORS = (
    UnidentifiedImageError, OSError, ValueError, SyntaxError,
    Image.DecompressionBombError, Image.DecompressionBombWarning,
)


def _read(image: Image.Image) -> str | None:
    results = zxingcpp.read_barcodes(image)
    if not results:
        return None
    return results[0].text.strip()


def is_image(data: bytes) -> bool:
    """True when the bytes are an image Pillow can read and that is not oversized (a photo without a code is still one)."""
    try:
        image = Image.open(io.BytesIO(data))
        check_size(image)
        image.load()
    except _DECODE_ERRORS:
        return False
    return True


def decode_qr(data: bytes, full_res_retry: bool = True) -> str | None:
    """Decode the first QR/barcode in an image, or None if unreadable.

    Pure CPU/IO work on the given bytes: callers must not hold the global
    DB lock while calling this. ``full_res_retry=False`` skips the expensive
    full-resolution second attempt (used where most photos have no code).
    """
    try:
        image = Image.open(io.BytesIO(data))
        check_size(image)
        if image.format == "JPEG":
            # DCT-domain downscale: much cheaper than a full decode.
            image.draft("RGB", (_MAX_SIDE, _MAX_SIDE))
        image.load()
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
    except _DECODE_ERRORS:
        return None

    image.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.BILINEAR)
    text = _read(image)
    if text is not None:
        return text

    if not full_res_retry:
        return None

    # A small/far QR code may only be readable at full resolution. Re-open
    # without draft so we really get the original pixels.
    try:
        original = Image.open(io.BytesIO(data))
        check_size(original)
        original.load()
        original = ImageOps.exif_transpose(original).convert("RGB")
    except _DECODE_ERRORS:
        return None
    if original.size == image.size:
        return None
    return _read(original)


def make_svg(text: str, box_size: int = 6, border: int = 2) -> str:
    """Render text as a QR code and return the SVG document as a string.

    The SVG carries a viewBox, so it scales to any CSS size (the badge page sets 30 mm).
    """
    img = qrcode.make(text, image_factory=SvgPathImage, box_size=box_size, border=border)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")
