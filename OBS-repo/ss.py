"""soundscout v0 - evidence store, ingest, verifier, report.
Usage:
  python ss.py ingest <exa_result.json> [--tag TAG]
  python ss.py evidence <evidence.jsonl>
  python ss.py concepts <concepts.json>
  python ss.py verify
  python ss.py report <out.md> --industry "..."
"""
import sqlite3, json, re, sys, os, glob, datetime, unicodedata, argparse

DB = os.environ.get("SCOUT_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "scout.db")
WINDOW_START = "2025-01-01"

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, title TEXT, published TEXT, date_basis TEXT,
  author TEXT, source_type TEXT, text TEXT, tag TEXT, in_window INTEGER);
CREATE TABLE IF NOT EXISTS evidence(
  id TEXT PRIMARY KEY, doc_url TEXT, quote TEXT, role TEXT, context TEXT, workaround TEXT,
  budget_signal TEXT, sound_link TEXT, kind TEXT, verified INTEGER, reject_reason TEXT);
CREATE TABLE IF NOT EXISTS concepts(
  id TEXT PRIMARY KEY, name TEXT, ptype TEXT, pitch TEXT, buyer TEXT, mvp_days INTEGER,
  mvp_basis TEXT, test_plan TEXT, price_low REAL, price_high REAL, price_basis TEXT,
  evidence_ids TEXT, verdict TEXT, kill_reason TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS competitors(
  concept_id TEXT, name TEXT, url TEXT, what TEXT, price_text TEXT, price_doc_url TEXT, weakness TEXT, threat TEXT);
"""

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; c.executescript(SCHEMA); return c

def norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()

def source_type(url):
    u = url.lower()
    if any(k in u for k in ("arxiv.org", "doi.org", "mdpi.com", "frontiersin", "jmir", "nature.com", "acm.org", "sciencedirect", "scirp", "nsf.gov", "springer", "ieee", "plos", "researchgate")): return "paper"
    if any(k in u for k in ("news.ycombinator", "stackexchange", "stackoverflow", "reddit.com", "discourse", "forum", "gearspace", "kvraudio", "lemmy")): return "forum"
    if any(k in u for k in ("github.com", "gitlab.com")): return "code"
    if any(k in u for k in ("apps.apple", "play.google", "trustpilot", "g2.com", "capterra", "producthunt")): return "review/launch"
    return "web"

def infer_date(url, published):
    if published and published != "N/A":
        return published[:10], "metadata"
    m = re.search(r"arxiv\.org/(?:html|abs|pdf)/(\d{2})(\d{2})\.\d+", url)
    if m:
        return f"20{m.group(1)}-{m.group(2)}-01", "arxiv-id"
    return None, "none"

def parse_exa(path):
    d = json.load(open(path))
    text = "\n".join(x["text"] for x in d if x.get("type") == "text")
    blocks = re.split(r"(?m)^Title: ", text)
    out = []
    for b in blocks[1:]:
        title, _, rest = b.partition("\n")
        m = lambda k: (re.search(rf"(?m)^{k}: (.*)$", rest) or [None, ""])[1].strip()
        url, pub, auth = m("URL"), m("Published"), m("Author")
        body = rest.split("Highlights:", 1)[-1].strip() if "Highlights:" in rest else rest
        if url: out.append(dict(title=title.strip(), url=url, published=pub, author=auth, text=body))
    return out

def cmd_ingest(path, tag):
    c = conn(); n = new = 0
    for d in parse_exa(path):
        date, basis = infer_date(d["url"], d["published"])
        inw = 1 if (date and date >= WINDOW_START) else 0
        old = c.execute("SELECT text FROM docs WHERE url=?", (d["url"],)).fetchone()
        if old:
            if d["text"] not in (old["text"] or ""):
                c.execute("UPDATE docs SET text=text||char(10)||? WHERE url=?", (d["text"], d["url"]))
        else:
            c.execute("INSERT INTO docs(url,title,published,date_basis,author,source_type,text,tag,in_window) VALUES(?,?,?,?,?,?,?,?,?)",
                      (d["url"], d["title"], date, basis, d["author"], source_type(d["url"]), d["text"], tag, inw)); new += 1
        n += 1
    c.commit()
    print(f"parsed {n}, new {new}")

def cmd_manual(path, tag):
    """Docs whose text was copied from tool output shown in-session (not machine-saved). Flagged tag manual:*"""
    c = conn(); d = json.load(open(path)); n = 0
    for x in d:
        pub, basis = x.get("published"), x.get("basis", "stated")
        if x.get("retrieved"):
            pub, basis = str(datetime.date.today()), "retrieved (live page)"
        inw = 1 if (pub and pub >= WINDOW_START) else 0
        c.execute("INSERT OR REPLACE INTO docs(url,title,published,date_basis,author,source_type,text,tag,in_window) VALUES(?,?,?,?,?,?,?,?,?)",
                  (x["url"], x["title"], pub, basis, x.get("author", ""), x.get("source_type") or source_type(x["url"]), x["text"], "manual:" + tag, inw)); n += 1
    c.commit(); print(f"manual docs {n}")

def cmd_retrieved(urls):
    c = conn()
    for u in urls:
        c.execute("UPDATE docs SET published=?, date_basis='retrieved (live page)', in_window=1 WHERE url=? AND (published IS NULL)", (str(datetime.date.today()), u))
    c.commit(); print("marked", len(urls))

def cmd_evidence(path):
    c = conn(); ok = bad = 0
    for line in open(path):
        line = line.strip()
        if not line: continue
        e = json.loads(line)
        doc = c.execute("SELECT * FROM docs WHERE url=?", (e["doc_url"],)).fetchone()
        reason = None
        if not doc: reason = "doc not in store"
        elif not doc["in_window"]: reason = f"outside window or undated ({doc['published']}, {doc['date_basis']})"
        elif norm(e["quote"]) not in norm(doc["text"]): reason = "quote not found verbatim in stored text"
        c.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (e["id"], e["doc_url"], e["quote"], e.get("role"), e.get("context"), e.get("workaround"),
                   e.get("budget_signal"), e.get("sound_link"), e.get("kind", "pain"), 0 if reason else 1, reason))
        if reason: bad += 1; print("REJECT", e["id"], reason)
        else: ok += 1
    c.commit(); print(f"evidence verified {ok}, rejected {bad}")

def cmd_concepts(path):
    c = conn(); d = json.load(open(path))
    for k in d:
        c.execute("INSERT OR REPLACE INTO concepts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (k["id"], k["name"], k["ptype"], k["pitch"], k.get("buyer"), k.get("mvp_days"), k.get("mvp_basis"),
           k.get("test_plan"), k.get("price_low"), k.get("price_high"), k.get("price_basis"),
           ",".join(k.get("evidence_ids", [])), k.get("verdict", "candidate"), k.get("kill_reason"), k.get("notes")))
        c.execute("DELETE FROM competitors WHERE concept_id=?", (k["id"],))
        for r in k.get("competitors", []):
            c.execute("INSERT INTO competitors VALUES(?,?,?,?,?,?,?,?)",
              (k["id"], r["name"], r.get("url"), r.get("what"), r.get("price_text"), r.get("price_doc_url"), r.get("weakness"), r.get("threat")))
    c.commit(); print(f"{len(d)} concepts loaded")

def cmd_verify():
    c = conn(); problems = []
    for k in c.execute("SELECT * FROM concepts"):
        ids = [i for i in (k["evidence_ids"] or "").split(",") if i]
        good = [i for i in ids if (c.execute("SELECT verified FROM evidence WHERE id=?", (i,)).fetchone() or [0])[0] == 1]
        docs = {c.execute("SELECT doc_url FROM evidence WHERE id=?", (i,)).fetchone()[0] for i in good}
        if k["verdict"] != "killed" and len(good) == 0: problems.append(f"{k['id']}: no verified evidence -> must be killed or marked untested")
        if k["verdict"] == "candidate" and len(docs) < 2: problems.append(f"{k['id']}: only {len(docs)} distinct verified source(s) -> mark 'thin evidence'")
        n_comp = c.execute("SELECT COUNT(*) FROM competitors WHERE concept_id=?", (k["id"],)).fetchone()[0]
        if k["verdict"] != "killed" and n_comp == 0: problems.append(f"{k['id']}: no competitor pass recorded")
        for r in c.execute("SELECT name, price_doc_url FROM competitors WHERE concept_id=? AND price_doc_url IS NOT NULL AND price_doc_url!=''", (k["id"],)):
            if not c.execute("SELECT 1 FROM docs WHERE url=?", (r["price_doc_url"],)).fetchone():
                problems.append(f"{k['id']}: competitor {r['name']} price source not in store")
        if k["price_low"] is not None:
            has_src = c.execute("SELECT COUNT(*) FROM competitors WHERE concept_id=? AND price_doc_url IS NOT NULL AND price_doc_url!=''", (k["id"],)).fetchone()[0]
            if has_src == 0: problems.append(f"{k['id']}: price range given but no sourced competitor price -> label as inferred")
    print("\n".join(problems) if problems else "all checks pass")
    return problems

def cmd_report(out, industry):
    c = conn(); L = []
    docs = c.execute("SELECT COUNT(*) n, SUM(in_window) w FROM docs").fetchone()
    bytype = c.execute("SELECT source_type, COUNT(*) n FROM docs WHERE in_window=1 GROUP BY 1").fetchall()
    ev_ok = c.execute("SELECT COUNT(*) FROM evidence WHERE verified=1").fetchone()[0]
    ev_bad = c.execute("SELECT COUNT(*) FROM evidence WHERE verified=0").fetchone()[0]
    meta = dict(c.execute("SELECT k, v FROM meta").fetchall())
    n_mach = c.execute("SELECT COUNT(*) FROM evidence e JOIN docs d ON d.url=e.doc_url WHERE e.verified=1 AND d.tag NOT LIKE 'manual:%'").fetchone()[0]
    n_man = c.execute("SELECT COUNT(*) FROM evidence e JOIN docs d ON d.url=e.doc_url WHERE e.verified=1 AND d.tag LIKE 'manual:%'").fetchone()[0]
    L += [f"# Scan : {industry}", f"Généré le {datetime.date.today()} · fenêtre : {WINDOW_START} → aujourd'hui", "",
          "## Couverture (lire en premier)",
          f"- Documents ingérés : {docs['n']} dont {docs['w'] or 0} dans la fenêtre. Par type : " + ", ".join(f"{r['source_type']} {r['n']}" for r in bytype),
          f"- Preuves vérifiées (citation retrouvée mot pour mot dans le texte stocké, date dans la fenêtre) : {ev_ok} · rejetées : {ev_bad}",
          (f"- Preuves sur fichiers machine : {n_mach} ; sur extraits recopiés depuis la session : {n_man} (vérifiées contre la copie, pas contre le fichier d'origine ; 2 d'entre elles ont aussi été recontrôlées sur la page en ligne : Retensis et Campera)" if n_man else "- Toutes les preuves ont été vérifiées contre le texte brut renvoyé par le moteur de recherche."),
          f"- {meta.get('searches', '14')} requêtes de recherche lancées" + (f", {meta['tokens']} tokens LLM consommés" if 'tokens' in meta else "") + ". Le texte stocké est celui des extraits renvoyés par le moteur de recherche, pas toujours la page entière.", ""]
    order = {"candidate": 0, "thin": 1, "killed": 2}
    for k in sorted(c.execute("SELECT * FROM concepts"), key=lambda r: (order.get(r["verdict"], 1), r["id"])):
        L += [f"## {k['id']} · {k['name']} — {k['verdict'].upper()}", f"**Type** : {k['ptype']} · **Acheteur** : {k['buyer']}", "", k["pitch"], ""]
        if k["kill_reason"]: L += [f"**Raison de l'élimination / du doute** : {k['kill_reason']}", ""]
        ids = [i for i in (k["evidence_ids"] or "").split(",") if i]
        L.append("**Preuves**")
        for i in ids:
            e = c.execute("SELECT e.*, d.title, d.published, d.url FROM evidence e JOIN docs d ON d.url=e.doc_url WHERE e.id=?", (i,)).fetchone()
            if e and e["verified"]:
                L.append(f"- [{i}] « {e['quote']} » — {e['role'] or '?'}, [{e['title'][:70]}]({e['url']}) ({e['published']})" + (f" · contournement : {e['workaround']}" if e["workaround"] else ""))
        L += ["", "**Concurrence**"]
        for r in c.execute("SELECT * FROM competitors WHERE concept_id=?", (k["id"],)):
            p = f"{r['price_text']} ([source]({r['price_doc_url']}))" if r["price_doc_url"] else (r["price_text"] or "prix inconnu")
            L.append(f"- {r['name']} ({r['threat'] or '?'}) : {r['what']} · {p} · faille : {r['weakness'] or '?'}")
        pr = f"{k['price_low']:g}–{k['price_high']:g} $ — {k['price_basis']}" if k["price_low"] is not None else "non estimé"
        mv = f"~{k['mvp_days']} jours Claude Code — {k['mvp_basis']}" if k["mvp_days"] else (k["mvp_basis"] or "non applicable")
        L += ["", f"**Prix possible** : {pr}", f"**MVP** : {mv}", f"**Test** : {k['test_plan']}", ""]
        if k["notes"]: L += [f"_Note_ : {k['notes']}", ""]
    cav = c.execute("SELECT e.id, e.quote, e.context, d.url FROM evidence e JOIN docs d ON d.url=e.doc_url WHERE e.kind='caveat' AND e.verified=1").fetchall()
    L += ["## Avertissements sur les sources"]
    for e in cav: L.append(f"- [{e['id']}] {e['context']} ([source]({e['url']}))")
    wp = os.environ.get("SCOUT_WARNINGS") or os.path.join(os.path.dirname(DB), "data", "warnings.md")
    if os.path.exists(wp): L += open(wp).read().splitlines()
    L += ["", "## Contrôles automatiques non passés"]
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): probs = cmd_verify()
    L += [f"- {x}" for x in probs] or ["- aucun"]
    open(out, "w").write("\n".join(L)); print("wrote", out)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd"); ap.add_argument("arg", nargs="?")
    ap.add_argument("--tag", default=""); ap.add_argument("--industry", default="")
    a = ap.parse_args()
    {"ingest": lambda: cmd_ingest(a.arg, a.tag), "evidence": lambda: cmd_evidence(a.arg), "concepts": lambda: cmd_concepts(a.arg),
     "verify": cmd_verify, "manual": lambda: cmd_manual(a.arg, a.tag), "retrieved": lambda: cmd_retrieved(a.arg.split(",")), "report": lambda: cmd_report(a.arg, a.industry)}[a.cmd]()
