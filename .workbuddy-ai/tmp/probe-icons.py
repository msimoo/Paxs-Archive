import re
p = "app/static/viewer/app.bundle.29858d89fb4b9b9d.js"
s = open(p, encoding="utf-8", errors="replace").read()

pats = {
    'fill:"currentColor"': r'fill:"currentColor"',
    'fill="currentColor"': r'fill="currentColor"',
    'stroke:"currentColor"': r'stroke:"currentColor"',
    'strokeWidth': r'strokeWidth',
    'viewBox 24x24': r'viewBox:"0 0 24 24"',
    'viewBox 25x25': r'viewBox:"0 0 25 25"',
    'viewBox 20x20': r'viewBox:"0 0 20 20"',
    'data-icon': r'data-icon',
    'Icon name=': r'name:"',
}
for k, v in pats.items():
    print(f"{k:28s}: {len(re.findall(v, s))}")

print("\n--- sample fill:currentColor contexts ---")
for m in list(re.finditer(r'fill:"currentColor"', s))[:2]:
    i = m.start()
    print("\n>>>", s[max(0, i-400):i+250])
