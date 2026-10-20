"""
Bridget's Cruise Tracker (cloud version)

Runs on a schedule in GitHub Actions. Each run:
  1. Reads Bridget's choices from her Google Sheet (what to search, deal threshold...).
     If the sheet can't be read or a row doesn't make sense, it falls back to the
     defaults below, so a typo on her phone never stops the tracker.
  2. Checks CruiseMapper for every search that's switched on.
  3. Writes docs/data/cruises.json (her web page reads this) and docs/data/history.json.
  4. Emails her only when a deal shows up that she hasn't been emailed about before.

Nothing here uses Claude, so runs cost no tokens.

Run it on your own computer too:  python cruise_tracker.py
(add --no-email to skip email, --sample to write made-up data for testing the page)
"""

from datetime import datetime, timedelta, timezone
from statistics import median
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import quote_plus
from urllib.request import urlopen, Request
import smtplib
import ssl
import time
import csv
import io
import json
import os
import re
import sys

# ============================================================
# DEFAULTS (used when the Google Sheet can't be read)
# ============================================================

DEFAULT_SETTINGS = {
    "deal_discount": 0.40,      # 40% below typical
    "months_ahead": 6,
    "passengers": 2,
    "excluded_lines": ["Carnival Cruise Line"],
    "email_alerts": True,
    "max_per_night": None,      # optional ceiling, per person per night
}

DEFAULT_SEARCHES = [
    {"name": "Florida short cruises", "ports": ["Tampa", "Port Canaveral", "Miami"],
     "min_nights": 3, "max_nights": 5},
    {"name": "Japan", "ports": ["Yokohama", "Kobe-Osaka"], "min_nights": 1, "max_nights": 7},
    {"name": "Hawaii", "ports": ["Honolulu"], "min_nights": 1, "max_nights": 7},
    {"name": "Spain", "ports": ["Barcelona", "Malaga", "Palma de Mallorca", "Valencia"],
     "min_nights": 1, "max_nights": 7},
]

MAX_PAGES_PER_PORT = 40     # safety stop; CruiseMapper shows 15 sailings per page
MAX_PORTS_TOTAL = 25        # keeps a run under ~1 hour even if the sheet gets long
HISTORY_DAYS = 180

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "docs", "data")
CONFIG_FILE = os.path.join(ROOT, "tracker_config.json")
STATE_FILE = os.path.join(ROOT, "state.json")          # not published; remembers emailed deals

# Words to look for in CruiseMapper itineraries when the port name differs
# ("round-trip from Kobe, Japan" for the "Kobe-Osaka" port, and so on)
PORT_MATCH = {"Kobe-Osaka": "Kobe", "Miami": "Miami", "Port Canaveral": "Canaveral"}

# CruiseMapper's length filter: 1 = 1-2 nights, 2 = 3-5, 3 = 6-10, 4 = 11-14, 5 = 15+
LENGTH_BANDS = [(1, 1, 2), (2, 3, 5), (3, 6, 10), (4, 11, 14), (5, 15, 999)]

# ============================================================
# SETTINGS FROM BRIDGET'S GOOGLE SHEET
# ============================================================

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    # Environment variables win (handy for testing)
    for key in ("sheet_id", "gid"):
        if os.environ.get(key.upper()):
            cfg[key] = os.environ[key.upper()]
    return cfg

def fetch_sheet_tab(sheet_id, gid):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid:
        url += f"&gid={gid}"   # no gid = the first tab
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8-sig")
    if text.lstrip().startswith("<"):
        raise ValueError("the sheet isn't shared as 'Anyone with the link can view'")
    return list(csv.reader(io.StringIO(text)))

def yes(value):
    return str(value).strip().lower() in ("yes", "y", "true", "on", "1", "x", "✓", "✔")

def to_number(value):
    m = re.search(r"-?\d+(\.\d+)?", str(value).replace(",", ""))
    return float(m.group(0)) if m else None

def parse_settings_rows(rows, notes):
    s = dict(DEFAULT_SETTINGS)
    for row in rows:
        if len(row) < 2 or not row[0].strip():
            continue
        label, value = row[0].strip().lower(), row[1].strip()
        if not value:
            continue
        num = to_number(value)
        if "deal" in label and num is not None:
            pct = num / 100 if num > 1 else num
            if 0.05 <= pct <= 0.6:
                s["deal_discount"] = pct
            else:
                notes.append(f"Deal threshold '{value}' looked off, so 40% was used.")
        elif "month" in label and num is not None:
            s["months_ahead"] = int(min(max(num, 1), 12))
        elif ("traveler" in label or "people" in label or "passenger" in label) and num:
            s["passengers"] = int(min(max(num, 1), 8))
        elif "skip" in label or "exclude" in label:
            s["excluded_lines"] = [x.strip() for x in re.split(r"[,;\n]", value) if x.strip()]
        elif "email" in label:
            s["email_alerts"] = yes(value)
        elif ("max" in label or "ceiling" in label) and num is not None:
            s["max_per_night"] = num if num > 0 else None
    return s

def parse_search_rows(rows, notes):
    searches = []
    header_seen = False
    for row in rows:
        row = [c.strip() for c in row] + [""] * 5
        if not header_seen:
            if any("departure port" in c.lower() for c in row):
                header_seen = True
            continue
        on, name, ports, mn, mx = row[:5]
        if not name and not ports:
            continue
        if not yes(on):
            continue
        port_list = [p.strip() for p in re.split(r"[,;\n]", ports) if p.strip()]
        if not port_list:
            notes.append(f"'{name}' has no departure ports, so it was skipped.")
            continue
        lo, hi = to_number(mn), to_number(mx)
        lo = int(lo) if lo else 1
        hi = int(hi) if hi else 7
        if lo > hi:
            lo, hi = hi, lo
        searches.append({"name": name or ", ".join(port_list), "ports": port_list,
                         "min_nights": max(1, lo), "max_nights": min(hi, 30)})
    return searches

def load_choices():
    """Returns (settings, searches, notes, source) from the sheet, or defaults."""
    notes = []
    cfg = load_config()
    if not cfg.get("sheet_id"):
        return dict(DEFAULT_SETTINGS), DEFAULT_SEARCHES, notes, "defaults"
    try:
        # One tab: settings at the top, then the "what to search" table
        # (its header row is the first one mentioning "ports").
        rows = fetch_sheet_tab(cfg["sheet_id"], cfg.get("gid", ""))
        split = next((i for i, r in enumerate(rows)
                      if any("departure port" in c.lower() for c in r)), len(rows))
        settings = parse_settings_rows(rows[:split], notes)
        searches = parse_search_rows(rows[split:], notes)
        if not searches:
            notes.append("No searches were switched on in the sheet, so the usual four were used.")
            searches = DEFAULT_SEARCHES
        return settings, searches, notes, "sheet"
    except Exception as e:
        notes.append(f"Couldn't read the settings sheet ({e}); used the usual settings.")
        return dict(DEFAULT_SETTINGS), DEFAULT_SEARCHES, notes, "defaults"

# ============================================================
# SCRAPER
# ============================================================

EXTRACT_JS = """rows => rows.map(r => {
  const tds = r.querySelectorAll('td');
  const img = r.querySelector('img.lineIconSmall');
  const a = r.querySelector('td a[href*="/ships/"]');
  const c = r.querySelector('.cruiseTitle a[href]');
  return {
    id: r.getAttribute('data-row') || '',
    date: tds[0] ? tds[0].textContent.trim() : '',
    ship: a ? a.textContent.trim() : '',
    shipUrl: a ? a.href : '',
    cruiseUrl: c ? c.href : '',
    line: img ? (img.getAttribute('title') || img.getAttribute('alt') || '') : '',
    itinerary: (r.querySelector('.cruiseTitle') || {textContent: ''}).textContent.replace(/\\s+/g, ' ').trim(),
    price: (r.querySelector('.cruisePrice') || {textContent: ''}).textContent.trim(),
  };
})"""

class Blocked(Exception):
    pass

def search_url(port, length_filter, page_num):
    return ("https://www.cruisemapper.com/cruise-search?portRegion=0&type=0"
            f"&portDeparture={quote_plus(port)}&portOfCall=&ship=&line="
            f"&duration={length_filter}&departureFrom=&price=10000&finder=cruise&page={page_num}")

def length_filters(lo, hi):
    return [f for f, a, b in LENGTH_BANDS if a <= hi and b >= lo]

def match_word(port):
    return PORT_MATCH.get(port, re.split(r"[-,(]", port)[0].strip())

def clean_line(raw):
    return re.sub(r"\s*Cruises cruise line$", "", raw).strip()

def parse_row(row, port, passengers):
    try:
        sail_date = datetime.strptime(row["date"], "%Y %b %d")
    except ValueError:
        return None
    nm = re.search(r"(\d+)\s+nights?", row["itinerary"])
    pm = re.search(r"\$([\d,]+)", row["price"])
    if not nm or not pm:
        return None
    if match_word(port).lower() not in row["itinerary"].lower():
        return None   # CruiseMapper ignores unknown port names and returns everything
    nights = int(nm.group(1))
    pp = int(pm.group(1).replace(",", ""))
    return {
        "id": row["id"] or f"{row['ship']}|{sail_date:%Y-%m-%d}",
        "date": sail_date, "nights": nights, "ship": row["ship"],
        "line": clean_line(row["line"]), "itinerary": row["itinerary"], "port": port,
        "per_person": pp, "per_night": round(pp / nights, 2), "total": pp * passengers,
        "url": row.get("cruiseUrl") or row["shipUrl"],
    }

def looks_blocked(page):
    title = (page.title() or "").lower()
    body = page.inner_text("body")[:2000].lower() if page.query_selector("body") else ""
    return ("just a moment" in title or "attention required" in title or "access denied" in title
            or "verify you are human" in body or "captcha" in body)

def scrape_port(page, port, lf, cutoff, passengers):
    """Returns (sailings, recognized). recognized=False means CruiseMapper didn't know the port."""
    found, recognized = [], True
    for page_num in range(1, MAX_PAGES_PER_PORT + 1):
        rows = None
        for attempt in range(2):
            try:
                page.goto(search_url(port, lf, page_num), timeout=45000)
                page.wait_for_timeout(1200)
                rows = page.eval_on_selector_all("tr[data-row]", EXTRACT_JS)
                break
            except Exception as e:
                print(f"    Error on page {page_num} (try {attempt + 1}): {e}")
                time.sleep(5)
        if rows is None:
            break
        if not rows:
            if page_num == 1 and looks_blocked(page):
                raise Blocked("CruiseMapper showed a robot check")
            break
        word = match_word(port).lower()
        if page_num == 1 and not any(word in r["itinerary"].lower() for r in rows):
            recognized = False
            break
        past_cutoff = False
        for row in rows:
            s = parse_row(row, port, passengers)
            if s is None:
                try:
                    if datetime.strptime(row["date"], "%Y %b %d") > cutoff:
                        past_cutoff = True
                except ValueError:
                    pass
                continue
            if s["date"] > cutoff:
                past_cutoff = True
                continue
            found.append(s)
        if past_cutoff:
            break
        time.sleep(1.5)
    return found, recognized

def scrape_all(settings, searches, notes):
    from playwright.sync_api import sync_playwright

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today + timedelta(days=round(settings["months_ahead"] * 30.5))
    ports_used = 0
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            locale="en-US")
        page = context.new_page()

        for srch in searches:
            print(f"Checking {srch['name']}...")
            sailings, unknown = {}, []
            for port in srch["ports"]:
                if ports_used >= MAX_PORTS_TOTAL:
                    notes.append(f"Skipped {port}: the sheet lists more than {MAX_PORTS_TOTAL} ports in total.")
                    continue
                ports_used += 1
                recognized_any = False
                for lf in length_filters(srch["min_nights"], srch["max_nights"]):
                    found, recognized = scrape_port(page, port, lf, cutoff, settings["passengers"])
                    recognized_any = recognized_any or recognized or bool(found)
                    for s in found:
                        if s["date"] < today:
                            continue
                        if not (srch["min_nights"] <= s["nights"] <= srch["max_nights"]):
                            continue
                        if any(x.lower() in s["line"].lower() for x in settings["excluded_lines"]):
                            continue
                        sailings[s["id"]] = s
                if not recognized_any:
                    unknown.append(port)
                print(f"  {port}: {sum(1 for s in sailings.values() if s['port'] == port)} sailings")
            if unknown:
                notes.append(f"{srch['name']}: CruiseMapper didn't recognize "
                             f"{', '.join(unknown)}. Check the spelling on cruisemapper.com/ports.")
            results.append({"search": srch, "sailings": list(sailings.values())})
            time.sleep(2)
        browser.close()
    return results

# ============================================================
# DEALS, HISTORY, STATE
# ============================================================

def score(results, settings, state):
    last_prices = state.get("prices", {})
    for gr in results:
        sailings = sorted(gr["sailings"], key=lambda s: s["per_night"])
        typical = median(s["per_night"] for s in sailings) if sailings else None
        deal_line = typical * (1 - settings["deal_discount"]) if typical else None
        ceiling = settings.get("max_per_night")
        for s in sailings:
            s["vs_typical"] = round(1 - s["per_night"] / typical, 3) if typical else None
            s["is_deal"] = bool(deal_line and s["per_night"] <= deal_line
                                and (ceiling is None or s["per_night"] <= ceiling))
            prev = last_prices.get(s["id"])
            s["price_change"] = (s["per_person"] - prev) if prev is not None else None
            s["new"] = prev is None and bool(last_prices)
        gr.update(sailings=sailings, typical=typical, deal_line=deal_line,
                  deals=[s for s in sailings if s["is_deal"]])
    return results

def sailing_json(s):
    return {"id": s["id"], "date": s["date"].strftime("%Y-%m-%d"), "nights": s["nights"],
            "ship": s["ship"], "line": s["line"], "itinerary": s["itinerary"], "port": s["port"],
            "per_person": s["per_person"], "per_night": s["per_night"], "total": s["total"],
            "vs_typical": s["vs_typical"], "is_deal": s["is_deal"], "url": s["url"],
            "price_change": s["price_change"], "new": s["new"]}

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)

def update_history(results, now):
    path = os.path.join(DATA_DIR, "history.json")
    hist = load_json(path, {})
    day = now.strftime("%Y-%m-%d")
    keep_after = (now - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    for gr in results:
        if not gr["sailings"]:
            continue
        name = gr["search"]["name"]
        pts = [p for p in hist.get(name, []) if p["d"] != day and p["d"] >= keep_after]
        pts.append({"d": day, "low": gr["sailings"][0]["per_night"],
                    "typ": round(gr["typical"], 2), "n": len(gr["sailings"])})
        hist[name] = sorted(pts, key=lambda p: p["d"])
    write_json(path, hist)
    return hist

# ============================================================
# EMAIL (only for deals she hasn't been told about)
# ============================================================

def send_email(new_deals, settings, page_url):
    sender = os.environ.get("EMAIL_SENDER", "")
    password = os.environ.get("EMAIL_PASSWORD", "").replace(" ", "")
    recipient = os.environ.get("EMAIL_RECIPIENT", "") or sender
    if not sender or not password:
        print("  Email skipped: EMAIL_SENDER / EMAIL_PASSWORD aren't set.")
        return False

    n = len(new_deals)
    subject = f"Cruise deal{'s' if n != 1 else ''}: {n} new sailing{'s' if n != 1 else ''} well below typical"
    rows = ""
    for name, s in new_deals[:15]:
        rows += f"""
        <tr>
          <td style="padding:10px;border-bottom:1px solid #eee;vertical-align:top;">
            <strong>{s['date']:%b %d, %Y}</strong><br>
            <span style="color:#777;font-size:12px;">{s['nights']} nights · {name}</span></td>
          <td style="padding:10px;border-bottom:1px solid #eee;vertical-align:top;">
            <a href="{s['url']}" style="color:#0b4f6c;">{s['ship']}</a><br>
            <span style="color:#777;font-size:12px;">{s['line']} · from {s['port']}</span></td>
          <td style="padding:10px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap;vertical-align:top;">
            <strong style="color:#1a7f4b;">${s['per_night']:.0f}/night</strong><br>
            <span style="color:#777;font-size:12px;">{s['vs_typical']:.0%} below typical<br>
            ${s['total']:,} for {settings['passengers']}</span></td>
        </tr>"""
    button = (f'<p style="text-align:center;margin:22px 0 6px;"><a href="{page_url}" '
              'style="background:#0b4f6c;color:#fff;padding:12px 22px;border-radius:999px;'
              'text-decoration:none;font-weight:bold;">See all cruises</a></p>') if page_url else ""
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;color:#222;">
      <h1 style="font-size:22px;margin:16px 0 4px;">New deals on Bridget's Cruises</h1>
      <p style="color:#666;margin:0 0 12px;">Sailings at least {settings['deal_discount']:.0%} below the typical
        nightly price for their destination. Prices are per person, cheapest cabin.</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">{rows}</table>
      {button}
      <p style="color:#999;font-size:12px;text-align:center;margin-top:14px;">
        Prices are CruiseMapper "from" fares and can change; confirm before booking.</p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, recipient
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as server:
            server.login(sender, password)
            server.sendmail(sender, [r.strip() for r in recipient.split(",")], msg.as_string())
        print(f"  Emailed {n} new deal(s).")
        return True
    except Exception as e:
        print(f"  Email error: {e}")
        return False

# ============================================================
# SAMPLE DATA (for testing the page without scraping)
# ============================================================

def sample_results(searches):
    import random
    random.seed(4)
    ships = [("Royal Caribbean", "Rhapsody of the Seas"), ("Norwegian", "Norwegian Pride of America"),
             ("Celebrity", "Celebrity Ascent"), ("MSC", "MSC Seashore"), ("Princess", "Diamond Princess"),
             ("Holland America", "Westerdam"), ("Virgin Voyages", "Valiant Lady")]
    base = {"Florida short cruises": 95, "Japan": 160, "Hawaii": 210, "Spain": 120}
    out, today = [], datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for srch in searches:
        sailings = []
        for i in range(random.randint(6, 28) if srch["name"] != "Japan" else 3):
            line, ship = random.choice(ships)
            nights = random.randint(srch["min_nights"] if srch["min_nights"] > 2 else 3, srch["max_nights"])
            pn = base.get(srch["name"], 130) * random.uniform(0.55, 1.5)
            port = random.choice(srch["ports"])
            sailings.append({"id": f"s{srch['name'][:3]}{i}", "date": today + timedelta(days=random.randint(5, 180)),
                             "nights": nights, "ship": ship, "line": line,
                             "itinerary": f"{nights} nights round-trip from {port}", "port": port,
                             "per_person": int(pn * nights), "per_night": round(int(pn * nights) / nights, 2),
                             "total": int(pn * nights) * 2, "url": "https://www.cruisemapper.com/"})
        out.append({"search": srch, "sailings": sailings})
    return out

# ============================================================
# MAIN
# ============================================================

def main():
    args = set(sys.argv[1:])
    now = datetime.now(timezone.utc)
    print(f"Bridget's Cruise Tracker --- {now:%Y-%m-%d %H:%M} UTC")

    settings, searches, notes, source = load_choices()
    print(f"Settings from: {source}; {len(searches)} search(es)")

    state = load_json(STATE_FILE, {"emailed": [], "prices": {}})
    out_path = os.path.join(DATA_DIR, "cruises.json")
    previous = load_json(out_path, {})

    status = "ok"
    try:
        results = sample_results(searches) if "--sample" in args else scrape_all(settings, searches, notes)
    except Blocked as e:
        results, status = [], "blocked"
        notes.append(f"{e}. Results below are from the last good check.")

    total = sum(len(gr["sailings"]) for gr in results)
    if status == "ok" and total == 0:
        status = "empty"
        notes.append("CruiseMapper returned nothing this time. Results below are from the last good check.")

    cfg = load_config()
    page_url = cfg.get("page_url", "")
    sheet_url = (f"https://docs.google.com/spreadsheets/d/{cfg['sheet_id']}/edit"
                 if cfg.get("sheet_id") else "")

    if status != "ok":
        # Keep the last good results on the page; just record that this check failed.
        previous.update(last_checked=now.isoformat(), status=status, notes=notes)
        previous.setdefault("sheet_url", sheet_url)
        write_json(out_path, previous)
        print(f"Check failed ({status}); kept previous results.")
        sys.exit(0 if previous.get("groups") else 1)

    results = score(results, settings, state)
    history = update_history(results, now)

    data = {
        "last_checked": now.isoformat(), "last_success": now.isoformat(), "status": "ok",
        "settings": {"deal_discount": settings["deal_discount"], "months_ahead": settings["months_ahead"],
                     "passengers": settings["passengers"], "excluded_lines": settings["excluded_lines"],
                     "max_per_night": settings["max_per_night"], "source": source},
        "notes": notes, "sheet_url": sheet_url,
        "groups": [{
            "name": gr["search"]["name"], "ports": gr["search"]["ports"],
            "min_nights": gr["search"]["min_nights"], "max_nights": gr["search"]["max_nights"],
            "count": len(gr["sailings"]), "deal_count": len(gr["deals"]),
            "typical": round(gr["typical"], 2) if gr["typical"] else None,
            "deal_line": round(gr["deal_line"], 2) if gr["deal_line"] else None,
            "sailings": [sailing_json(s) for s in gr["sailings"][:60]],
        } for gr in results],
    }
    write_json(out_path, data)
    print(f"Wrote {out_path}: {total} sailings, {sum(len(g['deals']) for g in results)} deals")

    # Email only deals not already emailed
    emailed = set(state.get("emailed", []))
    new_deals = [(gr["search"]["name"], s) for gr in results for s in gr["deals"] if s["id"] not in emailed]
    new_deals.sort(key=lambda x: -(x[1]["vs_typical"] or 0))
    if new_deals and settings["email_alerts"] and "--no-email" not in args and "--sample" not in args:
        if send_email(new_deals, settings, page_url):
            emailed.update(s["id"] for _, s in new_deals)
    elif not new_deals:
        print("  No new deals to email.")

    current_ids = {s["id"] for gr in results for s in gr["sailings"]}
    state = {"emailed": sorted(i for i in emailed if i in current_ids),
             "prices": {s["id"]: s["per_person"] for gr in results for s in gr["sailings"]}}
    if "--sample" not in args:
        write_json(STATE_FILE, state)
    print("Done.")

if __name__ == "__main__":
    main()
