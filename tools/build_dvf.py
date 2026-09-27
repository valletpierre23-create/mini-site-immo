#!/usr/bin/env python3
"""
Construit les fichiers de ventes utilisés par l'estimation d'Arpent, à partir des
« Demandes de valeurs foncières géolocalisées » (DGFiP / Etalab, Licence Ouverte 2.0).

Sortie (dossier --out) :
  meta.json            période couverte, nombre de ventes, date de génération
  d/{dep}.json         communes du département : nom, centre, nombre de ventes,
                       prix médian au m² par semestre (maisons « M », appartements « A »)
  c/{insee}.json       ventes des 36 derniers mois : maisons et appartements, sans adresse,
                       coordonnées arrondies à ~10 m

Règles de nettoyage (usuelles pour DVF) :
  - ventes uniquement (pas de VEFA, d'échange, d'adjudication…) ;
  - un seul logement (maison ou appartement) par mutation, dépendances admises,
    aucun local commercial ;
  - surface habitable 10–1 000 m², prix ≥ 10 000 €, prix au m² 300–30 000 €.

Usage : python tools/build_dvf.py --out arpent/dvf [--base URL] [--years 2021,2022] [--deps 37,75]
"""
import argparse
import csv
import gzip
import io
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

DEFAULT_BASE = "https://files.data.gouv.fr/geo-dvf/latest/csv/"
DETAIL_MONTHS = 36
MIN_SERIES = 5

csv.field_size_limit(10_000_000)


def fetch(url, tries=4):
    if url.startswith("/") or url.startswith("file:"):
        path = url[5:] if url.startswith("file:") else url
        with open(path, "rb") as f:
            return f.read()
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "arpent-dvf-builder"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:  # réseau capricieux : on réessaie
            last = e
            time.sleep(2 + i * 3)
    raise last


def listing(url):
    if url.startswith("/") or url.startswith("file:"):
        path = url[5:] if url.startswith("file:") else url
        return "\n".join(f'<a href="{n}{"/" if os.path.isdir(os.path.join(path, n)) else ""}">' for n in sorted(os.listdir(path)))
    return fetch(url).decode("utf-8", "replace")


def years(base):
    return sorted({int(y) for y in re.findall(r'href="(\d{4})/"', listing(base))})


def departments(base, year):
    return sorted(set(re.findall(r'href="([0-9AB]{2,3})\.csv\.gz"', listing(f"{base}{year}/departements/"))))


def num(v, cast=float):
    try:
        return cast(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def parse_department(raw):
    """Renvoie la liste des ventes retenues d'un fichier départemental (octets gzip)."""
    text = io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(raw)), encoding="utf-8", newline="")
    groups = defaultdict(list)
    for row in csv.DictReader(text):
        groups[row["id_mutation"]].append(row)

    out = []
    for rows in groups.values():
        first = rows[0]
        nature = first.get("nature_mutation", "Vente")
        if nature != "Vente":
            continue
        types = {r.get("type_local") or "" for r in rows}
        if any(t.startswith("Local") for t in types):
            continue
        dwellings = {}
        for r in rows:
            t = r.get("type_local")
            if t in ("Maison", "Appartement"):
                key = (r.get("id_parcelle"), t, r.get("surface_reelle_bati"), r.get("nombre_pieces_principales"), r.get("lot_1_numero") or r.get("lot1_numero"))
                dwellings[key] = r
        if len(dwellings) != 1:
            continue
        d = next(iter(dwellings.values()))
        price = num(first.get("valeur_fonciere"))
        surface = num(d.get("surface_reelle_bati"), int)
        if not price or not surface or price < 10_000 or not (10 <= surface <= 1000):
            continue
        sqm = price / surface
        if not (300 <= sqm <= 30_000):
            continue
        lat = num(d.get("latitude")) or next((num(r.get("latitude")) for r in rows if r.get("latitude")), None)
        lon = num(d.get("longitude")) or next((num(r.get("longitude")) for r in rows if r.get("longitude")), None)
        if lat is None or lon is None:
            continue
        kind = "M" if d["type_local"] == "Maison" else "A"
        land = 0
        if kind == "M":
            parcels = {}
            for r in rows:
                s = num(r.get("surface_terrain"), int)
                if s:
                    parcels[r.get("id_parcelle")] = max(parcels.get(r.get("id_parcelle"), 0), s)
            land = sum(parcels.values())
        date = first["date_mutation"]  # AAAA-MM-JJ
        out.append(
            (
                d["code_commune"],
                d.get("nom_commune") or "",
                round(lat, 4),
                round(lon, 4),
                int(date[:4]) * 100 + int(date[5:7]),
                kind,
                surface,
                num(d.get("nombre_pieces_principales"), int) or 0,
                int(round(price)),
                land,
            )
        )
    return out


def semester(ym):
    return f"{ym // 100}-{1 if ym % 100 <= 6 else 2}"


def series(values_by_period):
    rows = []
    for p in sorted(values_by_period):
        vals = values_by_period[p]
        if len(vals) >= MIN_SERIES:
            rows.append([p, int(round(statistics.median(vals))), len(vals)])
    return rows


def months_between(a, b):
    return (b // 100 - a // 100) * 12 + (b % 100 - a % 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default=os.environ.get("DVF_BASE", DEFAULT_BASE))
    ap.add_argument("--years", default="")
    ap.add_argument("--deps", default="")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    base = args.base if args.base.endswith("/") else args.base + "/"

    ys = [int(y) for y in args.years.split(",") if y] or years(base)
    print("Années :", ys, flush=True)
    tasks = []
    for y in ys:
        deps = [d for d in args.deps.split(",") if d] or departments(base, y)
        tasks += [(y, d) for d in deps]
    print(f"{len(tasks)} fichiers départementaux", flush=True)

    sales = []

    def work(task):
        y, d = task
        try:
            return parse_department(fetch(f"{base}{y}/departements/{d}.csv.gz"))
        except Exception as e:
            print(f"  ! {y}/{d} : {e}", file=sys.stderr, flush=True)
            return []

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(work, tasks):
            sales.extend(res)
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(tasks)} · {len(sales)} ventes", flush=True)

    if not sales:
        print("Aucune vente : rien à écrire", file=sys.stderr)
        sys.exit(1)

    last = max(s[4] for s in sales)
    first = min(s[4] for s in sales)
    names = {}
    centers = defaultdict(lambda: [0.0, 0.0, 0])
    by_commune = defaultdict(list)
    commune_series = defaultdict(lambda: {"M": defaultdict(list), "A": defaultdict(list)})
    dep_series = defaultdict(lambda: {"M": defaultdict(list), "A": defaultdict(list)})
    recent_counts = defaultdict(lambda: {"M": 0, "A": 0})

    for s in sales:
        insee, name, lat, lon, ym, kind, surface, rooms, price, land = s
        dep = insee[:3] if insee.startswith("97") else insee[:2]
        names[insee] = name
        c = centers[insee]
        c[0] += lat
        c[1] += lon
        c[2] += 1
        sqm = price / surface
        commune_series[insee][kind][semester(ym)].append(sqm)
        dep_series[dep][kind][semester(ym)].append(sqm)
        if months_between(ym, last) < DETAIL_MONTHS:
            by_commune[insee].append([lat, lon, ym, kind, surface, rooms, price, land])
            recent_counts[insee][kind] += 1

    out = args.out
    os.makedirs(os.path.join(out, "c"), exist_ok=True)
    os.makedirs(os.path.join(out, "d"), exist_ok=True)

    for insee, rows in by_commune.items():
        rows.sort(key=lambda r: -r[2])
        with open(os.path.join(out, "c", f"{insee}.json"), "w") as f:
            json.dump({"c": insee, "n": names.get(insee, ""), "v": rows}, f, separators=(",", ":"), ensure_ascii=False)

    deps = defaultdict(dict)
    for insee, c in centers.items():
        dep = insee[:3] if insee.startswith("97") else insee[:2]
        deps[dep][insee] = {
            "n": names.get(insee, ""),
            "lat": round(c[0] / c[2], 4),
            "lon": round(c[1] / c[2], 4),
            "k": recent_counts[insee],
            "s": {k: series(v) for k, v in commune_series[insee].items()},
        }
    for dep, communes in deps.items():
        with open(os.path.join(out, "d", f"{dep}.json"), "w") as f:
            json.dump(
                {"d": dep, "communes": communes, "s": {k: series(v) for k, v in dep_series[dep].items()}},
                f,
                separators=(",", ":"),
                ensure_ascii=False,
            )

    meta = {
        "source": "Demandes de valeurs foncières géolocalisées (DGFiP, Etalab) — Licence Ouverte 2.0",
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "from": f"{first // 100}-{first % 100:02d}",
        "to": f"{last // 100}-{last % 100:02d}",
        "sales": len(sales),
        "detailMonths": DETAIL_MONTHS,
        "communes": len(centers),
    }
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump(meta, f, ensure_ascii=False)
    print(json.dumps(meta, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
