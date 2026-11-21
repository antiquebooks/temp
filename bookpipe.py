#!/usr/bin/env python3
"""
bookpipe.py v2 - staged, resumable pipeline: book photos -> CSV in your Knihy schema.

Folder layout expected:
  photos/
    <location A>/   IMG_0001.jpg ...   (one folder = one location; books never span folders)
    <location B>/   ...

Stages (run in order; each is resumable, finished items are skipped):
  classify  label each photo (cover / title page / signature ...) from small thumbnails
  group     start a new book at each cover; assign location, index (0.. per folder), superindex (global);
            write contact sheets for your review
  extract   FROM THE IMAGES: title, author, publisher, year, language, signature, condition
  research  FROM THE WEB: illustrator, edition size/number, pages, factual SK description
  price     FROM THE WEB: priceRange estimate (averagePrice is calculated in code)
  enrich    text only: EN + DE translation of the description, 1-3 categories from categories.txt
  export    books.csv (exact schema) + books_review.csv (flags, sources, notes); --sheets pushes to Google Sheets
  rename    copy images to <superindex>_<n>.<ext> so file names map to the superindex column

Setup:  pip install google-genai pillow gspread ; export GEMINI_API_KEY=... ; create categories.txt
Models: run `python bookpipe.py models` to list what your key supports, then set the env vars
        FAST_MODEL (images, research, translation) and SMART_MODEL (pricing) to names from that list.
Originals are never modified or moved (rename COPIES unless you pass --move).
"""
import argparse, csv, io, json, math, os, re, shutil, sys, time
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps
from google import genai
from google.genai import types

WORK = Path("work")
FAST_MODEL = os.environ.get("FAST_MODEL", "gemini-2.5-flash")   # classify, extract, research, enrich
SMART_MODEL = os.environ.get("SMART_MODEL", "gemini-2.5-pro")   # pricing
EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
LABELS = ["cover", "back_cover", "spine", "title_page", "half_title",
          "flyleaf_or_signature", "colophon", "interior", "other"]

# exact column order of your Google Sheet / CSV
COLUMNS = ["title", "author", "illustrator", "publicationYear", "editionSize", "editionNumber", "publisher",
           "signature", "language", "descriptionSK", "descriptionEN", "descriptionDE", "condition",
           "averagePrice", "priceRange", "category1 NEW (mapped)", "category2 NEW (mapped)",
           "category3 NEW (mapped)", "pages", "index", "superindex", "location", "special",
           "image1", "image2", "image3", "image4"]

PROVENANCE = (
    "Collection provenance: estate of the Plicka family - Ester Plickova (ethnographer), her father "
    "Vladimir Plicka (teacher, writer), uncle Karel Plicka (photographer, filmmaker, ethnographer). "
    "Look for author inscriptions, dedications to the family, ownership stamps, signatures. "
    "Distinguish meaningful signatures from casual pencil notes. Never claim a signature is authentic; "
    "transcribe it, describe it, and say whether it looks plausible."
)


# ---------------- helpers ----------------
@lru_cache(maxsize=1)
def client():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Set GEMINI_API_KEY")
    return genai.Client(api_key=key)


def nk(text):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def natural_key(p):
    return nk(Path(p).name)


def scan(photos):
    """Return [(location_name, [Path,...]), ...] - one entry per top-level folder, files in shooting order.
    Photos in deeper subfolders count towards their top-level folder. Exits with a clear message if nothing found."""
    root = Path(photos)
    if not root.exists():
        sys.exit(f"ERROR: folder not found: {root.resolve()}\nCheck the --photos path (run from the BookProject folder).")
    other, out = {}, []

    def pics(items):
        res = []
        for p in items:
            if p.is_file():
                if p.suffix.lower() in EXTS:
                    res.append(p)
                else:
                    other[p.suffix.lower() or "(none)"] = other.get(p.suffix.lower() or "(none)", 0) + 1
        return sorted(res, key=lambda p: nk(p.relative_to(root).as_posix()))

    loose = pics(root.iterdir())
    if loose:
        out.append((root.name, loose))
    for d in sorted([d for d in root.iterdir() if d.is_dir()], key=natural_key):
        fs = pics(d.rglob("*"))
        if fs:
            out.append((d.name, fs))
    if other:
        print(f"Note: ignored files with other extensions: {other} (supported: {sorted(EXTS)}; "
              f"HEIC must be converted to JPG first)")
    if not out:
        sys.exit(f"ERROR: no photos found in {root.resolve()}\nExpected e.g. {root}/test/IMG_0001.jpg. "
                 f"Contents: {[x.name for x in list(root.iterdir())[:10]]}")
    return out


def rel(root, p):
    return Path(p).relative_to(root).as_posix()


def img_part(path, max_side):
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")


def load(name, default):
    p = WORK / name
    return json.loads(p.read_text("utf-8")) if p.exists() else default


def save(name, data):
    WORK.mkdir(exist_ok=True)
    (WORK / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


def log(msg):
    """Print and append to work/log.txt"""
    print(msg)
    WORK.mkdir(exist_ok=True)
    with open(WORK / "log.txt", "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")


def need(data, what, prev):
    if not data:
        sys.exit(f"STOP: {what} is empty. Run `python bookpipe.py {prev}` first. "
                 f"Check progress with `python bookpipe.py status`.")


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    return json.loads(m.group(0) if m else text)


def ask(parts, model, search=False, retries=5):
    """Call Gemini -> (parsed_json, grounding_urls). Backs off on rate limits / transient errors."""
    cfg = types.GenerateContentConfig(temperature=0)
    if search:  # JSON mime type can't be combined with the search tool; we parse text instead
        cfg.tools = [types.Tool(google_search=types.GoogleSearch())]
    else:
        cfg.response_mime_type = "application/json"
    for attempt in range(retries):
        try:
            r = client().models.generate_content(model=model, contents=parts, config=cfg)
            urls = []
            try:
                for ch in r.candidates[0].grounding_metadata.grounding_chunks or []:
                    if ch.web and ch.web.uri:
                        urls.append(ch.web.uri)
            except Exception:
                pass
            return parse_json(r.text), urls
        except Exception as e:
            wait = 15 * (attempt + 1)
            print(f"  ! {type(e).__name__}: {str(e)[:120]} - retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError("Gave up after retries (quota?). Re-run later; progress is saved.")


def to_int(v):
    if v in (None, ""):
        return ""
    try:
        return int(round(float(str(v).replace(",", ".").strip())))
    except ValueError:
        m = re.search(r"\d+", str(v))
        return int(m.group(0)) if m else ""


def first(*vals):
    for v in vals:
        if v not in (None, ""):
            return v
    return ""


# ---------------- 1: classify ----------------
def cmd_classify(a):
    root = Path(a.photos)
    labels = load("labels.json", {})
    for loc, files in scan(a.photos):
        todo = [i for i, f in enumerate(files) if rel(root, f) not in labels]
        log(f"[{loc}] {len(files)} photos, {len(todo)} still to classify")
        if not todo:
            continue
        size, ctx, start = 20, 2, todo[0]
        while start < len(files):
            c = ctx if start > 0 else 0
            chunk = files[start - c: start + size]
            parts = [
                f"Photos of antique books in shooting order. Each book starts with its front cover, followed by "
                f"1-3 detail photos (first inner page, title page, signature page - not fixed). The first {c} images "
                f"are context from the previous batch: do not report them. For every other image, in order, return a "
                f'JSON list of {{"n": <1-based position in this batch>, "label": one of {LABELS}, '
                f'"new_book": true if this is the front cover of a different book than the previous image}}. '
                f"Plain, damaged or vellum covers with little text still count as a cover."]
            for i, f in enumerate(chunk, 1):
                parts += [f"Image {i}:", img_part(f, 448)]
            result, _ = ask(parts, FAST_MODEL)
            if isinstance(result, dict):
                result = next((v for v in result.values() if isinstance(v, list)), [])
            got = 0
            for item in result:
                try:
                    n = int(item["n"])
                    if n > c:
                        labels[rel(root, chunk[n - 1])] = {"label": item.get("label", ""),
                                                           "new_book": bool(item.get("new_book"))}
                        got += 1
                except (KeyError, ValueError, IndexError, TypeError):
                    continue
            if got == 0:
                log("  WARNING: the model returned no usable labels for this batch")
            save("labels.json", labels)
            log(f"[{loc}] classified up to {min(start + size, len(files))}/{len(files)}")
            start += size
    log(f"classify done: {len(labels)} photos labelled, "
        f"{sum(1 for v in labels.values() if v['new_book'])} covers detected")


# ---------------- 2: group ----------------
def cmd_group(a):
    if (WORK / "groups.json").exists() and not a.force:
        sys.exit("work/groups.json exists (maybe hand-corrected). Use --force to rebuild - this renumbers superindex!")
    root = Path(a.photos)
    labels = load("labels.json", {})
    groups, sup = [], 1
    for loc, files in scan(a.photos):
        cur, idx = None, 0
        for f in files:
            key = rel(root, f)
            if cur is None or labels.get(key, {}).get("new_book"):
                cur = {"superindex": sup, "location": loc, "index": idx, "files": []}
                groups.append(cur)
                sup += 1
                idx += 1
            cur["files"].append(key)
    save("groups.json", groups)
    make_contact_sheets(a.photos, groups)
    unl = sum(1 for g in groups for fn in g["files"] if fn not in labels)
    if unl:
        log(f"WARNING: {unl} photos have no label - did `classify` run? Then every location becomes ONE book.")
    log(f"group done: {len(groups)} books in {len(set(g['location'] for g in groups))} locations.")
    print("Review work/contact/*.jpg. Fix work/groups.json by hand (move file names between books) if needed.")
    over = [g["superindex"] for g in groups if len(g["files"]) > 4]
    if over:
        print(f"WARNING: books with more than 4 photos (extra ones won't fit image1-4): {over}")


def make_contact_sheets(folder, groups, per_page=10, h=260):
    out = WORK / "contact"
    out.mkdir(parents=True, exist_ok=True)
    for pg in range(0, len(groups), per_page):
        rows = []
        for g in groups[pg: pg + per_page]:
            ims = []
            for fn in g["files"]:
                im = ImageOps.exif_transpose(Image.open(Path(folder) / fn)).convert("RGB")
                im.thumbnail((h * 2, h))
                ims.append(im)
            rows.append((g, ims))
        width = max(sum(i.width + 6 for i in ims) + 150 for _, ims in rows)
        sheet = Image.new("RGB", (width, len(rows) * (h + 6)), "white")
        d = ImageDraw.Draw(sheet)
        for r, (g, ims) in enumerate(rows):
            d.text((4, r * (h + 6) + 4), f"#{g['superindex']}\n{g['location'][:18]}\nidx {g['index']}", fill="red")
            x = 150
            for im in ims:
                sheet.paste(im, (x, r * (h + 6)))
                x += im.width + 6
        sheet.save(out / f"page_{pg // per_page + 1:03d}.jpg", quality=80)


# ---------------- 3: extract (images only) ----------------
EXTRACT_PROMPT = f"""You are an expert antiquarian cataloger. These photos show ONE book (cover first, then detail photos).
{PROVENANCE}
Read ONLY what is visible in the photos; transcribe Fraktur/Schwabacher/Latin carefully. If unsure leave "" and lower confidence.
Never write N/A or Unknown. Return one JSON object with keys:
title (original language, exactly as printed; include subtitle if on the title page), author, publisher,
publicationYear (integer or "" - use the printed year, also from Roman numerals/chronogram if readable),
language (uppercase code of the main language of the text: SK, CZ, DE, LA, EN, HU, FR, PL...; several -> "DE, LA"),
signature (true if a handwritten signature/dedication/inscription by an author or person is present, else false),
signature_text (transcription of inscription/dedication/stamp, "" if none),
signature_notes_sk (Slovak: what it is, link to the Plicka family or a notable person, plausibility; "" if none),
condition (SLOVAK, short and concrete as shown on e-shop, e.g. "dobrý stav, obtrepaná väzba, škvrny na obálke"),
condition_price_impact_sk (one Slovak sentence: how condition affects price),
visible_pages (integer if a last numbered page is visible, else ""),
confidence (0-1 overall), uncertain_fields (list of key names you are unsure about),
needs_better_image (true if signature/fine print needs a higher-resolution crop)."""


def cmd_extract(a):
    groups = load("groups.json", [])
    need(groups, "work/groups.json", "group --photos .\\photos")
    data = load("extracted.json", {})
    log(f"extract: {len(groups)} books, {len(data)} already done")
    for g in groups:
        k = str(g["superindex"])
        if k in data:
            continue
        parts = [EXTRACT_PROMPT]
        for fn in g["files"][:6]:
            parts += [f"File {fn}:", img_part(Path(a.photos) / fn, a.max_side)]
        data[k] = ask(parts, FAST_MODEL)[0]
        save("extracted.json", data)
        log(f"#{k}: {data[k].get('author', '')} - {str(data[k].get('title', ''))[:50]}")


# ---------------- 4: research (web) ----------------
RESEARCH_PROMPT = """You are an antiquarian bibliographer. Identify THIS edition of the book below using web search
(library catalogues such as VD16/VD17/VD18, USTC, ESTC, DNB, Czech and Slovak national library, WorldCat, Google Books,
Open Library, Wikidata, antiquarian dealers). The title, author, publisher and year were read from the book itself and
are authoritative: do not change them.
Find: illustrator (also engravers/ilustrátor), editionSize (print run, "náklad"), editionNumber (which edition, e.g. 2),
pages (total pages of THIS edition), and write descriptionSK.
Rules: only use facts you found for this exact edition; if you cannot confirm the same edition, leave the field "" and
set match_found false. Never guess. Integers only for numeric fields.
descriptionSK: Slovak, 3-5 sentences, precise antiquarian terminology, for an antique e-shop: what the book is about,
author's significance, edition, illustrations, rarity if sourced, and the visible condition/provenance data given below.
Do not add unsourced claims.
Return ONLY JSON: {"match_found": bool, "match_notes_sk": str, "illustrator": str, "editionSize": int or "",
"editionNumber": int or "", "pages": int or "", "publicationYear_if_missing": int or "", "descriptionSK": str}

Book data:
"""


def cmd_research(a):
    ext = load("extracted.json", {})
    need(ext, "work/extracted.json", "extract --photos .\\photos")
    data = load("researched.json", {})
    log(f"research: {len(ext)} books, {len(data)} already done")
    for k, rec in ext.items():
        if k in data:
            continue
        brief = {x: rec.get(x, "") for x in ("title", "author", "publisher", "publicationYear", "language",
                                              "condition", "signature", "signature_notes_sk", "visible_pages")}
        res, urls = ask([RESEARCH_PROMPT + json.dumps(brief, ensure_ascii=False)], FAST_MODEL, search=True)
        res["sources"] = urls
        data[k] = res
        save("researched.json", data)
        log(f"#{k}: match={res.get('match_found')} pages={res.get('pages')}")


# ---------------- 5: price (web) ----------------
PRICE_PROMPT = """You are an antiquarian book appraiser. Estimate the current market value in EUR of THIS copy.
Use web search for comparables: antikvariaty (antikvariat.sk, antikvariaty.cz, aukro, antikvariat.net), AbeBooks, ZVAB,
Biblio, auction results; match edition, year and the stated condition. Only use prices you actually found; never invent
comparables. Price every book, also cheap ones.
priceMin/priceMax = realistic range for this copy AS IT IS (including a premium for a signature/dedication if it is
plausible and meaningful, and the discount for poor condition). If a signature exists also give the range without it.
level: 1 common, 2 old/rare/illustrated, 3 signed/dedicated or Plicka provenance.
Return ONLY JSON: {"level": int, "priceMin": int, "priceMax": int, "priceMinNoSignature": int or "",
"priceMaxNoSignature": int or "", "comparables": [{"description": str, "price_eur": number, "url": str}],
"rationale_sk": "short Slovak explanation incl. condition and signature effect", "rarity_notes_sk": str,
"confidence": 0-1}

Book data:
"""


def merged(k, ext, res):
    """Image-read values win for title/author/publisher/year/language; web fills illustrator/edition/pages."""
    e, r = ext.get(k, {}), res.get(k, {})
    return {
        "title": e.get("title", ""), "author": e.get("author", ""), "publisher": e.get("publisher", ""),
        "publicationYear": to_int(first(e.get("publicationYear"), r.get("publicationYear_if_missing"))),
        "language": e.get("language", ""), "signature": bool(e.get("signature")),
        "condition": e.get("condition", ""),
        "illustrator": r.get("illustrator", ""), "editionSize": to_int(r.get("editionSize")),
        "editionNumber": to_int(r.get("editionNumber")),
        "pages": to_int(first(r.get("pages"), e.get("visible_pages"))),
    }


def cmd_price(a):
    ext, res = load("extracted.json", {}), load("researched.json", {})
    need(ext, "work/extracted.json", "extract --photos .\\photos")
    data = load("priced.json", {})
    log(f"price: {len(ext)} books, {len(data)} already done")
    for k in ext:
        if k in data:
            continue
        brief = merged(k, ext, res)
        brief.update({x: ext[k].get(x, "") for x in ("signature_text", "signature_notes_sk",
                                                      "condition_price_impact_sk")})
        brief["descriptionSK"] = res.get(k, {}).get("descriptionSK", "")
        out, urls = ask([PRICE_PROMPT + json.dumps(brief, ensure_ascii=False)], SMART_MODEL, search=True)
        out["search_sources"] = urls
        data[k] = out
        save("priced.json", data)
        log(f"#{k}: {out.get('priceMin')}-{out.get('priceMax')} EUR (level {out.get('level')})")


# ---------------- 6: enrich (text only) ----------------
def read_categories():
    p = Path("categories.txt")
    if not p.exists():
        sys.exit("Create categories.txt: one exact category name per line (from your category sheet)")
    return [l.strip() for l in p.read_text("utf-8").splitlines() if l.strip()]


def cmd_enrich(a):
    cats = read_categories()
    ext, res = load("extracted.json", {}), load("researched.json", {})
    need(ext, "work/extracted.json", "extract --photos .\\photos")
    data = load("enriched.json", {})
    log(f"enrich: {len(ext)} books, {len(data)} already done")
    todo = [k for k in ext if k not in data]
    for i in range(0, len(todo), a.batch):
        ids = todo[i: i + a.batch]
        books = {k: {"title": ext[k].get("title", ""), "author": ext[k].get("author", ""),
                     "descriptionSK": res.get(k, {}).get("descriptionSK", "")} for k in ids}
        prompt = (
            "For each book: (1) translate descriptionSK faithfully into English (descriptionEN) and German "
            "(descriptionDE), keeping antiquarian terminology, adding no facts; if descriptionSK is empty, leave both empty. "
            f"(2) choose 1-3 categories (closest fit first) from this exact list, copy names verbatim, never invent: "
            f"{json.dumps(cats, ensure_ascii=False)}\n"
            'Return a JSON object keyed by book id: {"12": {"category1": str, "category2": str, "category3": str, '
            '"descriptionEN": str, "descriptionDE": str}}. Unused categories = "".\n\n'
            + json.dumps(books, ensure_ascii=False))
        out, _ = ask([prompt], FAST_MODEL)
        data.update({str(k): v for k, v in out.items()})
        save("enriched.json", data)
        log(f"enriched {len(data)}/{len(ext)}")


# ---------------- 7: export ----------------
def new_name(g, n, fn):
    return f"{g['superindex']:05d}_{n}{Path(fn).suffix.lower()}"


def build_rows():
    cats = set(read_categories())
    groups = load("groups.json", [])
    ext, res = load("extracted.json", {}), load("researched.json", {})
    pr, en = load("priced.json", {}), load("enriched.json", {})
    rows, review, counters = [], [], {}
    for g in sorted(groups, key=lambda x: x["superindex"]):
        k = str(g["superindex"])
        if k not in ext:
            continue
        counters[g["location"]] = counters.get(g["location"], -1) + 1   # index restarts at 0 per location
        m, r, p, d, e = merged(k, ext, res), res.get(k, {}), pr.get(k, {}), en.get(k, {}), ext[k]
        why = []
        lo, hi = to_int(p.get("priceMin")), to_int(p.get("priceMax"))
        if lo != "" and hi != "":
            avg, prange = math.ceil((lo + hi) / 2), f"{lo}-{hi} Eur"   # calculated in code
        else:
            avg, prange = "", ""
            why.append("no price")
        cs = []
        for c in ("category1", "category2", "category3"):
            v = d.get(c, "")
            if v and v not in cats:
                why.append(f"invalid {c}: {v}")
                v = ""
            cs.append(v)
        if not cs[0]:
            why.append("no category")
        if m["publicationYear"] == "":
            why.append("no year")
        if isinstance(e.get("confidence"), (int, float)) and e["confidence"] < 0.6:
            why.append("low OCR confidence")
        if e.get("uncertain_fields"):
            why.append("uncertain: " + ",".join(map(str, e["uncertain_fields"])))
        if r and not r.get("match_found"):
            why.append("edition not confirmed on web")
        if m["signature"] or p.get("level") == 3:
            why.append("signature/provenance - check yourself")
        if e.get("needs_better_image"):
            why.append("needs better image")
        if len(g["files"]) > 4:
            why.append("more than 4 photos")
        imgs = [new_name(g, n, fn) for n, fn in enumerate(g["files"][:4], 1)]
        imgs += [""] * (4 - len(imgs))
        row = {
            "title": m["title"], "author": m["author"], "illustrator": m["illustrator"],
            "publicationYear": m["publicationYear"], "editionSize": m["editionSize"],
            "editionNumber": m["editionNumber"], "publisher": m["publisher"],
            "signature": "true" if m["signature"] else "false", "language": m["language"],
            "descriptionSK": r.get("descriptionSK", ""), "descriptionEN": d.get("descriptionEN", ""),
            "descriptionDE": d.get("descriptionDE", ""), "condition": m["condition"],
            "averagePrice": avg, "priceRange": prange,
            "category1 NEW (mapped)": cs[0], "category2 NEW (mapped)": cs[1], "category3 NEW (mapped)": cs[2],
            "pages": m["pages"], "index": counters[g["location"]], "superindex": g["superindex"],
            "location": g["location"], "special": "",
            "image1": imgs[0], "image2": imgs[1], "image3": imgs[2], "image4": imgs[3],
        }
        rows.append(row)
        srcs = [c.get("url", "") for c in p.get("comparables", []) if c.get("url")]
        srcs += p.get("search_sources", []) + r.get("sources", [])
        review.append({
            "superindex": g["superindex"], "location": g["location"], "index": row["index"],
            "needs_review": "yes" if why else "", "review_reasons": "; ".join(why),
            "priceRange_without_signature": (f"{to_int(p.get('priceMinNoSignature'))}-"
                                             f"{to_int(p.get('priceMaxNoSignature'))} Eur"
                                             if to_int(p.get("priceMinNoSignature")) != "" else ""),
            "price_level": p.get("level", ""), "price_rationale_sk": p.get("rationale_sk", ""),
            "rarity_notes_sk": p.get("rarity_notes_sk", ""), "condition_price_impact_sk": e.get("condition_price_impact_sk", ""),
            "signature_text": e.get("signature_text", ""), "signature_notes_sk": e.get("signature_notes_sk", ""),
            "web_match_notes_sk": r.get("match_notes_sk", ""), "ocr_confidence": e.get("confidence", ""),
            "sources": " | ".join(dict.fromkeys(srcs)), "original_files": ", ".join(g["files"]),
        })
    return rows, review


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def cmd_export(a):
    need(load("extracted.json", {}), "work/extracted.json (no books extracted yet)", "extract --photos .\\photos")
    rows, review = build_rows()
    write_csv("books.csv", COLUMNS, rows)
    write_csv("books_review.csv", list(review[0].keys()) if review else ["superindex"], review)
    flagged = sum(1 for r in review if r["needs_review"])
    log(f"books.csv: {len(rows)} books (your exact schema). books_review.csv: {flagged} flagged for review.")
    if a.sheets:
        import gspread
        gc = gspread.service_account(filename=os.environ.get("GSPREAD_KEY", "service_account.json"))
        ws = gc.open_by_key(os.environ["SHEET_ID"]).sheet1   # ID stays in the environment, never printed
        ws.append_rows([[r[c] for c in COLUMNS] for r in rows], value_input_option="USER_ENTERED")
        print("Rows appended to the Google Sheet in one operation.")


# ---------------- 8: rename ----------------
def cmd_rename(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    n_done = 0
    for g in load("groups.json", []):
        for n, fn in enumerate(g["files"][:4], 1):
            dst = out / new_name(g, n, fn)
            if not dst.exists():
                (shutil.move if a.move else shutil.copy2)(Path(a.photos) / fn, dst)
                n_done += 1
    print(f"{n_done} files {'moved' if a.move else 'copied'} to {out}/ as <superindex>_<n>.<ext>")


# ---------------- status ----------------
def cmd_status(a):
    if a.photos:
        print("PHOTOS FOUND:")
        for loc, files in scan(a.photos):
            print(f"  {loc}: {len(files)} photos")
    print("PROGRESS (work/ folder):")
    for name in ("labels", "groups", "extracted", "researched", "priced", "enriched"):
        d = load(f"{name}.json", None)
        print(f"  {name:10s}: " + ("missing - stage not run yet" if d is None else f"{len(d)} entries"))
    lg = WORK / "log.txt"
    if lg.exists():
        print("LAST LOG LINES:")
        for line in lg.read_text("utf-8").splitlines()[-8:]:
            print("  " + line)


# ---------------- models ----------------
def cmd_models(a):
    print(f"Currently configured: FAST_MODEL={FAST_MODEL}  SMART_MODEL={SMART_MODEL}\n")
    print("Models your API key can use for generateContent:")
    for m in client().models.list():
        acts = getattr(m, "supported_actions", None) or []
        if not acts or "generateContent" in acts:
            print("  " + m.name.replace("models/", ""))


# ---------------- cli ----------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("classify", cmd_classify), ("group", cmd_group), ("extract", cmd_extract),
                     ("research", cmd_research), ("price", cmd_price), ("enrich", cmd_enrich),
                     ("export", cmd_export), ("rename", cmd_rename), ("models", cmd_models), ("status", cmd_status)]:
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        if name in ("classify", "group", "extract", "rename"):
            s.add_argument("--photos", required=True, help="root folder; each subfolder = one location")
        if name == "status":
            s.add_argument("--photos", default=None, help="optional: also count photos per folder")
        if name == "group":
            s.add_argument("--force", action="store_true", help="overwrite existing work/groups.json")
        if name == "extract":
            s.add_argument("--max-side", type=int, default=2000, help="longest side sent to AI (originals untouched)")
        if name == "enrich":
            s.add_argument("--batch", type=int, default=10)
        if name == "export":
            s.add_argument("--sheets", action="store_true")
        if name == "rename":
            s.add_argument("--out", default="renamed")
            s.add_argument("--move", action="store_true", help="move instead of copy")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
