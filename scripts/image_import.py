"""Embed a raster reference at its native dimensions in local diagram formats."""
from __future__ import annotations

import base64
import hashlib
import io
import warnings
from pathlib import Path
import xml.etree.ElementTree as ET

from diagram_formats import convert_output

IMAGE_SUFFIXES = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
IMAGE_OUTPUTS = {".png", ".svg", ".drawio", ".excalidraw"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000


def image_output(source: Path, suffix: str) -> tuple[bytes, dict]:
    if suffix not in IMAGE_OUTPUTS:
        raise ValueError("image input supports .png, .svg, .drawio or .excalidraw output")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        raise ValueError("image input needs Pillow (python3 -m pip install Pillow)") from None

    with source.open("rb") as stream:
        raw = stream.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("raster input exceeds the 12 MiB limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            expected = IMAGE_SUFFIXES[source.suffix.lower()]
            with Image.open(io.BytesIO(raw), formats=[expected]) as probe:
                if probe.width * probe.height > MAX_IMAGE_PIXELS:
                    raise ValueError("raster input exceeds the 24-megapixel limit")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ValueError("animated or multipage raster inputs are unsupported")
                probe.verify()
            with Image.open(io.BytesIO(raw), formats=[expected]) as image:
                shown = ImageOps.exif_transpose(image)
                pixels = shown.convert("RGBA")
                width, height = pixels.size
                icc = image.info.get("icc_profile")
                out = io.BytesIO()
                pixels.save(out, format="PNG", icc_profile=icc)
            png = out.getvalue()
            with Image.open(io.BytesIO(png), formats=["PNG"]) as check:
                if check.size != (width, height) or check.tobytes() != pixels.tobytes():
                    raise ValueError("image pixels changed during PNG readback")
    except (OSError, UnidentifiedImageError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ValueError("raster is invalid or does not match its declared extension") from None

    digest = hashlib.sha256(png).hexdigest()
    result = dict(mode="raster_reference", editable_pixels=False, width=width, height=height,
                  pixel_readback="exact_rgba", embedded_png_sha256=digest)
    if suffix == ".png":
        return png, result
    encoded = base64.b64encode(png).decode("ascii")
    url = "data:image/png;base64," + encoded
    bounds = dict(x=0, y=0, width=width, height=height)

    if suffix in {".drawio", ".excalidraw"}:
        graph = dict(title="Image reference", nodes=[dict(
            id="raster-reference", native_shape="image", native_image=url, **bounds,
        )], arrows=[])
        return convert_output(graph, suffix[1:]).encode("utf-8"), result

    attrs = {key: str(value) for key, value in bounds.items()}
    root = ET.Element("svg", xmlns="http://www.w3.org/2000/svg", width=str(width), height=str(height),
                      viewBox=f"0 0 {width} {height}")
    ET.SubElement(root, "image", href=url, **attrs)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), result
