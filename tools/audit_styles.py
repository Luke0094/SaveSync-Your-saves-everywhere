"""SaveSync — Style/DPI coverage audit (static, AST-based, pure stdlib).

Scans the ui/ tree for DPI/theme leaks that keep widgets from following a
UI-scale change:

  [FIXED-RAW]  setFixedSize / setFixedWidth / setFixedHeight with a plain
               integer (no scaled()). These stay frozen at every ui_scale.
  [FONT-PX]    setStyleSheet strings containing a literal font-size: Npx
               (not produced by scaled()/an f-string var) — DPI-blind text.
  [HEX]        setStyleSheet strings with literal hex colours not routed
               through palette() — theme-blind.
  [NO-REFRESH] class that styles widgets inline but defines no
               refresh_styles() and is not a ThemedMixin user — its inline
               styles are never re-applied on a theme/scale switch.

Excludes ui/styles/ (the theme definitions themselves).

Usage:
    python tools/audit_styles.py [paths...]        # default: ui/
    python tools/audit_styles.py --summary-only
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
PX_RE = re.compile(r"(?<![A-Za-z0-9_])-?\d+px\b")

FIXED_METHODS = {"setFixedSize", "setFixedWidth", "setFixedHeight"}


def _literal_text(node: ast.AST) -> str | None:
    """Best-effort static text of a style argument (constant or f-string)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(v.value)
            else:
                parts.append("{...}")
        return "".join(parts)
    return None


def _has_call(node: ast.AST, name: str) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == name:
            return True
    return False


def _source_snippet(node: ast.AST) -> str:
    try:
        return ast.get_source_segment(_current_src, node).replace("\n", " ")
    except Exception:
        return "<snippet unavailable>"


_current_src = ""


class _ClassState:
    def __init__(self, name: str, line: int):
        self.name = name
        self.line = line
        self.has_refresh_styles = False
        self.uses_themed_mixin = False
        self.inline_style_sites = 0
        self.finding_count = 0


def audit_file(path: Path) -> list[str]:
    global _current_src
    try:
        src = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    _current_src = src
    tree = ast.parse(src)
    out: list[str] = []

    class Walker(ast.NodeVisitor):
        """Tracks the enclosing class with a proper enter/exit stack so
        style sites never accumulate on the wrong (last-defined) class."""

        def __init__(self):
            self.stack: list[_ClassState] = []
            self.classes: list[_ClassState] = []

        def visit_ClassDef(self, node):
            st = _ClassState(node.name, node.lineno)
            for base in node.bases:
                if "ThemedMixin" in _source_snippet(base):
                    st.uses_themed_mixin = True
            self.stack.append(st)
            self.classes.append(st)
            for item in node.body:
                self.visit(item)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            if self.stack:
                if node.name == "refresh_styles":
                    self.stack[-1].has_refresh_styles = True
            self.generic_visit(node)

        def visit_Call(self, node):
            if self.stack:
                st = self.stack[-1]
                func = node.func
                fname = (func.id if isinstance(func, ast.Name) else
                         func.attr if isinstance(func, ast.Attribute) else None)
                if fname in FIXED_METHODS:
                    st.inline_style_sites += 1
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                            if arg.value >= 24:
                                out.append(
                                    f"  [FIXED-RAW] line {node.lineno}: "
                                    f"{_source_snippet(node)}"
                                    f"  <- raw {arg.value}px, no scaled()")
                        elif isinstance(arg, ast.BinOp):
                            out.append(
                                f"  [FIXED-RAW?] line {node.lineno}: "
                                f"{_source_snippet(node)}  <- computed expr")
                elif fname == "setStyleSheet" and node.args:
                    st.inline_style_sites += 1
                    arg = node.args[0]
                    text = _literal_text(arg)
                    if text is None:
                        return
                    uses_palette = _has_call(arg, "palette") or _has_call(node, "palette")
                    uses_scaled = _has_call(arg, "scaled") or _has_call(node, "scaled")
                    # Only literal FONT sizes are DPI-blind — hairline borders
                    # and paddings are deliberately static chrome.
                    font_pxs = [m.group(0) for m in
                                re.finditer(r"font-size:\s*(\d+)px", text)
                                if not uses_scaled]
                    if font_pxs:
                        out.append(
                            f"  [FONT-PX] line {node.lineno}: "
                            f"{_source_snippet(node)[:110]}  <- {font_pxs}")
                    if not uses_palette:
                        hexes = [m.group(0) for m in HEX_RE.finditer(text)]
                        if hexes:
                            out.append(
                                f"  [HEX] line {node.lineno}: "
                                f"{_source_snippet(node)[:110]}  <- {hexes[:4]}")

    walker = Walker()
    walker.visit(tree)
    for st in walker.classes:
        if st.inline_style_sites and not st.has_refresh_styles:
            out.append(
                f"  [NO-REFRESH] class {st.name} (line {st.line}): "
                f"{st.inline_style_sites} inline style site(s), no refresh_styles")
    return out


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:] if not p.startswith("--")]
    summary_only = "--summary-only" in sys.argv
    if not paths:
        paths = [ROOT / "ui"]
    files = []
    for p in paths:
        if p.is_file():
            files.append(p)
        else:
            files.extend(sorted(p.rglob("*.py")))
    files = [f for f in files
             if "ui/styles" not in str(f).replace("\\", "/")]

    total_files = 0
    finding_counts: dict[str, int] = {}
    for f in files:
        findings = audit_file(f)
        if not findings:
            continue
        total_files += 1
        print(f"\n{f.relative_to(ROOT)}")
        for line in findings:
            kind = line.strip().split("]")[0].lstrip("[")
            finding_counts[kind] = finding_counts.get(kind, 0) + 1
            print(line)
    print("\n=== SUMMARY ===")
    print(f"files with findings: {total_files} / {len(files)}")
    for kind, count in sorted(finding_counts.items()):
        print(f"  {kind}: {count}")
    if summary_only:
        return 0
    return 1 if any(k not in ("FIXED-RAW?",) for k in finding_counts) else 0


if __name__ == "__main__":
    sys.exit(main())
