import os
import sys


for root in sys.argv[1:]:
    total = 0
    l0 = 0
    files = 0
    l0_marker = os.path.join("levels", "L0") + os.sep
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            size = os.path.getsize(path)
            total += size
            files += 1
            if l0_marker in path:
                l0 += size
    manifest = os.path.getsize(os.path.join(root, "MANIFEST"))
    print(root, files, total, l0, manifest)
