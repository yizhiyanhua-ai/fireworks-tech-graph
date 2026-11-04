"""Failures must remain actionable before any SVG has been written."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("diagnostic_generator", ROOT / "scripts/generate-from-template.py")
assert SPEC and SPEC.loader
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)
from render_diagnostics import RenderError


class RenderDiagnosticsTest(unittest.TestCase):
    def blocked_diagram(self) -> dict:
        return {
            "style": 6, "width": 600, "height": 300,
            "nodes": [
                {"id": "client", "x": 30, "y": 120, "width": 100, "height": 60, "label": "Client"},
                {"id": "wall", "x": 250, "y": 0, "width": 100, "height": 300, "label": "Wall"},
                {"id": "service", "x": 470, "y": 120, "width": 100, "height": 60, "label": "Service"},
            ],
            "arrows": [{"id": "request", "source": "client", "target": "service"}],
        }

    def test_no_route_names_edge_endpoints_and_rejected_candidates(self) -> None:
        with self.assertRaises(RenderError) as caught:
            generator.build_svg_with_report("architecture", self.blocked_diagram())
        detail = caught.exception.diagnostic
        self.assertEqual(detail["edge_id"], "request")
        self.assertEqual(detail["source"], "client")
        self.assertEqual(detail["target"], "service")
        self.assertEqual(detail["source_port"], "right")
        self.assertGreater(detail["rejected_candidates"]["obstacle_collision"], 0)
        self.assertEqual(sum(detail["rejected_candidates"].values()), detail["candidate_count"])
        self.assertIn("wall", [item.get("owner") for item in detail["blocking_obstacles"]])

    def test_container_header_blocker_keeps_owner_and_geometry(self) -> None:
        data = self.blocked_diagram()
        data["nodes"].pop(1)
        container = {"id": "access", "x": 180, "y": 40, "width": 240, "height": 220,
                     "label": "接入层 ACCESS", "header_prefix": "01"}
        data["containers"] = [container]
        bounds = generator.container_header_bounds(container, generator.parse_style(6)[1])
        data["arrows"][0]["route_points"] = [[(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2]]
        with self.assertRaises(RenderError) as caught:
            generator.build_svg_with_report("architecture", data)
        self.assertEqual(caught.exception.diagnostic["code"], "WAYPOINT_OBSTACLE")
        blocker = caught.exception.diagnostic["blocking_obstacles"][0]
        self.assertEqual(blocker["kind"], "container_header")
        self.assertEqual(blocker["owner"], "access")
        self.assertIn("01", blocker["header_text"])
        self.assertEqual(blocker["bounds"], list(bounds))

    def test_cli_writes_failure_report_without_overwriting_svg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, svg, report = root / "input.json", root / "output.svg", root / "failure.json"
            source.write_text(json.dumps(self.blocked_diagram()), encoding="utf-8")
            svg.write_text("keep-existing", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/fireworks.py"), "render", "architecture",
                 str(source), str(svg), "--report", str(report)], capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stderr)["diagnostic"], json.loads(report.read_text())["diagnostic"])
            self.assertEqual(svg.read_text(), "keep-existing")

    def test_failure_report_cannot_alias_svg_or_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, svg = root / "input.json", root / "output.svg"
            source.write_text(json.dumps(self.blocked_diagram()), encoding="utf-8")
            original = source.read_bytes()
            svg.write_text("keep-existing", encoding="utf-8")
            for report in (source, svg):
                with self.subTest(report=report.name):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "scripts/fireworks.py"), "render", "architecture",
                         str(source), str(svg), "--report", str(report)], capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("layout report must differ", result.stderr)
                    self.assertEqual(source.read_bytes(), original)
                    self.assertEqual(svg.read_text(), "keep-existing")


if __name__ == "__main__":
    unittest.main()
