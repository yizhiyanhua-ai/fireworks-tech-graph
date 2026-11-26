#!/usr/bin/env python3
"""Unified command line for rendering, validating, inspecting, and exporting diagrams."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from diagram_formats import convert_output, format_for, json_text, load_diagram, read_diagram_json  # noqa: E402
from diagram_update import GitDiff, markdown_report, review_update  # noqa: E402
from interactive_html import build_interactive_html  # noqa: E402
from motion import DEFAULT_MOTION_DURATION, MOTION_PRESETS, probe_motion_runtime, render_motion_gif  # noqa: E402
from validate_svg import run_check  # noqa: E402
from png_export import export_png  # noqa: E402
from image_import import IMAGE_SUFFIXES, image_output  # noqa: E402
from semantic_contracts import STYLE_NAMES  # noqa: E402


def _load_generator():
    spec = importlib.util.spec_from_file_location("fireworks_template_generator", SCRIPT_DIR / "generate-from-template.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load diagram generator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def command_doctor(_: argparse.Namespace) -> int:
    motion_runtime = probe_motion_runtime()
    probes = {
        "python": {"ok": sys.version_info >= (3, 9), "value": sys.version.split()[0]},
        "cairosvg": {"ok": importlib.util.find_spec("cairosvg") is not None},
        "pillow": {"ok": importlib.util.find_spec("PIL") is not None, "optional": True},
        "rsvg-convert": {"ok": shutil.which("rsvg-convert") is not None},
        "node": {"ok": shutil.which("node") is not None},
        "ffmpeg": {"ok": shutil.which("ffmpeg") is not None},
        "motion_renderer": motion_runtime,
    }
    probes["raster_export"] = {"ok": probes["cairosvg"]["ok"] or probes["rsvg-convert"]["ok"]}
    probes["animation_export"] = {"ok": motion_runtime["ok"], "optional": True}
    probes["version"] = version_info()
    probes["capabilities"] = {"svg": probes["python"]["ok"], "html": probes["python"]["ok"],
                              "png": probes["raster_export"]["ok"], "gif": motion_runtime["ok"],
                              "image_reference": probes["pillow"]["ok"]}
    print(json.dumps(probes, indent=2, sort_keys=True))
    return 0 if probes["python"]["ok"] and probes["raster_export"]["ok"] else 1


def version_info() -> dict:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    result = {"version": package["version"], "schema_version": 1, "skill_root": str(ROOT),
              "source": "installed-package", "styles": dict(STYLE_NAMES),
              "node_for_motion": ">=22.12.0"}
    if (ROOT / ".git").exists() and shutil.which("git"):
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD"], timeout=5)
        result.update(source="git-checkout", commit=commit.stdout.strip(), modified=dirty.returncode != 0)
    return result


def command_version(_: argparse.Namespace) -> int:
    print(json.dumps(version_info(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_export_png(args: argparse.Namespace) -> int:
    print(json.dumps(export_png(args.svg, args.output, args.width), indent=2, sort_keys=True))
    return 0


def _distinct_outputs(inputs, outputs) -> None:
    targets = [path.resolve() for path in outputs if path is not None]
    if len(targets) != len(set(targets)) or set(targets) & {path.resolve() for path in inputs}:
        raise ValueError("output paths must be distinct and different from their inputs")


def _write_text(path: Path, text: str) -> None:
    _write_bytes(path, text.encode("utf-8"))


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as file:
        pending = Path(file.name)
        try:
            file.write(data)
            file.close()
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def _positioned_export(svg, data, report):
    positioned = copy.deepcopy(data)
    boxes = {}
    for element in ET.fromstring(svg).iter():
        identifier = element.get("data-node-id")
        if identifier and element.get("data-graph-role") == "node":
            x, y, right, bottom = map(float, element.attrib["data-graph-bounds"].split(","))
            boxes[identifier] = dict(x=x, y=y, width=right - x, height=bottom - y)
    for node in positioned["nodes"]:
        node.update(boxes[node["id"]])
    for edge, routed in zip(positioned["arrows"], report["edges"]):
        points = routed["route"]
        edge.update(route_points=points[1:-1], x1=points[0][0], y1=points[0][1],
                    x2=points[-1][0], y2=points[-1][1], rendered_ports=True)
    return positioned


def command_validate(args: argparse.Namespace) -> int:
    diagram, warnings = load_diagram(args.input, args.page, args.mode)
    # Validation covers the same renderer boundary as ``render``. Style 8 is
    # intentionally AI-authored/static, so accepting JSON for it here would
    # promise a render path that does not exist.
    _load_generator().parse_style(diagram.style_index)
    print(json.dumps({
        "ok": True,
        "schema_version": diagram.schema_version,
        "input_schema": diagram.input_schema,
        "mode": diagram.mode,
        "style": {"id": diagram.style_index, "name": diagram.semantic_report["visual_theme"]},
        "semantics": dict(diagram.semantic_report),
        "nodes": len(diagram.nodes),
        "edges": len(diagram.edges),
        "import_warnings": warnings,
    }, indent=2, sort_keys=True))
    return 0


def command_render(args: argparse.Namespace) -> int:
    _distinct_outputs([args.input], [args.output, args.report])
    diagram, warnings = load_diagram(args.input, args.page, args.mode)
    data = diagram.as_dict()
    if args.strict_text:
        data["text_policy"] = "strict"
    if any(node.get("native_shape") == "image" for node in data["nodes"]):
        raise ValueError("themed render does not lay out embedded raster layers; use convert to preserve them")
    svg, report = _load_generator().build_svg_with_report(args.mode, data)
    if args.output.suffix.lower() in {".drawio", ".xml", ".excalidraw"}:
        fmt = format_for(args.output)
        quality = []
        output = convert_output(_positioned_export(svg, data, report), fmt, quality=quality)
        result = {"output": str(args.output), "format": fmt}
        if fmt == "excalidraw":
            result["layout_warnings"] = quality
            report["native_layout_warnings"] = quality
    else:
        output = svg
        result = {"svg": str(args.output)}
    _write_text(args.output, output)
    if args.report:
        _write_text(args.report, json_text(report))
    print(json.dumps({"ok": True, **result, "report": str(args.report) if args.report else None,
                      "typography": report["typography"], "palette": report["palette"],
                      "import_warnings": warnings}, sort_keys=True))
    return 0


def command_convert(args: argparse.Namespace) -> int:
    _distinct_outputs([args.input], [args.output])
    if args.input.suffix.lower() in IMAGE_SUFFIXES:
        if args.page is not None or args.strict_import or args.require_quality:
            raise ValueError("raster references use pixel readback and do not accept graph conversion options")
        suffix = args.output.suffix.lower()
        content, result = image_output(args.input, suffix)
        _write_bytes(args.output, content)
        print(json.dumps({"ok": True, "output": str(args.output), "format": suffix[1:], **result}, ensure_ascii=False))
        return 0
    diagram, warnings = load_diagram(args.input, args.page)
    data = diagram.as_dict()
    if args.strict_import and warnings:
        print(json.dumps({"ok": False, "import_warnings": warnings}, ensure_ascii=False))
        return 1
    fmt = format_for(args.output)
    if args.require_quality and fmt != "excalidraw":
        raise ValueError("--require-quality is available for editable Excalidraw exports")
    quality = []
    output = convert_output(data, fmt, quality=quality)
    if args.require_quality and quality:
        print(json.dumps({"ok": False, "layout_warnings": quality, "import_warnings": warnings}, ensure_ascii=False))
        return 1
    _write_text(args.output, output)
    print(json.dumps({"ok": True, "output": str(args.output), "format": fmt,
                      "nodes": len(data["nodes"]), "edges": len(data["arrows"]),
                      "import_warnings": warnings, "layout_warnings": quality if fmt == "excalidraw" else None}, ensure_ascii=False))
    return 0


def command_update(args: argparse.Namespace) -> int:
    _distinct_outputs([args.before, args.candidate], [args.output, args.report, args.markdown, args.svg])
    if format_for(args.output) != "json" or any(format_for(p) != "json" for p in (args.before, args.candidate)):
        raise ValueError("update requires Fireworks diagram JSON inputs and output")
    before = read_diagram_json(args.before)
    candidate = read_diagram_json(args.candidate)
    graph, report = review_update(before, candidate, GitDiff(args.repo, args.base_ref, args.head_ref),
                                  accept_layout=args.accept_layout)
    if args.report:
        _write_text(args.report, json_text(report))
    if args.markdown:
        _write_text(args.markdown, markdown_report(report, args.github_repo))
    if args.require_evidence and report["needs_review"]:
        print(json.dumps({"ok": False, **report}, ensure_ascii=False))
        return 1
    svg, _ = _load_generator().build_svg_with_report(graph["mode"], graph)
    with tempfile.TemporaryDirectory(prefix="fireworks-update-") as directory:
        checked = Path(directory) / "review.svg"
        checked.write_text(svg, encoding="utf-8")
        for name in ("xml", "markers", "collisions", "geometry", "composition"):
            ok, details = run_check(checked, name)
            if not ok:
                raise ValueError(f"updated diagram failed {name}: {details}")
    _write_text(args.output, json_text(graph))
    if args.svg:
        _write_text(args.svg, svg)
    print(json.dumps({"ok": True, "output": str(args.output), **report}, ensure_ascii=False))
    return 0


def command_check(args: argparse.Namespace) -> int:
    checks = args.check or ["xml", "markers", "collisions", "geometry", "composition"]
    results: dict[str, object] = {}
    ok = True
    for check in checks:
        passed, details = run_check(args.svg, check)
        results[check] = {"ok": passed, "details": details}
        ok = ok and passed
    print(json.dumps({"ok": ok, "checks": results}, indent=2, sort_keys=True))
    return 0 if ok else 1


def command_inspect(args: argparse.Namespace) -> int:
    root = ET.parse(args.svg).getroot()
    roles: dict[str, int] = {}
    for element in root.iter():
        role = element.get("data-graph-role")
        if role:
            roles[role] = roles.get(role, 0) + 1
    print(json.dumps({
        "generator": root.get("data-generator"),
        "schema_version": root.get("data-schema-version"),
        "style_id": root.get("data-style-id"),
        "visual_theme": root.get("data-visual-theme"),
        "diagram_type": root.get("data-diagram-type"),
        "semantic_profile": root.get("data-semantic-profile"),
        "semantic_valid": root.get("data-semantic-valid"),
        "viewBox": root.get("viewBox"),
        "roles": roles,
    }, indent=2, sort_keys=True))
    return 0


def command_export_html(args: argparse.Namespace) -> int:
    output = build_interactive_html(
        args.svg.read_text(encoding="utf-8"),
        args.title or args.svg.stem,
        {"slug": args.slug or args.svg.stem},
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    print(json.dumps({"ok": True, "html": str(args.output)}, sort_keys=True))
    return 0


def command_animate(args: argparse.Namespace) -> int:
    report = args.report or args.output.with_suffix(".motion.json")
    result = render_motion_gif(
        args.input,
        args.output,
        report_path=report,
        preset=args.preset,
        duration=args.duration,
        fps=args.fps,
        width=args.width,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_examples(_: argparse.Namespace) -> int:
    examples = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "fixtures").glob("*")) if path.suffix in {".json", ".svg"}]
    print(json.dumps({"examples": examples}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor").set_defaults(func=command_doctor)
    subparsers.add_parser("version", help="report installed version, source and style catalog").set_defaults(func=command_version)
    png = subparsers.add_parser("export-png", help="export PNG with bounded dimensions and pixel-size readback")
    png.add_argument("svg", type=Path)
    png.add_argument("output", type=Path)
    png.add_argument("--width", type=int, default=1920)
    png.set_defaults(func=command_export_png)

    validate = subparsers.add_parser("validate", help="validate and normalize diagram JSON")
    validate.add_argument("mode")
    validate.add_argument("input", type=Path)
    validate.add_argument("--page", type=int, help="select a 1-based Draw.io page")
    validate.set_defaults(func=command_validate)

    render = subparsers.add_parser("render", help="render diagram JSON to SVG")
    render.add_argument("mode")
    render.add_argument("input", type=Path)
    render.add_argument("output", type=Path)
    render.add_argument("--report", type=Path)
    render.add_argument("--strict-text", action="store_true", help="refuse output if Fireworks would omit or truncate text")
    render.add_argument("--page", type=int, help="select a 1-based Draw.io page")
    render.set_defaults(func=command_render)

    convert = subparsers.add_parser("convert", help="convert editable diagrams or embed a raster reference")
    convert.add_argument("input", type=Path)
    convert.add_argument("output", type=Path)
    convert.add_argument("--page", type=int, help="select a 1-based Draw.io page")
    convert.add_argument("--strict-import", action="store_true", help="fail if an input element is skipped or simplified")
    convert.add_argument("--require-quality", action="store_true", help="refuse Excalidraw export if label layout checks warn")
    convert.set_defaults(func=command_convert)

    update = subparsers.add_parser("update", help="review a proposed graph against a previous graph and a Git diff")
    update.add_argument("before", type=Path)
    update.add_argument("candidate", type=Path)
    update.add_argument("output", type=Path)
    update.add_argument("--repo", type=Path, default=Path.cwd())
    update.add_argument("--base-ref", required=True)
    update.add_argument("--head-ref", required=True)
    update.add_argument("--report", type=Path)
    update.add_argument("--markdown", type=Path, help="write a PR-ready change summary")
    update.add_argument("--github-repo", help="link source ranges to a GitHub owner/repository in Markdown")
    update.add_argument("--svg", type=Path, help="write a validated SVG preview")
    update.add_argument("--accept-layout", action="store_true", help="accept proposed positions and routes")
    update.add_argument("--require-evidence", action="store_true", help="refuse output if source evidence is missing")
    update.set_defaults(func=command_update)

    check = subparsers.add_parser("check", help="check an SVG artifact")
    check.add_argument("svg", type=Path)
    check.add_argument("--check", action="append", choices=("xml", "markers", "collisions", "geometry", "composition"))
    check.set_defaults(func=command_check)

    inspect = subparsers.add_parser("inspect", help="print semantic SVG metadata")
    inspect.add_argument("svg", type=Path)
    inspect.set_defaults(func=command_inspect)

    export = subparsers.add_parser("export-html", help="create an offline interactive HTML viewer")
    export.add_argument("svg", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("--title")
    export.add_argument("--slug")
    export.set_defaults(func=command_export_html)

    animate = subparsers.add_parser(
        "animate",
        help="create a validated animated GIF from a generated semantic SVG",
    )
    animate.add_argument("input", type=Path)
    animate.add_argument("output", type=Path)
    animate.add_argument("--preset", choices=("auto", *MOTION_PRESETS), default="auto")
    animate.add_argument("--duration", type=float, default=DEFAULT_MOTION_DURATION)
    animate.add_argument("--fps", type=int, default=20)
    animate.add_argument("--width", type=int, default=960)
    animate.add_argument("--report", type=Path)
    animate.add_argument("--dry-run", action="store_true")
    animate.set_defaults(func=command_animate)

    subparsers.add_parser("examples").set_defaults(func=command_examples)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, ET.ParseError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
