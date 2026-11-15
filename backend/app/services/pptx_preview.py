"""Extract the rendered slide elements needed for an in-browser PPTX preview."""

import base64
from hashlib import sha256
from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

EMU_PER_INCH = 914400


def _font_color(font):
    try:
        color = font.color.rgb
    except (AttributeError, TypeError, ValueError):
        return None
    return str(color) if color else None


def preview_presentation(path: Path) -> dict:
    """Return slide dimensions, positions, text styles, and embedded images from a saved deck."""
    deck = Presentation(str(path))
    image_data = {}
    slides = []

    for slide in deck.slides:
        shapes = []
        for shape in slide.shapes:
            position = {
                "x": shape.left / EMU_PER_INCH,
                "y": shape.top / EMU_PER_INCH,
                "width": shape.width / EMU_PER_INCH,
                "height": shape.height / EMU_PER_INCH,
            }
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                image = shape.image
                digest = sha256(image.blob).hexdigest()
                image_data.setdefault(
                    digest,
                    f"data:{image.content_type};base64,{base64.b64encode(image.blob).decode('ascii')}",
                )
                shapes.append({"type": "image", "image_key": digest, **position})
                continue

            if not shape.has_text_frame or not shape.text.strip():
                continue

            frame = shape.text_frame
            paragraphs = []
            for paragraph in frame.paragraphs:
                if not paragraph.text:
                    continue
                font = paragraph.font
                alignment = getattr(paragraph.alignment, "name", "LEFT").lower()
                paragraphs.append({
                    "text": paragraph.text,
                    "font_family": font.name or "Arial",
                    "font_size_pt": float(font.size.pt) if font.size else 12,
                    "color": _font_color(font),
                    "bold": bool(font.bold),
                    "alignment": alignment,
                })
            if paragraphs:
                shapes.append({
                    "type": "text",
                    "vertical_anchor": getattr(frame.vertical_anchor, "name", "TOP").lower(),
                    "paragraphs": paragraphs,
                    **position,
                })
        slides.append({"shapes": shapes})

    return {
        "width_inches": deck.slide_width / EMU_PER_INCH,
        "height_inches": deck.slide_height / EMU_PER_INCH,
        "images": image_data,
        "slides": slides,
    }
