#!/usr/bin/env python3
import inspect
import lgraph_db_python as lg

print("module", lg)
for name in sorted(dir(lg)):
    if name.startswith("_"):
        continue
    obj = getattr(lg, name)
    print("SYMBOL", name, type(obj))
    if inspect.isclass(obj) or inspect.isfunction(obj):
        try:
            print("  sig", inspect.signature(obj))
        except Exception as exc:
            print("  sig_err", repr(exc))
        doc = getattr(obj, "__doc__", None)
        if doc:
            print("  doc", doc[:300].replace("\n", "\\n"))
    if inspect.isclass(obj):
        for method_name in sorted(dir(obj)):
            if method_name.startswith("_"):
                continue
            method = getattr(obj, method_name)
            print("  METHOD", method_name, type(method))
            doc = getattr(method, "__doc__", None)
            if doc:
                print("    doc", doc[:300].replace("\n", "\\n"))
