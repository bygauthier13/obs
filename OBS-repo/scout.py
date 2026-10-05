"""soundscout v1 - autonomous scan: industry in, evidence-checked product report out.
Usage: python scout.py "podcasters" --since 2025-01-01 --max-tokens 1500000
Env: ANTHROPIC_API_KEY, EXA_API_KEY, MODEL_FAST, MODEL_STRONG (optional), SCOUT_MOCK=1 for offline test.
"""
import os, sys, json, re, time, argparse, datetime, concurrent.futures as cf
import urllib.request, urllib.error
import ss

MOCK = os.environ.get("SCOUT_MOCK") == "1"
MODEL_FAST = os.environ.get("MODEL_FAST", "")
MODEL_STRONG = os.environ.get("MODEL_STRONG") or MODEL_FAST
TODAY = str(datetime.date.today())

TAXONOMY = ("sound design / sample or SFX packs, music or jingle production as a service, plugins and scripts, small SaaS or web tools, "
            "audio analysis or diagnostics, attention/retention/focus audio, sonification, licensing or catalog products, courses and guides, "
            "data products. Exclude hardware. Think wild, but every idea must trace to evidence.")

class Budget:
    def __init__(self, limit): self.limit, self.used, self.calls, self.searches = limit, 0, 0, 0
    def ok(self): return self.used < self.limit
B = Budget(1_500_000)

# ---------- API wrappers (stdlib only, no dependencies) ----------
def post_json(url, headers, body, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode())
        except Exception: return e.code, {}
    except (urllib.error.URLError, TimeoutError): return 599, {}

def llm(stage, system, user, model=None, max_tokens=4000):
    if MOCK: return MOCKS[stage](user)
    if not B.ok(): raise RuntimeError("token budget exhausted")
    key = os.environ["ANTHROPIC_API_KEY"]; model = model or MODEL_FAST
    if not model: raise RuntimeError("MODEL_FAST is not set")
    for attempt in range(4):
        code, d = post_json("https://api.anthropic.com/v1/messages",
                       {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                       {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}, timeout=180)
        if code in (429, 500, 529, 599): time.sleep(2 ** attempt * 3); continue
        if code != 200: raise RuntimeError(f"Anthropic API error {code}: {str(d)[:300]}")
        B.used += d["usage"]["input_tokens"] + d["usage"]["output_tokens"]; B.calls += 1
        return "".join(b.get("text", "") for b in d["content"])
    raise RuntimeError("LLM API kept failing")

def jparse(text):
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try: return json.loads(t)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", t, re.S)
        if m: return json.loads(m.group(1))
        raise

def llm_json(stage, system, user, **kw):
    for _ in range(2):
        try: return jparse(llm(stage, system, user, **kw))
        except (ValueError, json.JSONDecodeError): user += "\n\nReturn ONLY valid JSON, nothing else."
    return None

def exa(query, n=10, since=None, chars=6000):
    B.searches += 1
    if MOCK: return MOCKS["exa"](query)
    body = {"query": query, "numResults": n, "type": "auto", "contents": {"text": {"maxCharacters": chars}}}
    if since: body["startPublishedDate"] = since + "T00:00:00.000Z"
    for attempt in range(3):
        code, d = post_json("https://api.exa.ai/search", {"x-api-key": os.environ["EXA_API_KEY"], "content-type": "application/json"}, body, timeout=90)
        if code in (429, 500, 503, 599): time.sleep(2 ** attempt * 3); continue
        if code != 200: raise RuntimeError(f"Exa API error {code}: {str(d)[:300]}")
        return d.get("results", [])
    return []

# ---------- store helpers ----------
def store_docs(c, results, tag, retrieved=False):
    new = 0
    for x in results:
        url, text = x.get("url"), (x.get("text") or "")
        if not url or len(text) < 200: continue
        pub = (x.get("publishedDate") or "")[:10] or None
        if retrieved and not pub: date, basis = TODAY, "retrieved (live page)"
        elif pub: date, basis = pub, "metadata"
        else: date, basis = ss.infer_date(url, "N/A")
        inw = 1 if (date and date >= SINCE) or (retrieved and basis.startswith("retrieved")) else 0
        if c.execute("SELECT 1 FROM docs WHERE url=?", (url,)).fetchone(): continue
        c.execute("INSERT INTO docs(url,title,published,date_basis,author,source_type,text,tag,in_window) VALUES(?,?,?,?,?,?,?,?,?)",
                  (url, x.get("title") or url, date, basis, x.get("author") or "", ss.source_type(url), text, tag, inw)); new += 1
    c.commit(); return new

def quote_ok(c, url, quote):
    d = c.execute("SELECT text, in_window FROM docs WHERE url=?", (url,)).fetchone()
    if not d: return "doc not in store"
    if not d["in_window"]: return "outside window or undated"
    if len(quote) < 15: return "quote too short"
    return None if ss.norm(quote) in ss.norm(d["text"]) else "quote not verbatim"

# ---------- stages ----------
SYS = "You are a rigorous market-research analyst. Never invent facts. Output only JSON when asked."

def stage_plan(industry, n_queries):
    j = llm_json("plan", SYS, f"""Industry to scan: {industry}
Goal: find painkiller or vitamin product opportunities involving music/sound that a solo music producer could build as a quick MVP.
Product space to consider: {TAXONOMY}
Write {n_queries} web search queries (natural-language descriptions of ideal pages) that would surface: first-person complaints and workarounds, what people pay for today, recent research papers, launches and discussion threads, and the tools incumbents sell. Mix purposes.
Return JSON: {{"queries":[{{"q":"...","purpose":"pain|workaround|price|research|launch"}}]}}""", model=MODEL_STRONG)
    return (j or {}).get("queries", [])[:n_queries]

def extract_one(industry, d):
    j = llm_json("extract", SYS, f"""Industry: {industry}
Document title: {d['title']}
URL: {d['url']}
TEXT:
{d['text'][:6000]}

Extract up to 4 items that bear on real problems, workarounds, spending, prices, research findings or competing products in this industry. Each item must have a VERBATIM quote copied exactly from the TEXT (15 to 300 characters, no paraphrase, no ellipsis). Skip anything generic.
Return JSON: {{"items":[{{"quote":"...","role":"who is speaking (e.g. freelance editor, vendor, researcher)","first_person":true|false,"vendor_interest":true|false,"context":"what it shows, one sentence","workaround":"what they do about it or empty","kind":"pain|workaround|price|research|competitor"}}]}}""")
    return d["url"], (j or {}).get("items", [])

def stage_extract(c, industry, max_docs, workers=6):
    docs = [dict(r) for r in c.execute("SELECT url,title,text FROM docs WHERE in_window=1 ORDER BY LENGTH(text) DESC LIMIT ?", (max_docs,))]
    n = c.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]; ok = bad = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        for url, items in ex.map(lambda d: extract_one(industry, d), docs):
            for it in items:
                q = (it.get("quote") or "").strip()
                reason = quote_ok(c, url, q); n += 1
                role = (it.get("role") or "?") + (" (témoignage direct)" if it.get("first_person") else " (tiers)") + (", intérêt commercial" if it.get("vendor_interest") else "")
                c.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"E{n:03d}", url, q, role, it.get("context"), it.get("workaround"), None, None, it.get("kind", "pain"), 0 if reason else 1, reason))
                ok += reason is None; bad += reason is not None
            if not B.ok(): break
    c.commit(); return ok, bad

def stage_ideate(c, industry):
    ev = c.execute("SELECT id, kind, role, quote, context FROM evidence WHERE verified=1 LIMIT 150").fetchall()
    listing = "\n".join(f"[{e['id']}] ({e['kind']}; {e['role']}) \"{e['quote'][:220]}\" => {e['context']}" for e in ev)
    j = llm_json("ideate", SYS, f"""Industry: {industry}
Verified evidence (each line is a real, dated, verbatim quote):
{listing}

Propose 10 to 14 product concepts a solo music producer with Claude Code could ship as an MVP in days and send to customers to test. Product space: {TAXONOMY}
Rules: cite only evidence ids from the list above (at least one each, prefer several distinct sources); do not state any fact not in the evidence; do not mention competitors or prices yet (a later pass does that). Write text fields in French.
Return JSON: {{"concepts":[{{"name":"","ptype":"","pitch":"","buyer":"","mvp_days":0,"mvp_basis":"short, say it is an estimate","test_plan":"how to put it in front of 10 customers and what to measure","evidence_ids":["E001"]}}]}}""", model=MODEL_STRONG, max_tokens=7000)
    cons = (j or {}).get("concepts", [])
    valid = {e["id"] for e in ev}
    for i, k in enumerate(cons, 1):
        k["id"] = f"C{i:02d}"; k["evidence_ids"] = [x for x in k.get("evidence_ids", []) if x in valid]
    return [k for k in cons if k["evidence_ids"]]

def stage_competition(c, industry, k):
    qs = llm_json("cq", SYS, f"""Concept: {k['name']} - {k['pitch']}
Industry: {industry}
Write 3 web search queries that would find (1) existing products or services that already do this, with their pricing pages, (2) products that tried and failed or complaints about them, (3) what customers currently pay for the closest alternative.
Return JSON: {{"queries":["..."]}}""") or {}
    urls = []
    for q in qs.get("queries", [])[:3]:
        res = exa(q, n=6)
        store_docs(c, res, "competition", retrieved=True)
        urls += [x["url"] for x in res if x.get("url")]
    docs = [dict(r) for r in c.execute(f"SELECT url,title,text FROM docs WHERE url IN ({','.join('?' * len(urls)) or 'NULL'}) AND in_window=1", urls)][:12]
    if not docs: return
    body = "\n\n".join(f"### {d['url']}\n{d['text'][:3500]}" for d in docs)
    j = llm_json("comp", SYS, f"""Concept: {k['name']} - {k['pitch']}
Pages:
{body}

List direct or near competitors found in these pages. For each, give a VERBATIM price quote from the page text if one exists (otherwise empty string). Never guess a price. Rate threat HAUTE/MOYENNE/BASSE for this concept.
Then give a possible price range for the concept ONLY if it can be anchored on the verified competitor prices; say in price_basis whether it is sourced or INFÉRÉ.
Write weakness/what/price_basis in French.
Return JSON: {{"competitors":[{{"name":"","url":"page url from above","what":"","price_text":"","price_quote":"verbatim or empty","price_doc_url":"page url or empty","weakness":"","threat":"HAUTE|MOYENNE|BASSE"}}],"price_low":null,"price_high":null,"price_basis":""}}""", model=MODEL_STRONG, max_tokens=3500) or {}
    comps = []
    for r in j.get("competitors", []):
        pq, pu = (r.get("price_quote") or "").strip(), (r.get("price_doc_url") or "").strip()
        if pq and pu and quote_ok(c, pu, pq) is None: r["price_text"] = r.get("price_text") or pq
        else: r["price_text"], r["price_doc_url"] = ("prix non vérifié" if r.get("price_quote") else (r.get("price_text") or "prix non trouvé")), ""
        comps.append(r)
    k["competitors"] = comps
    for f in ("price_low", "price_high", "price_basis"): k[f] = j.get(f)

def stage_judge(c, industry, k):
    cs = "\n".join(f"- {r['name']} ({r.get('threat')}): {r.get('what')} | {r.get('price_text')}" for r in k.get("competitors", []))
    ev = "\n".join(f"[{i}] {c.execute('SELECT quote FROM evidence WHERE id=?', (i,)).fetchone()[0][:160]}" for i in k["evidence_ids"][:8])
    j = llm_json("judge", SYS, f"""Concept: {k['name']} - {k['pitch']}
Evidence:
{ev}
Competitors found:
{cs or 'none found'}
Verdict rules: killed = free or cheap alternatives already served by 3+ strong competitors, or an unavoidable blocker; thin = evidence from fewer than 2 independent sources, only vendor-interest sources, or causal link unproven; candidate = real pain from 2+ independent sources AND a plausible gap versus competitors. Be blunt; no positive spin. Write kill_reason in French and say exactly what remains doubtful even for a candidate.
Return JSON: {{"verdict":"candidate|thin|killed","kill_reason":""}}""", model=MODEL_STRONG, max_tokens=800) or {}
    v = j.get("verdict", "thin"); v = v if v in ("candidate", "thin", "killed") else "thin"
    docs = {c.execute("SELECT doc_url FROM evidence WHERE id=?", (i,)).fetchone()[0] for i in k["evidence_ids"]}
    notes = []
    if v == "candidate" and len(docs) < 2: v = "thin"; notes.append("rétrogradé par le code : moins de 2 sources indépendantes")
    if v == "candidate" and not any(r.get("price_doc_url") for r in k.get("competitors", [])): v = "thin"; notes.append("rétrogradé par le code : aucun prix de concurrent vérifié")
    k["verdict"], k["kill_reason"], k["notes"] = v, j.get("kill_reason", ""), "; ".join(notes)

# ---------- main ----------
def run(industry, since, n_queries, max_docs, outdir):
    global SINCE; SINCE = since
    os.makedirs(outdir, exist_ok=True)
    ss.DB = os.path.join(outdir, "scout.db"); ss.WINDOW_START = since
    c = ss.conn()
    print("1/6 plan queries"); qs = stage_plan(industry, n_queries)
    print(f"2/6 search ({len(qs)} queries)")
    for q in qs:
        n = store_docs(c, exa(q["q"], n=10, since=since), q.get("purpose", "x")); print("  +", n, "docs |", q["q"][:80])
    print("3/6 extract evidence"); ok, bad = stage_extract(c, industry, max_docs); print(f"  verified {ok}, rejected {bad}")
    print("4/6 ideate"); cons = stage_ideate(c, industry); print(f"  {len(cons)} concepts")
    print("5/6 competition + judge")
    for k in cons:
        if not B.ok(): k["verdict"], k["kill_reason"] = "thin", "budget épuisé avant la passe concurrence"; k.setdefault("competitors", []); continue
        stage_competition(c, industry, k); stage_judge(c, industry, k); print("  ", k["id"], k["verdict"], k["name"][:60])
    p = os.path.join(outdir, "concepts.json"); json.dump(cons, open(p, "w"), ensure_ascii=False, indent=1)
    ss.cmd_concepts(p)
    vend = c.execute("SELECT COUNT(*) FROM evidence WHERE verified=1 AND role LIKE '%intérêt commercial%'").fetchone()[0]
    fp = c.execute("SELECT COUNT(*) FROM evidence WHERE verified=1 AND role LIKE '%témoignage direct%'").fetchone()[0]
    tot = c.execute("SELECT COUNT(*) FROM evidence WHERE verified=1").fetchone()[0]
    for k, v in (("searches", B.searches), ("tokens", B.used)): c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    c.commit()
    w = os.path.join(outdir, "warnings.md"); os.environ["SCOUT_WARNINGS"] = w
    open(w, "w").write(f"""- Sur {tot} preuves vérifiées, {fp} sont des témoignages directs et {vend} viennent de sources avec un intérêt commercial (vendeurs, agences). Le classement est fait par un modèle de langage et peut se tromper.
- Reddit, LinkedIn et les communautés privées ne sont pas lus : la douleur exprimée là-bas est absente de ce rapport.
- Les prix « INFÉRÉS » sont calculés entre prix sourcés, pas observés. Un prix « non vérifié » veut dire que la citation de prix n'a pas été retrouvée mot pour mot.
- Les verdicts sont des hypothèses de tri. Seul un test client prouve la demande.
- Budget : {B.used} tokens utilisés sur {B.limit} autorisés ({B.calls} appels).""")
    rp = os.path.join(outdir, "rapport.md"); ss.cmd_report(rp, industry); print("report:", rp)

# ---------- offline mocks ----------
SAMPLE = "Freelance editors say they lose 45 minutes per video searching sound libraries and still worry about licensing. One editor wrote: I just want a whoosh that fits and a license I can trust. Epidemic Sound costs about $15 a month for the personal plan." + " filler" * 60
MOCKS = {
 "plan": lambda u: '{"queries":[{"q":"editors complaints sound library search time","purpose":"pain"}]}',
 "exa": lambda q: [{"title": "Mock forum thread", "url": "https://example.com/thread-" + str(abs(hash(q)) % 999), "publishedDate": "2026-03-01T00:00:00.000Z", "text": SAMPLE}],
 "extract": lambda u: '{"items":[{"quote":"I just want a whoosh that fits and a license I can trust.","role":"freelance editor","first_person":true,"vendor_interest":false,"context":"time sink and licensing worry","workaround":"","kind":"pain"},{"quote":"this sentence is invented and not in the text","role":"x","first_person":false,"vendor_interest":true,"context":"x","workaround":"","kind":"pain"}]}',
 "ideate": lambda u: '{"concepts":[{"name":"Mock concept","ptype":"Pack","pitch":"Pitch","buyer":"Editors","mvp_days":2,"mvp_basis":"estimation","test_plan":"10 DMs","evidence_ids":["E001","E999"]}]}',
 "cq": lambda u: '{"queries":["mock competitor query"]}',
 "comp": lambda u: '{"competitors":[{"name":"Epidemic Sound","url":"https://example.com/x","what":"library","price_text":"","price_quote":"Epidemic Sound costs about $15 a month for the personal plan.","price_doc_url":"URLPLACEHOLDER","weakness":"lock-in","threat":"HAUTE"}],"price_low":9,"price_high":19,"price_basis":"INFÉRÉ"}',
 "judge": lambda u: '{"verdict":"candidate","kill_reason":"Mock: doute sur la demande."}',
}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("industry"); ap.add_argument("--since", default="2025-01-01")
    ap.add_argument("--queries", type=int, default=14); ap.add_argument("--max-docs", type=int, default=70)
    ap.add_argument("--max-tokens", type=int, default=1_500_000); ap.add_argument("--out", default=None)
    a = ap.parse_args(); B.limit = a.max_tokens
    out = a.out or os.path.join("runs", re.sub(r"[^a-z0-9]+", "-", a.industry.lower()).strip("-")[:40] + "-" + TODAY)
    if MOCK:  # make the mock competitor price point at a real mock doc
        _orig = MOCKS["comp"]; MOCKS["comp"] = lambda u: _orig(u).replace("URLPLACEHOLDER", re.findall(r"### (\S+)", u)[0])
    run(a.industry, a.since, a.queries, a.max_docs, out)
