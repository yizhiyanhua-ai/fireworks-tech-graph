from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote
import xml.etree.ElementTree as ET
import zlib

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "fireworks.py"

MODEL = """<mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="area" value="控制面" vertex="1" parent="1" style="swimlane;">
  <mxGeometry x="40" y="125" width="560" height="240" as="geometry"/>
</mxCell>
<UserObject id="2" label="&lt;b&gt;网关&lt;/b&gt;">
  <mxCell vertex="1" parent="area" style="html=1;fillColor=#edf6ff;">
    <mxGeometry x="30" y="80" width="130" height="60" as="geometry"/>
  </mxCell>
</UserObject>
<mxCell id="3" value="计费" vertex="1" parent="area" style="ellipse;">
  <mxGeometry x="350" y="80" width="130" height="60" as="geometry"/>
</mxCell>
<mxCell id="bill" edge="1" source="2" target="3" parent="area" value="提交用量">
  <mxGeometry as="geometry" relative="1"/>
</mxCell>
</root></mxGraphModel>"""


class CLIFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def cli(self, *args, expect=0):
        run = subprocess.run([sys.executable, str(CLI), *map(str, args)], capture_output=True,
                             text=True, cwd=ROOT)
        self.assertEqual(run.returncode, expect, run.stdout + run.stderr)
        return run

    def write(self, name, data):
        file = self.folder / name
        file.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return file

    def read(self, name):
        return json.loads((self.folder / name).read_text())


class DiagramInterchange(CLIFixture):
    def test_drawio_compressed_pages_and_editable_excalidraw_round_trip(self):
        compressor = zlib.compressobj(wbits=-15)
        payload = base64.b64encode(compressor.compress(quote(MODEL).encode()) + compressor.flush()).decode()
        source = self.write("source.drawio", f'<mxfile><diagram>{MODEL}</diagram><diagram name="研发">{payload}</diagram></mxfile>')
        self.assertIn("--page", self.cli("convert", source, self.folder / "miss.json", expect=1).stderr)
        self.cli("convert", source, self.folder / "edit.excalidraw", "--page", 2, "--strict-import")
        scene = self.read("edit.excalidraw")
        by_id = {item["id"]: item for item in scene["elements"]}
        self.assertEqual(by_id["2"]["frameId"], "area")
        self.assertEqual(by_id["2"]["x"], 70)
        self.assertEqual(by_id["2"]["backgroundColor"], "#edf6ff")
        self.assertEqual(by_id["bill"]["startBinding"]["elementId"], "2")
        self.assertEqual(by_id["bill"]["endBinding"]["elementId"], "3")
        self.assertEqual(by_id["2-text"]["text"], "网关")
        self.cli("convert", self.folder / "edit.excalidraw", self.folder / "back.drawio", "--strict-import")
        self.cli("convert", self.folder / "back.drawio", self.folder / "final.json", "--strict-import")
        graph = self.read("final.json")
        self.assertEqual([(n["id"], n["label"], n["x"]) for n in graph["nodes"]], [("2", "网关", 70), ("3", "计费", 390)])
        self.assertEqual(graph["arrows"][0]["label"], "提交用量")

    def test_direct_render_from_and_to_editable_formats(self):
        native = self.write("inputs.drawio", MODEL)
        svg = self.folder / "result.svg"
        self.cli("render", "architecture", native, svg)
        self.cli("check", svg)
        model = ROOT / "fixtures" / "c4-review-canvas-style9.json"
        for suffix in ("drawio", "excalidraw"):
            name = self.folder / f"c4.{suffix}"
            self.cli("render", "architecture", model, name)
            self.cli("convert", name, self.folder / f"{suffix}.json")
            round_trip = self.read(f"{suffix}.json")
            self.assertEqual(len(round_trip["nodes"]), 6)
            self.assertEqual(len(round_trip["arrows"]), 5)
            self.assertEqual(round_trip["containers"][0]["id"], "checkout-boundary")

    def test_report_omissions_and_reject_unsafe_or_invalid_input(self):
        scene = {"type": "excalidraw", "version": 2, "elements": [
            {"id": "box", "type": "rectangle", "x": -20, "y": -10, "width": 120, "height": 50},
            {"id": "photo", "type": "image"},
        ]}
        file = self.write("image.excalidraw", scene)
        output = self.folder / "graph.json"
        warning = json.loads(self.cli("convert", file, output, "--strict-import", expect=1).stdout)
        self.assertIn("photo", warning["import_warnings"][0])
        self.assertFalse(output.exists())
        self.cli("convert", file, output)
        self.assertEqual(self.read("graph.json")["nodes"][0]["x"], 32)
        entity = self.write("bad.drawio", '<!DOCTYPE x [<!ENTITY x "input">]>' + MODEL)
        self.assertIn("DTD", self.cli("convert", entity, output, expect=1).stderr)
        invalid = self.write("compressed.drawio", "<mxfile><diagram>eHg=</diagram></mxfile>")
        self.assertIn("compressed", self.cli("convert", invalid, output, expect=1).stderr)
        self.cli("convert", file, file, expect=1)

    def test_native_labels_styles_ports_and_quality(self):
        model = """<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>
        <object id="c4" placeholders="1" c4Name="Gateway" c4Type="System"
                label="%c4Name%&lt;br&gt;[%c4Type%]">
          <mxCell parent="1" vertex="1" style="html=1;fillColor=#08427b;fontColor=#ffffff;">
            <mxGeometry x="160" y="125" width="110" height="60" as="geometry"/>
          </mxCell>
        </object>
        <mxCell id="store" parent="1" vertex="1" value="Store&#10;Service"
                style="fontSize=11;verticalLabelPosition=bottom;">
          <mxGeometry x="280" y="210" width="48" height="48" as="geometry"/>
        </mxCell>
        <mxCell id="e" parent="1" edge="1" source="c4" target="store" value="Read"
                 style="edgeStyle=orthogonalEdgeStyle;exitX=0.5;exitY=1;entryX=0.25;entryY=0;dashed=1;strokeColor=#0073bb;">
          <mxGeometry relative="1" as="geometry"/>
        </mxCell></root></mxGraphModel>"""
        source = self.write("labels.drawio", model)
        target = self.folder / "labels.excalidraw"
        result = json.loads(self.cli("convert", source, target, "--strict-import", "--require-quality").stdout)
        self.assertEqual(result["layout_warnings"], [])
        canvas = {element["id"]: element for element in self.read(target.name)["elements"]}
        self.assertEqual(canvas["c4-text"]["text"], "Gateway\n[System]")
        self.assertEqual(canvas["c4-text"]["strokeColor"], "#ffffff")
        self.assertEqual(canvas["store-text"]["text"], "Store\nService")
        self.assertGreater(canvas["store-text"]["y"], canvas["store"]["y"] + canvas["store"]["height"])
        self.assertEqual(canvas["store-text"]["customData"]["fireworksLabelFor"], "store")
        line = canvas["e"]
        self.assertEqual((line["x"], line["y"]), (215, 185))
        self.assertEqual(line["points"], [[0, 0], [0, 12.5], [77, 12.5], [77, 25]])
        self.assertEqual((line["strokeColor"], line["strokeStyle"]), ("#0073bb", "dashed"))
        self.assertEqual(line["endBinding"]["fixedPoint"], [.25, 0])
        self.cli("convert", target, self.folder / "back.json", "--strict-import")
        graph = self.read("back.json")
        self.assertEqual([node["label"] for node in graph["nodes"]], ["Gateway\n[System]", "Store\nService"])
        self.assertEqual(graph["arrows"][0]["label"], "Read")
        self.assertEqual(graph["nodes"][0]["native_style"]["font_color"], "#ffffff")
        self.assertEqual(graph["nodes"][0]["native_style"]["font_size"], canvas["c4-text"]["fontSize"])
        self.assertEqual(graph["nodes"][1]["native_style"]["position"], "bottom")

    def test_endpoint_markers_preserve_four_directions(self):
        model = ET.fromstring(MODEL)
        root = model.find("root")
        edge = root.find("mxCell[@id='bill']")
        root.remove(edge)
        cases = [
            ("reverse", "block", "none", "triangle", None),
            ("forward", "none", "open", None, "arrow"),
            ("both", "diamond", "oval", "diamond", "circle"),
            ("bare", "none", "none", None, None),
        ]
        for index, (identifier, start, end, _, _) in enumerate(cases):
            item = ET.SubElement(root, "mxCell", id=identifier, parent="1", edge="1",
                                 style=f"startArrow={start};endArrow={end};")
            bounds = ET.SubElement(item, "mxGeometry", {"as": "geometry", "relative": "1"})
            for side, x in (("source", 60), ("target", 540)):
                ET.SubElement(bounds, "mxPoint", {"as": side + "Point", "x": str(x), "y": str(410 + 40 * index)})
        source = self.write("markers.drawio", ET.tostring(model, encoding="unicode"))
        target = self.folder / "markers.excalidraw"
        self.cli("convert", source, target, "--strict-import")
        restored = {e["id"]: e for e in self.read(target.name)["elements"]}
        self.cli("convert", target, self.folder / "markers-back.drawio", "--strict-import")
        back = ET.parse(self.folder / "markers-back.drawio")
        svg = self.folder / "markers.svg"
        self.cli("render", "architecture", source, svg)
        self.cli("check", svg)
        svg_edges = {e.get("data-edge-id"): e for e in ET.parse(svg).iter() if e.get("data-graph-role") == "edge"}
        for identifier, start, end, start_head, end_head in cases:
            self.assertEqual((restored[identifier]["startArrowhead"], restored[identifier]["endArrowhead"]),
                             (start_head, end_head))
            style = back.find(f".//mxCell[@id='{identifier}']").get("style")
            self.assertIn(f"startArrow={start};", style)
            self.assertIn(f"endArrow={end};", style)
            arrow = svg_edges[identifier]
            self.assertEqual(bool(arrow.get("marker-start")), start_head is not None)
            self.assertEqual(bool(arrow.get("marker-end")), end_head is not None)

    def test_standalone_text_keeps_color_without_gaining_a_drawio_border(self):
        source = self.write("text.excalidraw", {"type": "excalidraw", "version": 2, "elements": [
            {"id": "note", "type": "text", "x": 40, "y": 140, "width": 130, "height": 24,
             "text": "Read me", "fontSize": 13, "strokeColor": "#243b53", "textAlign": "left"},
        ]})
        target = self.folder / "text.drawio"
        self.cli("convert", source, target, "--strict-import")
        style = ET.parse(target).find(".//mxCell[@id='note']").get("style")
        self.assertIn("strokeColor=none;", style)
        self.assertNotIn("strokeColor=#243b53;", style)
        self.assertIn("fontColor=#243b53;", style)
        self.assertIn("fontSize=13.0;", style)
        self.assertIn("align=left;", style)

    def test_quality_refuses_to_overwrite_on_unfit_label(self):
        source = self.write("tiny.drawio", MODEL.replace('width="130" height="60"', 'width="15" height="10"')
                            .replace("网关", "UnbreakableLabelThatCannotFit"))
        output = self.write("kept.excalidraw", "existing contents")
        result = self.cli("convert", source, output, "--require-quality", expect=1)
        self.assertIn("label may exceed", " ".join(json.loads(result.stdout)["layout_warnings"]))
        self.assertEqual(output.read_text(), "existing contents")

    def test_renderer_strict_text_refuses_truncated_native_input(self):
        source = self.write("long.drawio", MODEL.replace("网关", "Very long gateway title " * 14))
        output = self.folder / "strict.svg"
        report = self.folder / "report.json"
        result = self.cli("render", "architecture", source, output, "--strict-text", "--report", report, expect=1)
        self.assertIn("TEXT_FIT", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is optional")
    def test_raster_reference_exact_pixel_readback_and_invalid_source(self):
        from PIL import Image

        source = self.folder / "sample.png"
        pixels = Image.new("RGBA", (2, 2))
        pixels.putpixel((0, 0), (150, 20, 250, 105))
        pixels.putpixel((1, 1), (0, 70, 30, 255))
        pixels.save(source)
        for suffix in ("png", "svg", "drawio", "excalidraw"):
            with self.subTest(suffix=suffix):
                output = self.folder / f"reference.{suffix}"
                result = json.loads(self.cli("convert", source, output).stdout)
                self.assertFalse(result["editable_pixels"])
                self.assertEqual(result["pixel_readback"], "exact_rgba")
                if suffix == "excalidraw":
                    scene = self.read(output.name)
                    file = scene["files"][scene["elements"][0]["fileId"]]
                    encoded = file["dataURL"].split(",", 1)[1]
                elif suffix == "drawio":
                    cell = ET.parse(output).find(".//mxCell[@id='raster-reference']")
                    encoded = cell.attrib["style"].split("image=data:image/png,", 1)[1].split(";", 1)[0]
                elif suffix == "svg":
                    node = ET.parse(output).find("{http://www.w3.org/2000/svg}image")
                    encoded = node.attrib["href"].split(",", 1)[1]
                png = output.read_bytes() if suffix == "png" else base64.b64decode(encoded, validate=True)
                with Image.open(io.BytesIO(png)) as image:
                    self.assertEqual(image.tobytes(), pixels.tobytes())
                self.assertEqual(hashlib.sha256(png).hexdigest(), result["embedded_png_sha256"])

        wrong_suffix = self.folder / "wrong.jpg"
        wrong_suffix.write_bytes(source.read_bytes())
        output = self.folder / "rejected.svg"
        self.assertIn("does not match", self.cli("convert", wrong_suffix, output, expect=1).stderr)
        self.assertFalse(output.exists())

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is optional")
    def test_embedded_image_survives_image_only_and_mixed_round_trips(self):
        from PIL import Image

        png = self.folder / "origin.png"
        Image.new("RGBA", (36, 24), (20, 180, 90, 105)).save(png)
        drawio = self.folder / "image.drawio"
        self.cli("convert", png, drawio)
        excal = self.folder / "image.excalidraw"
        self.cli("convert", drawio, excal, "--strict-import")
        scene = self.read(excal.name)
        image = scene["elements"][0]
        original_uri = scene["files"][image["fileId"]]["dataURL"]
        self.assertEqual(image["type"], "image")

        pure = self.folder / "pure.drawio"
        self.cli("convert", excal, pure, "--strict-import")
        self.cli("convert", pure, self.folder / "pure.json", "--strict-import")
        self.assertEqual(self.read("pure.json")["nodes"][0]["native_image"], original_uri)

        model = ET.parse(drawio)
        graph = model.find(".//root")
        vertex = ET.SubElement(graph, "mxCell", id="neighbor", value="Editable", vertex="1", parent="1")
        ET.SubElement(vertex, "mxGeometry", {"as": "geometry", "x": "160", "y": "120", "width": "120", "height": "55"})
        model.write(self.folder / "mixed.drawio", encoding="unicode")
        self.cli("convert", self.folder / "mixed.drawio", self.folder / "mixed.excalidraw", "--strict-import")
        self.cli("convert", self.folder / "mixed.excalidraw", self.folder / "mixed-back.drawio", "--strict-import")
        self.cli("convert", self.folder / "mixed-back.drawio", self.folder / "mixed-back.json", "--strict-import")
        nodes = {item["id"]: item for item in self.read("mixed-back.json")["nodes"]}
        self.assertEqual(nodes["raster-reference"]["native_image"], original_uri)
        self.assertEqual(nodes["neighbor"]["label"], "Editable")
        self.assertEqual((nodes["raster-reference"]["width"], nodes["raster-reference"]["height"]), (36, 24))

        scene["files"][image["fileId"]]["dataURL"] = "https://example.invalid/no-fetch.png"
        scene["elements"].append({"id": "box", "type": "rectangle", "x": 100, "y": 200, "width": 100, "height": 50})
        self.write("external.excalidraw", scene)
        rejected = self.folder / "external.drawio"
        check = self.cli("convert", self.folder / "external.excalidraw", rejected, "--strict-import", expect=1)
        self.assertIn("skipped image", check.stdout)
        self.assertFalse(rejected.exists())


class DiagramUpdate(CLIFixture):
    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.folder), *args], text=True).strip()

    def history(self):
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Fireworks test")
        self.write("gateway.go", "package sample\n\nfunc reserve() { oldBilling() }\n")
        self.write("unaltered.go", "package sample\n\nfunc untouched() {}\n")
        self.git("add", "gateway.go", "unaltered.go")
        self.git("commit", "-qm", "base")
        base = self.git("rev-parse", "HEAD")
        self.write("gateway.go", "package sample\n\nfunc reserve() { newBilling() }\n")
        self.git("commit", "-qam", "new billing endpoint")
        return base, self.git("rev-parse", "HEAD")

    def graph(self):
        return dict(schema_version=1, mode="architecture", style=1, width=640, height=370, title="Billing",
                    nodes=[dict(id="gateway", x=50, y=170, width=140, height=75, label="Gateway"),
                           dict(id="billing", x=420, y=170, width=140, height=75, label="Billing")],
                    arrows=[dict(id="reserve", source="gateway", target="billing", label="Reserve")])

    def update(self, before, after, refs, *extra, expect=0):
        return self.cli("update", before, after, self.folder / "merged.json", "--repo", self.folder,
                        "--base-ref", refs[0], "--head-ref", refs[1], "--report", self.folder / "report.json",
                        *extra, expect=expect)

    def test_verified_diff_preserves_layout_and_links_source(self):
        refs = self.history()
        before = self.write("before.json", self.graph())
        candidate = self.graph()
        candidate["nodes"][0]["x"] = 500
        candidate["nodes"].reverse()
        candidate["arrows"][0].update(label="Reserve using v2",
                                      source_refs=[dict(commit=refs[1], path="gateway.go", line=3)])
        after = self.write("candidate.json", candidate)
        self.update(before, after, refs, "--require-evidence", "--svg", self.folder / "preview.svg",
                    "--markdown", self.folder / "pr.md", "--github-repo", "demo/project")
        merged, report = self.read("merged.json"), self.read("report.json")
        self.assertEqual(merged["nodes"], self.graph()["nodes"])
        self.assertEqual(report["preserved_positions"], ["billing", "gateway"])
        self.assertEqual(len(report["changes"]), 1)
        self.assertEqual(report["changes"][0]["evidence"], "located_in_diff")
        self.assertIn(f"https://github.com/demo/project/blob/{refs[1]}/gateway.go#L3-L3", (self.folder / "pr.md").read_text())

    def test_missing_or_stale_evidence_is_not_published_in_strict_mode(self):
        refs = self.history()
        before = self.write("before.json", self.graph())
        candidate = self.graph()
        for reference in (dict(commit=refs[1], path="unaltered.go", line=3),
                          dict(commit=refs[0], path="gateway.go", line=3),
                          dict(commit=refs[1], path="../gateway.go", line=3)):
            with self.subTest(reference=reference):
                candidate["arrows"][0].update(label="Changed", source_refs=[reference])
                after = self.write("candidate.json", candidate)
                self.update(before, after, refs, "--require-evidence", expect=1)
                self.assertFalse((self.folder / "merged.json").exists())
                self.assertEqual(self.read("report.json")["needs_review"], 1)

    def test_deletion_has_explicit_old_side_evidence(self):
        refs = self.history()
        before = self.write("before.json", self.graph())
        candidate = self.graph()
        candidate["arrows"] = []
        candidate["removal_refs"] = {"reserve": [dict(commit=refs[0], path="gateway.go", line=3)]}
        after = self.write("candidate.json", candidate)
        self.update(before, after, refs, "--require-evidence")
        self.assertEqual(self.read("report.json")["changes"][0]["action"], "removed")
        self.assertNotIn("removal_refs", self.read("merged.json"))
