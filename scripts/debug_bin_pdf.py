import sys, fitz, json

path = sys.argv[1]
doc = fitz.open(path)
print(f"=== PÁGINAS: {len(doc)} ===\n")

for i, p in enumerate(doc):
    print(f"--- PÁGINA {i} : get_text('text') ---")
    print(p.get_text("text"))
    print(f"\n--- PÁGINA {i} : get_text('words', sort=True) ---")
    words = p.get_text("words", sort=True)
    for w in words:
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        print(f"  x0={x0:8.2f} y0={y0:7.2f} x1={x1:8.2f} y1={y1:7.2f}  '{text}'")
    print(f"\n--- PÁGINA {i} : get_text('blocks') ---")
    for b in p.get_text("blocks"):
        print(f"  block: {b}")
