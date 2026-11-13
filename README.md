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

1. **Websites**: upload one or more HTML files listing career-page URLs (`websites.html` to `websites9.html`;
   `websites5.html` holds STATION F startups, `websites6.html` startups funded in 2025–2026, Next40 / French Tech 120
   scale-ups and large groups; `websites7.html` AI labs with Paris teams, deep-tech, scale-ups and more large groups;
   `websites8.html` ESN, large to very small; `websites9.html` banks, insurers, asset managers, payment and credit
   fintechs, quant funds and the consulting firms with data teams), or paste URLs. Addresses written without quotes, in
   comments, are not read.
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
comes back as soon as its limit has passed. A second key gives a model its own quota. The models are ranked in three
quality levels, then the billed model, and quality comes before speed:

- a model stopped by a per-minute limit ("retry in 20 s") is replaced at once by another model **of the same level**, and
  used again as soon as its limit has passed;
- when every model of the best level is stopped only for a moment (up to 90 seconds), the offer waits for one of them
  instead of going to a weaker model; it goes down a level only when the better models are out for longer (daily quota,
  a pause);
- an offer read by a model of the third level (small models) is read again by a better model at the end of the scan, when
  one is free; the better reading counts, and the offer keeps a note of the first one.

Eight offers are analysed at a time. Each model starts with two requests at once and learns what it accepts: one more
after good answers about as fast as its first ones, one fewer after a much slower answer (a provider that queues
requests), half as many after a rate-limit refusal, up to six. Each model is also watched:
three unusable answers in a row pause it for 10 minutes (then 30, then 60), and two answers slower than 45 seconds in a
row pause it for 10 minutes. The activity log ends with what each model did. The order (`LEVELS` in `jobfinder/llm.py`):

1. Gemini 3.6 Flash → Gemini 3.5 Flash → Groq gpt-oss-120b → Nemotron 3.5 Super (NVIDIA, OpenRouter) → Cohere Command A
   Reasoning → Nemotron 3 Ultra (NVIDIA, OpenRouter);
2. Gemini 2.5 Flash → Command A → Qwen 3.8 → dots-3;
3. Gemini 3.5 Flash-Lite → Nemotron Lightning (NVIDIA, OpenRouter) → Command R+ → Gemini 3.1 Flash-Lite → gpt-oss-20b →
   Command R;
4. xAI (billed, last).

A local Ollama model also works (`ollama serve`). Free plans are small: Groq, for example, allows about 65 offers per key
per day. Two scans a day need several keys, or a paid key. Not usable without paid credits: SambaNova and Ollama
Cloud. Not usable at all: GitHub Models (closed in July 2026) and Kaggle (no AI chat API).

## How it works

- **Reading**: the app picks a reader per website. Platform readers use public APIs and feeds (Workday, SuccessFactors,
  iCIMS, Phenom, Eightfold, Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Teamtailor, Talentsoft, Welcome to the
  Jungle career sites, Personio, France Travail API…). Other websites go to a generic crawler that reads structured data, links,
  sitemaps and pages rendered with JavaScript once. Lab pages go to an AI page reader, and sites that need clicking go
  to an AI browser. A list that announces far more offers than it shows, with no next-page link to open ("523 offres",
  15 shown), is completed in the browser: its "more" button or link is clicked until the list stops growing, else its
  "next" control page after page (Crédit Mutuel: about 220 offers read instead of 15). Page numbers with a hidden
  "Page :" prefix are followed (Crédit Agricole: its 41 pages instead of the first one).
- **Hellowork search with your keywords** (step 3, ticked by default): at each scan, the app reads Hellowork's open page
  of every keyword of the search profile, and its job-title page ("data scientist", "research engineer"). Their offers go
  through the same filters as any website, the date range first: an offer whose card is older than the range is not
  opened, and every offer is dated from its own page. An offer listed under several keywords is read once, and an offer
  also on its company's site is shown once. Hellowork company pages listed in a website file are read too.
- **Website filters**: the app uses the website's own France filter and its "newest first" sort when it has them (the AI
  browser clicks them on sites that need it). Reading stops early only on a list that its own dates prove newest first
  (one pinned offer at the top allowed): then it stops at the first offers older than the date range (two in a row).
  A list whose dates are mixed, even with a "sort by date" option, is read to the end. Old offers are skipped without
  opening them when their card shows a date. Offers whose list card places them only outside France are not opened.
- **No offer dropped while reading**: an offer is judged by its list card only when the card's title agrees with the
  words of the offer's address (a card may show only a category such as "Consulting"); otherwise its page is opened.
  When a website's time or page budget runs out, or robots.txt closes an offer's page, the offer is kept as its card
  shows it. A Teamtailor feed holds 100 offers; when it is full and still inside the date range, the rest is read on
  the career site. A career page that has moved is read at the address its home page links, with a note to update it.
  A job platform that fails for a moment (HTTP 503) makes the website fail, so the scan reads it again at the end.
- **Filters, in order**:
  1. **Date**: from evidence only. Refresh, deadline and start dates are never used, nor the dates of other offers shown
     on an offer's page ("similar offers"): the page's text replaces the offer's own date only with an earlier one. An undated offer is
     returned, marked "not stated", with the first day the app saw it online ("online since", from its memory of the
     offers or the statistics history): it was published that day or before. So once that day is before the date range,
     the offer is outside the range and leaves the results like a dated offer of that age; a wider range brings it back.
     With "Last 24 hours", an undated offer shows in the scans of the day it is first seen and the next day (in the scan
     of 12 Nov 2026, 145 of the 213 relevant offers had no date: Inria, Ensta, Davidson...; before this rule, the same
     ones came back every day).
  2. **France**: tested against official communes, departments and regions. An offer open in several cities is kept
     when one of them is in France ("Bruxelles | Île-de-France", "Paris, Barcelona", "Munich - Berlin - London - Paris"),
     or when its text says it can be held in France ("living in Germany, France or Italy", "eligible to work from Belgium or
     France", "this role is based in Paris or in Barcelona"),
     and a location field is cut where the page's next field starts ("Palaiseau Avantages 40-45k€..." -> "Palaiseau").
  3. **Title**: French and English. Business roles are rejected without an AI call, unless the title also names a
     wanted role in full ("Marketing Data Scientist", "Data Scientist Finance"): the AI decides those.
- **AI analysis**: starts with the scan. Each website's offers go through the filters and to the AI as soon as the website
  is read, while the next websites are read; the CV match follows once every offer is analysed. The analysis confirms the
  type, place and date, scores relevance, and matches each relevant offer with your CVs. No offer is dropped on one
  failure: an unusable answer or an error sends the offer back to the queue, to be read again after the others; when
  every model is at its limit, the offers wait in the queue (up to 30 minutes) for the first model to be free again.
  The AI reads each offer without its boilerplate: the company presentation (often written twice), labels, values,
  diversity statement, benefits, breadcrumbs, application form and similar-offers list are removed, about 40% of the
  text; every sentence stating a fact (contract, salary, remote work, place, date, experience, degree, languages,
  duration) is kept. The dates and the quotes the AI gives are checked against the full text.
  An offer whose contract is not stated is kept ("contract: not stated"): the AI must quote the words that state a
  contract before an offer is set aside for its type, and the quote decides: "contrat CDD 6 mois" is a job (CDI / CDD),
  not an internship, while a PhD or post-doc stays one even though it is a fixed-term contract too.
  The same model can score the same offer 35 one time and 85 the next: an offer the AI turns down with a score of 25 or
  more is read a second time (with a low minimum relevance, the AI's "not relevant" decides, so this covers it too), and
  goes to "To check" ("AI readings disagree") when that reading finds it relevant.
- **Duplicates**: the same offer on several websites, or posted twice on one website, is shown once.
- **Unreachable job pages**: a website whose job pages do not answer twice in a row (candidat.francetravail.fr from
  some networks) is not opened again for 30 minutes; its offers are analysed with the text already read.

## Safety

- **Respect robots.txt** (step 3) is off by default: the client reads these websites under partner agreements. Off, no
  robots.txt rule is applied (Hellowork's search and next pages, SmartRecruiters' data feed, every offer page are read),
  and what robots.txt gives that costs no offer is still used: the sitemaps it lists (3DS: about 700 offers only there),
  and the pause it asks between two requests (up to 5 seconds, the website then gets more time). On, the app reads only what
  each website's robots.txt allows. Checked on the 372 websites on 2026-11-12: 336 have a robots.txt, and its rules
  close pages with offers on 20 of them. Either way the app keeps its own name, reads at most one page per second per
  website, never loads advertising or tracking requests, and never gets around an anti-bot page, a login or a captcha.
- **A scan that did not finish is resumed**: stopped with the Stop button, cut by the 6-hour limit, stopped because the AI
  credits or quotas ran out, or cut because the app was closed or the computer turned off. Its websites and results stay
  until the next launch, and the Search page says why it did not finish and offers two choices. **Resume** reads only the
  websites not read yet (and those cut short) and sends the offers not analysed yet to the AI first, with that scan's date
  range and search profile but today's AI keys and models, so credits or a key added in Settings are used. **Start again**
  reads every website from the first one (the offers read in the last 24 hours are still not opened or analysed again).
- **Websites that did not work**: after each scan (and while it runs), the Search page lists every website that was
  blocked, not reachable, gone (page not found), partly read, read without any offer, possibly incomplete, changed since
  the last scan or moved, with the reason and what to do; the list downloads as CSV. A career page that says it has no
  offer now ("We have no job openings at the moment!", "Nous n'avons pas d'offres en ce moment") works: it is not in
  that list, and no AI browser is opened for it.
- Text from websites is treated as untrusted. Private network addresses are refused. LaTeX runs without shell escape.
- The app has no login: keep it on `localhost`, or reach it through an SSH tunnel.

## Docker

```bash
docker build -t job-finder .
docker run -d -p 127.0.0.1:8501:8501 --env-file api_keys.txt -v job-finder-data:/app/data job-finder
```

## Limits

- Each scan reads 4 websites in parallel, reads every website (a safety stop after 6 hours) and spends at most 20 minutes per website; the AI
  analysis has no time limit. Every offer that passes the filters is analysed by the AI, the most promising first among
  those waiting; the billed model (xAI) answers at most 100 requests per scan. When every free quota is used up for
  longer than 30 minutes, the analysis stops with a message and the remaining offers stay "Not analysed" until the
  quotas come back.
- These sites can't be read automatically, so add the companies' own career pages instead:
  - LinkedIn, Indeed and Apec: login or anti-bot protection;
  - welcometothejungle.com: its servers refuse automated browsers (use `<company>.welcomekit.co` or `jobs.stationf.co`
    instead);
  - JobTeaser and Glassdoor: anti-bot protection.
- Workday may refuse requests for a few hours after several full scans in a row.
- Hellowork, with robots.txt respected: its robots.txt closes its search engine, its date filter and every next page (any
  address with "?"). A company page shows its 30 newest offers, and a keyword or job-title page its 20 newest offers in
  France: for a broad word ("intelligence artificielle", 7,800 offers) that is a few hours of offers, for a narrow one
  ("computer vision") all of them. With **Respect robots.txt** off, Hellowork's search is read for every keyword, sorted
  newest first and page after page until the offers are older than the date range, then the keyword pages (checked on
  2026-11-12 with the profile's 42 keywords and "Last 24 hours": 231 offers of the day instead of 75, in about 10 minutes).
  A Hellowork search address in a website file keeps its own filters (place, contract) the same way. Hellowork's company
  search stays closed: add a company's page by its address.