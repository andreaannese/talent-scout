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

UA = "TalentScout/0.3 (+https://github.com/andreaannese/talent-scout) python-urllib"
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
# One small query per occupation (one big query times out on the free public endpoint).
OCCUPATIONS = {"Q1622272": "hacker", "Q5482740": "programmer", "Q82594": "computer scientist",
               "Q205375": "inventor", "Q131524": "entrepreneur", "Q170790": "mathematician",
               "Q901": "scientist", "Q483501": "artist", "Q1028181": "painter", "Q639669": "musician",
               "Q177220": "singer", "Q36180": "writer", "Q33999": "actor", "Q2066131": "athlete"}
WIKIDATA_QUERY = """SELECT ?p ?pLabel ?birth ?desc ?site ?countryLabel WHERE {
  ?p wdt:P106 wd:%(occ)s; wdt:P569 ?birth.
  FILTER(?birth >= "%(min_year)d-01-01T00:00:00Z"^^xsd:dateTime && ?birth <= "%(max_year)d-12-31T00:00:00Z"^^xsd:dateTime)
  OPTIONAL { ?p wdt:P856 ?site. }
  OPTIONAL { ?p wdt:P27 ?country. }
  OPTIONAL { ?p schema:description ?desc FILTER(LANG(?desc)="en") }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} LIMIT %(limit)d"""

WIKIDATA_PAUSE = int(os.environ.get("WIKIDATA_PAUSE", "62"))  # public endpoint: max 1 query/min right now

def src_wikidata(min_age, max_age, limit):
    per = max(10, limit // len(OCCUPATIONS))
    done = set()
    for occ, label in OCCUPATIONS.items():
        q = WIKIDATA_QUERY % {"occ": occ, "min_year": THIS_YEAR - max_age, "max_year": THIS_YEAR - min_age, "limit": per}
        url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q)
        for attempt in range(2):
            try:
                data = json.loads(get(url, timeout=70, headers={"Accept": "application/sparql-results+json"})); break
            except Exception as e:
                print(f"  wikidata {label} failed ({e}){', retrying' if attempt == 0 else ''}", file=sys.stderr)
                data = None; time.sleep(WIKIDATA_PAUSE)
        if not data: continue
        n = 0
        for b in data["results"]["bindings"]:
            v = lambda k: b.get(k, {}).get("value")
            qid = v("p").rsplit("/", 1)[-1]
            if qid in done or not v("pLabel") or v("pLabel") == qid: continue
            done.add(qid); n += 1
            by = int(v("birth")[:4]) if v("birth") and v("birth")[0].isdigit() else None
            yield {"key": "wd:" + qid, "name": v("pLabel"), "birth_year": by,
                   "text": f"{v('pLabel')} ({label}, born {by}): {v('desc') or ''}",
                   "website": v("site"), "country": v("countryLabel"),
                   "source": "wikidata", "source_url": v("p")}
        print(f"  wikidata {label}: {n}")
        time.sleep(WIKIDATA_PAUSE)

# ---------------------------------------------------------------- Source 1b: Wikipedia search
# Uses the normal MediaWiki API (separate, more generous limits than the Wikidata query service).
WIKI_TOPICS = ["hacker", "programmer", "prodigy", "inventor", "founder", "entrepreneur", "olympiad",
               "mathematician", "scientist", "artist", "painter", "musician", "composer", "chess", "youngest"]

def src_wikipedia(min_age, max_age, per_query):
    api = "https://en.wikipedia.org/w/api.php"
    done = set()
    for year in range(THIS_YEAR - max_age, THIS_YEAR - min_age + 1):
        for topic in WIKI_TOPICS:
            params = {"action": "query", "format": "json", "list": "search", "srlimit": per_query,
                      "srprop": "snippet", "srsearch": f'incategory:"{year} births" {topic}'}
            try:
                data = json.loads(get(api + "?" + urllib.parse.urlencode(params), timeout=30))
            except Exception as e:
                print(f"  wikipedia {year} {topic} failed: {e}", file=sys.stderr); time.sleep(5); continue
            for r in data.get("query", {}).get("search", []):
                if r["pageid"] in done: continue
                done.add(r["pageid"])
                snippet = re.sub("<[^>]+>", "", unescape(r.get("snippet", "")))
                yield {"key": f"wp:{r['pageid']}", "name": r["title"], "birth_year": year,
                       "text": f"{r['title']} (born {year}, matched '{topic}'): {snippet}",
                       "website": None, "country": None, "source": "wikipedia",
                       "source_url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(r["title"].replace(" ", "_"))}
            time.sleep(0.5)
    print(f"  wikipedia: {len(done)} people")

# ---------------------------------------------------------------- Source 2: News RSS
NEWS_QUERIES = [
    '"year-old hacker"', '"teen hacker"', '"year-old inventor"', '"teen founder"',
    '"youngest ever" prodigy', '"year-old" startup founder', '"child prodigy" artist',
    '"year-old" scientist award', '"teen" "olympiad" gold', '"year-old" bug bounty',
]

def _rss_items(xml_text, per_query, source):
    root = ET.fromstring(xml_text)
    for it in list(root.iter("item"))[:per_query]:
        title = unescape(it.findtext("title") or "")
        desc = re.sub("<[^>]+>", " ", unescape(it.findtext("description") or ""))
        link = it.findtext("link") or title
        yield {"key": "news:" + link[:300], "name": None, "birth_year": None, "text": f"{title}. {desc}",
               "website": None, "country": None, "source": source, "source_url": link}

def news_gdelt(q, n):
    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(
        {"query": q + " sourcelang:english", "mode": "artlist", "format": "json", "maxrecords": n, "timespan": "12m"})
    for a in json.loads(get(url, timeout=40) or "{}").get("articles", []):
        yield {"key": "news:" + a["url"][:300], "name": None, "birth_year": None, "text": a.get("title", ""),
               "website": None, "country": a.get("sourcecountry"), "source": "gdelt", "source_url": a["url"]}

def news_bing(q, n):
    return _rss_items(get("https://www.bing.com/news/search?format=rss&q=" + urllib.parse.quote(q)), n, "bing_news")

def news_google(q, n):
    return _rss_items(get("https://news.google.com/rss/search?hl=en&gl=US&ceid=US:en&q=" + urllib.parse.quote(q)), n, "google_news")

def src_news(queries, per_query):
    for q in queries:
        for name, fn in (("gdelt", news_gdelt), ("bing", news_bing), ("google", news_google)):
            try:
                got = list(fn(q, per_query))
            except Exception as e:
                print(f"  news {name} '{q}' failed: {e}", file=sys.stderr); time.sleep(3); continue
            if got:
                print(f"  news {name} '{q}': {len(got)}"); yield from got; break
        time.sleep(6)  # GDELT asks for max ~1 request / 5 s

# ---------------------------------------------------------------- Scoring (free LLM)
LLM_PROMPT = """You screen candidates for a talent scouting list of people who did extraordinary
things at a young age (tech/hacking preferred, also art, science, sport, business).
Current year: {year}. From the text, return ONLY a JSON object:
{{"name": str|null, "birth_year": int|null, "age_now": int|null, "achievement": str (one sentence),
  "age_at_achievement": int|null, "category": "tech"|"science"|"art"|"business"|"sport"|"other",
  "score": int 1-10 (10 = globally exceptional, 1 = ordinary/not a person/irrelevant)}}
Text: {text}"""

# Free models, best first. Each is used until its free quota runs out, then the next one.
# Model names change often, so each provider is asked which models it offers right now and the
# best match for "prefer" (first pattern that matches, newest version) is picked automatically.
# "model" is only a fallback if the provider's model list can't be read.
# A provider is skipped when its API key env var is missing. Override with LLM_CHAIN (JSON list).
DEFAULT_CHAIN = [
    {"name": "gemini-pro",   "base": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY",
     "prefer": [r"^gemini-[\d.]+-pro$", r"^gemini-[\d.]+-pro-latest$", r"^gemini-pro-latest$"], "model": "gemini-2.5-pro"},
    {"name": "gemini-flash", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY",
     "prefer": [r"^gemini-[\d.]+-flash$", r"^gemini-flash-latest$", r"^gemini-[\d.]+-flash-lite$"], "model": "gemini-2.5-flash"},
    {"name": "groq",         "base": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
     "prefer": [r"gpt-oss-120b", r"llama-4-maverick", r"kimi-k2", r"llama-3\.3-70b", r"qwen", r"llama"], "model": "llama-3.3-70b-versatile"},
    {"name": "cerebras",     "base": "https://api.cerebras.ai/v1", "key_env": "CEREBRAS_API_KEY",
     "prefer": [r"gpt-oss-120b", r"qwen-3-235b", r"llama-?4", r"llama"], "model": "llama-3.3-70b"},
    {"name": "openrouter",   "base": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
     "prefer": [r"deepseek.*:free$", r"qwen3.*:free$", r"llama-4.*:free$", r"gpt-oss.*:free$", r":free$"], "model": "deepseek/deepseek-chat-v3-0324:free"},
    {"name": "mistral",      "base": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY",
     "prefer": [r"^mistral-medium-latest$", r"^mistral-small-latest$"], "model": "mistral-small-latest"},
]

def _version_key(model_id):
    return [int(n) for n in re.findall(r"\d+", model_id)]

def resolve_model(p):
    """Ask the provider for its current models and pick the best one for this slot."""
    if p.get("_resolved"): return p["model"]
    p["_resolved"] = True
    try:
        req = urllib.request.Request(p["base"].rstrip("/") + "/models",
              headers={"Authorization": f"Bearer {os.environ.get(p['key_env'], '')}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            ids = [m["id"].removeprefix("models/") for m in json.loads(r.read()).get("data", [])]
        bad = re.compile(r"preview|exp|tts|audio|image|vision|embed|guard|whisper|search|live|thinking", re.I)
        for pat in p.get("prefer", []):
            hits = [i for i in ids if re.search(pat, i) and not bad.search(i)]
            if hits:
                p["model"] = max(hits, key=_version_key); break
    except Exception as e:
        print(f"  {p['name']}: could not list models ({e}), using {p['model']}", file=sys.stderr)
    print(f"  {p['name']}: using model {p['model']}")
    return p["model"]

def load_chain():
    chain = json.loads(os.environ["LLM_CHAIN"]) if os.environ.get("LLM_CHAIN") else DEFAULT_CHAIN
    if os.environ.get("LLM_BASE_URL"):  # single custom endpoint still supported
        chain = [{"name": "custom", "base": os.environ["LLM_BASE_URL"], "model": os.environ.get("LLM_MODEL"),
                  "key_env": "LLM_API_KEY", "_resolved": True}] + chain
    return [p for p in chain if os.environ.get(p["key_env"]) or "localhost" in p["base"]]

CHAIN = load_chain()
EXHAUSTED = set()          # providers whose quota ran out during this run
USAGE = {}                 # provider -> successful calls (printed at the end)
LAST_MODEL = [None]        # model that scored the latest item
KEPT = []                  # people kept this run, printed at the end

def _is_quota_error(code, body):
    b = body.lower()
    return code in (402, 403) or (code == 429 and any(w in b for w in
           ("quota", "daily", "per day", "exhausted", "limit exceeded", "credits", "rpd", "tokens per day")))

def llm_score(item):
    prompt = LLM_PROMPT.format(year=THIS_YEAR, text=item["text"][:2000])
    for p in CHAIN:
        if p["name"] in EXHAUSTED: continue
        payload = {"model": resolve_model(p), "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
        headers = {"Authorization": f"Bearer {os.environ.get(p['key_env'], '')}"}
        for attempt in range(3):
            try:
                r = post_json(p["base"].rstrip("/") + "/chat/completions", payload, headers)
                out = r["choices"][0]["message"]["content"]
                USAGE[p["name"]] = USAGE.get(p["name"], 0) + 1
                LAST_MODEL[0] = p["name"]
                return json.loads(re.search(r"\{.*\}", out, re.S).group(0))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
                if _is_quota_error(e.code, body):
                    print(f"  {p['name']}: free quota used up, switching to next model", file=sys.stderr)
                    EXHAUSTED.add(p["name"]); break
                if e.code == 429 or e.code >= 500:  # per-minute limit or overloaded: wait, retry same model
                    time.sleep(15 * (attempt + 1)); continue
                print(f"  {p['name']} ({p['model']}): HTTP {e.code} {body[:200]}, trying next model", file=sys.stderr)
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
        u = urllib.parse.urlparse(url.strip())          # keep only https://xxxx.supabase.co
        self.base = f"{u.scheme or 'https'}://{u.netloc or u.path.split('/')[0]}/rest/v1/"
        self.h = {"apikey": key}
        if key.startswith("eyJ"):  # legacy service_role JWT also goes in Authorization
            self.h["Authorization"] = f"Bearer {key}"
    def _req(self, method, path, body=None, extra=None):
        req = urllib.request.Request(self.base + path, method=method,
              data=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json", "User-Agent": UA, **self.h, **(extra or {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                t = r.read().decode(); return json.loads(t) if t else None
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Supabase {method} {path.split('?')[0]} failed: HTTP {e.code} {e.read().decode()[:500]}\n"
                             f"Check that SUPABASE_URL looks like https://xxxx.supabase.co (no /rest/v1, no dashboard link).")
    def seen_keys(self):
        keys, off = set(), 0
        while True:  # memory = everything ever screened, kept or not
            rows = self._req("GET", f"seen?select=key&scored_by=not.is.null&scored_by=neq.keyword&limit=1000&offset={off}")
            keys |= {r["key"] for r in rows}
            if len(rows) < 1000: return keys
            off += 1000
    def mark_seen(self, keys):
        if keys: self._req("POST", "seen?on_conflict=key", [{"key": k, "scored_by": m} for k, m in keys],
                           {"Prefer": "resolution=merge-duplicates"})
    def save(self, row):  # never overwrites status/notes you set in the UI
        self._req("POST", "people?on_conflict=key", [row], {"Prefer": "resolution=merge-duplicates"})
    def close(self): pass

class SqliteStore:
    """Local fallback when Supabase is not configured."""
    def __init__(self):
        self.con = db()
        self.con.execute("CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, scored_by TEXT)")
    def seen_keys(self): return {r[0] for r in self.con.execute("SELECT key FROM seen WHERE scored_by IS NOT NULL AND scored_by != 'keyword'")}
    def mark_seen(self, keys): self.con.executemany("INSERT OR REPLACE INTO seen VALUES (?,?)", keys)
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
    if "wikipedia" in args.sources:
        print("Searching Wikipedia...")
        items += list(src_wikipedia(args.min_age, args.max_age, args.wiki_per_query))
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
        before = sum(USAGE.values())
        s = llm_score(it)
        batch.append((it["key"], LAST_MODEL[0] if sum(USAGE.values()) > before else "keyword"))
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
            KEPT.append((score, name, age, (s.get("achievement") or "")[:110]))
        if i % 10 == 0:
            store.mark_seen(batch); batch = []
            print(f"  {i}/{len(items)} scored, {kept} kept")
        if CHAIN: time.sleep(args.llm_delay)
    store.mark_seen(batch)
    store.close()
    for sc, nm, ag, ach in sorted(KEPT, key=lambda k: -k[0]):
        print(f"  [{sc}] {nm} (age {ag if ag is not None else '?'}): {ach}")
    print(f"Done. {kept} people kept. LLM usage: {USAGE or 'none (keyword mode)'}.")

def export(con, path="people.csv"):
    cur = con.execute("SELECT * FROM people ORDER BY score DESC, age ASC")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow([d[0] for d in cur.description]); w.writerows(cur)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="+", default=["news", "wikipedia", "wikidata"])
    ap.add_argument("--wiki-per-query", type=int, default=5)
    ap.add_argument("--min-age", type=int, default=14)
    ap.add_argument("--max-age", type=int, default=35, help="35 keeps 'older but did it young' people")
    ap.add_argument("--limit", type=int, default=300, help="max Wikidata rows")
    ap.add_argument("--per-query", type=int, default=15, help="news items per query")
    ap.add_argument("--min-score", type=int, default=6)
    ap.add_argument("--max-new", type=int, default=200, help="max new candidates per run (protects free quotas)")
    ap.add_argument("--llm-delay", type=float, default=2.0, help="seconds between LLM calls (free-tier rate limits)")
    run(ap.parse_args())
