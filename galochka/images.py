import io
import uuid
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

# Refuse oversized images instead of decoding them: Pillow only warns between 1x and 2x this limit,
# the warning is turned into an error below.
Image.MAX_IMAGE_PIXELS = 50_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)


class BadImage(ValueError):
    pass


def check_size(image: Image.Image) -> None:
    """Refuse an image over MAX_IMAGE_PIXELS before decoding it (independent of the warning filter)."""
    if image.width * image.height > Image.MAX_IMAGE_PIXELS:
        raise Image.DecompressionBombError(f"image has {image.width * image.height} pixels")


def normalize(data: bytes, max_side: int = 1024) -> bytes:
    """Decode, apply EXIF orientation, convert to RGB, downscale to max_side, re-encode as JPEG q85.

    Raises BadImage when the bytes are not a readable image. Copied from Cassandra
    without the crop/bbox code. Runs outside the DB lock.
    """
    try:
        image = Image.open(io.BytesIO(data))
        check_size(image)
        image.load()
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as e:
        raise BadImage(str(e)) from e

    width, height = image.size
    long_side = max(width, height)
    if long_side > max_side:
        scale = max_side / long_side
        new_size = (round(width * scale), round(height * scale))
        image = image.resize(new_size, Image.LANCZOS)

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def save_photo(photos_dir: Path, subdir: str, data: bytes) -> str:
    """Write data to <photos_dir>/<subdir>/<uuid4hex>.jpg and return the relative path."""
    rel_path = f"{subdir}/{uuid.uuid4().hex}.jpg"
    full_path = photos_dir / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(data)
    return rel_path


def load(photos_dir: Path, rel_path: str) -> Image.Image:
    image = Image.open(photos_dir / rel_path)
    image.load()
    return image.convert("RGB")
