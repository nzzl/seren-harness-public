"""Copy to tools/check_python.py; check a pure-Python src package with unittest."""
import argparse
import importlib
from pathlib import Path
import sys
import unittest


def verify_origins(package, source):
    for name, module in list(sys.modules.items()):
        if name != package and not name.startswith(package + "."):
            continue
        paths = list(getattr(module, "__path__", []))
        filename = getattr(module, "__file__", None)
        if filename:
            paths.append(filename)
        if not paths or any(not Path(path).resolve().is_relative_to(source) for path in paths):
            raise ValueError(f"Project module {name} resolved outside snapshot source {source}: {paths}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", help="Project import package, for example my_app")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = (root / "src").resolve()
    sys.path.insert(0, str(source))
    try:
        if not source.is_dir() or not source.is_relative_to(root):
            raise ValueError("Expected src directory inside the snapshot")
        importlib.import_module(args.package)
        verify_origins(args.package, source)
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        if suite.countTestCases() == 0:
            raise ValueError("No tests discovered; required validation did not run")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        verify_origins(args.package, source)
        return 0 if result.wasSuccessful() else 1
    except (ImportError, ValueError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
