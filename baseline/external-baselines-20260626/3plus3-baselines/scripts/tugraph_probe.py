#!/usr/bin/env python3
import importlib.util

mods = [
    "lgraph",
    "lgraph_db_python",
    "liblgraph_python_api",
    "lgraph_python_api",
    "liblgraph",
]
for mod in mods:
    spec = importlib.util.find_spec(mod)
    print(f"{mod}\t{'OK' if spec else 'MISS'}\t{spec.origin if spec else ''}")
