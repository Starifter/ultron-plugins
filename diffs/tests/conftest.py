"""Load the plugin the way Ultron does - `plugin.py` by path, under its own name -
so the tests import `ultron_plugin_diffs` and the `ultron_plugin_diffs_lib`
package it loads from beside itself."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugin.py"

if "ultron_plugin_diffs" not in sys.modules:
    spec = importlib.util.spec_from_file_location("ultron_plugin_diffs", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
