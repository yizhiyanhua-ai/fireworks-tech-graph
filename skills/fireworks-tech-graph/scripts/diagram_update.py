"""Review a candidate graph against a previous diagram and a pinned Git diff."""
from __future__ import annotations

import copy
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import quote

from diagram_ir import normalize_diagram

_GROUPS = ("containers", "nodes", "arrows")
_BOX = {"x", "y", "width", "height", "auto_place", "offset_y"}
_ROUTE = {"route_points", "source_port", "target_port", "label_dx", "label_dy",
          "x1", "y1", "x2", "y2", "routing_padding", "port_clearance"}
_REF_FIELDS = {"commit", "path", "line", "end_line"}
_MAX_SOURCE = 2 * 1024 * 1024
_MAX_REFS = 12


class GitDiff:
    def __init__(self, repo: Path, base: str, head: str):
        self.repo = repo
        self.base = self.git("rev-parse", "--verify", "--end-of-options", base + "^{commit}").strip()
        self.head = self.git("rev-parse", "--verify", "--end-of-options", head + "^{commit}").strip()
        self.paths: dict[str, tuple[int, int, list, list]] = {}

    def git(self, *args) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError(f"git {args[0]} failed for the supplied repository or revision")
        return result.stdout.decode("utf-8", errors="replace")

    def lines(self, revision, path):
        spec = f"{revision}:{path}"
        try:
            size = int(self.git("cat-file", "-s", spec))
        except ValueError:
            return 0
        if size > _MAX_SOURCE:
            return 0
        text = self.git("show", spec)
        return 0 if "\0" in text else len(text.splitlines())

    def ranges(self, path):
        if path not in self.paths:
            diff = self.git("diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--unified=0",
                            self.base, self.head, "--", ":(literal)" + path)
            before, after = [], []
            for old, old_len, new, new_len in re.findall(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.M):
                for start, count, side in ((old, old_len, before), (new, new_len, after)):
                    length = int(count) if count else 1
                    if length:
                        side.append((int(start), int(start) + length - 1))
            self.paths[path] = self.lines(self.base, path), self.lines(self.head, path), before, after
        return self.paths[path]

    def evidence(self, raw, removed):
        if not isinstance(raw, list) or not raw or len(raw) > _MAX_REFS:
            return [], ["require 1–12 source references"]
        verified, problems = [], []
        for reference in raw:
            if not isinstance(reference, dict) or set(reference) - _REF_FIELDS:
                problems.append("malformed source reference")
                continue
            revision, path, start = (reference.get(k) for k in ("commit", "path", "line"))
            end = reference.get("end_line", start)
            if (not isinstance(path, str) or not path or len(path) > 512 or any(ord(c) < 32 for c in path)
                    or "\\" in path or path != PurePosixPath(path).as_posix() or path.startswith("/")
                    or any(part in (".", "..") for part in path.split("/"))
                    or type(start) is not int or type(end) is not int or not 1 <= start <= end <= 2_000_000
                    or end - start >= 120):
                problems.append("invalid repository path or line range")
                continue
            if revision != (self.base if removed else self.head):
                problems.append("reference must use the pinned base commit for removals or head commit for updates")
                continue
            before_count, after_count, before, after = self.ranges(path)
            if end > (before_count if removed else after_count):
                problems.append(f"source line range is outside {path}")
                continue
            if not any(start <= right and end >= left for left, right in (before if removed else after)):
                problems.append(f"source range does not overlap a changed hunk in {path}")
                continue
            verified.append(dict(commit=revision, path=path, line=start, end_line=end))
        return verified, problems


def _explicit(data):
    for key in _GROUPS:
        values = data.get("edges", []) if key == "arrows" and key not in data else data.get(key, [])
        if not isinstance(values, list) or len(values) > 1200:
            raise ValueError(f"{key} must be an array of at most 1200 elements")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip()
               for item in values):
            raise ValueError(f"{key} require explicit, stable string IDs")


def _semantic(item, layout):
    return {key: value for key, value in item.items() if key not in layout and key != "source_refs"}


def review_update(base, candidate, git: GitDiff, accept_layout=False):
    for graph in (base, candidate):
        if not isinstance(graph, dict):
            raise ValueError("a graph must be an object")
        _explicit(graph)
    mode = base.get("mode") or base.get("template_type") or "architecture"
    old = normalize_diagram(base, mode).as_dict()
    new = normalize_diagram(candidate, mode).as_dict()
    removals = new.pop("removal_refs", {})
    if not isinstance(removals, dict):
        raise ValueError("removal_refs must map removed IDs to source references")

    changes, preserved, layout_changes = [], [], []
    actual_removals, moved = set(), set()
    for group in _GROUPS:
        layout = _ROUTE if group == "arrows" else _BOX
        previous = {item["id"]: item for item in old.get(group, [])}
        proposed = {item["id"]: item for item in new.get(group, [])}
        surviving = [i for i in previous if i in proposed]
        added = [i for i in proposed if i not in previous]
        for identifier in surviving:
            before, after = previous[identifier], proposed[identifier]
            endpoints_changed = group == "arrows" and any(before.get(k) != after.get(k) for k in ("source", "target"))
            # A connector edited around the same endpoints can keep its waypoints.
            # A rewired connector uses candidate routing and must pass geometry again.
            if not accept_layout and not endpoints_changed:
                for key in layout:
                    if key in before:
                        after[key] = copy.deepcopy(before[key])
                    else:
                        after.pop(key, None)
                if group != "arrows" and any(key in before for key in _BOX):
                    preserved.append(identifier)
            geometry_changed = any(before.get(key) != after.get(key) for key in layout)
            if geometry_changed:
                layout_changes.append(dict(id=identifier, kind=group))
                if group != "arrows":
                    moved.add(identifier)
            if _semantic(before, layout) == _semantic(after, layout):
                if "source_refs" in before:
                    after["source_refs"] = copy.deepcopy(before["source_refs"])
                else:
                    after.pop("source_refs", None)
                continue
            changes.append((group, identifier, "modified", after.get("source_refs"), after))
        for identifier in added:
            after = proposed[identifier]
            changes.append((group, identifier, "added", after.get("source_refs"), after))
        for identifier in previous.keys() - proposed.keys():
            actual_removals.add(identifier)
            changes.append((group, identifier, "removed", removals.get(identifier), None))
        new[group] = [proposed[i] for i in surviving + added]
    if set(removals) - actual_removals:
        raise ValueError("removal_refs contains IDs that were not removed")

    records = []
    for kind, identifier, action, refs, entry in sorted(changes, key=lambda change: change[:3]):
        verified, problems = git.evidence(refs, action == "removed")
        if entry is not None:
            if verified:
                entry["source_refs"] = verified
            else:
                entry.pop("source_refs", None)
        records.append(dict(kind=kind, id=identifier, action=action, source_refs=verified, issues=problems,
                            evidence="needs_review" if problems else "located_in_diff"))
    output = normalize_diagram(new, mode).as_dict()
    meta = {k for k in set(old) | set(new) if k not in _GROUPS and k != "removal_refs" and old.get(k) != new.get(k)}
    return output, dict(base=git.base, head=git.head, changes=records, metadata_changed=sorted(meta),
                        preserved_positions=sorted(preserved), moved_shapes=sorted(moved),
                        layout_changes=layout_changes,
                        needs_review=sum(change["evidence"] == "needs_review" for change in records))


def markdown_report(report, github_repo=None):
    if github_repo is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", github_repo):
        raise ValueError("--github-repo must be a GitHub owner/repository")

    def citation(ref):
        text = f"`{inline(ref['path'])}:{ref['line']}–{ref['end_line']}`"
        if github_repo is None:
            return text
        path = quote(ref["path"], safe="/")
        url = f"https://github.com/{github_repo}/blob/{ref['commit']}/{path}#L{ref['line']}-L{ref['end_line']}"
        return f"[{text}]({url})"

    def inline(value):
        # Render references as text for PR review, without accepting Markdown from input.
        return str(value).replace("\n", " ").replace("\r", "").replace("`", "'")

    lines = ["## Architecture diagram changes", "",
             f"Base `{report['base']}` · Head `{report['head']}`", "",
             f"{len(report['preserved_positions'])} shapes kept their existing layout. "
             f"{report['needs_review']} topology or semantic changes need source review.", ""]
    if not report["changes"]:
        lines.append("No topology or semantic changes.")
    for change in report["changes"]:
        citations = ", ".join(citation(ref) for ref in change["source_refs"])
        summary = f"- {change['action']} `{inline(change['id'])}` ({change['kind']})"
        if citations:
            summary += f" · {citations}"
        if change["issues"]:
            summary += " · needs review"
        lines.append(summary)
    if report["metadata_changed"]:
        metadata = ", ".join(f"`{inline(key)}`" for key in report["metadata_changed"])
        lines.extend(["", f"Diagram-level settings changed: {metadata}."])
    return "\n".join(lines) + "\n"
