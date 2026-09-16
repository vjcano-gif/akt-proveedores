"""Extrae datos semilla desde PROYECTO PROVEEDORES.xlsx hacia CSV."""
import re, unicodedata
import pandas as pd

XLSX = "/root/.claude/uploads/16b85f14-d908-59a7-91ed-04f3144fe2dc/aa548d54-PROYECTO_PROVEEDORES.xlsx"
OUT = "/home/claude/akt-proveedores/data"

# ---------- BOM ----------
bom = pd.read_excel(XLSX, sheet_name="BOM", header=0)
bom.columns = [str(c).strip() for c in bom.columns]
print("cols BOM:", list(bom.columns)[:12])

bom = bom.rename(columns={
    "Artículo (transformado)": "articulo_transformado",
    "Desc": "desc_transformado",
    "ID Cpte (consume)": "componente",
    "Descr Cpte": "desc_componente",
    "Cant": "cantidad",
    "Sec Operación": "secuencia",
    "Fase BOM": "fase",
    "Proveedor": "proveedor_codigo",
    "Desc Proveedor": "proveedor_nombre",
    "Familia": "familia",
    "Desc Fam": "desc_familia",
    "Grupo Art": "grupo_articulo",
    "Categoria": "categoria",
    "Color": "color",
    "Cd Origen": "cd_origen",
})
keep = ["articulo_transformado","desc_transformado","componente","desc_componente",
        "cantidad","secuencia","fase","proveedor_codigo","proveedor_nombre",
        "familia","desc_familia","grupo_articulo","categoria","color","cd_origen"]
bom = bom[[c for c in keep if c in bom.columns]].copy()
bom = bom.dropna(subset=["articulo_transformado","componente"])
for c in ["articulo_transformado","componente"]:
    bom[c] = bom[c].astype(str).str.strip().str.replace(r"\.0$","",regex=True)
bom["cantidad"] = pd.to_numeric(bom["cantidad"], errors="coerce").fillna(1.0)
bom["secuencia"] = pd.to_numeric(bom["secuencia"], errors="coerce").fillna(0).astype(int)
for c in bom.columns:
    if bom[c].dtype == object:
        bom[c] = bom[c].astype(str).str.strip().replace({"nan":"", "None":""})
bom = bom.drop_duplicates(subset=["articulo_transformado","componente","secuencia"])
bom.to_csv(f"{OUT}/bom.csv", index=False)
print("BOM:", len(bom), "| transformados únicos:", bom.articulo_transformado.nunique())

# ---------- ARTICULOS (derivados del BOM: transformados + componentes) ----------
a1 = bom[["articulo_transformado","desc_transformado","familia","desc_familia",
          "grupo_articulo","categoria","color"]].rename(
    columns={"articulo_transformado":"codigo","desc_transformado":"descripcion"})
a1["tipo"] = "TRANSFORMADO"
a2 = bom[["componente","desc_componente","familia","desc_familia",
          "grupo_articulo","categoria","color"]].rename(
    columns={"componente":"codigo","desc_componente":"descripcion"})
a2["tipo"] = "CRUDO"
art = pd.concat([a1,a2], ignore_index=True)
art["codigo"] = art["codigo"].astype(str).str.strip()
art = art[art.codigo.str.len() > 2]
# si un código aparece como transformado, gana ese tipo
art["_rank"] = (art["tipo"] == "TRANSFORMADO").astype(int)
art = art.sort_values("_rank", ascending=False).drop_duplicates(subset=["codigo"], keep="first")
art = art.drop(columns=["_rank"])
art["unidad"] = "UND"
art["activo"] = True
art.to_csv(f"{OUT}/articulos.csv", index=False)
print("ARTICULOS:", len(art), art.tipo.value_counts().to_dict())

# ---------- PROVEEDORES ----------
prov = bom[["proveedor_codigo","proveedor_nombre"]].copy()
prov = prov[(prov.proveedor_codigo != "") & (prov.proveedor_codigo != "nan")]
prov = prov.drop_duplicates(subset=["proveedor_codigo"]).rename(
    columns={"proveedor_codigo":"codigo","proveedor_nombre":"nombre"})
prov["activo"] = True
prov["tolerancia_averia_pct"] = 1.0
prov.to_csv(f"{OUT}/proveedores.csv", index=False)
print("PROVEEDORES:", len(prov))

# ---------- UBICACIONES / RESTRINGIDAS ----------
r = pd.read_excel(XLSX, sheet_name="bd RESTRINGIDA", header=0)
r.columns = ["un","ubic_ini","n1","n2","n3","n4","ubic_dest","d1","d2","d3","d4",
             "cerrada","inspeccion","no_restringida"][:len(r.columns)]
def norm(*parts):
    vals = []
    for p in parts:
        if p is None: continue
        s = str(p).strip()
        if s in ("", "nan", "None", "1", "1.0"): continue
        vals.append(s)
    return "-".join(vals)

rows = []
for _, x in r.iterrows():
    origen = norm(x.get("ubic_ini"), x.get("n1"), x.get("n2"), x.get("n3"), x.get("n4"))
    destino = norm(x.get("ubic_dest"), x.get("d1"), x.get("d2"), x.get("d3"), x.get("d4"))
    for code, rol in ((origen,"ORIGEN"), (destino,"DESTINO")):
        if not code: continue
        rows.append({
            "un": str(x.get("un") or "MOTOS").strip(),
            "codigo": code,
            "rol": rol,
            "cerrada": str(x.get("cerrada")).strip().upper() == "Y",
            "inspeccion": str(x.get("inspeccion")).strip().upper() == "Y",
            "restringida": str(x.get("no_restringida")).strip().upper() != "Y",
        })
ub = pd.DataFrame(rows).drop_duplicates(subset=["codigo"], keep="first")
ub["activo"] = True
ub.to_csv(f"{OUT}/ubicaciones.csv", index=False)
print("UBICACIONES:", len(ub))
print(ub.head(10).to_string())
