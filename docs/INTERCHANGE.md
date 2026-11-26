# Diagram interchange, image references, and Git-backed review

The CLI determines the interchange format from `.drawio` (or `.xml`), `.excalidraw`, or Fireworks `.json`. It runs locally and uses no editor service or model API. Start from an existing editable file, or ask the Fireworks Skill to produce a Fireworks JSON graph from a description.

## Generate or convert editable files

```sh
python3 scripts/fireworks.py render architecture fixtures/c4-review-canvas-style9.json checkout.drawio
python3 scripts/fireworks.py render architecture fixtures/c4-review-canvas-style9.json checkout.excalidraw

python3 scripts/fireworks.py convert existing.drawio diagram.json --page 2 --strict-import
python3 scripts/fireworks.py convert existing.drawio diagram.excalidraw --page 2 --require-quality
python3 scripts/fireworks.py convert existing.excalidraw diagram.drawio
python3 scripts/fireworks.py render architecture existing.excalidraw diagram.svg --strict-text
python3 scripts/fireworks.py check diagram.svg
```

- `render` runs Fireworks layout and reports geometry and typography results. Native output uses the coordinates and routed points produced by the renderer. `convert` maps the graph without running layout, so its output can include editor layouts that Fireworks will ask you to adjust when rendering SVG.
- Draw.io supports plain or compressed pages, including `mxGraphModel` XML. A multi-page file requires `--page N`; the selected page becomes one output document. Excalidraw accepts version 2 scene JSON.
- The interoperable subset covers stable IDs, labels, rectangle/ellipse/diamond shapes, frames or rectangular boundaries, basic shape colors, connectors and explicit bends. For Draw.io imports, the converter resolves object placeholders (such as C4 fields), keeps multiline labels and external label placement, uses explicit entry/exit ports, and approximates automatic orthogonal routing where the file omits bend coordinates. Moving a bound shape in the native editor keeps its connector attached. Import may translate the entire scene to leave room for the Fireworks canvas margin and title.
- The Fireworks JSON remains the source for domain semantics, source references, and theme or motion choices. Native exports carry the editable graph, including both connector endpoint markers and supported bound-label typography. Embedded PNG, JPEG, and WebP round-trip between Draw.io and Excalidraw as movable image layers, with Pillow installed. Remote images are never fetched. Image crops, flips, freehand, rotated shapes, arbitrary style libraries, and groups may be simplified or omitted. Unsupported elements may be skipped or simplified. Review `import_warnings`; `convert --strict-import` refuses output when it reports any warnings. The SVG renderer applies the Fireworks theme.
- Excalidraw output checks estimated text fit and label placement. For external labels, the check also considers connector segments and shape bounds. `--require-quality` refuses to write Excalidraw output if these checks warn. Inspect the resulting scene in the target editor to confirm typography and wiring. Use `--strict-text` on `render` to reject truncated or suppressed text. Native import warnings, estimated Excalidraw typography, and Fireworks SVG checks cover different failure cases.
- Editor-specific stencils such as AWS resource icons may become editable basic shapes. The importer reports them as simplifications. The project does not invoke an editor service to reconstruct proprietary assets.

## Use a screenshot or image as input

```sh
python3 -m pip install Pillow
python3 scripts/fireworks.py convert screenshot.png reference.drawio
python3 scripts/fireworks.py convert screenshot.jpg reference.excalidraw
python3 scripts/fireworks.py convert screenshot.webp reference.svg
python3 scripts/fireworks.py convert screenshot.webp reference.png
```

The raster path decodes a single PNG, JPEG or WebP, applies EXIF orientation and verifies an exact RGBA pixel readback before writing. SVG and both editors receive a self-contained PNG at the original oriented dimensions. The editor files expose one movable image layer. No online service is needed. Raster inputs are limited to 12 MiB and 24 megapixels. Native diagram files have a separate 8 MiB limit including embedded images. Use `convert` to preserve embedded layers between native editors. The themed SVG layout path reports that it cannot lay out these layers.

For editable components and connectors from an image, use the image as a reference and have the Fireworks Skill draft a structured JSON graph from it. Review ambiguous labels, arrowheads, and crossovers before accepting the draft. Run `validate`, then `render` into Draw.io, Excalidraw, or SVG. Run `check` against the SVG and inspect a render from the target editor. The CLI does not infer graph semantics from pixels or extract an embedded editable document.

## Update a diagram alongside a PR

Keep the baseline Fireworks JSON in version control. The agent or reviewer reads the actual Git diff and writes a **candidate** JSON with the same stable IDs. New and changed elements carry an optional `source_refs` array. Each entry has a full commit SHA, a repository-relative file path, and a 1-based line or line range inside that revision.

For an added or changed relationship in the candidate:

```json
{
  "id": "gateway-billing",
  "source": "gateway",
  "target": "billing",
  "label": "Reserve usage via v2",
  "source_refs": [{
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "path": "internal/gateway/client.go",
    "line": 83,
    "end_line": 86
  }]
}
```

Use the pinned **head** commit for new or modified elements. For a removed element, put the corresponding references under the candidate's top-level `removal_refs`, keyed by removed ID, with a range on the **base** commit. Source paths and lines must exist and overlap a changed hunk on the corresponding side. A location in a diff is evidence for a reviewer to inspect. It does not establish the semantic meaning of the change.

```sh
BASE=$(git merge-base origin/main HEAD)
HEAD_REV=$(git rev-parse HEAD)
git show "${BASE}:docs/architecture.json" > before.json
python3 scripts/fireworks.py update before.json candidate.json updated.json \
  --repo . --base-ref "$BASE" --head-ref "$HEAD_REV" \
  --require-evidence --report review.json --markdown review.md \
  --github-repo example-org/example-repo --svg review.svg
```

The command keeps existing positions and routes when possible, and recomputes changed connections through the Fireworks renderer. Pass `--accept-layout` for a deliberate change to positions and routing. The JSON and Markdown reports list added, removed and changed objects, existing positions retained, and source locations. `--github-repo` adds commit-pinned links to the Markdown report. `--require-evidence` returns a nonzero exit and leaves the updated graph and SVG unwritten if evidence is missing or stale. Check failures also leave the graph and SVG unwritten.

In GitHub Actions, check out the commits with sufficient history, run the command with strict evidence enabled, and append `review.md` to `$GITHUB_STEP_SUMMARY`. Publish `review.svg` as a CI artifact if the repository already has an artifact upload step. The script does not publish or merge the PR.
