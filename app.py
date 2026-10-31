"""app.py - Job Finder (Ubuntu / GNOME look).   Run:  streamlit run app.py
Flow: 1. career websites  ->  2. your CV  ->  3. analysis  ->  results."""
import atexit
import hashlib
import html
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

from jobfinder import cv as cvm
from jobfinder import llm
from jobfinder.analysis import analyze_pipeline
from jobfinder.classify import OFFER_TYPES
from jobfinder.config import APPLICANT_DEFAULT, CONFIG_DEFAULT, PRESET_DAYS, PROFILE_DEFAULT, run_config
from jobfinder.scan import Progress, run_scan
from jobfinder.store import (ENV_KEYS, CoverLetter, CVDocument, Job, SessionLocal, Source, ensure_gitignore, get_setting,
                             init_db, load_keys, load_new_sources, save_key, set_setting, wipe_session_data)
from jobfinder.web import extract_urls

ASSETS = Path(__file__).parent / "assets"
st.set_page_config(page_title="Job Finder", page_icon=str(ASSETS / "icon.svg"), layout="wide")
init_db()
ensure_gitignore()
st.html(f"<style>{(ASSETS / 'ubuntu.css').read_text(encoding='utf-8')}</style>")


@st.cache_resource
def _launch():
    """Runs once per launch: websites and offers from a previous run are deleted (fresh start)."""
    wipe_session_data()
    atexit.register(wipe_session_data)  # and again when the app is turned off
    return Progress()


prog = _launch()
with SessionLocal() as _s:
    CFG, PROFILE, APPLICANT = (get_setting(_s, "config", CONFIG_DEFAULT), get_setting(_s, "profile", PROFILE_DEFAULT),
                               get_setting(_s, "applicant", APPLICANT_DEFAULT))
c, p, ap = (json.loads(json.dumps(x)) for x in (CFG, PROFILE, APPLICANT))  # working copies, auto-saved at the end
KEYS = load_keys()
PRESETS = list(PRESET_DAYS) + ["All dates"]
STATUS = {"relevant": ("Relevant", "green"), "review": ("To check", "yellow"), "candidate": ("Not analysed", ""),
          "excluded": ("Excluded", "red")}


# =================================================================== shell: GNOME top panel, header bar, dock
def shell(title, subtitle=""):
    st.html(f'<div class="ub-panel"><span class="ub-activities">Activities</span>'
            f'<span class="ub-clock">{datetime.now():%a %d %b  %H:%M}</span><span class="ub-tray"><span>⏻</span><span>▾</span></span></div>'
            f'<div class="ub-headerbar"><div><span class="ub-title">{title}</span><span class="ub-sub">{subtitle}</span></div>'
            '<div class="ub-wm"><span>&#8211;</span><span>&#9633;</span><span class="close">&#10005;</span></div></div>')


def counts():
    with SessionLocal() as s:
        return dict(sites=s.query(Source).count(), cvs=s.query(CVDocument).filter_by(enabled=True).count(),
                    relevant=s.query(Job).filter(Job.status == "relevant").count(),
                    review=s.query(Job).filter(Job.status == "review").count())


def dock():
    n, d = counts(), prog.snap()[0]
    st.logo(str(ASSETS / "logo.svg"), size="large", icon_image=str(ASSETS / "icon.svg"))
    with st.sidebar:
        st.html(f'<div class="ub-dockstat"><span>Websites</span><b>{n["sites"]}</b></div>'
                f'<div class="ub-dockstat"><span>CVs</span><b>{n["cvs"]}</b></div>'
                f'<div class="ub-dockstat"><span>Relevant offers</span><b>{n["relevant"]}</b></div>'
                f'<div class="ub-dockstat"><span>To check</span><b>{n["review"]}</b></div>'
                f'<div class="ub-dockstat"><span>Analysis</span><b>{"running" if d["running"] else "idle"}</b></div>')


def chips(items):  # texts come from websites and CV file names: always escaped
    return '<div class="ub-chips">' + "".join(f'<span class="ub-chip {cls}">{html.escape(str(txt))}</span>'
                                              for txt, cls in items if txt) + "</div>"


def web_link(u):
    """Only http(s) links are offered as buttons (a scraped 'javascript:' URL is never clickable)."""
    return u if str(u or "").lower().startswith(("http://", "https://")) else None


def step(n, title, done):
    st.html(f'<div class="ub-step"><span class="ub-stepnum {"done" if done else ""}">{"✓" if done else n}</span>{title}</div>')


def autosave(name, new, old):
    snap = json.dumps(new, sort_keys=True, default=str)
    if snap != json.dumps(old, sort_keys=True, default=str) and snap != st.session_state.get("_saved_" + name):
        with SessionLocal() as s:
            set_setting(s, name, new)
        st.session_state["_saved_" + name] = snap
        st.rerun()


def _sig(files):
    return hashlib.sha1("|".join(f"{f.name}:{f.size}" for f in files).encode()).hexdigest()


# =================================================================== page 1: the three steps
def step_websites(running, n):
    with st.container(border=True):
        step(1, "Career websites", n["sites"] > 0)
        files = st.file_uploader("HTML file(s) with the career pages — read, never executed", type=["html", "htm"],
                                 accept_multiple_files=True, disabled=running)
        if files and _sig(files) != st.session_state.get("sites_sig"):  # loaded as soon as the files are chosen
            for f in files:
                if f.size > 2_000_000:
                    st.toast(f"{f.name} is over 2 MB: skipped", icon=":material/warning:")
            urls = list(dict.fromkeys(u for f in files if f.size <= 2_000_000
                                      for u in extract_urls(f.getvalue().decode("utf-8", errors="replace"))))
            with SessionLocal() as s:
                load_new_sources(s, urls)
            prog.reset()
            st.session_state["sites_sig"] = _sig(files)
            st.rerun()
        with st.expander("…or paste URLs"):
            pasted = st.text_area("One URL per line", height=90, label_visibility="collapsed")
            if st.button("Use these URLs", disabled=running or not extract_urls(pasted)):
                with SessionLocal() as s:
                    load_new_sources(s, extract_urls(pasted))
                prog.reset()
                st.rerun()
        if n["sites"]:
            with SessionLocal() as s:
                srcs = s.query(Source).all()
            st.caption(f"**{len(srcs)} websites** ready. Loading another file replaces them. Search words, page numbers and "
                       "tracking codes from a past search are removed from company URLs, so every website is read whole "
                       "and filtered the same way; country filters and job-board searches are kept.")
            with st.expander("Websites and how each one was read"):
                st.dataframe(pd.DataFrame([dict(website=x.label, host=urlparse(x.url).hostname, read_by=x.platform or "—",
                                                status=x.status, offers=x.jobs_found, note=x.last_error, url=x.url)
                                           for x in srcs]),
                             width="stretch", hide_index=True, height=min(38 + 35 * len(srcs), 420),
                             column_config={"url": st.column_config.LinkColumn("url", display_text="open")})


def step_cv(running, n):
    with st.container(border=True):
        step(2, "Your CV", n["cvs"] > 0)
        up = st.file_uploader("PDF CV(s) — compared with every relevant offer", type=["pdf"], accept_multiple_files=True,
                              key="cvup", disabled=running)
        if up and _sig(up) != st.session_state.get("cv_sig"):
            with SessionLocal() as s:
                for f in up:
                    try:
                        cvm.add_cv(s, f.name, f.getvalue())
                    except ValueError as e:
                        st.error(f"{f.name}: {e}")
            st.session_state["cv_sig"] = _sig(up)
            st.rerun()
        with SessionLocal() as s:
            for cv in s.query(CVDocument).all():
                a, b = st.columns([6, 1], vertical_alignment="center")
                a.html(chips([(cv.name, "aubergine"), (cv.language, ""),
                              ("needs OCR (scanned PDF)" if cv.status == "needs_ocr" else "", "red")]))
                if b.button("Remove", key=f"rm{cv.id}", disabled=running):
                    cvm.delete_cv(s, cv.id)
                    st.rerun()
        if c["model"]["provider"] in llm.PAID:
            c["send_cv_remote_ok"] = st.checkbox(f"Allow sending my CV text to {c['model']['provider']} (hosted model)",
                                                 c["send_cv_remote_ok"])
        with st.expander("Your details (only for cover letters)"):
            ap["name"] = st.text_input("Full name", ap["name"])
            x, y = st.columns(2)
            ap["email"], ap["phone"] = x.text_input("Email", ap["email"]), y.text_input("Phone", ap["phone"])
            ap["address"] = st.text_area("Address", ap["address"], height=68)
            ap["linkedin"], ap["website"] = x.text_input("LinkedIn URL", ap["linkedin"]), y.text_input("Website", ap["website"])


def _monitor():
    d, log = prog.snap()
    if d["running"]:
        st.session_state["was_running"] = True
    elif st.session_state.pop("was_running", False):
        st.rerun()  # the analysis just finished: refresh counts
    if not (d["running"] or d["finished"]):
        return
    st.progress(min(d["sources_done"] / (d["sources_total"] or 1), 1.0),
                text=f"{d['task'].capitalize()} — websites {d['sources_done']} / {d['sources_total']}")
    if d["ai_total"]:
        st.progress(min(d["ai_done"] / d["ai_total"], 1.0), text=f"AI reading offers {d['ai_done']} / {d['ai_total']}")
    m = st.columns(4)
    m[0].metric("Offers in the date range", d["jobs"])
    m[1].metric("Too old (hidden)", d["date_x"])
    m[2].metric("Not France / not wanted", d["loc_x"] + d["title_x"])
    m[3].metric("Relevant", d["relevant"])
    if d["ai_msg"]:
        st.warning(d["ai_msg"])
    if not d["running"] and d["finished"]:
        st.page_link(RESULTS, label="See the results", icon=":material/arrow_forward:")
    with st.expander("Activity log"):
        st.code("\n".join(log[-60:]) or "—", language=None)


def step_analysis(running, n):
    with st.container(border=True):
        step(3, "Analysis", bool(prog.snap()[0]["finished"]))
        cur = p.get("offer_types") or ["job"]
        p["offer_types"] = st.pills("I am looking for", list(OFFER_TYPES), selection_mode="multi", default=cur,
                                    format_func=OFFER_TYPES.get, key="types") or cur
        pre = c["date_preset"] if c["date_preset"] in PRESETS else "Last 30 days"
        c["date_preset"] = st.segmented_control("Published within", PRESETS, default=pre, key="preset") or pre
        m = c["model"]
        st.caption(f"AI model **{m['provider']} / {m['model']}** — change it in Settings. "
                   + ("" if n["cvs"] else "No CV yet: offers are ranked, but not compared with a CV."))
        x, y, _ = st.columns([1, 1, 3])
        if x.button("Start", type="primary", disabled=running or not n["sites"], icon=":material/play_arrow:", width="stretch"):
            with SessionLocal() as s:
                ids = [r.id for r in s.query(Source).all()]
            cfg, prof, keys = json.loads(json.dumps(c)), json.loads(json.dumps(p)), dict(KEYS)
            cfg["_keys"] = keys  # in memory only (never saved): lets the AI read pages and offers
            threading.Thread(target=run_scan, args=(ids, cfg, prof, prog, lambda pr: analyze_pipeline(cfg, prof, keys, pr)),
                             daemon=True).start()
            st.rerun()
        if y.button("Stop", disabled=not running, icon=":material/stop:", width="stretch"):
            prog.cancel.set()
        st.fragment(run_every=2 if running else None)(_monitor)()


def page_search():
    shell("Job Finder", "1 · websites   2 · CV   3 · analysis")
    running, n = prog.snap()[0]["running"], counts()
    step_websites(running, n)
    step_cv(running, n)
    step_analysis(running, n)


# =================================================================== page 2: results
def _rows():
    with SessionLocal() as s:
        out = []
        for j in s.query(Job).filter(Job.status != "out_of_range").all():
            ti = (j.extra or {}).get("title_info") or {}
            r = j.relevance_json or {}
            kind = r.get("contract_kind") if r.get("contract_kind") not in (None, "unknown") else (ti.get("training") or ti.get("contract") or "")
            out.append(dict(uid=j.uid, title=j.title, company=j.employer, location=j.location_original or "",
                            published=j.pub_date.date() if j.pub_date else None, type=kind,
                            topics=", ".join(r.get("research_topics") or ti.get("topics", [])),
                            relevance=j.relevance_score, match=j.match_score, status=j.status))
    return pd.DataFrame(out)


def page_results():
    shell("Results", "offers found")
    df = _rows()
    if df.empty:
        st.html('<div class="ub-empty"><div class="ub-big">No offers yet</div>Follow the three steps on the Search page.</div>')
        return
    views = {"Relevant": ["relevant"], "To check": ["review"], "Not analysed": ["candidate"], "Excluded": ["excluded"], "All": None}
    label = lambda k: f"{k} ({len(df) if views[k] is None else int(df.status.isin(views[k]).sum())})"
    a, b = st.columns([3, 2])
    view = a.segmented_control("Show", list(views), default="Relevant", format_func=label, key="view") or "Relevant"
    q = b.text_input("Filter", placeholder="title, company, topic…")
    v = df if views[view] is None else df[df.status.isin(views[view])]
    if q:
        v = v[(v.title + " " + v.company.fillna("") + " " + v.topics + " " + v.type).str.lower().str.contains(q.lower(), regex=False)]
    v = v.sort_values(["match", "relevance", "published"], ascending=False, na_position="last")
    sel = st.dataframe(v.drop(columns=["uid", "status"]), width="stretch", hide_index=True, height=min(38 + 35 * max(len(v), 1), 420),
                       on_select="rerun", selection_mode="single-row", key="grid",
                       column_config={"title": st.column_config.TextColumn(width="medium"),
                                      "location": st.column_config.TextColumn(width="small"),
                                      "company": st.column_config.TextColumn(width="small"),
                                      "published": st.column_config.DateColumn(format="DD MMM YYYY"),
                                      "relevance": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
                                      "match": st.column_config.ProgressColumn("CV match", min_value=0, max_value=100, format="%d")})
    st.download_button("Export CSV", v.drop(columns=["uid"]).to_csv(index=False), "offers.csv", icon=":material/download:")
    rows = sel.selection.rows if sel and sel.selection else []
    if rows:
        job_detail(v.iloc[rows[0]].uid)
    else:
        st.caption("Select a row to open the offer.")


def job_detail(uid):
    with SessionLocal() as s:
        j = s.query(Job).filter_by(uid=uid).first()
        ti, r = (j.extra or {}).get("title_info") or {}, j.relevance_json or {}
        label, cls = STATUS.get(j.status, (j.status, ""))
        with st.container(border=True):
            st.subheader(j.title, anchor=False)
            kind = r.get("contract_kind") if r.get("contract_kind") not in (None, "unknown") else ti.get("training")
            st.html(chips([(j.employer, "aubergine"), (label, cls), (OFFER_TYPES.get(kind or "", ""), "orange"),
                           ("research subject" if ti.get("kind") == "research_topic" else "", "orange"),
                           ({"fr": "français", "en": "English"}.get(ti.get("lang"), ""), "")]
                          + [(t, "") for t in (r.get("research_topics") or ti.get("topics") or [])[:6]]))
            l1, l2, _ = st.columns([1, 1, 3])
            if web_link(j.url):
                l1.link_button("Open the offer", j.url.split("jboffer=")[0].rstrip("?&"), icon=":material/open_in_new:", width="stretch")
            if web_link(j.apply_url) and j.apply_url != j.url and "jboffer=" not in j.apply_url:
                l2.link_button("Apply page", j.apply_url, icon=":material/send:", width="stretch")
            a, b = st.columns(2)
            a.html(f'<div class="ub-kv"><b>Published</b> {f"{j.pub_date:%d %b %Y}" if j.pub_date else "unknown"}</div>')
            a.caption(f"{j.date_source or '—'} · “{(j.pub_date_text or '')[:80]}”")
            b.html(f'<div class="ub-kv"><b>Location</b> {html.escape(j.location_original or "unknown")}</div>')
            b.caption(j.location_evidence or "")
            if r:
                st.markdown(f"**Relevance {r.get('relevance_score', 0):.0f}/100** · {r.get('reasoning_summary', '')}")
            if j.status == "review":
                st.info("To check: " + (j.review_flags or "").replace("_", " ").replace(",", " · "))
                if st.button("I checked it: keep this offer", icon=":material/check:"):
                    j.status, j.review_flags = ("relevant" if j.relevance_score is not None else "candidate"), ""
                    s.commit()
                    st.rerun()
            elif j.status == "excluded":
                st.caption(f"Excluded: {j.reason}")
        with st.expander("Description"):
            st.write(j.description or "(none)")
        _cv_and_letter(s, j)


def _cv_and_letter(s, j):
    cvs = [x for x in s.query(CVDocument).filter_by(enabled=True).all() if x.status == "ok"]
    if not cvs:
        return
    with st.container(border=True):
        st.html('<div class="ub-section">CV match & cover letter</div>')
        if j.match_json:
            st.dataframe(pd.DataFrame([{"cv": k, "score": x["match_score"], "advice": x["recommendation"],
                                        "missing": ", ".join(x["missing_requirements"]), "not shown in CV": ", ".join(x["not_evidenced"])}
                                       for k, x in j.match_json.items()]), width="stretch", hide_index=True)
        names = [x.name for x in cvs]
        x1, x2, x3 = st.columns([1, 1, 2])
        if not j.match_json and x1.button("Compare with my CV", icon=":material/compare_arrows:", width="stretch"):
            try:
                cvm.match_job(s, j, run_config(CFG), KEYS)
                st.rerun()
            except llm.AIError as e:
                st.error(str(e))
        cvn = x3.selectbox("CV for the letter", names, names.index(j.best_cv) if j.best_cv in names else 0, label_visibility="collapsed")
        if x2.button("Write cover letter", icon=":material/edit_note:", width="stretch"):
            if not APPLICANT["name"]:
                st.error("Add your name in step 2 (Your details) first.")
            else:
                try:
                    cv = next(x for x in cvs if x.name == cvn)
                    cvm.save_letter(s, j, cvn, cvm.build_tex(APPLICANT, j, cvm.generate_letter_content(s, j, cv, run_config(CFG), KEYS)))
                    st.rerun()
                except llm.AIError as e:
                    st.error(str(e))
        for cl in s.query(CoverLetter).filter_by(job_uid=j.uid).order_by(CoverLetter.version.desc()).all()[:1]:
            if not Path(cl.tex_path).exists():
                continue  # the letter file was deleted from the disk
            txt = st.text_area(f"Letter v{cl.version} (LaTeX, editable)", Path(cl.tex_path).read_text(encoding="utf-8"), height=260,
                               key=f"tx{cl.id}")
            c1, c2, c3 = st.columns(3)
            if c1.button("Save as new version", key=f"sv{cl.id}", icon=":material/save:"):
                cvm.save_letter(s, j, cl.cv_name, txt, "edited")
                st.rerun()
            c2.download_button("Download .tex", txt, f"{j.uid}.tex")
            if cl.pdf_path and Path(cl.pdf_path).exists():
                c3.download_button("Download PDF", Path(cl.pdf_path).read_bytes(), f"{j.uid}.pdf")


# =================================================================== page 3: settings (AI model + search profile)
def settings_ai():
    provs = list(c["providers"])
    models = st.session_state.setdefault("models", {})
    a, b = st.columns([1, 2], vertical_alignment="bottom")
    prov = a.selectbox("Provider", provs, index=provs.index(CFG["model"]["provider"]) if CFG["model"]["provider"] in provs else 0,
                       format_func=lambda x: "ollama (local, free)" if x == "ollama" else x)
    if prov != "ollama":
        have = prov in KEYS or os.environ.get(ENV_KEYS[prov])
        key = b.text_input(f"API key for {prov}", type="password", placeholder="key saved" if have else "paste your key")
        if key and st.button("Save key"):
            try:
                save_key(prov, key)
                st.toast(f"Key saved for {prov}")
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    if st.button("Load the models", icon=":material/sync:"):
        with st.spinner("Contacting the provider…"):
            ok, ms_, msg = llm.list_models(prov, c, KEYS)
        (st.success if ok else st.error)(msg)
        if ok:
            models[prov] = ms_
    if models.get(prov):
        ms = models[prov]
        cur = CFG["model"]["model"] if CFG["model"]["provider"] == prov and CFG["model"]["model"] in ms else ms[0]
        ok = llm.VERIFIED.get(prov, [])
        c["model"] = {"provider": prov, "model": st.selectbox("Model", ms, ms.index(cur),
                                                              format_func=lambda m: f"{m}  (tested)" if m in ok else m)}
        st.caption("Tested: checked on this app's tasks (offer analysis, publication date, lab pages, browser).")
    st.caption(f"In use: **{c['model']['provider']} / {c['model']['model']}**")


def settings_profile():
    p["description"] = st.text_area("My research interests (every offer is compared with this)", p["description"])
    a, b = st.columns(2)
    p["keywords"] = [x.strip() for x in a.text_area("Wanted topics / titles — English or French, one per line",
                                                    "\n".join(p["keywords"]), height=280).splitlines() if x.strip()]
    a.caption("A topic also finds its translation: *computer vision* matches *vision par ordinateur*.")
    p["exclude_titles"] = [x.strip() for x in b.text_area("Words that exclude an offer", "\n".join(p["exclude_titles"]),
                                                          height=200).splitlines() if x.strip()]
    c["min_relevance"] = b.slider("Minimum relevance", 0, 100, int(c["min_relevance"]))


def page_settings():
    shell("Settings", "saved automatically")
    t1, t2 = st.tabs(["AI model", "Search profile"])
    with t1, st.container(border=True):
        settings_ai()
    with t2, st.container(border=True):
        settings_profile()


# =================================================================== navigation
RESULTS = st.Page(page_results, title="Results", icon=":material/work:", url_path="results")
dock()
st.navigation([st.Page(page_search, title="Search", icon=":material/travel_explore:", default=True), RESULTS,
               st.Page(page_settings, title="Settings", icon=":material/settings:", url_path="settings")]).run()
for _n, _new, _old in (("config", c, CFG), ("profile", p, PROFILE), ("applicant", ap, APPLICANT)):
    autosave(_n, _new, _old)
