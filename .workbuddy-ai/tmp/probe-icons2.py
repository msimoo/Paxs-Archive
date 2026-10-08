import re
p = "app/static/viewer/app.bundle.29858d89fb4b9b9d.js"
s = open(p, encoding="utf-8", errors="replace").read()

# Lucide icon factory definitions:  Xx("Name",[ ... ])
names = re.findall(r'\w+\("([A-Z][A-Za-z0-9]+)",\[\["', s)
print("Lucide icon definitions:", len(names))
for n in sorted(set(names))[:60]:
    print("  ", n)

print("\n--- toolbar 'icon:' props ---")
icons = sorted(set(re.findall(r'icon:"([^"]{2,40})"', s)))
for i in icons[:80]:
    print("  ", i)
