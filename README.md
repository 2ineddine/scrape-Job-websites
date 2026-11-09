# Job Finder

Job Finder reads company and lab career websites and finds research and engineering offers in France. It dates each
offer from evidence and ranks it with AI against a search profile and your CVs. It can also draft French cover letters
in LaTeX. It is a single-user Streamlit app.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
streamlit run app.py          # http://localhost:8501
```

Optional: a LaTeX distribution (`latexmk`), to compile cover letters to PDF.

## Use

1. **Websites**: upload one or more HTML files listing career-page URLs (`websites.html` to `websites6.html`;
   `websites5.html` holds STATION F startups, `websites6.html` startups funded in 2025–2026, Next40 / French Tech 120
   scale-ups and large groups; `websites7.html` AI labs with Paris teams, deep-tech, scale-ups and more large groups), or paste URLs.
2. **CV**: upload PDF CVs. A CV is sent to a hosted AI only after you tick the consent box.
3. **Analysis**: choose the offer types and the date range (24 h to 30 days), then press **Start**.

The **Statistics** page shows the job market of the field, computed every 15 days from the offers the AI analysed: most requested skills, skills missing from the CV, companies hiring, cities, offers per week, contract, degree, seniority, languages, remote work, salaries stated and best CV matches. Each figure is compared with the previous 15 days. A skill counts only when the offer names it, so a model's guess is dropped; "skills missing from the CV" are the skills the offers name that the CV does not mention. The offers' facts are kept 6 months for it, even though each scan starts fresh. Tick "Update the statistics at the end of this scan" in step 3 to recompute them after a scan. The **AI quota** page shows what each AI key has left.

The **Results** page has five views: Relevant, To check, Not analysed, Excluded and All. Each offer shows the evidence
for its date and place, its relevance and its CV match; the list exports to CSV. Every launch starts a fresh session.
Settings, keys and CVs are kept.

## AI keys

Put the keys in `api_keys.txt`, next to `app.py`. The file is git-ignored. You can also enter them in Settings, or set
them as environment variables.

```
GEMINI_API_KEY=...     GEMINI_API_KEY_2=...     GEMINI_API_KEY_3=...     GEMINI_API_KEY_4=...     GROQ_API_KEY=...     GROQ_API_KEY_2=...     GROQ_API_KEY_3=...     GROQ_API_KEY_4=...
NVIDIA_API_KEY=...     OPENROUTER_API_KEY=...   OPENROUTER_API_KEY_2=...   OPENROUTER_API_KEY_3=...   COHERE_API_KEY=...   COHERE_API_KEY_2=...   COHERE_API_KEY_3=...   XAI_API_KEY=...  (billed)
FRANCE_TRAVAIL_CLIENT_ID=...   FRANCE_TRAVAIL_CLIENT_SECRET=...      (optional, official job API)
JOBSPIPE_API_KEY=...                                                (optional, job-data API)
```

With `JOBSPIPE_API_KEY`, the address `https://jobspipe.dev/` in a website list searches the JobsPipe API. It searches offers in France whose
title matches the profile, within the date range, coming from career sites, ATS platforms and France Travail.
Indeed and LinkedIn are left out, because their terms forbid automated reading. Each scan reads at most 250 offers: one credit per new offer each month.

The app starts with the strongest model. When a model reaches its limit, the next one takes over, and the better model
comes back as soon as its limit has passed. A second key gives a model its own quota. The order (`LADDER` in
`jobfinder/llm.py`) is:

Gemini 3.6 Flash → Gemini 3.5 Flash → Groq gpt-oss-120b → Nemotron 3.5 Super (NVIDIA, OpenRouter) → Cohere Command A
Reasoning → Nemotron 3 Ultra (NVIDIA, OpenRouter) → Gemini 2.5 Flash → Command A → Qwen 3.8 → dots-3 → Gemini 3.5
Flash-Lite → Nemotron Lightning → Command R+ → Gemini 3.1 Flash-Lite → gpt-oss-20b → Command R → xAI (billed, last).

A local Ollama model also works (`ollama serve`). Free plans are small: Groq, for example, allows about 65 offers per key
per day. Two scans a day need several keys, or a paid key. Not usable without paid credits: SambaNova and Ollama
Cloud. Not usable at all: GitHub Models (closed in July 2026) and Kaggle (no AI chat API).

## How it works

- **Reading**: the app picks a reader per website. Platform readers use public APIs and feeds (Workday, SuccessFactors,
  iCIMS, Phenom, Eightfold, Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Teamtailor, Talentsoft, Welcome to the
  Jungle career sites, Personio, France Travail API…). Other websites go to a generic crawler that reads structured data, links,
  sitemaps and pages rendered with JavaScript once. Lab pages go to an AI page reader, and sites that need clicking go
  to an AI browser.
- **Filters, in order**:
  1. **Date**: from evidence only. Refresh, deadline and start dates are never used. An undated offer is kept and
     marked "date not stated".
  2. **France**: tested against official communes, departments and regions.
  3. **Title**: French and English. Business roles are rejected without an AI call.
- **AI analysis**: confirms the type, place and date, scores relevance, and matches each relevant offer with your CVs.
- **Duplicates**: the same offer on several websites, or posted twice on one website, is shown once.

## Safety

- robots.txt is respected, and anti-bot protections are never bypassed. Blocked websites are reported with the reason.
- Text from websites is treated as untrusted. Private network addresses are refused. LaTeX runs without shell escape.
- The app has no login: keep it on `localhost`, or reach it through an SSH tunnel.

## Docker

```bash
docker build -t job-finder .
docker run -d -p 127.0.0.1:8501:8501 --env-file api_keys.txt -v job-finder-data:/app/data job-finder
```

## Limits

- Each scan reads 4 websites in parallel, stops after 90 minutes, spends at most 20 minutes per website, and sends at
  most 400 offers to the AI.
- These sites can't be read automatically, so add the companies' own career pages instead:
  - LinkedIn, Indeed and Apec: login or anti-bot protection;
  - welcometothejungle.com: its servers refuse automated browsers (use `<company>.welcomekit.co` or `jobs.stationf.co`
    instead);
  - JobTeaser and Glassdoor: anti-bot protection.
- Workday may refuse requests for a few hours after several full scans in a row.

## Project

```
app.py               interface (Search, Results, Settings)
jobfinder/           crawler.py, adapters.py (platform readers), scan.py (engine), dates.py, classify.py,
                     llm.py (models), analysis.py, cv.py, store.py (SQLite, keys), config.py (defaults, limits)
assets/ static/      navy theme, Telegraf font
tests/               offline tests: python -m pytest -q
```
