"""The parts of the `diffs` plugin, loaded by `plugin.py` from beside it.

`engine` reads a diff, `highlight` colours it, `html` draws it, `store` keeps
it, `files` renders it to a PNG or a PDF and `tool` is what the model calls.
None of them but `files` and `tool` imports Ultron at all.
"""
