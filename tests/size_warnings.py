"""Advisory production-code size checks; run from the repository root."""
import ast
from pathlib import Path


FILE_LINES = 300
FUNCTION_LINES = 50


def main():
    for path in sorted(Path(".tiny-harness").rglob("*.py")):
        source = path.read_text()
        length = len(source.splitlines())
        if length > FILE_LINES:
            print(f"WARNING {path}: {length} lines exceeds the {FILE_LINES}-line review trigger")
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = node.end_lineno - node.lineno + 1
                if length > FUNCTION_LINES:
                    print(f"WARNING {path}:{node.lineno}: {node.name} has {length} lines "
                          f"(review trigger: {FUNCTION_LINES})")


if __name__ == "__main__":
    main()
