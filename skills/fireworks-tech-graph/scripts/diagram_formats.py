"""Local, deliberately bounded Draw.io and Excalidraw graph interchange."""
from __future__ import annotations

import base64
import binascii
import hashlib
from html import escape
from html.parser import HTMLParser
from io import BytesIO
from itertools import islice
import json
import math
from pathlib import Path
import re
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import zlib

from diagram_ir import DiagramIR, normalize_diagram
from fireworks_geometry import estimate_text_width
from style_quality import contrast_ratio
from validate_svg import Bounds, segment_hits_bounds

MAX_BYTES = 8 * 1024 * 1024
MAX_ELEMENTS = 1200
MAX_POINTS = 80
_SUFFIXES = {".json": "json", ".drawio": "drawio", ".xml": "drawio", ".excalidraw": "excalidraw"}


def format_for(path: Path) -> str:
    try:
        return _SUFFIXES[path.suffix.lower()]
    except KeyError:
        raise ValueError("diagram interchange requires .json, .drawio, .xml or .excalidraw") from None


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _read(path: Path) -> str:
    with path.open("rb") as file:
        data = file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("diagram exceeds the 8 MiB interchange limit")
    return data.decode("utf-8-sig")


def read_diagram_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("diagram JSON must be an object")
    return data


def _number(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a finite coordinate") from None
    if isinstance(value, bool) or not math.isfinite(result) or abs(result) > 100_000:
        raise ValueError(f"{label} is outside the supported coordinate range")
    return result


def _geometry(source: dict, identifier: str) -> dict:
    result = {key: _number(source.get(key, 0), f"{identifier}.{key}") for key in ("x", "y", "width", "height")}
    if result["width"] <= 0 or result["height"] <= 0:
        raise ValueError(f"{identifier} needs positive width and height")
    return result


class _Label(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "div", "p"):
            self.parts.append("\n")


def _color(value):
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{3}", value):
        return "#" + "".join(char * 2 for char in value[1:])
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else None


def _label(value, html=False) -> str:
    if not isinstance(value, str):
        return ""
    if html:
        parser = _Label()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    return "\n".join(line for part in value.splitlines() if (line := " ".join(part.split())))


def _style_number(value, default, minimum, maximum):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, result)) if math.isfinite(result) else default


def _native_style(properties, label_source=""):
    color = _color(properties.get("fontColor"))
    inline = re.search(r"font-size:\s*([0-9.]+)px", label_source)
    size = properties.get("fontSize") or (inline.group(1) if inline else None)
    return dict(font_size=_style_number(size, 12, 7, 72), font_color=color,
                align=properties.get("align", "center"), vertical=properties.get("verticalAlign", "middle"),
                position=properties.get("verticalLabelPosition"), dashed=properties.get("dashed") == "1",
                stroke_width=_style_number(properties.get("strokeWidth"), 1, 0.5, 12),
                rounded=properties.get("rounded") == "1",
                transparent_stroke=properties.get("strokeColor") == "none")


def _drawio_label(entry, cell, properties, warnings, identifier):
    value = entry.get("label", cell.get("value", ""))
    if entry.get("placeholders") == "1":
        def substitute(match):
            name = match.group(1)
            if name not in entry.attrib:
                warnings.append(f"unresolved Draw.io label property {name} on {identifier}")
            return escape(entry.get(name, "")) if properties.get("html") == "1" else entry.get(name, "")
        value = re.sub(r"%([A-Za-z_][\w.-]*)%", substitute, value)
    return _label(value, properties.get("html") == "1"), value


def _drawio_port(properties, prefix):
    if all(prefix + axis in properties for axis in ("X", "Y")):
        return [_style_number(properties[prefix + axis], .5, 0, 1) for axis in ("X", "Y")]
    return None


# The native editors have different names for common endpoint markers.
_DRAWIO_MARKERS = {
    "none": None, "classic": "arrow", "open": "arrow", "openThin": "arrow",
    "block": "triangle", "blockThin": "triangle", "oval": "circle",
    "diamond": "diamond", "diamondThin": "diamond", "dash": "bar",
}
_EXCAL_MARKERS = {"arrow": "open", "triangle": "block", "circle": "oval", "diamond": "diamond", "bar": "dash"}
_RASTER_FORMATS = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}
_IMAGE_URI = re.compile(r"data:image/(png|jpeg|webp)(?:;base64)?,([A-Za-z0-9+/]+={0,2})", re.ASCII)


def _embedded_image(uri):
    match = _IMAGE_URI.fullmatch(uri) if isinstance(uri, str) and len(uri) <= MAX_BYTES else None
    if not match:
        raise ValueError("expected a bounded embedded PNG, JPEG or WebP")
    media, encoded = match.groups()
    try:
        from PIL import Image
    except ImportError:
        raise ValueError("embedded image interchange needs Pillow") from None
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(BytesIO(raw), formats=[_RASTER_FORMATS[media]]) as image:
            if image.width * image.height > 24_000_000 or getattr(image, "n_frames", 1) != 1:
                raise ValueError("unsupported embedded image dimensions or frame count")
            image.verify()
    except (binascii.Error, OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError("invalid embedded raster image") from None
    return f"data:image/{media};base64,{encoded}"


def _import_image(uri, identifier, warnings):
    try:
        return _embedded_image(uri)
    except ValueError as error:
        warnings.append(f"skipped image {identifier}: {error}")
        return None


def _excal_label_style(element):
    return dict(font_size=_style_number(element.get("fontSize"), 16, 7, 72),
                font_color=_color(element.get("strokeColor")), align=element.get("textAlign", "center"),
                vertical=element.get("verticalAlign", "middle"))


def _marker(value, mapping, default, identifier, endpoint, warnings):
    if value is None:
        return None
    if isinstance(value, str) and value in mapping:
        return mapping[value] if isinstance(mapping, dict) else value
    warnings.append(f"simplified {endpoint} marker on {identifier}: {value!r}")
    return default


def edge_markers(edge):
    start = edge.get("start_marker")
    end = edge.get("end_marker", None if edge.get("native_line") else "arrow")
    if any(marker is not None and (not isinstance(marker, str) or marker not in _EXCAL_MARKERS)
           for marker in (start, end)):
        raise ValueError(f"unsupported endpoint marker on {edge['id']}")
    return start, end


def _xml(text: str) -> ET.Element:
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ValueError("XML declarations for DTDs and entities are unsupported")
    return ET.fromstring(text)


def _drawio_page(text: str, page):
    root = _xml(text)
    if root.tag == "mxGraphModel" and page in (None, 1):
        return root, "Draw.io diagram"
    if root.tag != "mxfile":
        raise ValueError("expected a Draw.io mxfile or mxGraphModel")
    pages = root.findall("diagram")
    if page is None and len(pages) > 1:
        raise ValueError("Draw.io has multiple pages; select one with --page (1-based)")
    page = 1 if page is None else page
    if not 1 <= page <= len(pages):
        raise ValueError("Draw.io page does not exist")
    selected = pages[page - 1]
    model = selected.find("mxGraphModel")
    if model is None:
        encoded = "".join((selected.text or "").split())
        stream = zlib.decompressobj(-zlib.MAX_WBITS)
        try:
            decoded = stream.decompress(base64.b64decode(encoded, validate=True), MAX_BYTES + 1)
        except zlib.error:
            raise ValueError("invalid compressed Draw.io page") from None
        if len(decoded) > MAX_BYTES or not stream.eof or stream.unused_data:
            raise ValueError("invalid or oversized compressed Draw.io page")
        model = _xml(unquote(decoded.decode("ascii"), errors="strict"))
    if model.tag != "mxGraphModel":
        raise ValueError("expected a Draw.io mxGraphModel")
    return model, selected.get("name", "Draw.io diagram")


def _drawio(text: str, page, warnings: list) -> dict:
    model, title = _drawio_page(text, page)
    root = model.find("root")
    if root is None or len(root) > MAX_ELEMENTS:
        raise ValueError("Draw.io page has no root or exceeds the cell limit")
    cells = {}
    for entry in root:
        cell = entry if entry.tag == "mxCell" else entry.find("mxCell")
        if cell is None:
            warnings.append(f"skipped Draw.io element {entry.tag}")
            continue
        identifier = entry.get("id") or cell.get("id")
        if not identifier or identifier in cells:
            raise ValueError("Draw.io cells need unique IDs")
        cells[identifier] = (entry, cell)
    parents = {cell.get("parent") for _, cell in cells.values() if cell.get("vertex") == "1"}
    edge_ids = {i for i, (_, c) in cells.items() if c.get("edge") == "1"}

    def origin(identifier, seen=()):
        if not identifier or identifier not in cells:
            return 0.0, 0.0
        if identifier in seen or len(seen) >= 64:
            raise ValueError("invalid Draw.io parent hierarchy")
        cell = cells[identifier][1]
        geo = cell.find("mxGeometry")
        x, y = origin(cell.get("parent"), (*seen, identifier))
        if geo is not None:
            x += _number(geo.get("x", 0), identifier)
            y += _number(geo.get("y", 0), identifier)
        return x, y

    connected = {c.get(key) for _, c in cells.values() if c.get("edge") == "1" for key in ("source", "target")}
    containers, nodes, edges = [], [], []
    labels = {}
    for identifier, (entry, cell) in cells.items():
        style = cell.get("style", "")
        geo = cell.find("mxGeometry")
        parent = cell.get("parent")
        # Draw.io accepts both data:image/png,... and data:image/png;base64,...
        # inside its semicolon-delimited style.
        style = re.sub(r"(image=data:image/(?:png|jpeg|webp));base64,", r"\1,", style)
        pieces = style.split(";")
        properties = dict(part.split("=", 1) for part in pieces if "=" in part)
        label, label_source = _drawio_label(entry, cell, properties, warnings, identifier)
        native_style = _native_style(properties, label_source)
        if cell.get("vertex") == "1":
            if parent in edge_ids:
                offset = geo.find("mxPoint[@as='offset']") if geo is not None else None
                labels.setdefault(parent, []).append(dict(
                    label=label, **native_style,
                    fraction=_style_number(geo.get("x") if geo is not None else None, 0, -1, 1) / 2 + .5,
                    dx=_number(offset.get("x", 0), identifier) if offset is not None else 0,
                    dy=_number(offset.get("y", 0), identifier) if offset is not None else 0,
                ))
                continue
            if geo is None or geo.get("relative") == "1":
                warnings.append(f"skipped relative Draw.io vertex {identifier}")
                continue
            item = dict(id=identifier, label=label, native_style=native_style, **_geometry(geo.attrib, identifier))
            item["x"], item["y"] = origin(identifier)
            for native, field in (("fillColor", "fill"), ("strokeColor", "stroke")):
                if color := _color(properties.get(native)):
                    item[field] = color
            if properties.get("rotation", "0") != "0":
                warnings.append(f"rotation not imported for {identifier}")
            if parent in parents and parent in cells and cells[parent][1].get("vertex") == "1":
                item["parent"] = parent
            if "image" in properties or properties.get("shape") == "image":
                image = _import_image(properties.get("image"), identifier, warnings)
                if image is None:
                    continue
                item.update(native_shape="image", native_image=image)
                if any(properties.get(key) == "1" for key in ("flipH", "flipV", "imageFlipH", "imageFlipV")):
                    warnings.append(f"image flip not imported for {identifier}")
                nodes.append(item)
                continue
            boundary = properties.get("shape") == "mxgraph.aws4.group" or (
                properties.get("fillColor") == "none" and properties.get("verticalAlign") == "top"
                and properties.get("dashed") == "1" and not properties.get("shape"))
            if boundary:
                item["native_boundary"] = True
            if identifier in parents or (boundary and identifier not in connected) or "swimlane" in pieces or "group" in pieces:
                containers.append(item)
            else:
                shape = next((s for s in ("ellipse", "rhombus", "text") if s in style.split(";")), "")
                if shape:
                    item["native_shape"] = "diamond" if shape == "rhombus" else shape
                if properties.get("shape", "").startswith("mxgraph."):
                    warnings.append(f"stencil simplified for Draw.io vertex {identifier}: {properties['shape']}")
                nodes.append(item)
        elif cell.get("edge") == "1":
            item = dict(id=identifier, label=label, native_style=native_style)
            if color := _color(properties.get("strokeColor")):
                item["stroke"] = color
            if properties.get("edgeStyle") in {"orthogonalEdgeStyle", "elbowEdgeStyle"}:
                item["native_orthogonal"] = True
            for key, prefix in (("start_port", "exit"), ("end_port", "entry")):
                if (port := _drawio_port(properties, prefix)) is not None:
                    item[key] = port
            item["start_marker"] = _marker(properties.get("startArrow", "none"), _DRAWIO_MARKERS,
                                           "arrow", identifier, "start", warnings)
            item["end_marker"] = _marker(properties.get("endArrow", "classic"), _DRAWIO_MARKERS,
                                         "arrow", identifier, "end", warnings)
            if item["end_marker"] is None:
                item["native_line"] = True
            ox, oy = origin(parent)
            points = [] if geo is None else geo.findall("Array[@as='points']/mxPoint")
            if len(points) > MAX_POINTS:
                raise ValueError(f"too many bends on Draw.io edge {identifier}")
            if points:
                item["route_points"] = [[ox + _number(p.get("x"), identifier), oy + _number(p.get("y"), identifier)] for p in points]
            for endpoint, n in (("source", 1), ("target", 2)):
                if cell.get(endpoint):
                    item[endpoint] = cell.get(endpoint)
                elif geo is not None:
                    point = geo.find(f"mxPoint[@as='{endpoint}Point']")
                    if point is not None:
                        item[f"x{n}"] = ox + _number(point.get("x"), identifier)
                        item[f"y{n}"] = oy + _number(point.get("y"), identifier)
            edges.append(item)
    for edge in edges:
        if children := labels.get(edge["id"]):
            edge["label"] = "\n".join(filter(None, [edge["label"], *(label["label"] for label in children)]))
            edge["native_label_style"] = {key: value for key, value in children[0].items() if key != "label"}
            if len(children) > 1:
                warnings.append(f"merged multiple connector labels on {edge['id']}")
    return _imported(title, nodes, edges, containers, warnings)


def _excalidraw(text: str, warnings: list) -> dict:
    scene = json.loads(text)
    if not isinstance(scene, dict) or scene.get("type") != "excalidraw" or scene.get("version") != 2:
        raise ValueError("expected an Excalidraw version 2 scene")
    elements = scene.get("elements")
    if not isinstance(elements, list) or len(elements) > MAX_ELEMENTS:
        raise ValueError("Excalidraw scene has invalid or too many elements")
    files = scene.get("files", {})
    if not isinstance(files, dict):
        raise ValueError("Excalidraw image files must be an object")
    by_id, bound_labels = {}, {}
    for element in elements:
        if not isinstance(element, dict) or not isinstance(element.get("id"), str) or not element["id"]:
            raise ValueError("Excalidraw elements need string IDs")
        identifier = element["id"]
        if identifier in by_id:
            raise ValueError(f"duplicate Excalidraw element: {identifier}")
        by_id[identifier] = element
        metadata = element.get("customData")
        owner = element.get("containerId") or (metadata.get("fireworksLabelFor") if isinstance(metadata, dict) else None)
        if not element.get("isDeleted") and element.get("type") == "text" and isinstance(owner, str):
            bound_labels.setdefault(owner, []).append(element)

    nodes, edges, containers = [], [], []
    for identifier, element in by_id.items():
        if element.get("isDeleted"):
            continue
        kind = element.get("type")
        metadata = element.get("customData")
        owner = element.get("containerId") or (metadata.get("fireworksLabelFor") if isinstance(metadata, dict) else None)
        if kind == "text" and isinstance(owner, str) and owner in by_id and not by_id[owner].get("isDeleted"):
            continue
        labels = bound_labels.get(identifier, [])
        bound_text = "\n".join(filter(None, (_label(text.get("text")) for text in labels)))
        if kind in ("arrow", "line"):
            raw_points = element.get("points")
            if not isinstance(raw_points, list) or not 2 <= len(raw_points) <= MAX_POINTS:
                raise ValueError(f"{identifier} needs 2–{MAX_POINTS} points")
            ox, oy = (_number(element.get(a, 0), identifier) for a in ("x", "y"))
            points = []
            for point in raw_points:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError(f"invalid point on {identifier}")
                points.append([ox + _number(point[0], identifier), oy + _number(point[1], identifier)])
            item = dict(id=identifier, label=bound_text,
                        x1=points[0][0], y1=points[0][1], x2=points[-1][0], y2=points[-1][1],
                        route_points=points[1:-1], rendered_ports=True)
            for endpoint, key in (("source", "startBinding"), ("target", "endBinding")):
                binding = element.get(key)
                if isinstance(binding, dict) and isinstance(binding.get("elementId"), str):
                    item[endpoint] = binding["elementId"]
            if color := _color(element.get("strokeColor")):
                item["stroke"] = color
            item["native_style"] = dict(dashed=element.get("strokeStyle") == "dashed",
                                      stroke_width=_style_number(element.get("strokeWidth"), 1, .5, 12))
            if labels:
                item["native_label_style"] = _excal_label_style(labels[0])
            item["start_marker"] = _marker(element.get("startArrowhead"), _EXCAL_MARKERS.keys(),
                                           "arrow", identifier, "start", warnings)
            item["end_marker"] = _marker(element.get("endArrowhead", None if kind == "line" else "arrow"),
                                         _EXCAL_MARKERS.keys(), "arrow", identifier, "end", warnings)
            if item["end_marker"] is None:
                item["native_line"] = True
            edges.append(item)
        elif kind in ("rectangle", "ellipse", "diamond", "text", "frame", "magicframe", "image"):
            if kind == "image":
                file_id = element.get("fileId")
                asset = files.get(file_id) if isinstance(file_id, str) else None
                image = _import_image(asset.get("dataURL") if isinstance(asset, dict) else None, identifier, warnings)
                if image is None:
                    continue
                if element.get("crop") or element.get("scale", [1, 1]) != [1, 1]:
                    warnings.append(f"image crop or flip not imported for {identifier}")
            item = dict(id=identifier, **_geometry(element, identifier))
            if kind == "image":
                item["native_image"] = image
            for native, field in (("backgroundColor", "fill"), ("strokeColor", "stroke")):
                if color := _color(element.get(native)):
                    item[field] = color
            item["label"] = _label(element.get("name") if kind in ("frame", "magicframe") else element.get("text")) or bound_text
            item["native_style"] = dict(dashed=element.get("strokeStyle") == "dashed",
                                        rounded=bool(element.get("roundness")),
                                        stroke_width=_style_number(element.get("strokeWidth"), 1, .5, 12),
                                        transparent_stroke=element.get("strokeColor") == "transparent")
            if kind == "text":
                # For an Excalidraw text element strokeColor is the text color.
                # Forwarding it as a Draw.io shape stroke adds a visible box.
                item.pop("stroke", None)
                item["native_style"]["transparent_stroke"] = True
            if kind == "text" or labels:
                text = element if kind == "text" else labels[0]
                item["native_style"].update(_excal_label_style(text))
                if isinstance(text.get("customData"), dict) and text["customData"].get("fireworksLabelFor") == identifier:
                    if _style_number(text.get("y"), item["y"], -100_000, 100_000) >= item["y"] + item["height"]:
                        item["native_style"]["position"] = "bottom"
            if isinstance(element.get("frameId"), str):
                item["parent"] = element["frameId"]
            if kind in ("frame", "magicframe") or (kind == "rectangle" and isinstance(metadata, dict) and metadata.get("fireworksBoundary") is True):
                if kind == "rectangle":
                    item["native_boundary"] = True
                containers.append(item)
            else:
                item["native_shape"] = kind
                nodes.append(item)
        else:
            warnings.append(f"skipped unsupported Excalidraw element {identifier} ({kind})")
        if element.get("angle"):
            warnings.append(f"rotation not imported for {identifier}")
        if any(not isinstance(group, str) or not group.startswith("fireworks-label-") for group in element.get("groupIds", [])):
            warnings.append(f"freeform grouping not imported for {identifier}")
    return _imported("Excalidraw diagram", nodes, edges, containers, warnings)


def _imported(title, nodes, edges, containers, warnings):
    if not nodes and not containers:
        raise ValueError("input contains no supported graph shapes")
    supported = {item["id"] for item in nodes}
    container_ids = {item["id"] for item in containers}
    for item in [*nodes, *containers]:
        if item.get("parent") and item["parent"] not in container_ids:
            warnings.append(f"unsupported container for {item['id']}")
            del item["parent"]
    kept = []
    for edge in edges:
        if any((edge[k] not in supported if k in edge else f"x{i}" not in edge or f"y{i}" not in edge)
               for k, i in (("source", 1), ("target", 2))):
            warnings.append(f"skipped connector {edge['id']} with unsupported endpoint")
        else:
            kept.append(edge)
    geometry = [(s["x"], s["y"]) for s in [*nodes, *containers]]
    ends = [(s["x"] + s["width"], s["y"] + s["height"]) for s in [*nodes, *containers]]
    extras = [p for e in kept for p in e.get("route_points", [])]
    extras += [[e[f"x{i}"], e[f"y{i}"]] for e in kept for i in (1, 2) if f"x{i}" in e]
    x0, y0 = (min(p[i] for p in geometry + extras) for i in range(2))
    dx, dy = max(0.0, 32 - x0), max(0.0, 120 - y0)
    for item in [*nodes, *containers]:
        item["x"] += dx
        item["y"] += dy
    for edge in kept:
        for point in edge.get("route_points", []):
            point[0] += dx
            point[1] += dy
        for i in (1, 2):
            if f"x{i}" in edge:
                edge[f"x{i}"] += dx
                edge[f"y{i}"] += dy
    return dict(schema_version=1, mode="architecture", style=1, semantic_profile="generic",
                title=title, width=max(420, max(p[0] for p in ends + extras) + dx + 48),
                height=max(300, max(p[1] for p in ends + extras) + dy + 48),
                containers=containers, nodes=nodes, arrows=kept)


def load_diagram(path: Path, page=None, mode=None) -> tuple[DiagramIR, list[str]]:
    fmt = format_for(path)
    warnings = []
    if page is not None and fmt != "drawio":
        raise ValueError("--page is only supported for Draw.io input")
    if fmt == "json":
        data = read_diagram_json(path)
    elif fmt == "drawio":
        data = _drawio(_read(path), page, warnings)
    else:
        data = _excalidraw(_read(path), warnings)
    if mode is not None and fmt != "json":
        data["mode"] = mode
    default_mode = data.get("mode") or data.get("template_type") or "architecture"
    return normalize_diagram(data, mode or default_mode), warnings


def _export_geometry(data):
    shapes = {}
    for item in [*data.get("containers", []), *data["nodes"]]:
        geo = _geometry(item, item["id"])
        parent = item.get("parent") or item.get("deployment_id")
        shapes[item["id"]] = (item, geo, parent)
    containers = {item["id"] for item in data.get("containers", [])}
    for identifier, (_, _, parent) in shapes.items():
        seen = {identifier}
        while parent:
            if parent not in containers:
                raise ValueError(f"{identifier} references unknown container: {parent}")
            if parent in seen:
                raise ValueError(f"container hierarchy cycles at {identifier}")
            seen.add(parent)
            parent = shapes[parent][2]
    return shapes


def _edge_points(edge, shapes):
    anchors, boxes = [], []
    for index, key in enumerate(("source", "target"), 1):
        box = shapes.get(edge.get(key))
        geo = box[1] if box else None
        boxes.append(geo)
        port = edge.get("start_port" if index == 1 else "end_port")
        if edge.get("rendered_ports") or (geo is None and f"x{index}" in edge and f"y{index}" in edge):
            point = [_number(edge[f"x{index}"], edge["id"]), _number(edge[f"y{index}"], edge["id"])]
        elif geo is not None:
            fx, fy = port if port is not None else (.5, .5)
            point = [geo["x"] + geo["width"] * fx, geo["y"] + geo["height"] * fy]
        else:
            raise ValueError(f"{edge['id']} is missing a connector endpoint")
        anchors.append(point)

    bends = edge.get("route_points", [])
    if not edge.get("rendered_ports"):
        for index, port_key in enumerate(("start_port", "end_port")):
            geo = boxes[index]
            if geo is None or edge.get(port_key) is not None:
                continue
            point = anchors[index]
            aim = (bends[0 if index == 0 else -1] if bends else anchors[1 - index])
            delta_x, delta_y = aim[0] - point[0], aim[1] - point[1]
            if delta_x or delta_y:
                if edge.get("native_orthogonal"):
                    axis = int(abs(delta_y) * geo["width"] > abs(delta_x) * geo["height"])
                    delta = (delta_x, delta_y)[axis]
                    point[axis] += math.copysign(geo[("width", "height")[axis]] / 2, delta)
                else:
                    scale = 0.5 / max(abs(delta_x) / geo["width"], abs(delta_y) / geo["height"])
                    point[0] += delta_x * scale
                    point[1] += delta_y * scale
        if edge.get("native_orthogonal") and not bends:
            (x, y), (tx, ty) = anchors
            if x != tx and y != ty:
                start, end = edge.get("start_port"), edge.get("end_port")
                start_axis = int(start is not None and start[1] in (0, 1)) if start else int(abs(ty - y) > abs(tx - x))
                end_axis = int(end[1] in (0, 1)) if end else start_axis
                if start_axis != end_axis:
                    bends = [[x, ty]] if start_axis else [[tx, y]]
                elif start_axis:
                    halfway = (y + ty) / 2
                    bends = [[x, halfway], [tx, halfway]]
                else:
                    halfway = (x + tx) / 2
                    bends = [[halfway, y], [halfway, ty]]
    points = []
    for point in [anchors[0], *bends, anchors[1]]:
        if not points or points[-1] != point:
            points.append(point)
    if len(points) < 2:
        raise ValueError(f"{edge['id']} has coincident endpoints and no route")
    return points


def _wrap_label(label, font_size, limit):
    lines = []
    for paragraph in label.splitlines():
        line = ""
        for token in re.findall(r"[\u3001-\u9fff][\ufe0e\ufe0f]*|[^\s\u3001-\u9fff]+|\s+", paragraph):
            candidate = line + token if line else token.lstrip()
            if line.strip() and not token.isspace() and estimate_text_width(candidate, font_size) > limit:
                lines.append(line.rstrip())
                line = token
            else:
                line = candidate
        if line.strip():
            lines.append(line.rstrip())
    return lines


def _point_on_edge(points, fraction):
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    remaining = sum(lengths) * fraction
    for a, b, length in zip(points, points[1:], lengths):
        if length and remaining <= length:
            ratio = remaining / length
            dx, dy = b[0] - a[0], b[1] - a[1]
            return a[0] + dx * ratio, a[1] + dy * ratio, -dy / length, dx / length
        remaining -= length
    return points[-1][0], points[-1][1], 0, 1


def _drawio_output(data, shapes):
    mxfile = ET.Element("mxfile", compressed="false")
    page = ET.SubElement(mxfile, "diagram", id="fireworks", name=str(data.get("title", "Diagram")))
    model = ET.SubElement(page, "mxGraphModel")
    root = ET.SubElement(model, "root")
    ids = set(shapes) | {e["id"] for e in data["arrows"]}
    # Preserve the IDs exchanged with editors, including native numeric IDs.
    # Choose root/layer IDs outside the diagram's own ID space.
    root_id, layer_id = islice((str(n) for n in range(len(ids) + 2) if str(n) not in ids), 2)
    ET.SubElement(root, "mxCell", id=root_id)
    ET.SubElement(root, "mxCell", id=layer_id, parent=root_id)
    container_ids = {c["id"] for c in data.get("containers", [])}
    for identifier, (item, geo, parent) in shapes.items():
        native = item.get("native_shape")
        style = {"ellipse": "ellipse;", "diamond": "rhombus;", "text": "text;"}.get(native, "rounded=1;")
        appearance = item.get("native_style", {})
        if native == "image":
            image = _embedded_image(item.get("native_image"))
            style = "shape=image;aspect=fixed;image=" + image.replace(";base64,", ",", 1) + ";"
        if identifier in container_ids:
            style = "swimlane;startSize=24;fillColor=none;"
        if item.get("native_boundary"):
            style = "fillColor=none;verticalAlign=top;"
        if appearance.get("transparent_stroke"):
            style += "strokeColor=none;"
        if color := _color(appearance.get("font_color")):
            style += f"fontColor={color};"
        if "font_size" in appearance:
            style += f"fontSize={_style_number(appearance['font_size'], 12, 7, 72)};"
        if appearance.get("dashed"):
            style += "dashed=1;"
        if appearance.get("align") in ("left", "center", "right"):
            style += f"align={appearance['align']};"
        if appearance.get("vertical") in ("top", "middle", "bottom"):
            style += f"verticalAlign={appearance['vertical']};"
        if appearance.get("position") == "bottom":
            style += "verticalLabelPosition=bottom;verticalAlign=top;"
        for field, native in (("fill", "fillColor"), ("stroke", "strokeColor")):
            if color := _color(item.get(field)):
                style += f"{native}={color};"
        cell = ET.SubElement(root, "mxCell", id=identifier, vertex="1", parent=parent or layer_id,
                             value=str(item.get("label", "")), style=style + "whiteSpace=wrap;html=0;")
        relative = dict(geo)
        if parent:
            for key in ("x", "y"):
                relative[key] -= shapes[parent][1][key]
        ET.SubElement(cell, "mxGeometry", {"as": "geometry", **{k: str(v) for k, v in relative.items()}})
    for edge in data["arrows"]:
        attr = {k: edge[k] for k in ("source", "target") if edge.get(k)}
        start, end = edge_markers(edge)
        appearance = edge.get("native_style", {})
        label_style = edge.get("native_label_style", appearance)
        # Stored bend points must survive, including a hand-drawn diagonal.
        router = "none" if edge.get("route_points") or not edge.get("native_orthogonal") else "orthogonalEdgeStyle"
        end_arrow = "block" if "end_marker" not in edge and not edge.get("native_line") else _EXCAL_MARKERS.get(end, "none")
        style = f"edgeStyle={router};html=0;startArrow={_EXCAL_MARKERS.get(start, 'none')};endArrow={end_arrow};"
        if color := _color(edge.get("stroke")):
            style += f"strokeColor={color};"
        if color := _color(label_style.get("font_color")):
            style += f"fontColor={color};"
        if "font_size" in label_style:
            style += f"fontSize={_style_number(label_style['font_size'], 12, 7, 72)};"
        if appearance.get("dashed"):
            style += "dashed=1;"
        points = _edge_points(edge, shapes)
        for endpoint, prefix, point in (("source", "exit", points[0]), ("target", "entry", points[-1])):
            if endpoint in attr:
                box = shapes[attr[endpoint]][1]
                px = (point[0] - box["x"]) / box["width"]
                py = (point[1] - box["y"]) / box["height"]
                if 0 <= px <= 1 and 0 <= py <= 1:
                    style += f"{prefix}X={px};{prefix}Y={py};"
        cell = ET.SubElement(root, "mxCell", id=edge["id"], edge="1", parent=layer_id,
                             value=str(edge.get("label", "")), style=style, **attr)
        geo = ET.SubElement(cell, "mxGeometry", {"as": "geometry", "relative": "1"})
        for end, point in (("source", points[0]), ("target", points[-1])):
            if end not in attr:
                ET.SubElement(geo, "mxPoint", {"as": end + "Point", "x": str(point[0]), "y": str(point[1])})
        if len(points) > 2:
            bends = ET.SubElement(geo, "Array", {"as": "points"})
            for x, y in points[1:-1]:
                ET.SubElement(bends, "mxPoint", x=str(x), y=str(y))
    return ET.tostring(mxfile, encoding="unicode", xml_declaration=True) + "\n"


def _excalidraw_output(data, shapes, quality):
    elements = {}
    files = {}
    ids = set(shapes) | {edge["id"] for edge in data["arrows"]}
    containers = {c["id"] for c in data.get("containers", [])}
    label_boxes = []
    routes = {edge["id"]: _edge_points(edge, shapes) for edge in data["arrows"]}
    segments = [(a, b) for route in routes.values() for a, b in zip(route, route[1:])]
    borders = []
    for identifier in sorted(containers):
        _, box, _ = shapes[identifier]
        left, top = box["x"], box["y"]
        right, bottom = left + box["width"], top + box["height"]
        borders.extend([((left, top), (right, top)), ((right, top), (right, bottom)),
                        ((right, bottom), (left, bottom)), ((left, bottom), (left, top))])
    obstacles = [geo for identifier, (item, geo, parent) in shapes.items()
                 if identifier not in containers and item.get("native_shape") != "text"
                 and not (parent and not item.get("label") and not item.get("fill") and geo == shapes[parent][1])]

    def base(identifier, kind, geometry, frame=None):
        seed = int.from_bytes(hashlib.sha256(identifier.encode()).digest()[:4], "big") >> 1
        item = dict(id=identifier, type=kind, **geometry, angle=0, strokeColor="#1e1e1e",
                    backgroundColor="transparent", fillStyle="solid", strokeWidth=2, strokeStyle="solid",
                    roughness=0, opacity=100, groupIds=[], frameId=frame, roundness=None, seed=seed,
                    version=1, versionNonce=seed, isDeleted=False, boundElements=[], updated=0,
                    link=None, locked=False)
        elements[identifier] = item
        return item

    def add_text(identifier, label, geo, frame, font_size, color, alignment,
                 container_id=None, detached_owner=None):
        item = base(identifier, "text", geo, frame)
        item.update(text=label, originalText=label, fontSize=font_size, fontFamily=2, textAlign=alignment,
                    strokeColor=color, verticalAlign="middle", containerId=container_id, lineHeight=1.25, autoResize=False)
        if detached_owner:
            item["customData"] = {"fireworksLabelFor": detached_owner}
            group = "fireworks-label-" + identifier
            item["groupIds"] = [group]
            elements[detached_owner]["groupIds"].append(group)
        label_boxes.append(geo)

    def overlaps(a, b):
        return (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"]
                and a["y"] < b["y"] + b["height"] and b["y"] < a["y"] + a["height"])

    def attach_text(item, label, appearance, *, position=None, route=None, identifier=None):
        if not label:
            if identifier == item["id"]:
                base(identifier, "text", {key: item[key] for key in ("x", "y", "width", "height")}, item["frameId"]).update(
                    text="", originalText="", fontSize=14, fontFamily=2, textAlign="center", verticalAlign="middle",
                    containerId=None, lineHeight=1.25, autoResize=False)
            return
        owner = item["id"]
        identifier = identifier or owner + "-text"
        if identifier != owner:
            while identifier in ids:
                identifier += "-text"
            ids.add(identifier)
        size = _style_number(appearance.get("font_size"), 14, 7, 72)
        color = _color(appearance.get("font_color")) or "#20252b"
        align = appearance.get("align")
        align = align if align in ("left", "right", "center") else "center"
        box = item
        background = _color(box.get("backgroundColor")) if not position and not route else None
        background = background or "#ffffff"
        if contrast_ratio(color, background) < 4.5:
            color = max(("#20252b", "#ffffff", "#111111"), key=lambda candidate: contrast_ratio(candidate, background))
        padding = 0 if identifier == owner else 10
        width = (min(175, max(75, box["width"] * 2.2)) if position == "outside"
                 else 155 if route else max(12, box["width"] - padding))
        height_limit = max(8, box["height"] - (0 if identifier == owner else 8))
        lines = _wrap_label(label, size, width)
        longest = max((estimate_text_width(line, size) for line in lines), default=0)
        while size > 8 and (longest > width or (not position and not route and len(lines) * size * 1.25 > height_limit)):
            size = max(8, size - 1)
            lines = _wrap_label(label, size, width)
            longest = max((estimate_text_width(line, size) for line in lines), default=0)
        text = "\n".join(lines)
        height = max(size * 1.25, len(lines) * size * 1.25)
        if longest > width or (not route and not position and height > height_limit):
            quality.append(f"label may exceed its available area: {owner}")
        width = max(longest + 2, 4)
        x = box["x"] + (box["width"] - width) / 2
        y = box["y"] + (box["height"] - height) / 2
        if align == "left":
            x = box["x"] + 6
        elif align == "right":
            x = box["x"] + box["width"] - width - 6
        candidates = [(x, y)]
        if position == "inside-top":
            candidates = [(x, box["y"] + 5)]
        if position == "outside":
            y = box["y"] + box["height"] + 5
            candidates = [(x, y)]
            candidates += [(x + shift, y) for shift in (-16, 16, -32, 32, -48, 48)]
            candidates += [(box["x"] - width - 7, y), (box["x"] + box["width"] + 7, y),
                           (x, box["y"] - height - 6)]
            candidates += [(x + shift, y + height + 4) for shift in (0, -26, 26)]
        if route:
            pos = appearance.get("fraction", .5)
            dx, dy = appearance.get("dx", 0), appearance.get("dy", 0)
            candidates = []
            for clearance in (5, 11, 20):
                for fraction in (pos, .35, .65, .22, .78):
                    cx, cy, nx, ny = _point_on_edge(route, fraction)
                    if clearance == 5 and (dx or dy):
                        candidates.append((cx + dx - width / 2, cy + dy - height / 2))
                    offset = (abs(nx) * width + abs(ny) * height) / 2 + clearance
                    for direction in (-1, 1):
                        candidates.append((cx + direction * nx * offset - width / 2,
                                           cy + direction * ny * offset - height / 2))
        choices = [dict(x=x, y=y, width=width, height=height) for x, y in candidates]
        blocked = label_boxes + (obstacles if route or position in ("outside", "inside-top") else [])
        def clear(candidate):
            if any(overlaps(candidate, other) for other in blocked):
                return False
            if not (position or route):
                return True
            x, y = candidate["x"], candidate["y"]
            bounds = Bounds(x - 2, y - 2, x + width + 2, y + height + 2)
            return (not any(segment_hits_bounds(a, b, bounds) for a, b in segments)
                    and not any(segment_hits_bounds(a, b, bounds) for a, b in borders))

        geo = next((candidate for candidate in choices if clear(candidate)), None)
        if geo is None:
            geo = choices[0]
            quality.append(f"no clear label placement: {owner}")
        # The editor moves a bound arrow label to its midpoint and a bound
        # shape label inside its box. Preserve external placement using a group.
        if identifier == owner:
            add_text(owner, text, geo, item["frameId"], size, color, align)
        elif position or route:
            add_text(identifier, text, geo, item["frameId"], size, color, align, detached_owner=owner)
        else:
            measured = geo
            bound = {**geo, "x": box["x"] + 5, "width": max(4, box["width"] - 10)}
            add_text(identifier, text, bound, item["frameId"], size, color, align, container_id=owner)
            label_boxes[-1] = measured
            item["boundElements"].append(dict(id=identifier, type="text"))

    for identifier, (item, geo, parent) in shapes.items():
        label = str(item.get("label", ""))
        appearance = item.get("native_style", {})
        if identifier in containers and not item.get("native_boundary"):
            base(identifier, "frame", geo, parent)["name"] = label
        elif item.get("native_shape") == "image":
            image = _embedded_image(item.get("native_image"))
            file_id = hashlib.sha256(image.encode("ascii")).hexdigest()[:40]
            files[file_id] = dict(id=file_id, mimeType=image.split(";", 1)[0][5:], dataURL=image, created=0)
            shape = base(identifier, "image", geo, parent)
            shape.update(fileId=file_id, status="saved", crop=None, scale=[1, 1], strokeColor="transparent")
            attach_text(shape, label, appearance, position="outside")
        elif item.get("native_shape") == "text":
            attach_text(dict(id=identifier, frameId=parent, **geo), label, appearance, identifier=identifier)
        else:
            kind = item.get("native_shape")
            shape = base(identifier, kind if kind in ("rectangle", "ellipse", "diamond") else "rectangle", geo, parent)
            for field, native in (("fill", "backgroundColor"), ("stroke", "strokeColor")):
                if color := _color(item.get(field)):
                    shape[native] = color
            if appearance.get("transparent_stroke") and shape["backgroundColor"] != "transparent":
                shape["strokeColor"] = shape["backgroundColor"]
            shape["strokeWidth"] = _style_number(appearance.get("stroke_width"), 2, .5, 12)
            if appearance.get("dashed"):
                shape["strokeStyle"] = "dashed"
            if appearance.get("rounded"):
                shape["roundness"] = {"type": 3}
            if item.get("native_boundary"):
                shape["customData"] = {"fireworksBoundary": True}
            position = "inside-top" if item.get("native_boundary") else ("outside" if appearance.get("position") == "bottom" else None)
            if position == "inside-top" and (not appearance.get("font_color") or appearance.get("font_color") == "#AAB7B8"):
                appearance = {**appearance, "font_color": shape["strokeColor"], "align": "left"}
            attach_text(shape, label, appearance, position=position)
    for edge in data["arrows"]:
        points = routes[edge["id"]]
        x, y = points[0]
        bounds = dict(x=x, y=y, width=max(p[0] for p in points) - min(p[0] for p in points),
                      height=max(p[1] for p in points) - min(p[1] for p in points))
        start, end = edge_markers(edge)
        free_line = start is None and end is None and not (edge.get("source") or edge.get("target"))
        item = base(edge["id"], "line" if free_line else "arrow", bounds)
        appearance = edge.get("native_style", {})
        item["strokeWidth"] = _style_number(appearance.get("stroke_width"), 2, .5, 12)
        if color := _color(edge.get("stroke")):
            item["strokeColor"] = color
        if appearance.get("dashed"):
            item["strokeStyle"] = "dashed"
        item.update(points=[[px - x, py - y] for px, py in points], startArrowhead=start,
                    endArrowhead=end, lastCommittedPoint=None, elbowed=False)
        for endpoint, key, index in (("source", "startBinding", 0), ("target", "endBinding", -1)):
            target = elements.get(edge.get(endpoint))
            item[key] = None
            if target is not None:
                px, py = points[index]
                fixed = [min(1, max(0, (p - target[axis]) / target[size]))
                         for p, axis, size in ((px, "x", "width"), (py, "y", "height"))]
                item[key] = dict(elementId=target["id"], fixedPoint=fixed, mode="orbit", focus=0, gap=1)
                target["boundElements"].append(dict(id=edge["id"], type="arrow"))
        label_style = edge.get("native_label_style", appearance)
        attach_text(item, str(edge.get("label", "")), label_style, route=points)
    return json_text(dict(type="excalidraw", version=2, source="https://excalidraw.com",
                          elements=list(elements.values()), appState=dict(viewBackgroundColor="#ffffff"), files=files))


def convert_output(data: dict, fmt: str, *, quality: list[str] | None = None) -> str:
    if fmt == "json":
        return json_text(data)
    shapes = _export_geometry(data)
    if fmt == "drawio":
        output = _drawio_output(data, shapes)
    elif fmt == "excalidraw":
        output = _excalidraw_output(data, shapes, quality if quality is not None else [])
    else:
        raise ValueError(f"unsupported output format: {fmt}")
    if len(output.encode("utf-8")) > MAX_BYTES:
        raise ValueError("native output exceeds the 8 MiB interchange limit")
    return output
