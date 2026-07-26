import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _application_text_files() -> list[Path]:
    files = set(ROOT.glob("*.py"))
    for pattern in (
        "*.json", "*.yml", "*.yaml", "*.md", "*.sql", "Dockerfile",
    ):
        files.update(ROOT.glob(pattern))
        files.update((ROOT / "migrations").rglob(pattern))
    return sorted(path for path in files if path.is_file())


def _literal_mode(call: ast.Call) -> str | None:
    mode_node = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
            break
    if mode_node is None:
        return "r"
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return mode_node.value
    return None


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(keyword.arg == name for keyword in call.keywords)


class _TextIoVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        is_builtin_open = isinstance(function, ast.Name) and function.id == "open"
        if is_builtin_open:
            mode = _literal_mode(node)
            if (mode is None or "b" not in mode) and not _has_keyword(
                    node, "encoding"):
                self.violations.append((node.lineno, "text open without encoding"))

        if (
            isinstance(function, ast.Attribute)
            and function.attr in {"read_text", "write_text"}
            and not _has_keyword(node, "encoding")
        ):
            self.violations.append(
                (node.lineno, f"{function.attr} without encoding"))

        if (
            isinstance(function, ast.Attribute)
            and function.attr in {"encode", "decode"}
            and not node.args
            and not _has_keyword(node, "encoding")
        ):
            self.violations.append(
                (node.lineno, f"{function.attr} without encoding"))
        self.generic_visit(node)


class EncodingBoundaryTests(unittest.TestCase):
    def test_application_owned_text_files_are_valid_utf8(self):
        invalid = []
        for path in _application_text_files():
            try:
                path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                invalid.append(f"{path.relative_to(ROOT)}: {exc}")
        self.assertEqual(invalid, [])

    def test_production_python_text_io_has_an_explicit_encoding(self):
        violations = []
        for path in _application_text_files():
            if path.suffix != ".py":
                continue
            visitor = _TextIoVisitor()
            visitor.visit(ast.parse(path.read_bytes(), filename=str(path)))
            violations.extend(
                f"{path.relative_to(ROOT)}:{line}: {message}"
                for line, message in visitor.violations
            )
        self.assertEqual(violations, [])

    def test_production_files_do_not_contain_common_utf8_mojibake(self):
        suspicious = tuple(chr(codepoint) for codepoint in (
            0x00C2, 0x00C3, 0x00D0, 0x00D1, 0xFFFD,
        ))
        violations = []
        for path in _application_text_files():
            text = path.read_bytes().decode("utf-8")
            if any(character in text for character in suspicious):
                violations.append(str(path.relative_to(ROOT)))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
