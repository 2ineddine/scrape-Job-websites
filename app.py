"""app.py - Job Finder (navy website look, Telegraf type).   Run:  streamlit run app.py
Flow: 1. career websites  ->  2. your CV  ->  3. analysis  ->  results."""
import atexit
import hashlib
import html
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import altair as alt
import pandas as pd
import streamlit as st

from jobfinder import cv as cvm
from jobfinder import llm
from jobfinder import market
from jobfinder.analysis import analyze_pipeline
from jobfinder.classify import OFFER_TYPES
from jobfinder.config import APPLICANT_DEFAULT, CONFIG_DEFAULT, PRESET_DAYS, PROFILE_DEFAULT, run_config, upgrade_profile
from jobfinder.scan import Progress, run_scan
from jobfinder.store import (ENV_KEYS, CoverLetter, CVDocument, Job, SessionLocal, Source, ensure_gitignore, get_setting,
                             init_db, load_keys, load_new_sources, save_key, scan_running_elsewhere, set_setting,
                             wipe_session_data)
from jobfinder.web import extract_urls, offer_link

ASSETS = Path(__file__).parent / "assets"
st.set_page_config(page_title="Job Finder", page_icon=str(ASSETS / "icon.svg"), layout="wide")
init_db()
ensure_gitignore()
st.html(f"<style>{(ASSETS / 'navy.css').read_text(encoding='utf-8')}</style>")


@st.cache_resource
def _launch():
    """Runs once per launch: websites and offers from a previous run are deleted (fresh start)."""
    wipe_session_data()  # the analysed offers are first copied into the market history
    with SessionLocal() as s:
        market.run_if_due(s)  # the statistics, every 15 days
    atexit.register(wipe_session_data)  # and again when the app is turned off
    return Progress()


prog = _launch()
with SessionLocal() as _s:
    CFG, PROFILE, APPLICANT = (get_setting(_s, "config", CONFIG_DEFAULT), get_setting(_s, "profile", PROFILE_DEFAULT),
                               get_setting(_s, "applicant", APPLICANT_DEFAULT))
    # providers added to the app since these settings were saved (NVIDIA): known without re-saving the settings
    CFG = {**CFG, "providers": {**CONFIG_DEFAULT["providers"], **(CFG.get("providers") or {})}}
    PROFILE, _upgraded = upgrade_profile(PROFILE)  # keywords added to the defaults since this profile was saved
    if _upgraded:
        set_setting(_s, "profile", PROFILE)
c, p, ap = (json.loads(json.dumps(x)) for x in (CFG, PROFILE, APPLICANT))  # working copies, auto-saved at the end
KEYS = load_keys()
PRESETS = list(PRESET_DAYS) + ["All dates"]
STATUS = {"relevant": ("Relevant", "green"), "review": ("To check", "yellow"), "candidate": ("Not analysed", ""),
          "excluded": ("Excluded", "red")}


# =================================================================== shell: navy top bar and page header
def shell(title, subtitle="", eyebrow="Job Finder"):
    """The page's navy header: its title, one line about it, and the live counters."""
    n, d = counts(), prog.snap()[0]
    stats = [(n["sites"], "websites", ""), (n["cvs"], "CVs", ""), (n["relevant"], "relevant offers", ""),
             (n["review"], "to check", ""), ("running" if d["running"] else "idle", "analysis", "live" if d["running"] else "")]
    st.html(f'<div class="jf-hero"><div class="jf-eyebrow">{html.escape(eyebrow)}</div><h1>{html.escape(title)}</h1>'
            f'<p class="jf-lead">{html.escape(subtitle)}</p><div class="jf-stats">'
            + "".join(f'<div class="jf-stat {cls}"><b>{v}</b><span>{k}</span></div>' for v, k, cls in stats) + "</div></div>")


def counts():
    with SessionLocal() as s:
        return dict(sites=s.query(Source).count(), cvs=s.query(CVDocument).filter_by(enabled=True).count(),
                    relevant=s.query(Job).filter(Job.status == "relevant").count(),
                    review=s.query(Job).filter(Job.status == "review").count())


def brand():
    st.logo(str(ASSETS / "logo.svg"), size="large")  # the whole logo in the navy top bar


def chips(items):  # texts come from websites and CV file names: always escaped
    return '<div class="jf-chips">' + "".join(f'<span class="jf-chip {cls}">{html.escape(str(txt))}</span>'
                                              for txt, cls in items if txt) + "</div>"


def web_link(u):
    """Only http(s) links are offered as buttons (a scraped 'javascript:' URL is never clickable)."""
    return u if str(u or "").lower().startswith(("http://", "https://")) else None


def step(n, title, done):
    st.html(f'<div class="jf-step"><span class="jf-stepnum {"done" if done else ""}">{"✓" if done else n}</span>{title}</div>')


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
    with st.container(border=True, key="card_websites"):
        step(1, "Career websites", n["sites"] > 0)
        files = st.file_uploader("HTML file(s) with the career pages — read, never executed", type=["html", "htm"],
                                 accept_multiple_files=True, disabled=running)
        if files and _sig(files) != st.session_state.get("sites_sig"):  # loaded as soon as the files are chosen
            for f in files:
                if f.size > 2_000_000:
                    st.toast(f"{f.name} is over 2 MB: skipped", icon=":material/warning:")
            urls = list(dict.fromkeys(u for f in files if f.size <= 2_000_000
                                      for u in extract_urls(f.getvalue().decode("utf-8", errors="replace"))))
            try:
                with SessionLocal() as s:
                    load_new_sources(s, urls)
            except RuntimeError as e:
                st.error(str(e))
                return
            prog.reset()
            st.session_state["sites_sig"] = _sig(files)
            st.rerun()
        with st.expander("…or paste URLs"):
            pasted = st.text_area("One URL per line", height=90, label_visibility="collapsed")
            if st.button("Use these URLs", disabled=running or not extract_urls(pasted)):
                try:
                    with SessionLocal() as s:
                        load_new_sources(s, extract_urls(pasted))
                except RuntimeError as e:
                    st.error(str(e))
                    return
                prog.reset()
                st.rerun()
        if n["sites"]:
            with SessionLocal() as s:
                srcs = s.query(Source).all()
            st.caption(f"**{len(srcs)} websites** ready. Loading another file replaces them. Search words, page numbers and "
                       "tracking codes from a past search are removed from company URLs, so every website is read whole "
                       "and filtered the same way; country filters and job-board searches are kept.")
            worse = [x for x in srcs if x.health and not x.health.endswith("read again now")]
            if worse:  # the health report: websites that worked at the previous scan and do not any more
                st.warning(f"**{len(worse)} website(s) changed since the previous scan**: "
                           + "; ".join(f"{x.label}: {x.health}" for x in worse[:5]) + (" …" if len(worse) > 5 else ""),
                           icon=":material/monitor_heart:")
            with st.expander("Websites and how each one was read"):
                st.dataframe(pd.DataFrame([dict(website=x.label, host=urlparse(x.url).hostname, read_by=x.platform or "—",
                                                status=READ_OUTCOME.get(x.outcome, x.status), offers=x.jobs_found,
                                                on_lists=x.listed, since_last_scan=x.health, note=x.last_error, url=x.url)
                                           for x in srcs]),
                             width="stretch", hide_index=True, height=min(38 + 35 * len(srcs), 420),
                             column_config={"url": st.column_config.LinkColumn("url", display_text="open")})


READ_OUTCOME = {"offers_found": "offers found", "nothing_in_range": "read: nothing in the date range",
                "no_offers_detected": "no offers detected", "partial": "possibly incomplete"}


def step_cv(running, n):
    with st.container(border=True, key="card_cv"):
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
                a.html(chips([(cv.name, "navy"), (cv.language, ""),
                              ("needs OCR (scanned PDF)" if cv.status == "needs_ocr" else "", "red")]))
                if b.button("Remove", key=f"rm{cv.id}", disabled=running):
                    cvm.delete_cv(s, cv.id)
                    st.rerun()
        hosted = sorted({x[0] for x in llm.model_chain(c, KEYS) if x[0] in llm.PAID}) if c["auto_models"] else []
        if hosted:  # the models switch automatically: the consent names every provider that could receive the CV
            ok = st.checkbox(f"Allow sending my CV text to these hosted providers: {', '.join(hosted)}",
                             set(hosted) <= set(c["cv_consent_providers"]),
                             help="Free plans may keep or use what they receive: check each provider's terms.")
            c["cv_consent_providers"] = hosted if ok else []
        elif c["model"]["provider"] in llm.PAID:
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
        if llm.USAGE["model"]:
            st.caption(f"Model in use: {llm.USAGE['model']}")
    m = st.columns(4)
    m[0].metric("Offers in the date range", d["jobs"])
    m[1].metric("Too old (hidden)", d["date_x"])
    m[2].metric("Not France / not wanted", d["loc_x"] + d["title_x"])
    m[3].metric("Relevant", d["relevant"])
    if d["ai_msg"]:
        st.warning(d["ai_msg"])
    if d["type_x"] and not d["running"]:
        st.warning(f"{d['type_x']} offers were set aside only because of their type (internship, PhD, apprenticeship...). "
                   "If you want them too, select these types in 'I am looking for' and analyse again.")
    if not d["running"] and d["finished"]:
        st.page_link(RESULTS, label="See the results", icon=":material/arrow_forward:")
    with st.expander("Activity log"):
        st.code("\n".join(log[-60:]) or "—", language=None)


def step_analysis(running, n):
    with st.container(border=True, key="card_analysis"):
        step(3, "Analysis", bool(prog.snap()[0]["finished"]))
        cur = p.get("offer_types") or ["job"]
        p["offer_types"] = st.pills("I am looking for", list(OFFER_TYPES), selection_mode="multi", default=cur,
                                    format_func=OFFER_TYPES.get, key="types") or cur
        pre = c["date_preset"] if c["date_preset"] in PRESETS else "Last 30 days"
        c["date_preset"] = st.segmented_control("Published within", PRESETS, default=pre, key="preset") or pre
        m = c["model"]
        first = llm.model_chain(c, KEYS)[0]
        st.caption((f"AI models: starts with **{first[0]} / {first[1]}** and switches when a limit is reached — see Settings. "
                    if c["auto_models"] else f"AI model **{m['provider']} / {m['model']}** — change it in Settings. ")
                   + ("" if n["cvs"] else "No CV yet: offers are ranked, but not compared with a CV."))
        with SessionLocal() as s:
            due = market.due(s)
        c["stats_with_scan"] = st.checkbox(
            "Update the statistics at the end of this scan", value=bool(c.get("stats_with_scan")), disabled=running,
            help="Computes the Statistics page from the offers analysed in the last 15 days as soon as this scan's AI analysis "
                 "ends. Unticked, the statistics are updated every 15 days" + (" (due now)." if due else "."))
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
    shell("Find research & engineering offers in France",
          "Load the career websites, add your CV, then let the app read every offer, date it, place it and rank it.",
          "Search · 3 steps")
    elsewhere = scan_running_elsewhere()
    if elsewhere:  # a scan started outside this app (script, other window): its data is kept and nothing can change
        st.warning("A scan is running in another process: its websites and offers are kept. Loading websites and "
                   "starting a scan are locked until it ends — reload this page then.", icon=":material/lock:")
    running, n = prog.snap()[0]["running"] or elsewhere, counts()
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
                            # ISO text: sorts by date, and an offer without a stated date shows an empty cell, not 'None'
                            published=f"{j.pub_date:%Y-%m-%d}" if j.pub_date else "", first_seen=(j.extra or {}).get("first_seen") or "", type=kind,
                            topics=", ".join(r.get("research_topics") or ti.get("topics", [])),
                            relevance=j.relevance_score, match=j.match_score, link=web_link(offer_link(j.url, j.title, j.apply_url)),
                            status=j.status))
    return pd.DataFrame(out)


def page_results():
    shell("Results", "Every offer found, with its exact link, its publication date and why it was kept.", "Offers")
    df = _rows()
    if df.empty:
        st.html('<div class="jf-empty"><div class="jf-big">No offers yet</div>Follow the three steps on the Search page.</div>')
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
                                      "published": st.column_config.TextColumn(
                                          help="Empty: the website does not state it (see 'first seen')"),
                                      "first_seen": st.column_config.TextColumn("first seen", help="The day the app first found the offer"),
                                      "relevance": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d"),
                                      "match": st.column_config.ProgressColumn("CV match", min_value=0, max_value=100, format="%d"),
                                      "link": st.column_config.LinkColumn("offer", display_text="open")})
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
        with st.container(border=True, key="card_offer"):
            st.subheader(j.title, anchor=False)
            kind = r.get("contract_kind") if r.get("contract_kind") not in (None, "unknown") else ti.get("training")
            st.html(chips([(j.employer, "navy"), (label, cls), (OFFER_TYPES.get(kind or "", ""), "blue"),
                           ("research subject" if ti.get("kind") == "research_topic" else "", "blue"),
                           ({"fr": "français", "en": "English"}.get(ti.get("lang"), ""), "")]
                          + [(t, "") for t in (r.get("research_topics") or ti.get("topics") or [])[:6]]))
            l1, l2, _ = st.columns([1, 1, 3])
            link = web_link(offer_link(j.url, j.title, j.apply_url))
            if link:
                l1.link_button("Open the offer", link, icon=":material/open_in_new:", width="stretch", type="primary")
            if web_link(j.apply_url) and j.apply_url not in (j.url, link) and "jboffer=" not in j.apply_url:
                l2.link_button("Apply page", j.apply_url, icon=":material/send:", width="stretch")
            a, b = st.columns(2)
            seen = (j.extra or {}).get("first_seen")
            when = f"{j.pub_date:%d %b %Y}" if j.pub_date else "not stated by the website" + (f" — first seen by the app on {seen}" if seen else "")
            a.html(f'<div class="jf-kv"><b>Published</b> {when}</div>')
            a.caption(f"{j.date_source or '—'} · “{(j.pub_date_text or '')[:80]}”")
            b.html(f'<div class="jf-kv"><b>Location</b> {html.escape(j.location_original or "unknown")}</div>')
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
    with st.container(border=True, key="card_letter"):
        st.html('<div class="jf-section">CV match & cover letter</div>')
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
    c["auto_models"] = st.toggle("Switch models automatically", c["auto_models"],
                                 help="Starts with the most capable model. When a limit is reached, the next one takes over.")
    if c["auto_models"]:
        nth = {"#2": " (second key)", "#3": " (third key)", "#4": " (fourth key)"}
        order = [f"{i}. {p} / {m}" + nth.get(k[len(p):], "") for i, (p, m, k) in enumerate(llm.model_chain(c, KEYS), 1)]
        st.caption("Order of use: " + " → ".join(order))
    provs = list(c["providers"])
    models = st.session_state.setdefault("models", {})
    a, b = st.columns([1, 2], vertical_alignment="bottom")
    prov = a.selectbox("Provider", provs, index=provs.index(CFG["model"]["provider"]) if CFG["model"]["provider"] in provs else 0,
                       format_func=lambda x: {"ollama": "ollama (local, free)", "openrouter": "Open Router", "xai": "xAI (Grok)", "cohere": "Cohere", "nvidia": "NVIDIA NIM"}.get(x, x))
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
    shell("Settings", "The AI model and your search profile. Every change is saved automatically.", "Preferences")
    t1, t2 = st.tabs(["AI model", "Search profile"])
    with t1, st.container(border=True, key="card_ai"):
        settings_ai()
    with t2, st.container(border=True, key="card_profile"):
        settings_profile()


# =================================================================== page: market statistics (every 15 days)
NAVY, INK_DIM = "#1B3A6B", "#5B6B82"
LABELS = {"job": "Job (CDI / CDD)", "internship": "Internship", "apprenticeship": "Apprenticeship", "phd_postdoc": "PhD / post-doc",
          "phd": "PhD", "master": "Master", "engineer": "Engineering degree", "bachelor": "Bachelor", "none": "None stated",
          "intern": "Intern", "junior": "Junior", "mid": "Mid-level", "senior": "Senior", "lead": "Lead / principal",
          "onsite": "On site", "hybrid": "Hybrid", "remote": "Remote", "unknown": "Not stated"}


def hbar(rows, label, value="offers", prev=None):
    """Ranked counts: one series, one colour, sorted, value written at the bar's end, tooltip on hover."""
    df = pd.DataFrame([[LABELS.get(str(k), k), v] for k, v in rows], columns=[label, value])
    if df.empty:
        st.caption("No data in this period yet.")
        return
    tip = [alt.Tooltip(f"{label}:N"), alt.Tooltip(f"{value}:Q")]
    if prev is not None:
        df["previous period"] = [dict(prev).get(k, 0) for k, _ in rows]
        tip.append(alt.Tooltip("previous period:Q"))
    base = alt.Chart(df).encode(
        y=alt.Y(f"{label}:N", sort="-x", title=None, axis=alt.Axis(ticks=False, domain=False, labelLimit=260, labelColor="#0E1C33",
                                                                    labelOverlap=False, labelPadding=6)),
        x=alt.X(f"{value}:Q", title=None, axis=alt.Axis(grid=False, labels=False, ticks=False, domain=False)))
    bars = base.mark_bar(color=NAVY, cornerRadiusEnd=4, height=14).encode(tooltip=tip)
    text = base.mark_text(align="left", dx=5, color=INK_DIM, fontSize=12).encode(text=f"{value}:Q")
    st.altair_chart((bars + text).properties(height=28 * len(df) + 8, background="transparent").configure_view(stroke=None),
                    width="stretch")


def page_stats():
    shell("Statistics", "The job market of your field: computed every 15 days from the offers the AI analysed.", "Market")
    with SessionLocal() as s:
        snaps = market.snapshots(s, 2)
        kept = s.query(market.MarketOffer).count()
        due_now = market.due(s)
    cur = snaps[0] if snaps else None
    prev = snaps[1].data if len(snaps) > 1 else None
    with st.container(border=True, key="card_stats_head"):
        a, b = st.columns([3, 1], vertical_alignment="center")
        if cur:
            nxt = cur.created + market.timedelta(days=market.PERIOD_DAYS)
            a.markdown(f"**Period:** {cur.period_start:%d %b} – {cur.period_end:%d %b %Y} · next computation on "
                       f"**{nxt:%d %b}** · {kept:,} analysed offers kept in the history (6 months)")
        else:
            a.markdown(f"No statistics yet: they are computed from the offers the AI analyses (history: {kept:,} offers). "
                       "Run a scan with AI analysis first.")
        if b.button("Compute now", disabled=not kept, help="Statistics of the last 15 days, from the offers kept in the history"):
            with SessionLocal() as s:
                market.archive_offers(s)
                market.compute_snapshot(s)
            st.rerun()
        if due_now and cur:
            st.caption("15 days have passed: the next scan with AI analysis computes a new snapshot.")
    if not cur:
        return
    d = cur.data
    pv = (lambda k: prev.get(k)) if prev else (lambda k: None)
    share = lambda rows, key: round(100 * dict(rows).get(key, 0) / max(1, sum(v for _, v in rows)))
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Relevant offers" if d["base"] == "relevant" else "Analysed offers", d["relevant"] or d["analysed"],
              delta=(d["relevant"] - pv("relevant")) if prev else None)
    m2.metric("Companies hiring", d["companies"], delta=(d["companies"] - pv("companies")) if prev else None)
    m3.metric("Remote or hybrid", f"{share(d['remote'], 'remote') + share(d['remote'], 'hybrid')} %")
    m4.metric("Average CV match", f"{d['avg_match']:.0f} / 100" if d.get("avg_match") is not None else "—",
              help="Shown once CVs are compared with relevant offers")
    if d["base"] != "relevant":
        st.info("No offer was judged relevant in this period: the figures cover every offer the AI analysed.")
    c1, c2 = st.columns(2)
    with c1, st.container(border=True, key="card_stats_skills"):
        st.markdown("**Most requested skills**")
        st.caption("Number of offers asking for each skill, tool or method: what to put first on the CV.")
        hbar(d["top_skills"], "skill", prev=pv("top_skills"))
    with c2, st.container(border=True, key="card_stats_gaps"):
        st.markdown("**Skills missing from the CV**")
        st.caption("Requirements of the offers that the CV does not show: what to learn or to make visible.")
        hbar(d["skill_gaps"], "requirement", prev=pv("skill_gaps"))
    c3, c4 = st.columns(2)
    with c3, st.container(border=True, key="card_stats_companies"):
        st.markdown("**Companies hiring the most**")
        hbar(d["top_companies"], "company", prev=pv("top_companies"))
    with c4, st.container(border=True, key="card_stats_cities"):
        st.markdown("**Cities**")
        hbar(d["cities"], "city", prev=pv("cities"))
    with st.container(border=True, key="card_stats_weeks"):
        st.markdown("**Offers per week**")
        wk = pd.DataFrame(d["weeks"], columns=["week", "offers"])
        if len(wk):
            st.altair_chart(alt.Chart(wk).mark_bar(color=NAVY, cornerRadiusEnd=4, size=26)
                            .encode(x=alt.X("week:N", title=None, axis=alt.Axis(labelAngle=0, ticks=False, domain=False)),
                                    y=alt.Y("offers:Q", title=None, axis=alt.Axis(grid=True, gridColor="#E9EEF6", domain=False, ticks=False)),
                                    tooltip=["week", "offers"]).properties(height=180, background="transparent")
                            .configure_view(stroke=None), width="stretch")
    st.markdown('<div class="jf-section">Profile of the offers</div>', unsafe_allow_html=True)
    p1, p2, p3 = st.columns(3)
    for col, key, title in ((p1, "contracts", "Contract"), (p2, "degree", "Degree required"), (p3, "seniority", "Seniority")):
        with col, st.container(border=True, key=f"card_stats_{key}"):
            st.markdown(f"**{title}**")
            hbar(d[key], title.lower())
    p4, p5 = st.columns(2)
    with p4, st.container(border=True, key="card_stats_languages"):
        st.markdown("**Languages required**")
        hbar(d["languages"], "language")
    with p5, st.container(border=True, key="card_stats_remote"):
        st.markdown("**Remote work**")
        hbar(d["remote"], "remote work")
    t1, t2 = st.tabs(["Best CV matches", "Salaries stated"])
    with t1:
        if d["best_titles"]:
            st.dataframe(pd.DataFrame(d["best_titles"], columns=["offer", "company", "CV match"]), hide_index=True, width="stretch")
        else:
            st.caption("Upload a CV: the offers it matches best are listed here.")
    with t2:
        if d["salaries"]:
            st.dataframe(pd.DataFrame(d["salaries"], columns=["offer", "company", "salary as written"]), hide_index=True, width="stretch")
        else:
            st.caption("No offer of this period states a salary.")
    with st.expander("Websites giving the most offers of your field"):
        hbar(d["websites"], "website")


# =================================================================== page 4: what each AI key has left
QUOTA_SLOTS = ("gemini", "gemini#2", "gemini#3", "gemini#4", "groq", "groq#2", "groq#3", "groq#4", "nvidia", "openrouter", "openrouter#2", "openrouter#3", "cohere", "cohere#2", "cohere#3", "xai")
PROVIDER_NAME = {"gemini": "Gemini", "groq": "Groq", "nvidia": "NVIDIA NIM", "openrouter": "OpenRouter", "cohere": "Cohere",
                 "xai": "xAI (billed)"}
COHERE_MONTH = 1000  # Cohere trial keys: 1,000 calls a month


@st.cache_data(ttl=300, show_spinner=False)
def _openrouter_info(key):
    return llm.openrouter_key_info(key)


def _share(left, total):
    return max(0.0, min(1.0, left / total)) if total else 0.0


def _model_bar(prov, model, slot, rec, now, gone):
    """One model on one key: what the provider reported, or what the app counted."""
    e, today = rec.get(f"{slot}|{model}", {}), time.strftime("%Y-%m-%d")
    calls = e.get("calls", 0) if e.get("day") == today else 0
    until = max(e.get("until", 0), llm._EXHAUSTED.get((prov, model, slot), 0))
    if f"{slot}|{model}" in gone:
        st.progress(0.0, text=f"{model} — no longer offered on this key: skipped for a week")
    elif e.get("error") and e.get("error_at", 0) > e.get("last_ok", 0) and now - e.get("error_at", 0) < 86400:
        st.progress(0.0, text=f"{model} — refused by the provider: {e['error'][:90]}")
    elif until > now:
        st.progress(0.0, text=f"{model} — limit reached, back at {datetime.fromtimestamp(until):%H:%M} ({calls} call{'s' if calls != 1 else ''} today)")
    elif e.get("limit") and e.get("day") == today:
        st.progress(_share(e["remaining"], e["limit"]),
                    text=f"{model} — {e['remaining']:,} of {e['limit']:,} requests left today (reported by {PROVIDER_NAME[prov]})")
    elif e.get("learned"):
        left = max(0, e["learned"] - calls)
        st.progress(_share(left, e["learned"]), text=f"{model} — about {left} of ~{e['learned']} calls left today "
                                                     "(limit reached on an earlier day)")
    else:
        st.progress(1.0, text=f"{model} — available · {calls} call{'s' if calls != 1 else ''} today (no remaining count published)")


def page_quota():
    shell("AI quota", "What each API key has left: as the provider reports it, or as the app counts its calls.", "Settings")
    st.caption("Gemini quotas reset around 09:00 (midnight Pacific time); Groq frees requests gradually over the day; "
               "Cohere's trial limit is monthly; NVIDIA limits requests per minute. Models are used in this order, best first.")
    rec, now, gone, keys = llm.quota_record(), time.time(), llm._gone(), load_keys()
    today = time.strftime("%Y-%m-%d")
    for slot in QUOTA_SLOTS:
        prov = slot.split("#")[0]
        key = keys.get(slot) or (os.environ.get(ENV_KEYS.get(slot, ""), "") if slot in ENV_KEYS else "")
        if not key:
            continue
        with st.container(border=True, key=f"card_quota_{slot.replace('#', '_')}"):
            nth = {"#2": " — second key", "#3": " — third key", "#4": " — fourth key"}.get(slot[len(prov):], "")
            st.markdown(f"**{PROVIDER_NAME[prov]}**{nth} · `{llm.mask(key)}`")
            entries = {k.split("|", 1)[1]: v for k, v in rec.items() if k.startswith(slot + "|")}
            if prov == "cohere":
                used = sum(v.get("month_calls", 0) for v in entries.values() if v.get("month") == today[:7])
                st.progress(_share(COHERE_MONTH - used, COHERE_MONTH),
                            text=f"Key — about {max(0, COHERE_MONTH - used):,} of {COHERE_MONTH:,} trial calls left this month")
            if prov == "openrouter":
                info = _openrouter_info(key)
                cap = 50 if info.get("is_free_tier", True) else 1000  # free models: 50 a day, 1,000 after $10 of credits
                used = sum(v.get("calls", 0) for v in entries.values() if v.get("day") == today)
                st.progress(_share(cap - used, cap), text=f"Key — about {max(0, cap - used)} of {cap} free-model requests left today"
                            + ("" if info else " (OpenRouter's account figures could not be read)"))
            for p_, model in llm.LADDER:
                if p_ == prov:
                    _model_bar(prov, model, slot, rec, now, gone)
    jp_key = keys.get("jobspipe") or os.environ.get("JOBSPIPE_API_KEY", "")
    if jp_key:
        with SessionLocal() as s:
            jp = get_setting(s, "jobspipe_credits", {})
        with st.container(border=True, key="card_quota_jobspipe"):
            st.markdown(f"**JobsPipe** (job data, not AI) · `{llm.mask(jp_key)}`")
            if jp.get("allowance"):
                st.progress(_share(jp["remaining"], jp["allowance"]),
                            text=f"{jp['remaining']:,} of {jp['allowance']:,} credits left "
                                 f"(at the last scan, {datetime.fromtimestamp(jp['at']):%d %b %H:%M})")
            else:
                st.progress(1.0, text="Credits are shown after the first scan that reads JobsPipe")


# =================================================================== navigation
RESULTS = st.Page(page_results, title="Results", icon=":material/work:", url_path="results")
brand()
st.navigation([st.Page(page_search, title="Search", icon=":material/travel_explore:", default=True), RESULTS,
               st.Page(page_stats, title="Statistics", icon=":material/insights:", url_path="statistics"),
               st.Page(page_quota, title="AI quota", icon=":material/speed:", url_path="quota"),
               st.Page(page_settings, title="Settings", icon=":material/settings:", url_path="settings")], position="top").run()
for _n, _new, _old in (("config", c, CFG), ("profile", p, PROFILE), ("applicant", ap, APPLICANT)):
    autosave(_n, _new, _old)
