#!/usr/bin/env python3
"""Receipt parser — Swedish store receipts → structured JSON/CSV.

Usage:
  python3 scripts/receipt-parse.py <receipt.pdf|receipt.txt> [--out-dir receipts/parsed] [--csv receipts/data/items.csv]

Pipeline:
  PDF  -> text (pypdf)
  TEXT -> store-template parser (ICA done; Coop/Willys/Lidl/Hemköp skeleton)
  JSON -> receipts/parsed/<id>.json + append to items.csv / receipts.csv

Drop new receipts in receipts/inbox/ and run:
  python3 scripts/receipt-parse.py receipts/inbox/*.pdf
"""

import argparse, csv, hashlib, json, re, sys
from datetime import datetime
from pathlib import Path

SEP = re.compile(r"^-{10,}$")
AMOUNT = r"\d{1,4}(?:\.\d{3})*,\d{2}|\d+\.\d{2}"
AMOUNT_RE = re.compile(r"([+-]?)(" + AMOUNT + r")\s*$")
WEIGHT_RE = re.compile(r"([\d,]+)\s*kg\s*\*\s*([\d,]+)\s*kr/kg", re.I)
QTY_RE = re.compile(r"([\d,]+)\s*(st|pkt|paket|kg|g|l|dl)\b", re.I)

def to_num(s: str) -> float:
    return float(s.replace(" ", "").replace(".", "").replace(",", ".")) if "." in s.replace(",", ".") and s.count(",") <= 1 and s.count(".") <= 1 else float(s.replace(" ", "").replace(",", "."))

def extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------- ICA
def parse_ica(lines: list[str]) -> dict:
    """ICA receipt. Store name = first line; separator-delimited blocks."""
    rec = {"store_chain": "ICA", "store": lines[0].strip() if lines else ""}
    # split on separator lines
    blocks, cur = [], []
    for ln in lines:
        if SEP.match(ln.strip()):
            if cur: blocks.append(cur); cur = []
        else:
            cur.append(ln)
    if cur: blocks.append(cur)
    # block 0: header (tel/orgnr), block 1: items, block 2: totals, block 3: VAT/member
    items, seen_total = [], False
    for blk in blocks:
        for ln in blk:
            s = ln.strip()
            if not s: continue
            m = re.search(r"Orgnr:\s*(\d+)", s)
            if m: rec["orgnr"] = m.group(1); continue
            if s.startswith("Totalt ") and "varor" in s:
                rec["item_count"] = int(re.search(r"(\d+)", s).group(1)); continue
            if s.startswith("Totalt ") and "SEK" in s:
                rec["total"] = to_num(re.search(r"([\d.,]+)\s*SEK", s).group(1)); continue
            if s.startswith("Mottaget"):
                m = re.search(r"Mottaget\s+(\S+)\s+([\d.,]+)", s)
                if m: rec.setdefault("payment", {})["method"] = m.group(1); rec.setdefault("payment", {})["amount"] = to_num(m.group(2))
                continue
            if re.match(r"^(MASTERCARD|VISA|AMEX|DANKORT|SWISH|KLARNA|PAYPAL|KONTANT)\b", s, re.I):
                m = re.search(r"\*{4,}(\d{1,4})", s)
                rec.setdefault("payment", {})["card"] = s.split()[0].upper()
                if m: rec.setdefault("payment", {})["last4"] = m.group(1)
                continue
            m = re.search(r"\d{4}-\d{2}-\d{2}\s+(\d{2}):(\d{2})", s)
            if m and "date" not in rec:
                rec["date"] = m.group(0)[:10]; rec["time"] = m.group(1) + ":" + m.group(2); continue
            if re.match(r"^\d+,\d{2}\s+[\d.,]+\s+[\d.,]+\s+[\d.,]+$", s):
                p = s.split()
                rec.setdefault("vat", []).append({"rate": to_num(p[0]), "moms": to_num(p[1]), "netto": to_num(p[2]), "brutto": to_num(p[3])})
                continue
            if "spar" in s.lower() and "klubbmedlem" in s.lower():
                m = re.search(r"([\d.,]+)\s*$", s)
                if m: rec["savings"] = to_num(m.group(1))
                continue
            m = re.search(r"Medlemsnummer:\s*(\d+)", s)
            if m: rec["member_no"] = m.group(1); continue
        # item block = first block that has a line ending in an amount and looks like goods
        if not seen_total and not any(k in ("total",) for k in rec) and _looks_like_item_block(blk):
            items = _parse_ica_items(blk); seen_total = True
    rec["items"] = items
    return rec

def _looks_like_item_block(blk: list[str]) -> bool:
    if not any(AMOUNT_RE.search(ln.strip()) for ln in blk):
        return False
    upper = "\n".join(blk).upper()
    return not any(kw in upper for kw in ("TOTALT", "MOTTAGET", "KÖP", "MOMS%", "MEDLEMSNUMMER", "POÄNGGRUNDANDE", "SPARA KVITTOT"))

JUNK_NAMES = ("TOTALT", "MOTTAGET", "KÖP", "BUTIK:", "REF:", "TVR:", "AID:", "KONTAKTLÖS",
              "SOM KLUBBMEDLEM", "POÄNGGRUNDANDE", "* = EJ BONUSGRUNDANDE", "MEDLEMSNUMMER:",
              "SPARA KVITTOT", "VÄLKOMMEN ÅTER", "ÖPPETTIDER:", "DU BETJÄNADES AV", "KASSA:",
              "MOMS%", "TEL:", "ORGNR:", "FOREL", "HUDDINGE")

def _is_junk_name(name: str) -> bool:
    up = name.upper()
    return not name or up.startswith(JUNK_NAMES) or bool(re.search(r"\d{4}-\d{2}-\d{2}", name))

def _parse_ica_items(blk: list[str]) -> list[dict]:
    """Item block state machine.

    Lines:
      <name>                    -> pending name (item continues on next lines)
      <qty>kg*<price>kr/kg <amt> -> qty/unit_price for current item
      Klubbpris:... -<amt>      -> discount on current item
      <name...> <amt>           -> completes an item (name from this line or pending)
    """
    items, cur, pending = [], None, None
    for ln in blk:
        s = ln.strip()
        if not s: continue
        w = WEIGHT_RE.search(s)
        if w:
            # weight line: creates the pending item (name on its own line) or completes cur
            if pending and not _is_junk_name(pending):
                cur = {"name": pending, "qty": to_num(w.group(1)), "unit": "kg",
                       "unit_price": to_num(w.group(2)), "amount": None, "discount": 0.0, "bonus": True}
                items.append(cur)
                pending = None
            elif cur:
                cur["qty"], cur["unit"], cur["unit_price"] = to_num(w.group(1)), "kg", to_num(w.group(2))
            else:
                continue
            m = AMOUNT_RE.search(s)
            if m and cur and cur.get("amount") is None:
                cur["amount"] = to_num(m.group(2))
            continue
        if s.startswith("Klubbpris"):
            m = re.search(r"-?\s*([\d.,]+)\s*$", s)
            if m and cur: cur["discount"] = -abs(to_num(m.group(1)))
            continue
        m = AMOUNT_RE.search(s)
        if m and not re.search(r"\d{4}-\d{2}-\d{2}", s):
            name = s[:m.start()].strip().lstrip("* ").strip()
            if not name and pending and not _is_junk_name(pending):
                name = pending
            if not _is_junk_name(name):
                cur = {"name": name, "qty": None, "unit": None, "unit_price": None,
                       "amount": to_num(m.group(2)), "discount": 0.0, "bonus": not s.startswith("*")}
                if m.group(1) == "-": cur["amount"] = -cur["amount"]
                items.append(cur)
                q = QTY_RE.search(name)
                if q and re.search(rf"(\d+\s*{re.escape(q.group(2))})\s*$", name, re.I):
                    cur["qty"], cur["unit"] = to_num(q.group(1)), q.group(2).lower()
                else:
                    m2 = re.search(r"(\d+)\s*st\s*\*", name, re.I)  # e.g. "2st*31,19"
                    if m2: cur["qty"], cur["unit"] = int(m2.group(1)), "st"
            pending = None
            continue
        # plain line: could be a name for the next item — but skip offer-annotation
        # markers (Rabatt:/Kupong:/Erbjudande:/Prisnedsättning) so they don't
        # overwrite a pending item name (e.g. "KARRÉ BIT BF" then "Rabatt:ERBJUDANDE")
        if re.match(r"^(Rabatt|Kupong|Klubbpris|Erbjudande|Prisnedsättning)", s, re.I):
            continue
        if not _is_junk_name(s):
            pending = s
    return items

# ---------------------------------------------------------------- dispatch
PARSERS = [("ICA", parse_ica)]

def detect_and_parse(lines: list[str]) -> dict:
    txt = "\n".join(lines)
    lower = txt.lower()
    if "klubbpris" in lower or "medlemsnummer" in lower or "poänggrundande" in lower:
        rec = parse_ica(lines)
    else:
        raise ValueError("Unknown receipt format (not ICA). Add a template — Coop/Willys/Lidl/Hemköp next.")
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out-dir", default="receipts/parsed")
    ap.add_argument("--csv", default="receipts/data/items.csv")
    ap.add_argument("--receipts-csv", default="receipts/data/receipts.csv")
    args = ap.parse_args()

    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    items_csv = Path(args.csv); items_csv.parent.mkdir(parents=True, exist_ok=True)
    rec_csv = Path(args.receipts_csv); rec_csv.parent.mkdir(parents=True, exist_ok=True)

    for f in args.files:
        p = Path(f)
        try:
            text = extract_text(p)
            lines = text.splitlines()
            rec = detect_and_parse(lines)
        except Exception as e:
            print(f"FAIL {p.name}: {e}"); continue
        rec["source"] = p.name
        rid = hashlib.sha1(f"{rec.get('date','')}{rec.get('store','')}{rec.get('total','')}".encode()).hexdigest()[:10]
        rec["id"] = rid
        jp = out_dir / f"{rid}.json"
        jp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        # append to CSVs (skip if id already present)
        if rec_csv.exists() and rid in rec_csv.read_text(encoding="utf-8"):
            print(f"SKIP {p.name} (already parsed -> {rid})"); continue
        with rec_csv.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if rec_csv.stat().st_size == 0:
                w.writerow(["id", "date", "time", "store", "item_count", "total", "payment", "card", "last4", "savings", "source"])
            pay = rec.get("payment", {})
            w.writerow([rid, rec.get("date"), rec.get("time"), rec.get("store"), rec.get("item_count"), rec.get("total"), pay.get("method"), pay.get("card"), pay.get("last4"), rec.get("savings"), p.name])
        with items_csv.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if items_csv.stat().st_size == 0:
                w.writerow(["receipt_id", "date", "store", "name", "qty", "unit", "unit_price", "amount", "discount", "bonus"])
            for it in rec.get("items", []):
                w.writerow([rid, rec.get("date"), rec.get("store"), it["name"], it.get("qty"), it.get("unit"), it.get("unit_price"), it.get("amount"), it.get("discount", 0), it.get("bonus", True)])
        print(f"OK   {p.name}: {rec.get('date')} {rec.get('store')} | {len(rec.get('items', []))} items | {rec.get('total')} SEK -> {rid}")

if __name__ == "__main__":
    main()
