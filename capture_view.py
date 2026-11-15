"""Make UI captures cheap to send back to a chat.

A chat pays for every image it receives, by its pixel count (roughly width x height / 750 tokens for Claude), and keeps
paying for it on every later turn while it stays in context: a UI loop of twenty 1280 x 720 reloads would send twenty
~1,200-token images. The capture is still rendered at the size the chat asked for (layout depends on it); only what
comes back changes:

- view "fit" (default): the whole frame scaled down to at most FIT_EDGE pixels on its long edge (~440 tokens at 16:9);
- view "crop": one region at full resolution, given as `region` [x, y, w, h] in capture pixels, or found by `element`
  (a substring of a UI element's name, class or type, matched against the layout facts below);
- view "full": the frame as rendered; view "none": no image at all (the path is still returned).

Layout facts: the capture also writes `layout/<capture>.json` with every visible element's bounds, font and text
(Editor/InteractionRuntimeReview/AuditFacts.WriteLayout, the UI audit's own format). With `element`, the matching
elements come back as text (bounds in capture pixels, text, font size), which often answers "did it move / wrap / fit"
without looking at a picture.
"""
import io
import json
from pathlib import Path

from PIL import Image

FIT_EDGE = 768
CROP_PAD = 16
MAX_MATCHES = 12
VIEWS = ("fit", "crop", "full", "none")

SCHEMA_PROPERTIES = {
    "view": {"enum": list(VIEWS), "description": "What image to send back (the capture is always rendered at width x "
             "height): fit (default, whole frame scaled to 768 px), crop (region or element at full resolution), full, "
             "none (text only)."},
    "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
               "description": "Crop [x, y, w, h] in capture pixels (view crop)."},
    "element": {"type": "string", "description": "Find UI elements whose name, class or type contains this text: their "
                "bounds, text and font come back as text, and view crop crops to the first match."},
}


def layout_path(shot):
    shot = Path(shot)
    return shot.parent / "layout" / (shot.stem + ".json")


def _elements(shot):
    path = layout_path(shot)
    if not path.is_file():
        return None, 1.0
    record = json.loads(path.read_text(encoding="utf-8"))
    return record.get("elements", []), float(record.get("scale") or 1.0)


def find(shot, needle):
    elements, scale = _elements(shot)
    if elements is None:
        return None
    needle = needle.lower()
    matches = []
    for item in elements:
        haystack = " ".join([item.get("name", ""), item.get("type", ""), *item.get("classes", [])]).lower()
        if needle not in haystack or not item.get("visible"):
            continue
        x, y, w, h = (round(value * scale) for value in item["bounds"])
        row = {"key": item["key"][-120:], "bounds": [x, y, w, h]}
        if item.get("text"):
            row["text"] = item["text"]
            row["font_size"] = round(item.get("fontSize", 0) * scale, 1)
        matches.append(row)
    return matches


def _encode(image):
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def shape(shot, view="fit", region=None, element=None):
    """Returns (PNG bytes or None, summary additions)."""
    view = view or "fit"
    if view not in VIEWS:
        raise ValueError(f"view must be one of {VIEWS}")
    extra = {"layout": str(layout_path(shot)) if layout_path(shot).is_file() else None}
    matches = find(shot, element) if element else None
    if element:
        extra["elements"] = (matches or [])[:MAX_MATCHES] if matches is not None else "no layout facts for this capture"
        if matches and len(matches) > MAX_MATCHES:
            extra["elements_more"] = len(matches) - MAX_MATCHES
    if view == "none":
        return None, extra
    image = Image.open(shot).convert("RGB")
    if view == "crop":
        box = region
        if box is None and matches:
            box = matches[0]["bounds"]
        if box is None:
            hint = ""
            if element:
                elements, _ = _elements(shot)
                names = sorted({c for item in elements or () if item.get("visible") for c in item.get("classes", [])
                                if not c.startswith("unity-")})
                hint = f" No visible element matches {element!r}; visible classes include: {', '.join(names[:40])}."
            raise ValueError("view crop needs region [x, y, w, h] or an element that matches a visible UI element." + hint)
        x, y, w, h = box
        left, top = max(0, int(x) - CROP_PAD), max(0, int(y) - CROP_PAD)
        right, bottom = min(image.width, int(x + w) + CROP_PAD), min(image.height, int(y + h) + CROP_PAD)
        if right <= left or bottom <= top:
            raise ValueError(f"The crop {box} is outside the {image.width} x {image.height} capture")
        image = image.crop((left, top, right, bottom))
        extra["crop"] = [left, top, right - left, bottom - top]
    elif view == "fit":
        edge = max(image.size)
        if edge > FIT_EDGE:
            factor = FIT_EDGE / edge
            image = image.resize((round(image.width * factor), round(image.height * factor)), Image.LANCZOS)
            extra["scaled_to"] = list(image.size)
    extra["image_tokens_about"] = round(image.width * image.height / 750)
    return _encode(image), extra
