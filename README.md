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

1. **Websites**: upload one or more HTML files listing career-page URLs (`websites.html` to `websites8.html`;
   `websites5.html` holds STATION F startups, `websites6.html` startups funded in 2025–2026, Next40 / French Tech 120
   scale-ups and large groups; `websites7.html` AI labs with Paris teams, deep-tech, scale-ups and more large groups;
   `websites8.html` ESN, large to very small), or paste URLs. Addresses written without quotes, in comments, are not read.
2. **CV**: upload PDF CVs. A CV is sent to a hosted AI only after you tick the consent box.
3. **Analysis**: choose the offer types and the date range (24 h to 30 days), then press **Start**. One bar per
   uploaded file shows how many of its websites were searched: read, partly read, blocked or failed, and not reached
   (the scan stopped first). The websites not searched are listed under the bars at the end.

The **Statistics** page shows the job market of the field, computed every 15 days from the offers the AI analysed: most requested skills, skills missing from the CV, companies hiring, cities, offers per week, contract, degree, seniority, languages, remote work, salaries stated and best CV matches. Each figure is compared with the previous 15 days. A skill counts only when the offer names it, so a model's guess is dropped; "skills missing from the CV" are the skills the offers name that the CV does not mention. The offers' facts are kept 6 months for it, even though each scan starts fresh. Tick "Update the statistics at the end of this scan" in step 3 to recompute them after a scan. The **AI quota** page shows what each AI key has left.

The **Results** page has five views: Relevant, To check, Not analysed, Excluded and All. The table uses the screen's
width and shows 20 offers at once. Each offer shows the evidence for its date and place, how long it has been online,
its contract, its relevance and its CV match; the list exports to CSV. Every launch starts a fresh session.
Settings, keys and CVs are kept.

Offers read in the last 24 hours are kept with their AI analysis, even after a restart or a new file: a new scan does not
open their pages again, and the AI does not analyse them again, which saves the AI quota. An analysis is reused only for
the same search profile, and a CV match only for the same CVs; an offer outside the new date range is hidden. Step 3
shows how many offers are kept; **Delete them** deletes them and the current results, so the next scan reads and
analyses everything again.

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
comes back as soon as its limit has passed. A second key gives a model its own quota. During a scan each model is
watched: three unusable answers in a row pause it for 10 minutes (then 30, then 60), and two answers slower than 45
seconds in a row pause it for 10 minutes, so the scan never waits on a misbehaving model. Four offers are analysed at
a time; one model serves at most two of them at once, and the others go to the next model in the order, so the best
models do most of the work. The activity log ends with what each model did. The order (`LADDER` in
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
- **Website filters**: the app uses the website's own France filter and its "newest first" sort when it has them (the AI
  browser clicks them on sites that need it). On a newest-first list, reading stops at the first offers older than the
  date range (two in a row, in case one old offer is pinned at the top). Offers whose list card places them only outside
  France are not opened.
- **Filters, in order**:
  1. **Date**: from evidence only. Refresh, deadline and start dates are never used. An undated offer is always
     returned, marked "not stated", with the first day the app saw it online ("online since", from its memory of the
     offers or the statistics history): it was published that day or before.
  2. **France**: tested against official communes, departments and regions. An offer open in several cities is kept
     when one of them is in France ("Bruxelles | Île-de-France", "Paris, Barcelona", "Munich - Berlin - London - Paris"),
     or when its text says it can be held in France ("living in Germany, France or Italy", "eligible to work from Belgium or
     France", "this role is based in Paris or in Barcelona"),
     and a location field is cut where the page's next field starts ("Palaiseau Avantages 40-45k€..." -> "Palaiseau").
  3. **Title**: French and English. Business roles are rejected without an AI call, unless the title also names a
     wanted role in full ("Marketing Data Scientist", "Data Scientist Finance"): the AI decides those.
- **AI analysis**: confirms the type, place and date, scores relevance, and matches each relevant offer with your CVs.
  The AI reads each offer without its boilerplate: the company presentation (often written twice), labels, values,
  diversity statement, benefits, breadcrumbs, application form and similar-offers list are removed, about 40% of the
  text; every sentence stating a fact (contract, salary, remote work, place, date, experience, degree, languages,
  duration) is kept. The dates and the quotes the AI gives are checked against the full text.
  An offer whose contract is not stated is kept ("contract: not stated"): the AI must quote the words that state a
  contract before an offer is set aside for its type, and the quote decides: "contrat CDD 6 mois" is a job (CDI / CDD),
  not an internship, while a PhD or post-doc stays one even though it is a fixed-term contract too.
  The same model can score the same offer 35 one time and 85 the next: an offer excluded with a borderline score (25 up
  to the minimum relevance) is read a second time, and goes to "To check" ("AI readings disagree") when that reading
  finds it relevant.
- **Duplicates**: the same offer on several websites, or posted twice on one website, is shown once.
- **Unreachable job pages**: a website whose job pages do not answer twice in a row (candidat.francetravail.fr from
  some networks) is not opened again for 30 minutes; its offers are analysed with the text already read.

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

- Each scan reads 4 websites in parallel, stops after 90 minutes and spends at most 20 minutes per website. Every offer
  that passes the filters is analysed by the AI, best offers first; the billed model (xAI) answers at most 100 requests
  per scan, so when every free quota is used up, the remaining offers stay "Not analysed" until the quotas come back.
- These sites can't be read automatically, so add the companies' own career pages instead:
  - LinkedIn, Indeed and Apec: login or anti-bot protection;
  - welcometothejungle.com: its servers refuse automated browsers (use `<company>.welcomekit.co` or `jobs.stationf.co`
    instead);
  - JobTeaser and Glassdoor: anti-bot protection.
- Workday may refuse requests for a few hours after several full scans in a row.
- Hellowork company pages: only the first page (about the 30 newest offers) is read; robots.txt closes the next ones.

## Project

```
app.py               interface (Search, Results, Settings)
jobfinder/           crawler.py, adapters.py (platform readers), scan.py (engine), dates.py, classify.py,
                     llm.py (models), analysis.py, trim.py (text sent to the AI), cv.py, store.py (SQLite, keys),
                     config.py (defaults, limits)
assets/ static/      dark navy theme, Telegraf font (the only typeface)
tests/               offline tests: python -m pytest -q
```
