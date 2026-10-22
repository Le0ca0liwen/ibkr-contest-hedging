"""Load prototype optimizer/scenario modules next to the baseline `hedge` package in one process.

PROTO_A (default ~/ss-protoA) holds the prototype tree; its hedge/optimizer.py is imported as
hedge.optimizer_proto (with hedge.linmodel from the same tree), so baseline and prototype see
identical inputs and share every other module.
"""
import importlib.util
import os
import sys
from pathlib import Path


def load(name, path, alias):
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def proto_optimizer(tree=None):
    tree = Path(tree or os.environ.get("PROTO_A") or Path.home() / "ss-protoA")
    import hedge  # noqa: F401  baseline package must be importable first

    load("hedge.linmodel", tree / "hedge" / "linmodel.py", "hedge.linmodel")
    return load("hedge.optimizer_proto", tree / "hedge" / "optimizer.py", "hedge.optimizer_proto")
