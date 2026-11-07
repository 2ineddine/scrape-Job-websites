# Job Finder

Job Finder is a single-user application that monitors company and laboratory career websites, extracts research and
engineering offers, establishes their actual publication date, retains offers located in France (or remote positions
open to France), and uses a language model to assess their relevance, compare them with the user's CVs and draft
French cover letters in LaTeX.

The interface is built with Streamlit: a navy website look (top navigation bar, navy page headers with live
counters, white cards) set in the Telegraf typeface, which the application serves itself (`static/fonts`).

## Contents

1. [Features](#features)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Usage](#usage)
6. [How it works](#how-it-works)
7. [Data and privacy](#data-and-privacy)
8. [Deployment](#deployment)
9. [Project structure](#project-structure)
10. [Testing](#testing)
11. [Known limitations](#known-limitations)

## Features

- Reads heterogeneous career websites through dedicated platform readers (Workday, SuccessFactors, iCIMS, Phenom,
  Eightfold, Jibe, Workable, SmartRecruiters, Greenhouse, Lever, Ashby, Teamtailor, Talentsoft, WordPress and others),
  a generic crawler, and two AI-assisted readers for laboratory pages and JavaScript-only sites.
- Uses each website's own filters (country, posting period, sort by date) and stops reading a website as soon as the
  selected date range is covered.
- Determines the publication date from evidence, never from a template, update, start or deadline date.
- Analyses job titles in French and English, including research-subject titles that describe a problem rather than a
  role.
- Classifies offers by type: employment (CDI / CDD), internship, apprenticeship, PhD / post-doctoral position.
- Scores relevance against a research profile and matches relevant offers against one or more CVs.
- Generates editable cover letters in LaTeX, compiled to PDF when a LaTeX distribution is installed.

## Requirements

- Python 3.10 or later (tested with Python 3.11).
- Chromium for Playwright (installed by the command below), used for JavaScript rendering and the AI browser.
- Access to a language model: a local Ollama server or an API key for a hosted provider.
- Optional: a LaTeX distribution (`latexmk`, `pdflatex`) to compile cover letters to PDF.

## Installation

```bash
git clone <repository-url> job-finder
cd job-finder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Optional, for PDF cover letters on Debian / Ubuntu:

```bash
sudo apt install latexmk texlive-latex-recommended texlive-latex-extra texlive-lang-french
```

## Configuration

### Language model

A single model is used for every AI task. It is selected in **Settings > AI model**.

| Provider | Setup |
|---|---|
| Ollama (local, default) | `ollama serve`, then `ollama pull llama3.1` |
| Groq, Gemini, OpenAI, Anthropic, OpenRouter | enter the API key in Settings, or set the environment variable `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `OPENROUTER_API_KEY` |

Models validated on the application's tasks (offer analysis, publication date selection, laboratory page reading,
browser navigation) are labelled "tested" in Settings:

| Provider | Model | Notes |
|---|---|---|
| Groq | `qwen/qwen3.8-27b` | fastest; recommended |
| Groq | `openai/gpt-oss-20b`, `openai/gpt-oss-120b` | |
| Gemini | `gemini-2.5-flash`, `gemini-3.1-flash-lite`, `gemini-3.5-flash` | slower responses |

Gemma, speech, image and other non-chat models are not offered.

Free hosted plans are limited. At the time of writing, the Groq free plan allows about 200,000 tokens per day per model
(roughly one moderate search), and the Gemini free plan allows only 20 requests per day for `gemini-2.5-flash`, which is
not enough for a search over many websites. For regular use over large website lists, use a paid API key or a local
Ollama model. When a daily quota is exhausted, the application stops sending requests to that model until the quota
resets and states when this will happen.

### API keys

Keys entered in the application are stored in `api_keys.txt` next to `app.py`. This file is excluded from version
control. Environment variables take the same role and are preferred for server deployments.

### France Travail

France Travail's offers (its own and those of its partner job boards) are read only through its official "Offres
d'emploi" API, with your own access; its web pages are never read. At the time of writing, France Travail no longer
opens this API to new users on francetravail.io and grants access on request:

1. ask for access with the contact form of https://francetravail.io (organisation, use: job search for your client);
2. once granted, create an application on francetravail.io that subscribes to the "Offres d'emploi" API;
3. add its two identifiers to `api_keys.txt` (or as environment variables):

   ```
   FRANCE_TRAVAIL_CLIENT_ID=...
   FRANCE_TRAVAIL_CLIENT_SECRET=...
   ```

4. add a France Travail address to the website list: `https://www.francetravail.fr/accueil` searches the profile's
   keywords; a search address such as `https://candidat.francetravail.fr/offres/recherche?motsCles=data+scientist`
   searches its own words.

Each keyword is one search, newest first, limited to the date range ("published within 1, 3, 7, 14 or 31 days"). The
offer's link is its France Travail page; for a partner offer, the application link is the partner's page. Without
the identifiers the website is reported with these steps and nothing is sent. This reader is tested against the API's
documented answers; it has not yet been run against the live API.

### Search profile

**Settings > Search profile** holds the research interests (free text), the wanted topics and titles (French or
English, one per line), the words that exclude an offer, and the minimum relevance score. Settings are saved
automatically. The default list holds about 130 job titles and topics in both languages (machine learning
and AI roles, perception, LiDAR / SLAM, imaging, audio and speech, signal processing, robotics, MLOps, generative AI...).
Keywords added to the defaults in a new version are added once to a saved profile; the user's own keywords stay, and a
keyword deleted afterwards is not added back. Every keyword is given to the AI that judges relevance.

## Usage

Start the application:

```bash
source .venv/bin/activate
streamlit run app.py
```

Then open `http://localhost:8501`. The **Search** page guides the user through three steps:

1. **Career websites.** Upload one or more HTML files that list career page URLs (for example `websites.html`), or paste
   URLs directly. Files are read as text and never executed. `websites5.html` lists STATION F startups in AI, robotics,
   vision and data (their pages on the STATION F job board) and Future 40 alumni on their own career pages.
2. **CV.** Upload one or more PDF CVs. Contact details for cover letters can be entered in this step. Sending CV text to
   a hosted model requires explicit consent (checkbox).
3. **Analysis.** Select the offer types (jobs, internships, apprenticeships, PhD / post-doc) and the publication period
   (24 hours, 3, 7, 14 or 30 days, or all dates), then select **Start**. Progress, counters and an activity log are
   displayed while the analysis runs; it can be stopped at any time.

The **Results** page lists offers in five views:

| View | Content |
|---|---|
| Relevant | offers within the range, in France, relevant to the profile |
| To check | offers whose publication date or location could not be proven |
| Not analysed | offers not yet read by the AI (for example after a quota limit) |
| Excluded | offers rejected, with the reason |
| All | every offer within the date range |

Selecting an offer displays the evidence for its publication date and location, the relevance assessment, the CV match
and the cover letter tools. The list can be filtered and exported to CSV.

Each launch starts a new session: websites and offers from the previous session are deleted. Settings, API keys and
CVs are kept.

## How it works

### Reading strategy

Each website is processed by the most precise reader available, selected automatically from its URL and page content:

1. a platform reader using the platform's public data (API, feed or structured list);
2. the generic crawler (structured data, embedded page data, job links and listing dates, pagination, sitemap, one
   JavaScript rendering);
3. the AI page reader, for pages that describe offers as text, tables or PDF files;
4. the PDF reader, without AI, for offers published as PDF files when the AI page reader is not available or found nothing;
5. the AI browser, which operates a headless browser for sites that require interaction.

| Platform | Data source | Publication date |
|---|---|---|
| Workday | JSON API with the site's France filter (country list, or the first level of its location hierarchy); detail API with every location of an offer listed in several places | `startDate` of each posting |
| SAP SuccessFactors | job table sorted by date; job page | listing date, `datePosted` |
| iCIMS | job list with the site's date sort; job page | posting date in the list |
| Phenom (Thales, Bouygues, Orange, Philips) | search API with the country filter | `postedDate` |
| Eightfold (STMicroelectronics) | job API with location filter; detail API | creation time |
| Jibe (Schneider Electric, Publicis) | job API sorted by date; URL filters kept | `posted_date` |
| Workable | job API with location filter; detail API | `published` |
| SmartRecruiters | postings API with country filter, or the public career page | `releasedDate`, `datePosted` |
| Greenhouse, Lever, Ashby, Recruitee, Breezy | official job-board APIs | first publication date |
| Talentsoft (CEA, Air France, Dassault Aviation, Framatome) | main RSS feed, the category feeds of `tous-les-flux-rss.aspx` (contract, domain, region), then the list pages; one offer number, one address | feed `pubDate`, date on the list card, else the position in the newest-first list between two dated offers |
| Teamtailor, RSS feeds | the site's job feed, then its list pages | `pubDate` |
| France Travail | official "Offres d'emploi" API with your access (see [France Travail](#france-travail)) | `dateCreation` |
| Hellowork | company pages and keyword pages (`/fr-fr/emploi/mot-cle_deep-learning.html`); a search URL, which robots.txt closes, is read on the keyword page of its words | `datePosted`, confirmed by "Publiée le" |
| Talentview (Parrot) | public campaign list | last activation date |
| Welcome to the Jungle career sites (`<company>.welcomekit.co`, the STATION F job board `jobs.stationf.co`) | the company's page of offers, in plain HTML; a `welcometothejungle.com/.../companies/<company>` address is read on the company's own career site when it has one | `datePosted` of the offer page |
| Softy (Inserm) | page data of the offer list, newest first | "mise en ligne" date (the offer's last update) |
| Offers published as PDF files | the offer's own page when its card links one, else the PDF (when `robots.txt` allows it), else the card's title | WordPress post date, date written in the PDF, PDF creation date; the month in the PDF's address only proves an offer too old |
| WordPress job post types | REST API, sorted by date | publication date |
| Other websites | JSON-LD, embedded page data, job links, sitemap | labelled date on the offer |
| Laboratory pages | AI page reader; titles and dates must appear on the page | date written next to the offer |
| Interactive sites | AI browser | as above |

Additional detection rules:

- offer links are recognised by their address (job folders in French and English, identifiers in a careers folder) and
  by their text (a gender mark such as "H/F" or "m/w/d", or a contract type in a title-length text); template links
  (`@@LINK@@`, `{{id}}`) and pagination links are ignored;
- a page that yields no offer from its static HTML is rendered once with JavaScript (cookie banners are accepted, "load
  more" buttons are used when they can be clicked), and the job data it downloads is read;
- offers found in page data are matched to the page's own links, and when they have no publication date but point to a
  job platform (for example SmartRecruiters), the offers are read on that platform;
- titles hidden behind "Read more" buttons are taken from the card heading, and site names are removed from page
  titles;
- a careers home page linking to its job list is followed one step to identify the platform;
- a careers page that loads a Greenhouse, Ashby or Lever board without naming it is matched to the board carrying the
  company's name, accepted only when the board's offers link back to the company's website or the page names the
  platform. No board is ever guessed from the address of a job board or of a platform hosting many companies' career
  sites (`welcometothejungle.com`, `welcomekit.co`, `jobs.stationf.co`, the ATS platforms): its name is the
  platform's, not the company's;
- an expired offer URL is replaced by the list it belonged to;
- menus, legal pages, list pages and spontaneous application pages are never taken for offers;
- a title that names only kinds of offers ("Postdoc, PhD or Master 2", "Apprenticeships, internships, thesis and VIE",
  "Open positions for 2026 internships/PhDs") is a page about offers and is excluded; "PhD position" or "Stage M2"
  alone may be one offer and stays. A template placeholder ("<<INTERNAL JOB DESCRIPTION>>") is not a title;
- when the page data mixes other content with its offers, an entry outside the offers' numbered folder, with no place
  and no contract, is dropped (Ubisoft's "Anti-Cheat Software" next to `/careers/search/744000160215749-...`);
- gender marks are removed from titles, including the non-binary forms "(F/H/NB)" and "(W/M/X)";
- structured offer data (JSON-LD) that its template leaves invalid - quotes or backslashes not escaped in the offer's
  text, as on Welcome to the Jungle career sites - is repaired before reading, so the offer keeps its publication date,
  employer and places.

### URL normalisation

Career page URLs are often copied from a browser with the filters of a past search. Before a website is read, its URL is
put in a uniform form (`jobfinder/urlclean.py`):

| Element of the URL | Treatment |
|---|---|
| tracking, display and session parameters (`gad_source`, `width`, `hashed`, `createNewAlert`), empty filters, `all` values, search button values | removed |
| page numbers (`page=3`, `pagenum=3`, `list-b4837029c4_page=1`, `/page/2`) | removed: reading starts on the first page |
| every filter on company career sites: search words, contract types, cities, country, date sorts, saved searches in the path (`/go/Jobs-in-France/3677001`) | removed: the application applies France and the date itself |
| the site's language and the identifiers a page needs (`lang`, `locale`, `LCID`, `CLID`, `page=joblisting`) | kept |
| search words and location on multi-company job boards (Hellowork, Welcome to the Jungle search, optioncarriere, stage.fr, JobTeaser, APEC) | kept: without them a job board lists the entire market |
| contract types on multi-company job boards | removed when search words remain, kept when they are the only narrowing filter |
| address of a single offer (`/offres/2025-09455`) or of a moved page (CNRS) | replaced by the address of the list |

When a website is read, France and the date are applied in this order:

1. the platform reader (Workday, SuccessFactors, Phenom, Eightfold, Jibe and others);
2. `OWN_FILTERS` in `jobfinder/sitefilters.py`, for websites whose filter cannot be found on the page;
3. the website's own search form or "sort by date" link;
4. the AI browser, when a website still gives mostly offers outside France and an AI model is available.

The websites table of step 1 shows what was removed from each URL, and duplicate URLs are merged.

### Reading checks

"Nothing failed" does not mean "everything was read", so each reading is checked. The websites table of step 1 shows
the result:

| Status | Meaning |
|---|---|
| offers found | offers inside the date range were read |
| read: nothing in the date range | the lists were read, and all their offers are older than the range |
| no offers detected | the website answered, but no offer was recognised: it usually needs an AI reader |
| possibly incomplete | fewer than half of the offers the website announces were read, or far fewer than usual |

- A website that was down or refused once gets one more try at the end of the scan. `robots.txt` refusals are not retried.
- When a website announces many more offers than its page gives, its sitemap is read as well. A sitemap without dates is
  read in its own order and left after 50 offers in a row older than the range (3DS lists its newest offers first).
- When a company's own career site blocks automated reading (HTTP 403, anti-bot page) or shows no offer, the app looks
  for the company's board on an open job platform (SmartRecruiters, Workable, Recruitee, Teamtailor, Greenhouse, Ashby,
  Lever), named after its domain. A board is used only when the platform names the same company and lists offers
  (ALTEN on SmartRecruiters for alten.fr); otherwise the website stays "blocked" with its reason. Anti-bot protections
  are never bypassed.
- `robots.txt` closing one offer's page or PDF only skips that document: the offer is kept with what its list shows. Only
  a refusal of the website's own page makes the website "blocked".
- The headless browsers apply `robots.txt` to the page's own data requests too (documents, frames, XHR / fetch): a job
  widget whose API host closes itself to robots (TalentLink, used by ONERA's job offers) stays empty instead of being
  read through the browser.
- An offer titled only by its reference number ("DTIS-2027-23" in ONERA's internship feed) is always opened, and its
  page's short description gives its subject.
- On a lab page read by the AI, the offers' cards are also read without AI: the same offer keeps the model's title with
  the card's date (WordPress post, PDF) and openable page; offers the model left out are added.
- HTTP 406 from a website's firewall, and HTTP 999 (the "request denied" of Workday and LinkedIn), count as a refusal
  ("blocked"), not as a technical failure.
- When the address is the job platform itself (`*.myworkdayjobs.com`, `careers.smartrecruiters.com`...), a refusal of its
  API is reported as such: its pages come from the same servers, so neither the generic reader nor the AI browser is tried.
- A job feed linked as a visible "Flux RSS" link is read like a declared one, when its address names jobs or
  internships (ONERA: `/stages/fr/rss.xml`). When even the oldest offer of a feed is inside the range, the page is read
  too, for the older offers the feed no longer holds.
- The same offer found on several websites (same title, employer and city) is shown once: the copy from the company's
  own site, dated by the earliest reliable date of its copies; the other copies are excluded as "duplicate".
- On one website, the same offer is shown once when it appears in two languages (`/fr/jobs/x` and `/en/jobs/x`) or
  is published again with the same text (a job board reposting it through several advertising links, a company
  republishing it under a new reference); the earliest copy is kept and the other links stay listed with it. Offers
  with the same title but another text (another team, subject or client) remain separate openings.
- An offer whose page states no publication date is still returned in its normal list, with the flag
  "publication date unknown" and the day the app first saw it.
- A date can never be later than the day the app first saw the offer: an offer re-dated by its website ("refreshed"
  to look new) keeps its first-seen day, and the change is shown in its date source.
- Every offer carries the link that opens it (results table, CSV export, "Open the offer"): its own page; else its
  job-platform page; else, for an offer written as text on a list page, that page with a text anchor (`#:~:text=`) that
  browsers open scrolled to the title. A link chosen by the AI page reader is checked against the link inside the offer's
  own block on the page, and offers that come with an id only get the address pattern of their sibling offers.
- A list whose next page is a form sent with POST (CNRS: `toPage('SearchForm', 2)`) is paged the way a browser does it.
- When a page is gone (404, 410, 5xx), its home page is searched for a career page, and the possible new address is shown in
  the note. The address is not replaced automatically.
- A reader recognised from a word in the page code is dropped if it returns other offers than the page shows, from another
  website.
- Two small tables survive between sessions: the usual number of offers per website, and the day each offer was first seen.
  An offer without a date that was already online before the chosen range is hidden, as it cannot be newer.
- Health report: at the end of each scan, every website is compared with its previous scan. A website that was read and is
  now blocked or failing, or whose lists hold less than 40 % of the offers they held for the same date range, is listed in a
  warning above the websites table and in its "since last scan" column.
- A scan holds a lock (`data/scan.lock`). While a scan runs in another process, opening the application does not delete
  its websites and offers, and loading websites or starting a scan is locked until it ends.

### Automatic model switching

The application starts with the most capable model. When a limit is reached, the next model takes over, with the same provider
or another one, and the better model returns as soon as its limit has passed. Providers without a key are skipped.

| Order | Model |
|---|---|
| 1 | Gemini `gemini-3.5-flash` |
| 2 | Groq `openai/gpt-oss-120b` |
| 3 | Gemini `gemini-2.5-flash` |
| 4 | Open Router `nvidia/nemotron-3.5-super-vl:free` |
| 5 | Cohere `command-a-03-2025` |
| 6 | Cohere `command-r-plus-08-2024` |
| 7 | Groq `qwen/qwen3.8-27b` |
| 8 | Gemini `gemini-3.1-flash-lite` |
| 9 | Open Router `dots-studio/dots-3-note-preview:free` |
| 10 | Cohere `command-r-08-2024` |
| 11 | Groq `openai/gpt-oss-20b` |
| 12 | xAI `grok-4.3` (billed per use, so it comes after every free quota) |
| last | the model selected in Settings |

A second key can be added as `GEMINI_API_KEY_2` or `GROQ_API_KEY_2` in `api_keys.txt`; each model of that provider is then tried
with both keys. Cohere comes after the models whose limit returns every day, because its trial limit is monthly. The
order is a judgement, not a benchmark, and is set by `LADDER` in `jobfinder/llm.py`. Before CV matching, the consent box names
every hosted provider that could receive the CV. Switching can be turned off in Settings to use a single model.

The AI's reading of a public lab page is kept for 30 days, so an unchanged page is not sent to the AI again; every other AI
answer (offer analysis, CV matching, letters) is deleted with the session data.

A model the provider no longer offers on a key (HTTP 404 "no longer available", for example `gemini-2.5-flash` on a new
Google account), or an account without credits, is skipped for 7 days, even after a restart (setting `ai_gone`).

### Website filters and early stop

The website's own filters are applied whenever they exist, so that fewer and more recent pages are read:

| Context | Filters applied |
|---|---|
| Workday, Phenom, Eightfold, Jibe, SmartRecruiters, Workable | country filter (France) |
| SuccessFactors | sort by date; country list when it offers France |
| iCIMS | sort by posting date; day / month order inferred from the dates |
| Jibe, WordPress | sort by date |
| Other websites | GET search forms and links: country or location, "posted within" (smallest period covering the range), sort by most recent; deadline sorts are ignored |
| AI browser | location, posting period and "newest first" controls selected on the page |
| Filters present in the supplied URL | always preserved |

Reading stops as soon as the selected range is covered:

- when the website strictly sorts its list by date (SuccessFactors, iCIMS, Jibe, Workable, Talentview, sitemap, or a site
  where the application selected "newest first" or "posted within"), reading stops at the first offer older than the
  range; for a 24-hour range this is usually the first page;
- lists that are mostly sorted or assumed to be sorted stop after two pages without any offer in the range; this includes
  Workday, where some sites pin a block of offers and restart the list after it;
- lists that cannot be sorted (Phenom, Eightfold, Greenhouse, Lever, Ashby) are retrieved in a few large requests and
  filtered by date before any job page is opened.

### Publication date

- An explicit label in the offer ("Date de publication", "Publié le", "Posted on") takes precedence over feed or
  refresh dates.
- Update, start and deadline dates are never used as publication dates; future dates are rejected.
- An ambiguous date such as `06/10/2026` is accepted only when both readings give the same result, unless the page
  establishes the order (a day above 12, or a 12-hour clock indicating the US format).
- The same date repeated on several different offers of a page is treated as a template date and not trusted.
- The language model never writes a date: it selects one of the dates found in the text, and the selection is
  verified.

Offers are filtered in the following order: publication date, location, then title.

### Location

Locations are read from structured data, listing fields and labelled text ("Lieu de travail", "Localisation",
"Location"). An offer is retained when it is located in France or is remote and open to France; offers with no
verifiable location are placed in "To check".

- French places are checked against the official list of communes, departments and regions
  (`jobfinder/data/fr_places.txt`, from geo.api.gouv.fr): "Gif sur Yvette", "Saint-Paul-lez-Durance", "Chusclan (30)"
  are in France. Hyphens, accents and case do not matter, and one wrong letter is forgiven in a name of several words
  ("GIF SUY YVETTE"). One-word names that are also English words, foreign cities or a company (Vienne, Bruges, Mons,
  La Haye, Orange) are left out of the list.
- Every country (English and French names), US states, Canadian provinces and large foreign cities are known, so
  "Riga, Latvia" or "Montevideo, Uruguay" is excluded as outside France instead of being placed in "To check". A French
  name of several words is read first: "Porto-Vecchio" and "Saint-Maurice" are in France.
- Without a location field, the place written in the offer's text is used when a cue introduces it and it is a real
  place: "location_on Lyon", "notre agence de Lille", "poste basé à Toulouse", "(H/F) à Lyon", "based in London",
  "location in France", a label ("Lieu du stage : • ENS Paris-Saclay", "Ville : Centre Inria de l'Université Grenoble
  Alpes"), a postcode before its town ("06107 Nice"), a breadcrumb ("CDI / FRANCE Occitanie / Montpellier") or the
  offer's title followed by its town ("SOFTWARE CRAFTSMAN Paris"). A label starts with a capital letter, so "mobility
  location: Evidence from Kunming, China" in a bibliography is not read as one, and a search form's prompt
  ("Localisation : Veuillez sélectionner") is not a place. A number ("1758") or a date ("Jeudi, 5 novembre, 2026") in
  the location field is not a place.
- A commune written without the end of its name is recognised when the start is unambiguous ("Montbonnot" for
  Montbonnot-Saint-Martin, "Le Chesnay" for Le Chesnay-Rocquencourt, "CHERBOURG"); when several communes start the same
  way, the place is French but is shown as written. A French street address with its postcode ("11 boulevard
  d'Alembert, 78280"), the French national research institutes (CNRS, Inria, INRAE, CEA, Inserm, ONERA...) and
  Roissy-Charles-de-Gaulle are in France; an institute's site abroad is named with its country ("Inria Chile").
- When the location field says nothing ("2 sites", a misspelt town), the offer's text can only confirm France; a
  foreign place named there does not exclude an offer that may have France among its other sites.
- Workday writes "2 sites" for an offer in several places; its address starts with the main one
  (`/job/France-Courbevoie/...`), which is used when it is in France. Otherwise the offer's page lists all its places.

### Title analysis

The title analysis (`jobfinder/classify.py`) handles both conventional job titles (for example "Ingénieur Machine
Learning H/F", "Senior Data Scientist (m/w/d)") and research-subject titles (for example "Apprentissage profond pour
la détection d'anomalies dans les séries temporelles"). It removes gender marks and reference codes, detects the
language, role family, offer type and research topics, and returns one of three verdicts: pass, review by the
language model, or reject. The topic vocabulary is bilingual: "computer vision" also matches "vision par ordinateur".

Titles made of technical terms are recognised through an extended vocabulary covering the research fields (machine
learning, deep learning, computer vision, audio and speech, signal processing, NLP, robotics, data science, MLOps) and
broader technical fields (embedded systems and hardware such as FPGA, ASIC, VHDL or UVM; HPC and GPU; data engineering;
software and cloud; cybersecurity; control and estimation). A title in a research field passes; a title in a broader
technical field is reviewed by the language model. Only sales, administrative and support titles, explicitly excluded
words and unwanted offer types are rejected on the title alone; an internship or thesis subject expressed in
specialised terms is always reviewed by the language model. Rejected roles include store, cashier, finance and management
control, purchasing, HR, legal, reception and customer-service roles, by full role name ("contrôleur de gestion",
"hôte de caisse"), so that technical titles in those fields ("Stage - Analyste data RH") are still reviewed. Startup
business roles are rejected too, whatever their AI flavour ("Bras droit du CEO - Legaltech IA", "Founders Associate",
"Chief of Staff", product and UX/UI designers, "SDR / chargé de prospection"); "SDR" alone stays a signal-processing
subject (Software-Defined Radio).

### AI analysis and CV matching

For each offer in the range, the language model receives the offer text, the numbered candidate dates and the search
profile. It returns the offer type, location evidence, publication date choice and a relevance score with a short
justification. An unusable answer affects only that offer; the analysis continues. Two rules protect offers
from a model's mistakes:

- the offer type stated in the title (Stage, Alternance, Thèse, Post-doc, CDI, CDD) replaces the model's answer;
- an offer whose title matches the profile by itself is never excluded on the model's judgment alone: when the model
  calls it irrelevant or non-technical, it goes to "To check" with that reason. Relevant offers are then compared
with each CV (skills, experience, research topics, education, languages), and evidence quoted from a CV is verified
against the CV text.

## Data and privacy

| Data | Location |
|---|---|
| Database (websites, offers, settings) | `data/jobs.db` (SQLite) |
| Uploaded CVs | `data/cvs/` (removed from disk when a CV is removed in the application) |
| Cover letters | `data/letters/` |
| API keys | `api_keys.txt` or environment variables |

- CV text is sent to a hosted model only after explicit consent in the application.
- Page content passed to the model is marked as untrusted, and instructions found in it are not followed.
- Text taken from websites is escaped before display, and only HTTP(S) links are offered.
- Cover letter fields are escaped for LaTeX, and compilation runs with `-no-shell-escape`.
- URLs are checked against private and local network addresses before any request.
- robots.txt is respected according to RFC 9309, including the data requests made by a page rendered in the headless
  browser, and anti-bot protections are never bypassed.

## Deployment

Job Finder is designed for a single user: it has one database, uses its owner's API keys, stores CVs on the host and
has no authentication. It must not be exposed on a public address. Recommended options:

**Local use.** Run `streamlit run app.py` and open `http://localhost:8501`.

**Remote server through an SSH tunnel.** Start the application on the server, then on the local machine:

```bash
ssh -L 8501:localhost:8501 user@server
```

and open `http://localhost:8501`.

**Docker.** The image contains Python, the dependencies and Chromium (LaTeX is not included; letters are provided as
`.tex` files):

```bash
docker build -t job-finder .
docker run -d --name job-finder -p 127.0.0.1:8501:8501 \
  -e GROQ_API_KEY=<key> -v job-finder-data:/app/data job-finder
```

Binding to `127.0.0.1` keeps the port local to the host; access it through an SSH tunnel. API keys are passed as
environment variables and are never copied into the image. If network access is required, place the application
behind a reverse proxy with HTTPS and authentication.

Operational limits are defined in `jobfinder/config.py` (`LIMITS`): 4 websites read in parallel, a maximum duration of
90 minutes per analysis and 20 minutes per website (also enforced while job pages are being opened), 300 job pages
opened per website, and 150 offers analysed by the
language model per run.

## Project structure

```
app.py                  Streamlit interface: Search (three steps), Results, Settings
assets/                 Interface styles (navy.css), logo and icon (letters drawn from Telegraf)
static/fonts/           Telegraf font files, served by the app (server.enableStaticServing)
.streamlit/config.toml  Theme and upload limit
Dockerfile              Container image (Python, dependencies, Chromium)
requirements.txt        Python dependencies
jobfinder/
  config.py             Paths, default settings, operational limits
  store.py              SQLite models, settings, API key file, website list
  web.py                HTTP fetcher, robots.txt, URL validation, text helpers
  dates.py              Publication date parsing and reconciliation
  classify.py           Location check, bilingual title analysis, offer types
  adapters.py           Platform readers and platform detection
  crawler.py            Reader selection, generic crawler, sitemap reader, JavaScript rendering
  sitefilters.py        Use of the website's own search filters
  pagereader.py         AI page reader for laboratory pages and PDF offers
  browser.py            AI browser
  scan.py               Scanning engine: parallel reading, storage, filtering, early stop
  llm.py                Language model providers, structured answers, cache, quotas
  analysis.py           AI analysis of each offer
  cv.py                 CV extraction, CV matching, LaTeX cover letters
  data/fr_places.txt    Official French communes, departments and regions (location check)
tests/                  Offline test suite (no network access)
```

## Testing

```bash
python -m pytest -q
```

The test suite runs offline. Platform readers are tested against recorded response shapes.

## Known limitations

- LinkedIn and Indeed require authentication or block automated access (Indeed answers with a Cloudflare challenge and
  its robots.txt closes every offer page); they are reported without any request, and the company's own career page
  should be used instead. Apec protects its whole website, robots.txt included, with an anti-bot service and has no
  public API: it is reported the same way.
- Websites that refuse automated access (HTTP 403, anti-bot pages), forbid it in robots.txt, have an invalid
  certificate or are unavailable are reported per website and never bypassed. At the time of writing this includes
  JobTeaser, Glassdoor, Carrefour, Equans, Acensi, Tuito, Inetum (HTTP 406) and optioncarriere search URLs; the LISN offer pages are restricted to internal access. When such a company publishes its offers on a job
  platform (SmartRecruiters, Workable, Recruitee, Teamtailor, Greenhouse), they are read there instead.
- Welcome to the Jungle (`welcometothejungle.com`: its search and its company pages) loads its offers with a script, and
  its servers refuse automated browsers (HTTP 403). A company page is read on the company's own career site on the same
  platform (`<company>.welcomekit.co`) when it has one; otherwise it is reported, and the company's own career site
  should be added (for a STATION F startup, its page on `jobs.stationf.co`).
- Workday sites may refuse requests for a while (HTTP 403) after a large worldwide list has been read; their France
  filter keeps a normal search small.
- When robots.txt forbids a search URL but not the page itself, the page and its sitemap are read instead.
- Network problems (name resolution, timeouts) are retried and reported as "unreachable", never as "blocked"; a run
  made while the network is down should be started again.
- Some career home pages give access to offers only through a search form; the URL of the result list should be
  supplied (for example `https://recrutement.michelin.fr/en/job-offer-result-list`).
- A portal home page is not a job list; the URL of a search result page should be supplied (France Travail is read
  through its API, see [France Travail](#france-travail)).
- Hellowork's keyword pages show their first 20 offers without JavaScript (the next pages need `?`, which robots.txt
  closes): a keyword page gives the newest offers of that keyword, enough for a daily search.
- Some SuccessFactors sites populate their country filter with JavaScript; the application then filters by location
  itself.
- Phenom and Eightfold lists cannot be sorted by date and are read in full.
- Talentview job pages (Parrot) require JavaScript; their offers carry a title, location, contract type and date, but no
  description.
- Offers whose page states no publication date (for example Inria, some laboratory pages) are shown in their normal
  list, marked "date not stated", with the day the application first saw them.
- Free hosted plans limit tokens per minute and per day. Oversized requests are split automatically; an exhausted
  daily quota stops the analysis with an explicit message.
