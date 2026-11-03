#!/usr/bin/env python3
"""Talent Scout MVP: finds young exceptional people from free sources.

Zero dependencies (Python 3.9+ standard library only). Free LLM optional.

Sources:
  1. Wikidata (SPARQL): people born recently with notable occupations/awards
  2. Google News RSS: articles like "16-year-old hacker", "teen inventor"

Pipeline: discover -> score (free LLM or keyword fallback) -> contact lookup
          (adults only, from their own official website) -> SQLite + CSV.
"""
import argparse, csv, datetime as dt, json, os, re, sqlite3, sys, time
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from html import unescape

UA = "TalentScoutMVP/0.1 (research tool)"
DB = os.environ.get("SCOUT_DB", "scout.db")
THIS_YEAR = dt.date.today().year

# ---------------------------------------------------------------- HTTP utils
def get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def post_json(url, payload, headers, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": UA, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# ---------------------------------------------------------------- Storage
def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS people(
        key TEXT PRIMARY KEY, name TEXT, birth_year INT, age INT, age_band TEXT,
        achievement TEXT, age_at_achievement INT, category TEXT, score INT,
        country TEXT, website TEXT, contact_email TEXT, contact_type TEXT,
        source TEXT, source_url TEXT, found_at TEXT)""")
    return con

def age_band(age):
    if age is None: return "unknown"
    if age < 18: return "minor"
    if age <= 20: return "18-20"
    return "adult"

# ---------------------------------------------------------------- Source 1: Wikidata
# Occupations / fields that signal "did something remarkable young".
WIKIDATA_QUERY = """
SELECT ?p ?pLabel ?birth ?desc ?site ?countryLabel (GROUP_CONCAT(DISTINCT ?occLabel; separator=", ") AS ?occs) WHERE {
  VALUES ?occ { wd:Q1622272 wd:Q82594 wd:Q5482740 wd:Q205375 wd:Q131524 wd:Q483501
                wd:Q639669 wd:Q1028181 wd:Q33999 wd:Q36180 wd:Q901 wd:Q177220 wd:Q170790 }
  ?p wdt:P31 wd:Q5; wdt:P106 ?occ; wdt:P569 ?birth.
  FILTER(YEAR(?birth) >= %(min_year)d && YEAR(?birth) <= %(max_year)d)
  OPTIONAL { ?p wdt:P856 ?site. }
  OPTIONAL { ?p wdt:P27 ?country. }
  OPTIONAL { ?p schema:description ?desc FILTER(LANG(?desc)="en") }
  ?occ rdfs:label ?occLabel FILTER(LANG(?occLabel)="en")
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} GROUP BY ?p ?pLabel ?birth ?desc ?site ?countryLabel LIMIT %(limit)d
"""
# Q1622272 hacker, Q82594 computer scientist, Q5482740 programmer, Q205375 inventor,
# Q131524 entrepreneur, Q483501 artist, Q639669 musician, Q1028181 painter,
# Q33999 actor, Q36180 writer, Q901 scientist, Q177220 singer, Q170790 mathematician

def src_wikidata(min_age, max_age, limit):
    q = WIKIDATA_QUERY % {"min_year": THIS_YEAR - max_age, "max_year": THIS_YEAR - min_age, "limit": limit}
    url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q)
    data = json.loads(get(url, timeout=90))
    for b in data["results"]["bindings"]:
        v = lambda k: b.get(k, {}).get("value")
        by = int(v("birth")[:4]) if v("birth") and v("birth")[0].isdigit() else None
        yield {"key": "wd:" + v("p").rsplit("/", 1)[-1], "name": v("pLabel"), "birth_year": by,
               "text": f"{v('pLabel')}: {v('desc') or ''}. Occupations: {v('occs')}",
               "website": v("site"), "country": v("countryLabel"),
               "source": "wikidata", "source_url": v("p")}

# ---------------------------------------------------------------- Source 2: News RSS
NEWS_QUERIES = [
    '"year-old hacker"', '"teen hacker"', '"year-old inventor"', '"teen founder"',
    '"youngest ever" prodigy', '"year-old" startup founder', '"child prodigy" artist',
    '"year-old" scientist award', '"teen" "olympiad" gold', '"year-old" bug bounty',
]

def src_news(queries, per_query):
    for q in queries:
        url = "https://news.google.com/rss/search?hl=en&gl=US&ceid=US:en&q=" + urllib.parse.quote(q)
        try:
            root = ET.fromstring(get(url))
        except Exception as e:
            print(f"  news '{q}' failed: {e}", file=sys.stderr); continue
        for it in root.iter("item"):
            if per_query <= 0: break
            title = unescape(it.findtext("title") or "")
            desc = re.sub("<[^>]+>", " ", unescape(it.findtext("description") or ""))
            yield {"key": "news:" + (it.findtext("link") or title)[:300], "name": None, "birth_year": None,
                   "text": f"{title}. {desc}", "website": None, "country": None,
                   "source": "google_news", "source_url": it.findtext("link")}
        time.sleep(1)

# ---------------------------------------------------------------- Scoring (free LLM)
LLM_PROMPT = """You screen candidates for a talent scouting list of people who did extraordinary
things at a young age (tech/hacking preferred, also art, science, sport, business).
Current year: {year}. From the text, return ONLY a JSON object:
{{"name": str|null, "birth_year": int|null, "age_now": int|null, "achievement": str (one sentence),
  "age_at_achievement": int|null, "category": "tech"|"science"|"art"|"business"|"sport"|"other",
  "score": int 1-10 (10 = globally exceptional, 1 = ordinary/not a person/irrelevant)}}
Text: {text}"""

# Free models, best first. Each is used until its free quota runs out, then the next one.
# A provider is skipped when its API key env var is missing. Override with LLM_CHAIN (JSON list).
DEFAULT_CHAIN = [
    {"name": "gemini-pro",   "base": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-pro",   "key_env": "GEMINI_API_KEY"},
    {"name": "gemini-flash", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash", "key_env": "GEMINI_API_KEY"},
    {"name": "groq-llama",   "base": "https://api.groq.com/openai/v1",  "model": "llama-3.3-70b-versatile",           "key_env": "GROQ_API_KEY"},
    {"name": "cerebras",     "base": "https://api.cerebras.ai/v1",      "model": "llama-3.3-70b",                     "key_env": "CEREBRAS_API_KEY"},
    {"name": "openrouter-ds","base": "https://openrouter.ai/api/v1",    "model": "deepseek/deepseek-chat-v3-0324:free","key_env": "OPENROUTER_API_KEY"},
    {"name": "mistral",      "base": "https://api.mistral.ai/v1",       "model": "mistral-small-latest",              "key_env": "MISTRAL_API_KEY"},
]

def load_chain():
    chain = json.loads(os.environ["LLM_CHAIN"]) if os.environ.get("LLM_CHAIN") else DEFAULT_CHAIN
    if os.environ.get("LLM_BASE_URL"):  # single custom endpoint still supported
        chain = [{"name": "custom", "base": os.environ["LLM_BASE_URL"], "model": os.environ.get("LLM_MODEL"),
                  "key_env": "LLM_API_KEY"}] + chain
    return [p for p in chain if os.environ.get(p["key_env"]) or "localhost" in p["base"]]

CHAIN = load_chain()
EXHAUSTED = set()          # providers whose quota ran out during this run
USAGE = {}                 # provider -> successful calls (printed at the end)

def _is_quota_error(code, body):
    b = body.lower()
    return code in (402, 403) or (code == 429 and any(w in b for w in
           ("quota", "daily", "per day", "exhausted", "limit exceeded", "credits", "rpd", "tokens per day")))

def llm_score(item):
    prompt = LLM_PROMPT.format(year=THIS_YEAR, text=item["text"][:2000])
    for p in CHAIN:
        if p["name"] in EXHAUSTED: continue
        payload = {"model": p["model"], "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
        headers = {"Authorization": f"Bearer {os.environ.get(p['key_env'], '')}"}
        for attempt in range(3):
            try:
                r = post_json(p["base"].rstrip("/") + "/chat/completions", payload, headers)
                out = r["choices"][0]["message"]["content"]
                USAGE[p["name"]] = USAGE.get(p["name"], 0) + 1
                return json.loads(re.search(r"\{.*\}", out, re.S).group(0))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
                if _is_quota_error(e.code, body):
                    print(f"  {p['name']}: free quota used up, switching to next model", file=sys.stderr)
                    EXHAUSTED.add(p["name"]); break
                if e.code == 429:  # per-minute limit: wait and retry same model
                    time.sleep(15 * (attempt + 1)); continue
                print(f"  {p['name']}: HTTP {e.code}, trying next model", file=sys.stderr)
                EXHAUSTED.add(p["name"]); break
            except Exception as e:  # bad JSON from model: try next model for this item only
                print(f"  {p['name']}: unusable answer ({e})", file=sys.stderr); break
        else:
            continue  # per-minute retries spent; move on for this item
    return keyword_score(item)

KEYWORDS = {"hack": 3, "youngest": 3, "prodigy": 3, "olympiad": 3, "gold medal": 2, "founder": 2,
            "invent": 2, "award": 1, "record": 1, "bug bounty": 3, "vulnerab": 2, "scholarship": 1,
            "thiel": 3, "forbes": 2, "30 under 30": 2, "patent": 2, "exhibit": 1, "album": 1}

def keyword_score(item):
    t = item["text"].lower()
    s = 2 + sum(w for k, w in KEYWORDS.items() if k in t)
    m = re.search(r"(\d{1,2})[- ]year[- ]old", t)
    age_at = int(m.group(1)) if m else None
    cat = "tech" if re.search(r"hack|program|software|computer|ai\b|cyber|bug", t) else \
          "art" if re.search(r"artist|paint|music|singer|actor|writer|album", t) else \
          "science" if re.search(r"scien|olympiad|mathemat|physic|research", t) else "other"
    return {"name": item.get("name"), "birth_year": item.get("birth_year"), "age_now": None,
            "achievement": item["text"][:200], "age_at_achievement": age_at,
            "category": cat, "score": min(s, 10)}

# ---------------------------------------------------------------- Contact lookup
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD = ("example.", "sentry", "wixpress", ".png", ".jpg", "noreply", "no-reply", "@2x")

def find_contact(website):
    """Look for an email published on the person's own site (home + contact pages)."""
    if not website: return None, None
    pages = [website] + [urllib.parse.urljoin(website, p) for p in ("contact", "about", "contact/", "about/")]
    for u in pages:
        try: html = get(u, timeout=15)
        except Exception: continue
        found = [e for e in set(EMAIL_RE.findall(html.replace("[at]", "@").replace("(at)", "@")))
                 if not any(b in e.lower() for b in BAD)]
        if found:
            pro = [e for e in found if re.match(r"(contact|press|booking|info|hello|management|media)@", e.lower())]
            return (pro or found)[0], ("public_business" if pro else "public_on_own_site")
    return None, None

# ---------------------------------------------------------------- Storage backends
COLS = ["key", "name", "birth_year", "age", "age_band", "achievement", "age_at_achievement", "category",
        "score", "country", "website", "contact_email", "contact_type", "source", "source_url", "found_at"]

class SupabaseStore:
    """Online storage through Supabase's REST API (tables created by schema.sql)."""
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.h = {"apikey": key}
        if key.startswith("eyJ"):  # legacy service_role JWT also goes in Authorization
            self.h["Authorization"] = f"Bearer {key}"
    def _req(self, method, path, body=None, extra=None):
        req = urllib.request.Request(self.base + path, method=method,
              data=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json", "User-Agent": UA, **self.h, **(extra or {})})
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode(); return json.loads(t) if t else None
    def seen_keys(self):
        keys, off = set(), 0
        while True:  # memory = everything ever screened, kept or not
            rows = self._req("GET", f"seen?select=key&limit=1000&offset={off}")
            keys |= {r["key"] for r in rows}
            if len(rows) < 1000: return keys
            off += 1000
    def mark_seen(self, keys):
        if keys: self._req("POST", "seen?on_conflict=key", [{"key": k} for k in keys],
                           {"Prefer": "resolution=ignore-duplicates"})
    def save(self, row):  # never overwrites status/notes you set in the UI
        self._req("POST", "people?on_conflict=key", [row], {"Prefer": "resolution=merge-duplicates"})
    def close(self): pass

class SqliteStore:
    """Local fallback when Supabase is not configured."""
    def __init__(self):
        self.con = db()
        self.con.execute("CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY)")
    def seen_keys(self): return {r[0] for r in self.con.execute("SELECT key FROM seen UNION SELECT key FROM people")}
    def mark_seen(self, keys): self.con.executemany("INSERT OR IGNORE INTO seen VALUES (?)", [(k,) for k in keys])
    def save(self, row):
        self.con.execute(f"INSERT OR REPLACE INTO people VALUES ({','.join('?'*len(COLS))})", [row[c] for c in COLS])
    def close(self): self.con.commit(); export(self.con)

def get_store():
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        print("Storage: Supabase"); return SupabaseStore(url, key)
    print("Storage: local scout.db"); return SqliteStore()

# ---------------------------------------------------------------- Main
def run(args):
    store = get_store()
    seen = store.seen_keys()
    items = []
    if "wikidata" in args.sources:
        print("Querying Wikidata...")
        try: items += list(src_wikidata(args.min_age, args.max_age, args.limit))
        except Exception as e: print(f"  wikidata failed: {e}", file=sys.stderr)
    if "news" in args.sources:
        print("Reading news RSS...")
        items += list(src_news(NEWS_QUERIES, args.per_query))
    uniq = {}
    for it in items:
        if it["key"] not in seen: uniq.setdefault(it["key"], it)
    items = list(uniq.values())[: args.max_new]
    print(f"{len(items)} new candidates to score (memory: {len(seen)} already screened)")

    kept, batch = 0, []
    for i, it in enumerate(items, 1):
        s = llm_score(it)
        batch.append(it["key"])
        name = s.get("name") or it.get("name")
        by = it.get("birth_year") or s.get("birth_year")
        age = (THIS_YEAR - by) if by else s.get("age_now")
        try: score = int(s.get("score") or 0)
        except (TypeError, ValueError): score = 0
        if name and score >= args.min_score:
            band = age_band(age)
            email, ctype = None, None
            if band in ("18-20", "adult"):
                email, ctype = find_contact(it.get("website"))
            elif band == "minor":
                ctype = "minor: contact via guardian/school/organizer only"
            store.save(dict(zip(COLS, (it["key"], name, by, age, band, s.get("achievement"),
                s.get("age_at_achievement"), s.get("category"), score, it.get("country"), it.get("website"),
                email, ctype, it["source"], it["source_url"],
                dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))))
            kept += 1
        if i % 10 == 0:
            store.mark_seen(batch); batch = []
            print(f"  {i}/{len(items)} scored, {kept} kept")
        if CHAIN: time.sleep(args.llm_delay)
    store.mark_seen(batch)
    store.close()
    print(f"Done. {kept} people kept. LLM usage: {USAGE or 'none (keyword mode)'}.")

def export(con, path="people.csv"):
    cur = con.execute("SELECT * FROM people ORDER BY score DESC, age ASC")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow([d[0] for d in cur.description]); w.writerows(cur)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="+", default=["wikidata", "news"])
    ap.add_argument("--min-age", type=int, default=14)
    ap.add_argument("--max-age", type=int, default=35, help="35 keeps 'older but did it young' people")
    ap.add_argument("--limit", type=int, default=300, help="max Wikidata rows")
    ap.add_argument("--per-query", type=int, default=15, help="news items per query")
    ap.add_argument("--min-score", type=int, default=6)
    ap.add_argument("--max-new", type=int, default=200, help="max new candidates per run (protects free quotas)")
    ap.add_argument("--llm-delay", type=float, default=2.0, help="seconds between LLM calls (free-tier rate limits)")
    run(ap.parse_args())
