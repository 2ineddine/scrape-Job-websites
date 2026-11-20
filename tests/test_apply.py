"""'Open and fill the application': which field asks for what, what goes in it, and a real form filled in a browser. The
window never submits, never touches a file, a password or a consent box, and never replaces a value already there."""
import pytest

from jobfinder.apply import (MAX_LINES, clamp_lines, field_kind, fill_page, form_message, form_values, has_form, pick_option,
                             split_name)

@pytest.fixture(autouse=True)
def _website_memory_in_tmp(tmp_path, monkeypatch):
    """Claude's answers and what the app learns on each website go to a temporary folder, never the user's data/."""
    from jobfinder import claude_forms
    monkeypatch.setattr(claude_forms, "FORMS_DIR", tmp_path / "forms")
    monkeypatch.setattr(claude_forms, "SHIPPED_DIR", tmp_path / "shipped")  # never the repository's own files


AP = {"civility": "M.", "first_name": "Jean", "last_name": "Dupont", "email": "jean.dupont@example.org", "phone": "0612345678",
      "address": "12 rue des Lilas\n75011 Paris", "country": "France", "linkedin": "https://linkedin.com/in/jd",
      "website": "https://github.com/jd", "degree": "Master", "school": "Sorbonne Université", "grad_year": "2024",
      "field_of_study": "Machine learning", "work_authorization": "yes", "nationality": "Française",
      "how_heard": "Site carrière de l'entreprise", "name": "Jean Dupont"}


def F(label="", **kw):
    return {"tag": "input", "type": "text", "label": label, **kw}


def test_fields_are_recognised_in_french_and_english():
    assert field_kind(F("Prénom *")) == "first_name" and field_kind(F("First name")) == "first_name"
    assert field_kind(F("Nom *")) == "last_name" and field_kind(F("Last Name")) == "last_name"
    assert field_kind(F("", name="lastName")) == "last_name" and field_kind(F("", autocomplete="given-name")) == "first_name"
    assert field_kind(F("Adresse e-mail")) == "email" and field_kind(F("Adresse")) == "address"
    assert field_kind(F("Téléphone portable")) == "phone" and field_kind(F("Code postal")) == "postal_code"
    assert field_kind(F("Ville")) == "city" and field_kind(F("Pays de résidence")) == "country"
    assert field_kind(F("Profil LinkedIn")) == "linkedin" and field_kind(F("GitHub / Portfolio")) == "website"
    assert field_kind(F("Diplôme le plus élevé")) == "degree" and field_kind(F("École / Université")) == "school"
    assert field_kind(F("Année d'obtention du diplôme")) == "grad_year"
    assert field_kind(F("Comment avez-vous connu notre entreprise ?")) == "how_heard"  # a question may name the company
    assert field_kind({"tag": "textarea", "type": "textarea", "label": "Lettre de motivation"}) == "cover_letter"
    assert field_kind({"tag": "textarea", "type": "textarea", "label": "Commentaires"}) is None  # a free box: left alone
    # someone else's details or a company's are never the user's
    assert field_kind(F("Nom de l'entreprise actuelle")) is None and field_kind(F("Nom du référent")) is None
    assert field_kind(F("Nom d'utilisateur")) is None and field_kind(F("Indicatif pays")) == "phone_code"  # +33 (20 Nov 2026)


def test_yes_no_questions_and_lists_take_the_right_answer():
    q = {"tag": "input", "type": "radio", "label": "Oui", "group": "Êtes-vous autorisé(e) à travailler en France ? Oui Non"}
    assert field_kind(q) == "work_authorization"
    assert field_kind({**q, "group": "Aurez-vous besoin d'un parrainage de visa ?"}) == "sponsorship"
    assert pick_option(["Choisir", "Oui", "Non"], "work_authorization", "yes") == 1
    assert pick_option(["Choisir", "Oui", "Non"], "sponsorship", "no") == 2
    assert pick_option(["--", "Madame", "Monsieur"], "civility", "M.") == 2 and pick_option(["Mme", "M."], "civility", "Mme") == 0
    assert pick_option(["Select", "LinkedIn", "Site carrière de l'entreprise", "Autre"], "how_heard", "Site carrière") == 2
    assert pick_option(["--", "Company website", "Indeed"], "how_heard", "Site carrière de l'entreprise") == 1
    assert pick_option(["--", "France", "Belgique"], "country", "France") == 1
    assert pick_option(["--", "Master 1", "Master 2"], "degree", "Master") is None  # ambiguous: left to the user


def test_the_details():
    v = form_values(AP)
    assert (v["first_name"], v["last_name"], v["full_name"]) == ("Jean", "Dupont", "Jean Dupont")
    assert (v["address"], v["postal_code"], v["city"]) == ("12 rue des Lilas", "75011", "Paris")
    assert (v["work_authorization"], v["sponsorship"]) == ("yes", "no")
    assert form_values({"name": "Marie-Anne de La Tour"})["last_name"] == "de La Tour" and split_name("") == ("", "")
    assert form_values({**AP, "work_authorization": ""})["work_authorization"] == ""  # not said: left to the user


FORM = """<html><body><form>
<label for=c>Civilité</label><select id=c><option>Choisir</option><option>Mme</option><option>M.</option></select>
<label for=fn>Prénom *</label><input id=fn>
<label for=ln>Nom *</label><input id=ln>
<label>Adresse e-mail <input type=email name=email></label>
<input name=phone placeholder="Téléphone">
<label for=pw>Mot de passe</label><input id=pw type=password>
<label for=cv>CV</label><input id=cv type=file>
<label for=city>Ville</label><input id=city value="Lyon">
<fieldset><legend>Êtes-vous autorisé(e) à travailler en France ?</legend>
  <label><input type=radio name=auth value=1>Oui</label><label><input type=radio name=auth value=0>Non</label></fieldset>
<label for=src>Comment avez-vous connu cette offre ?</label>
<select id=src><option>--</option><option>LinkedIn</option><option>Site carrière de l'entreprise</option></select>
<label for=lm>Lettre de motivation</label><textarea id=lm></textarea>
<label><input type=checkbox id=rgpd> J'accepte les conditions</label>
<button type=submit>Envoyer</button></form></body></html>"""


def test_a_real_form_is_filled_but_never_submitted():
    pw_api = pytest.importorskip("playwright.sync_api")
    with pw_api.sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as e:  # no browser installed on this machine
            pytest.skip(str(e))
        page = br.new_page()
        page.set_content(FORM)
        assert has_form(page)
        done = fill_page(page, form_values(AP), "Madame, Monsieur,\n\nMa lettre.")
        get = lambda sel: page.eval_on_selector(sel, "e => e.value")  # noqa: E731
        assert (get("#fn"), get("#ln"), get("[name=email]"), get("[name=phone]")) == ("Jean", "Dupont", AP["email"], AP["phone"])
        assert page.eval_on_selector("#c", "e => e.options[e.selectedIndex].text") == "M."
        assert page.eval_on_selector("#src", "e => e.options[e.selectedIndex].text") == "Site carrière de l'entreprise"
        assert page.is_checked("input[name=auth][value='1']") and get("#lm").startswith("Madame, Monsieur,")
        assert get("#city") == "Lyon"  # already filled: never replaced
        assert get("#pw") == "" and not page.is_checked("#rgpd")  # no password, no consent ticked
        assert page.eval_on_selector("#cv", "e => e.files.length") == 0  # the CV is the user's to attach
        assert "cover_letter" in done and "work_authorization" in done
        br.close()


def test_the_message_pasted_in_the_form_is_five_lines_at_most():
    """The user's rule: the cover-letter text pasted into a form is 5 lines at most (greeting, 3 sentences, signature),
    whatever the AI writes; each line stays readable."""
    t = form_message(AP, ["Votre poste d'ingénieur IA correspond à mon parcours en vision par ordinateur.",
                          "J'ai conçu des modèles de détection en PyTorch pendant deux ans.",
                          "Je suis disponible pour un entretien dès cette semaine."])
    lines = t.splitlines()
    assert len(lines) == 5 and lines[0] == "Madame, Monsieur," and lines[-1] == "Cordialement, Jean Dupont"
    long = form_message(AP, ["mot " * 200, "deux", "trois", "quatre", "cinq"])  # the AI wrote too much: still 5 lines
    assert len(long.splitlines()) == MAX_LINES and all(len(x) <= 220 for x in long.splitlines())
    assert long.splitlines()[1].endswith("…")
    assert len(clamp_lines("a\nb\n\nc\nd\ne\nf\ng").splitlines()) == MAX_LINES



# --------------------------------------------------------------------------- accounts
def test_each_website_gets_its_own_strong_password_kept_on_this_computer_only(tmp_path):
    import os
    import stat
    from jobfinder.apply import account, load_accounts, new_password, set_account, site_of
    pw = {new_password() for _ in range(200)}
    assert len(pw) == 200  # never the same twice
    for p in pw:
        assert len(p) == 16 and any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p)
        assert sum(c in "!#%*-_@" for c in p) >= 2 and sum(c.isdigit() for c in p) >= 2  # Sorbonne Université's rule
    path = tmp_path / "accounts.json"
    assert account("sanofi.wd3.myworkdayjobs.com", "me@example.org", path=path) is None  # none yet, none made
    a = account("sanofi.wd3.myworkdayjobs.com", "me@example.org", create=True, path=path)
    b = account("careers.ey.com", "me@example.org", create=True, path=path)
    assert a["password"] != b["password"] and a["state"] == "creating"
    assert account("sanofi.wd3.myworkdayjobs.com", "other@example.org", create=True, path=path) == a  # reused, never remade
    set_account("sanofi.wd3.myworkdayjobs.com", path, state="created")
    assert load_accounts(path)["sanofi.wd3.myworkdayjobs.com"]["state"] == "created"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600  # this computer's user only
    assert site_of("https://www.Talan.com/jobs/1") == "talan.com"
    from jobfinder.apply import account_site
    # many companies share SuccessFactors' hosts, each with its own accounts
    assert account_site("https://career5.successfactors.eu/career?company=Atos&career_job_req_id=1") == \
        "career5.successfactors.eu/atos" != account_site("https://career5.successfactors.eu/careers?company=allianz")
    assert account_site("https://sanofi.wd3.myworkdayjobs.com/fr-FR/SanofiCareers/login") == "sanofi.wd3.myworkdayjobs.com"


# --------------------------------------------------------------------------- what kind of page
def _f(label, type_="text", **kw):
    return {"tag": "input", "type": type_, "label": label, "name": "", "elid": "", "placeholder": "", "aria": "", "near": "",
            "value": "", "checked": False, **kw}


def _b(text, tag="button", type_="submit"):
    return {"id": "x", "tag": tag, "text": text, "type": type_}


def test_the_kind_of_page_is_recognised():
    from jobfinder.apply import code_fields, page_kind
    sign_in = {"fields": [_f("Adresse e-mail"), _f("Mot de passe", "password")],
               "buttons": [_b("Se connecter"), _b("Créer un compte", "a", "")], "captcha": False, "title": ""}
    assert page_kind(sign_in) == "sign_in"  # a 'create an account' LINK does not make it a sign-up page
    sign_up = {"fields": [_f("Prénom"), _f("Nom"), _f("E-mail"), _f("Mot de passe", "password"),
                          _f("Confirmez le mot de passe", "password")], "buttons": [_b("Créer mon compte")], "captcha": False,
               "title": ""}
    assert page_kind(sign_up) == "sign_up"
    code = {"fields": [_f("Code de vérification")], "buttons": [_b("Valider")], "captcha": False, "title": ""}
    assert page_kind(code) == "email_code"
    form = {"fields": [_f("Prénom"), _f("Nom"), _f("E-mail"), _f("Code postal")], "buttons": [_b("Envoyer")], "captcha": False,
            "title": ""}
    assert page_kind(form) == "application_form" and not code_fields(form["fields"])  # a postal code is no email code
    # one password field among many details: an application form (its password stays the user's), not a sign-in
    assert page_kind({**form, "fields": form["fields"] + [_f("Mot de passe", "password")]}) == "application_form"
    offer = {"fields": [], "buttons": [_b("Postuler", "a", "")], "captcha": False, "title": ""}
    assert page_kind(offer) == "offer_page"
    assert page_kind({"fields": [_f("Rechercher")], "buttons": [_b("OK")], "captcha": False, "title": ""}) == "other"


# --------------------------------------------------------------------------- a small website, end to end
LOGIN = """<html><body><h1>Espace candidat</h1><form action="form.html" method="get">
<label for=e>Adresse e-mail</label><input id=e name=email type=email>
<label for=p>Mot de passe</label><input id=p name=pw type=password>
<button type=submit>Se connecter</button></form>
<a href="signup.html">Créer un compte</a></body></html>"""
SIGNUP = """<html><body><h1>Créer votre compte</h1><form action="code.html" method="get">
<label for=fn>Prénom</label><input id=fn name=fn><label for=ln>Nom</label><input id=ln name=ln>
<label for=em>E-mail</label><input id=em name=em type=email>
<label for=p1>Mot de passe</label><input id=p1 name=p1 type=password>
<label for=p2>Confirmez le mot de passe</label><input id=p2 name=p2 type=password>
<label><input type=checkbox id=t name=t required> J'accepte les conditions générales d'utilisation</label>
<label><input type=checkbox id=nl name=nl> Recevoir la newsletter et les offres partenaires</label>
CAPTCHA<button type=submit>Créer mon compte</button></form></body></html>"""
CODE = """<html><body><h1>Vérifiez votre e-mail</h1><form action="form.html" method="get">
<label for=c>Code de vérification</label><input id=c name=c><button type=submit>Valider</button></form></body></html>"""
OFFER = '<html><body><h1>Ingénieur IA</h1><p>Missions...</p><a href="login.html">Postuler</a></body></html>'


def _site(tmp_path, captcha=False, stuck=False):
    (tmp_path / "offer.html").write_text(OFFER, encoding="utf-8")
    login = LOGIN.replace("type=submit>Se connecter", "type=button>Se connecter") if stuck else LOGIN
    (tmp_path / "login.html").write_text(login, encoding="utf-8")
    (tmp_path / "signup.html").write_text(SIGNUP.replace(
        "CAPTCHA", '<div class="g-recaptcha" data-sitekey="x"></div>' if captcha else ""), encoding="utf-8")
    (tmp_path / "code.html").write_text(CODE, encoding="utf-8")
    (tmp_path / "form.html").write_text(FORM, encoding="utf-8")
    return (tmp_path / "offer.html").as_uri()


def _wait(w, uid, busy=("opening", "working"), timeout=60):
    import time
    end = time.time() + timeout
    while time.time() < end and w.status.get(uid, {}).get("state") in busy:
        time.sleep(0.2)
    st = w.status[uid]
    if st["state"] == "error" and "Executable doesn't exist" in st["message"]:
        pytest.skip("no browser installed")
    return st


def _job(**kw):
    return {"values": form_values(AP), "letter": "Madame, Monsieur,\nMa lettre.", "email": AP["email"], "allow_terms": False,
            "ai_key": "", "code": "", **kw}


@pytest.fixture
def window(tmp_path):
    pytest.importorskip("playwright.sync_api")
    from jobfinder.apply import ApplyWindow
    return ApplyWindow(tmp_path / "profile", headless=True, accounts_path=tmp_path / "accounts.json")


def test_an_account_is_created_once_then_used_for_the_next_applications(tmp_path, window):
    """Offer -> 'Postuler' -> sign-in page (no account yet) -> 'Créer un compte' -> account form filled with a password
    made for this website (terms ticked: allowed; newsletter never) -> email code given by the user -> application form
    filled. The next application on the same website signs in with that account."""
    from jobfinder.apply import load_accounts
    url = _site(tmp_path)
    window.open("o1", url, _job(allow_terms=True))
    st = _wait(window, "o1")
    assert st["state"] == "needs_code", st
    acct = next(iter(load_accounts(tmp_path / "accounts.json").values()))
    assert acct["email"] == AP["email"] and acct["state"] == "waiting_code" and len(acct["password"]) == 16
    assert any("creating the account" in x for x in st["steps"])
    window.resume("o1", _job(allow_terms=True, code="123456"))
    st = _wait(window, "o1")
    assert st["state"] == "filled" and {"first_name", "email", "cover_letter"} <= set(st["filled"]), st
    assert next(iter(load_accounts(tmp_path / "accounts.json").values()))["state"] == "created"
    # the next offer on the same website: signed in with the account, never a second one
    window.open("o2", url, _job(allow_terms=True))
    st = _wait(window, "o2")
    assert st["state"] == "filled", st
    assert any("signing in with the account" in x for x in st["steps"])
    assert len(load_accounts(tmp_path / "accounts.json")) == 1


def test_terms_and_captchas_are_left_to_the_user(tmp_path, window):
    from jobfinder.apply import load_accounts
    url = _site(tmp_path)
    window.open("t1", url, _job(allow_terms=False))  # the user did not allow the app to accept terms
    st = _wait(window, "t1")
    assert st["state"] == "needs_user" and "terms" in st["message"], st
    assert next(iter(load_accounts(tmp_path / "accounts.json").values()))["state"] == "creating"  # not created yet

    cap = tmp_path / "cap"
    cap.mkdir()
    url2 = _site(cap, captcha=True)
    window.open("t2", url2, _job(allow_terms=True))
    st = _wait(window, "t2")
    assert st["state"] == "needs_user" and "captcha" in st["message"], st  # the app never solves a captcha


def test_a_page_that_does_not_move_on_stops_instead_of_looping(tmp_path, window):
    from jobfinder.apply import account, set_account, site_of
    url = _site(tmp_path, stuck=True)  # its sign-in button sends nothing
    account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")  # an account exists on it
    set_account(site_of(url), tmp_path / "accounts.json", state="created")
    window.open("s1", url, _job())
    st = _wait(window, "s1")
    assert st["state"] == "needs_user" and "does not move on" in st["message"], st


def test_a_dry_run_never_signs_in_nor_creates_an_account(tmp_path, window):
    from jobfinder.apply import load_accounts
    url = _site(tmp_path)
    window.open("d1", url, _job(dry_run=True))
    st = _wait(window, "d1")
    assert st["state"] == "dry_run" and st["wall"] == "sign_in", st
    assert load_accounts(tmp_path / "accounts.json") == {}


# --------------------------------------------------------------------------- Claude reads the pages the rules do not know
def test_claude_gets_the_page_structure_only_and_reads_each_page_once(tmp_path, monkeypatch):
    import json
    from jobfinder import claude_forms as cf
    monkeypatch.setattr(cf, "FORMS_DIR", tmp_path / "forms")
    fields = [_f("Votre petit nom", value="Jean", name="nick", elid="nick"), _f("Mot de passe", "password", value="•")]
    buttons = [_b("Étape suivante")]
    text = cf.describe("Candidature", fields, buttons)
    assert "Jean" not in text and "Votre petit nom" in text and "Étape suivante" in text  # structure, never a value
    assert cf.plan_page("", "x.fr", "u", "t", fields, buttons) == (None, "no Anthropic key")
    calls = []

    class Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class Resp:
        stop_reason = "end_turn"

        def __init__(self, t):
            self.content = [Block(t)]

    answer = {"page_kind": "application_form", "button": cf.button_key(buttons[0]), "button_role": "continue",
              "fields": [{"key": cf.field_key(fields[0]), "role": "first_name"}, {"key": "invented", "role": "email"}]}

    class Client:
        def __init__(self, api_key):
            self.beta = self
            self.messages = self

        def create(self, **kw):
            calls.append(kw)
            return Resp(json.dumps(answer))
    monkeypatch.setattr(cf.anthropic, "Anthropic", Client)
    plan, how = cf.plan_page("sk-ant-test", "x.fr", "https://x.fr/apply", "Candidature", fields, buttons)
    assert how == "Claude" and plan.page_kind == "application_form"
    assert [(x.key, x.role) for x in plan.fields] == [(cf.field_key(fields[0]), "first_name")]  # an invented key: dropped
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["type"] == "json_schema" and "Jean" not in json.dumps(kw)
    plan2, how2 = cf.plan_page("sk-ant-test", "x.fr", "https://x.fr/apply", "Candidature", fields, buttons)
    assert how2 == "saved" and len(calls) == 1  # the same page later: the saved answer, no second call
    assert (tmp_path / "forms" / "x.fr.json").exists()

    class Refusing(Client):
        def create(self, **kw):
            r = Resp("")
            r.stop_reason = "refusal"
            return r
    monkeypatch.setattr(cf.anthropic, "Anthropic", Refusing)
    assert cf.plan_page("sk-ant-test", "y.fr", "u", "t", fields, buttons) == (None, "declined")


def test_a_field_only_claude_recognised_is_filled_with_the_right_detail(tmp_path):
    pw_api = pytest.importorskip("playwright.sync_api")
    from jobfinder import claude_forms as cf
    from jobfinder.apply import fill_page
    page_html = ('<html><body><label for=n>Votre petit nom</label><input id=n name=nick>'
                 '<label for=e>E-mail</label><input id=e type=email></body></html>')
    with pw_api.sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as e:
            pytest.skip(str(e))
        page = br.new_page()
        page.set_content(page_html)
        key = cf.field_key({"tag": "input", "type": "input", "name": "nick", "elid": "n", "label": "Votre petit nom"})
        done = []
        for fr in page.frames:
            from jobfinder.apply import fill_frame
            done += fill_frame(fr, form_values(AP), "", None, {key: "first_name"})
        assert page.eval_on_selector("#n", "e => e.value") == "Jean" and "first_name" in done
        assert fill_page is not None
        br.close()



def test_each_website_s_specifics_are_saved_and_used_at_the_next_visit(tmp_path, window, monkeypatch):
    """An offer page whose 'apply' button says 'Je veux ce poste' (no rule knows it): Claude reads it once; the app saves
    the button that worked, the route and the details of the fields Claude named. At the next visit Claude is not asked
    again: the saved button is pressed. No password is ever saved there."""
    import json
    from jobfinder import claude_forms as cf
    (tmp_path / "offer.html").write_text('<html><body><h1>Ingénieur IA</h1><button onclick="location.href=\'form.html\'">'
                                         'Je veux ce poste</button></body></html>', encoding="utf-8")
    (tmp_path / "form.html").write_text(FORM, encoding="utf-8")
    url = (tmp_path / "offer.html").as_uri()
    calls = []

    class Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class Client:
        def __init__(self, api_key):
            self.beta = self
            self.messages = self

        def create(self, **kw):
            calls.append(kw)
            text = kw["messages"][0]["content"]
            key = next(line.split("key=")[1].split(" text=")[0].strip("'\"") for line in text.splitlines()
                       if "Je veux ce poste" in line)
            r = type("R", (), {})()
            r.stop_reason, r.content = "end_turn", [Block(json.dumps({"page_kind": "offer_page", "fields": [],
                                                                       "button": key, "button_role": "apply"}))]
            return r
    monkeypatch.setattr(cf.anthropic, "Anthropic", Client)
    window.open("m1", url, _job(ai_key="sk-ant-test"))
    st = _wait(window, "m1")
    assert st["state"] == "filled" and len(calls) == 1, st
    from jobfinder.apply import site_of
    memory = cf.learned(site_of(url))
    # the 'apply' buttons pressed in turn on this website (one here), the route, never a password
    assert memory["buttons"]["apply"] == ["Je veux ce poste"] and memory["route"] == ["offer_page", "application_form"]
    assert "password" not in memory.get("fields", {}).values() and memory["visits"] == 1

    class Down(Client):
        def create(self, **kw):
            raise AssertionError("Claude must not be asked again")
    monkeypatch.setattr(cf.anthropic, "Anthropic", Down)
    window.open("m2", url, _job(ai_key="sk-ant-test"))
    st = _wait(window, "m2")
    assert st["state"] == "filled" and st.get("route") == "offer_page → application_form", st


# --------------------------------------------------------------------------- the 'apply' button, and pages of real websites
def test_the_apply_button_is_the_surest_one_and_never_the_one_that_sends_a_form():
    from jobfinder.apply import apply_rank
    rank = lambda text, **kw: apply_rank({"tag": "button", "text": text, "type": "", **kw})  # noqa: E731
    assert rank("Apply Manually") == 0 and rank("Postuler manuellement") == 0  # Workday's choice, before anything else
    assert rank("Postuler") == rank("JE POSTULE") == rank("Postuler à l'offre") == rank("Apply for this job") == 1
    assert rank("send Postuler") == 2  # an icon's name before the word
    assert rank("Façonne ta place !", tag="a", href="/jobs/1-ia/applications/new") == 3  # the link says where it goes
    assert rank("I'm interested") == 4  # a talent community on some websites: last
    for text in ("Submit application", "Envoyer ma candidature", "Apply with LinkedIn", "Comment postuler", "Search & Apply",
                 "Apply now Help", "Candidature spontanée", "Apply Now Médecin du travail - CDI", "Créer une alerte"):
        assert rank(text) is None, text
    # the button that sends an application form is never pressed, whatever it says
    assert rank("Postuler", submits=True, formFields=3) is None and rank("Postuler", submits=True, formFile=True) is None
    assert rank("Postuler", submits=True, formFields=0) == 1  # a form of its own (no fields): it only opens the next page
    assert rank("S'inscrire et postuler à cette offre", tag="a") == 1  # Chronopost
    # the platform's own mark in the code, whatever the words (Teamtailor: 'Join your next adventure', 'Façonne ta place !')
    assert rank("Join your next adventure", hint="click->careersite--jobs--form-overlay#showFormOverlay ") == 2
    assert rank("Partager la page", hint="click->common--dropdown#toggle trigger-dropdown-2") is None
    assert rank("Jetzt bewerben »") == 1 and rank("Postuler par mail", tag="a", href="mailto:rh@x.fr") is None
    assert rank("Skip to main content", tag="a", href="") is None  # a link within the page ('/apply/…#main') is no step


def test_an_offer_applied_to_by_email_says_where_to_write():
    from jobfinder.apply import by_email
    scan = {"buttons": [{"tag": "a", "text": "Postuler par mail", "href": "mailto:recrutement@x.fr?subject=Offre 12"},
                        {"tag": "a", "text": "Contact", "href": "mailto:dpo@x.fr"}]}
    assert by_email([(None, scan)]) == "recrutement@x.fr"
    assert by_email([(None, {"buttons": [{"tag": "a", "text": "Écrivez-nous", "href": "mailto:info@x.fr"}]})]) == ""
    # an address written in the offer's text, beside 'Contacts' (a doctoral offer, lib.upmc.fr): taken; a support one never
    text = {"buttons": [], "mails": [{"addr": "support@o2switch.fr", "before": "contact the support"},
                                     {"addr": "olivier.couture@sorbonne-universite.fr", "before": "Rémunération 1750 euros Contacts "}]}
    assert by_email([(None, text)]) == "olivier.couture@sorbonne-universite.fr"
    assert by_email([(None, {"buttons": [], "mails": [{"addr": "a@b.fr", "before": "Le laboratoire étudie les galaxies."}]})]) == ""


def test_the_words_beside_a_field_and_its_type_when_its_label_says_nothing():
    assert field_kind(F("", placeholder="John Doe", near="Full name *")) == "full_name"
    assert field_kind(F("", placeholder="john@mail.com", type="email")) == "email"
    assert field_kind(F("", placeholder="+33 6 00 00 00 00", type="tel")) == "phone"
    assert field_kind(F("", near="Prénom Nom")) is None  # the words of a row naming two fields: never guessed
    assert field_kind(F("Nom et prénom")) == "full_name" and field_kind(F("Adresse postale")) == "address"
    assert field_kind(F("Nom de l'entreprise", near="Prénom")) is None  # a company's field: never the user's


def _pages(tmp_path, **pages):
    for name, text in pages.items():
        (tmp_path / f"{name}.html").write_text(text, encoding="utf-8")
    (tmp_path / "form.html").write_text(FORM, encoding="utf-8")
    return (tmp_path / "offer.html").as_uri()


BANNER = """<div id=cmp style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:10"><div style="background:#fff;
margin:200px auto;width:420px;padding:20px">Ce site utilise des cookies.
<button onclick="location.href='accepted.html'">Tout accepter</button>
<button onclick="document.getElementById('cmp').remove()">Tout refuser</button></div></div>"""


def test_a_cookie_banner_is_refused_then_the_application_opens(tmp_path, window):
    url = _pages(tmp_path, offer=f'<html><body><h1>Ingénieur IA</h1><a href="form.html">Postuler</a>{BANNER}</body></html>')
    window.open("c1", url, _job())
    st = _wait(window, "c1")
    assert st["state"] == "filled", st  # never 'accepted.html': the banner's 'accept' is never pressed
    assert any("cookie" in x for x in st["steps"])


def test_a_form_whose_fields_the_app_does_not_know_is_never_sent(tmp_path, window):
    url = _pages(tmp_path, offer="""<html><body><h1>Data Scientist</h1>
<form onsubmit="location.href='sent.html'; return false;"><label for=a>Votre identité</label><input id=a>
<label for=b>Où vous joindre</label><input id=b><button type=submit>Postuler</button></form></body></html>""")
    window.open("n1", url, _job())
    st = _wait(window, "n1")
    assert st["state"] == "no_form", st
    assert "sent.html" not in window.pages.get("n1", type("P", (), {"url": ""})).url


def test_a_menu_behind_the_apply_button_and_workday_s_apply_manually(tmp_path, window):
    url = _pages(tmp_path, offer="""<html><body><h1>Ingénieur</h1><button onclick="var m=document.getElementById('m');
m.style.display = m.style.display=='block' ? 'none' : 'block'">Apply now</button>
<div id=m style="display:none"><a href="wd.html">Apply Now</a></div></body></html>""",
                 wd="""<html><body><h1>Senior Engineer</h1><a href="#" onclick="document.getElementById('mo').style.display='block';
return false;">Apply</a><div id=mo style="display:none"><a href="resume.html">Autofill with Resume</a>
<a href="form.html">Apply Manually</a></div></body></html>""")
    window.open("w1", url, _job())
    st = _wait(window, "w1")
    assert st["state"] == "filled", st  # the menu's link, then 'Apply Manually': never the same button pressed in a loop
    assert st["steps"][:4] == ["'Apply now'", "'Apply Now'", "'Apply'", "'Apply Manually'"], st["steps"]


def test_a_page_built_by_its_scripts_and_an_apply_button_in_a_frame(tmp_path, window):
    url = _pages(tmp_path, offer="""<html><body><h1>Offer</h1><div id=app></div><script>setTimeout(function () {
document.getElementById('app').innerHTML = '<iframe src="inner.html" style="width:600px;height:200px"></iframe>'; }, 2500)
</script></body></html>""", inner='<html><body><a href="form.html" target="_top">Postuler maintenant</a></body></html>')
    window.open("f1", url, _job())
    st = _wait(window, "f1")
    assert st["state"] == "filled", st


def test_cv_first_websites_and_gone_offers_are_said_plainly(tmp_path, window):
    url = _pages(tmp_path, offer="""<html><body><h1>Postuler en 1 clic</h1><button>POSTULER</button>
<label>Déposer votre CV <input type=file></label></body></html>""")
    window.open("v1", url, _job())
    st = _wait(window, "v1")
    assert st["state"] == "needs_user" and "CV" in st["message"], st
    gone = tmp_path / "gone"
    gone.mkdir()
    url = _pages(gone, offer="""<html><head><title>Page introuvable</title></head><body><h1>Désolé, cette page n'existe pas</h1>
<a href="index.html">Accueil</a></body></html>""")
    window.open("g1", url, _job())
    st = _wait(window, "g1")
    assert st["state"] == "gone", st


def test_an_application_form_that_also_makes_the_account_is_filled_never_sent(tmp_path):
    pw_api = pytest.importorskip("playwright.sync_api")
    from jobfinder.apply import drive, load_accounts
    url = _pages(tmp_path, offer="""<html><body><form onsubmit="location.href='sent.html'; return false;">
<label for=fn>Prénom</label><input id=fn><label for=ln>Nom</label><input id=ln>
<label for=em>E-mail</label><input id=em type=email>
<label for=p1>Mot de passe</label><input id=p1 type=password>
<label for=p2>Confirmation du mot de passe</label><input id=p2 type=password>
<label for=cv>CV</label><input id=cv type=file><button type=submit>Postuler</button></form></body></html>""")
    with pw_api.sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as e:
            pytest.skip(str(e))
        page = br.new_page()
        page.goto(url)
        page, st = drive(page, _job(email="accounts@example.org"), accounts_path=tmp_path / "accounts.json")
        assert st["state"] == "filled" and "account" in st["message"], st
        acct = next(iter(load_accounts(tmp_path / "accounts.json").values()))
        assert page.eval_on_selector("#p1", "e => e.value") == acct["password"] == page.eval_on_selector("#p2", "e => e.value")
        assert page.eval_on_selector("#em", "e => e.value") == "accounts@example.org" == acct["email"]
        assert acct["state"] == "creating" and "sent.html" not in page.url  # made when the USER sends the form
        br.close()


def test_a_website_that_confirms_the_account_by_a_link(tmp_path, window):
    from jobfinder.apply import load_accounts
    url = _site(tmp_path)
    (tmp_path / "signup.html").write_text(SIGNUP.replace('action="code.html"', 'action="sent.html"').replace("CAPTCHA", ""),
                                          encoding="utf-8")
    (tmp_path / "sent.html").write_text("<html><body><h1>Vérifiez votre e-mail</h1><p>Un lien d'activation vous a été "
                                        "envoyé.</p></body></html>", encoding="utf-8")
    window.open("l1", url, _job(allow_terms=True))
    st = _wait(window, "l1")
    assert st["state"] == "needs_link", st
    assert next(iter(load_accounts(tmp_path / "accounts.json").values()))["state"] == "waiting_link"


# --------------------------------------------------------------------------- what the app knows of each website
def test_the_website_specifics_shipped_with_the_app_and_this_computer_s_own():
    import json
    import shutil
    from jobfinder import claude_forms as cf
    cf.learn("x.fr", buttons={"apply": "Je veux ce poste"}, route=["offer_page", "application_form"], result="filled")
    cf.save_plan("x.fr", "sig1", cf.PagePlan(page_kind="offer_page", fields=[], button="button|Go", button_role="apply"),
                 "https://x.fr/job/1?session=abc")
    assert cf.export_specifics() == 1
    shipped = json.loads((cf.SHIPPED_DIR / "x.fr.json").read_text(encoding="utf-8"))
    assert "session=abc" not in json.dumps(shipped) and not {"last", "visits"} & set(shipped["_learned"])
    shutil.rmtree(cf.FORMS_DIR)  # a new computer: nothing learned there yet
    assert cf.learned("x.fr")["buttons"]["apply"] == "Je veux ce poste" and cf.cached_plan("x.fr", "sig1").button == "button|Go"
    cf.learn("x.fr", buttons={"apply": "Postuler"})  # this computer's own visits are the latest word
    assert cf.learned("x.fr")["buttons"]["apply"] == "Postuler"
    assert cf.learned("x.fr")["route"] == ["offer_page", "application_form"]


def test_the_free_models_read_a_page_when_there_is_no_anthropic_key():
    from jobfinder import claude_forms as cf
    fields, buttons, asked = [_f("Votre petit nom", name="nick", elid="nick")], [_b("Étape suivante")], []

    def free(prompt, schema, system):
        asked.append(prompt)
        return schema.model_validate({"page_kind": "application_form", "button": cf.button_key(buttons[0]),
                                      "button_role": "continue", "fields": [{"key": cf.field_key(fields[0]), "role": "first_name"},
                                                                            {"key": "invented", "role": "email"}]})
    plan, how = cf.plan_page("", "z.fr", "u", "t", fields, buttons, free=free)
    assert how == "free AI" and [(x.key, x.role) for x in plan.fields] == [(cf.field_key(fields[0]), "first_name")]
    assert "Votre petit nom" in asked[0] and "page_kind" in asked[0]
    assert cf.plan_page("", "z.fr", "u", "t", fields, buttons, free=free)[1] == "saved" and len(asked) == 1

    def broken(prompt, schema, system):
        raise RuntimeError("no model has a key")
    assert cf.plan_page("", "w.fr", "u", "t", fields, buttons, free=broken) == (None, "free AI unavailable")


def test_workday_s_sign_in_choice_and_sign_ins_that_ask_the_email_first(tmp_path, window):
    """After 'Apply Manually' Workday offers Apple, Google or email: the email way (never another website's account).
    iCIMS, Oracle and IBM ask the email alone first: the account email, then 'Next'."""
    from jobfinder.apply import email_first
    url = _pages(tmp_path, offer="""<html><body><h1>Engineer</h1><a href="choice.html">Apply</a></body></html>""",
                 choice="""<html><body><h1>Sign In</h1><a href="google.html">Sign in with Google</a>
<a href="apple.html">Sign in with Apple</a><a href="email.html">Sign in with email</a></body></html>""",
                 email="""<html><head><title>Candidate login</title></head><body><form action="login.html">
<label for=e>Email address</label><input id=e name=e type=email><button type=submit>Next</button></form></body></html>""")
    (tmp_path / "login.html").write_text(LOGIN, encoding="utf-8")
    window.open("e1", url, _job(dry_run=True))
    st = _wait(window, "e1")
    assert st["state"] == "dry_run" and st["wall"] == "sign_in", st
    assert "'Sign in with email'" in st["steps"] and not any("Google" in x or "Apple" in x for x in st["steps"])
    window.open("e2", (tmp_path / "email.html").as_uri(), _job())
    st = _wait(window, "e2")
    assert any("account email given" in x for x in st["steps"]), st  # then the password page: an account is needed
    # a newsletter box is no sign-in page
    news = {"fields": [_f("Votre e-mail", "email")], "buttons": [_b("OK")], "title": "Nos offres d'emploi", "heading": "Data"}
    assert email_first(news, "https://x.fr/carrieres/offre-12") is None
    assert email_first({**news, "title": "Connexion"}, "https://x.fr/candidat") is not None


def test_making_the_accounts_once_stops_signed_in_and_fills_no_offer(tmp_path, window):
    from jobfinder.apply import load_accounts
    url = _site(tmp_path)
    window.open("o1", url, _job(allow_terms=True, accounts_only=True))
    st = _wait(window, "o1")
    assert st["state"] == "needs_code", st
    window.resume("o1", _job(allow_terms=True, accounts_only=True, code="123456"))
    st = _wait(window, "o1")
    assert st["state"] == "account_ready" and not st.get("filled"), st  # an offer the user did not choose: left empty
    assert next(iter(load_accounts(tmp_path / "accounts.json").values()))["state"] == "created"


def test_a_website_that_turns_the_window_away_is_said_plainly(tmp_path, window):
    url = _pages(tmp_path, offer="<html><head><title>403 Forbidden</title></head><body>403 Forbidden</body></html>")
    window.open("b1", url, _job())
    st = _wait(window, "b1")
    assert st["state"] == "blocked", st  # never 'no form found': the user opens the offer in their own browser


def test_a_field_hidden_since_the_last_reading_never_takes_another_field_s_place():
    """Hublo (19 Nov 2026): a field marked at one reading and skipped at the next (read-only now) kept its mark; a new
    field got the same number, and the first name was never filled (two elements for one mark)."""
    pw_api = pytest.importorskip("playwright.sync_api")
    from jobfinder.apply import fill_frame, scan_page
    with pw_api.sync_playwright() as pw:
        try:
            br = pw.chromium.launch()
        except Exception as e:
            pytest.skip(str(e))
        page = br.new_page()
        page.set_content('<label for=a>E-mail</label><input id=a type=email>')
        scan_page(page)
        page.evaluate("""() => { document.getElementById('a').readOnly = true;
            document.body.insertAdjacentHTML('afterbegin', '<label for=b>Prénom</label><input id=b>'); }""")
        fr, sc = scan_page(page)[0]
        assert fill_frame(fr, form_values(AP), "", sc) == ["first_name"]
        assert page.eval_on_selector("#b", "e => e.value") == "Jean" and page.eval_on_selector("#a", "e => e.value") == ""
        br.close()


def test_an_account_form_sent_by_enregistrer_and_a_form_left_unsent_is_redone(tmp_path, window):
    """SNCF (19 Nov 2026): the account form's button says 'Enregistrer' (an input of type button). And an account whose
    form was filled but never sent is no account: the next visit opens 'create an account' again (same password), it does
    not try to sign in with it."""
    from jobfinder.apply import account, load_accounts, site_of
    url = _site(tmp_path)
    (tmp_path / "signup.html").write_text(
        SIGNUP.replace("CAPTCHA", "").replace('<button type=submit>Créer mon compte</button>',
                                              '<input type=button value="Enregistrer" onclick="this.form.submit()">'),
        encoding="utf-8")
    first = account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")  # filled earlier, never sent
    window.open("r1", url, _job(allow_terms=True))
    st = _wait(window, "r1")
    assert st["state"] == "needs_code", st  # 'Enregistrer' pressed: the website asks the code
    assert any("opening 'create an account'" in x for x in st["steps"]) and any("creating the account" in x for x in st["steps"])
    acct = load_accounts(tmp_path / "accounts.json")[site_of(url)]
    assert acct["password"] == first["password"] and acct["state"] == "waiting_code"


def test_after_the_account_form_a_page_saying_nothing_known_waits_for_the_email(tmp_path, window):
    from jobfinder.apply import load_accounts
    url = _site(tmp_path)
    (tmp_path / "signup.html").write_text(SIGNUP.replace('action="code.html"', 'action="thanks.html"').replace("CAPTCHA", ""),
                                          encoding="utf-8")
    (tmp_path / "thanks.html").write_text("<html><body><h1>Bienvenue !</h1><p>Merci.</p></body></html>", encoding="utf-8")
    window.open("k1", url, _job(allow_terms=True))
    st = _wait(window, "k1")
    assert st["state"] == "needs_link" and "account form is sent" in st["message"], st  # never 'no form found'
    assert next(iter(load_accounts(tmp_path / "accounts.json").values()))["state"] == "sent"


def test_successfactors_account_form_its_privacy_statement_and_its_postuler_button(tmp_path, window):
    """SAP SuccessFactors' account page: the privacy statement is read and accepted through a link (a window with
    'Accepter'); its only button, 'Postuler', sends an application (19 Nov 2026: the website then asked the CV): never
    pressed."""
    from jobfinder.apply import load_accounts
    url = _site(tmp_path)
    (tmp_path / "login.html").write_text(LOGIN, encoding="utf-8")
    (tmp_path / "signup.html").write_text("""<html><body><h1>Créer un compte</h1><form action="code.html" method="get"
onsubmit="if (!window.ok) { document.title = 'REFUSED'; return false; }">
<label for=u>E-mail : *</label><input id=u name=username><label for=u2>Resaisissez l'adresse e-mail : *</label><input id=u2>
<label for=p1>Choisir un mot de passe : *</label><input id=p1 type=password>
<label for=p2>Confirmer le mot de passe : *</label><input id=p2 type=password>
<label for=fn>Prénom : *</label><input id=fn><label for=ln>Nom : *</label><input id=ln>
<label for=r>Si vous êtes référé par un employé, son e-mail</label><input id=r name=r>
<a href="#" onclick="document.getElementById('d').style.display='block'; return false;">Lire et accepter la déclaration de confidentialité</a>
<div id=d style="display:none"><p>Déclaration…</p><button type=button onclick="window.ok = true; this.parentNode.style.display='none'">Accepter</button></div>
<button type=submit>Postuler</button></form></body></html>""", encoding="utf-8")
    window.open("sf1", url, _job(allow_terms=True))
    st = _wait(window, "sf1")
    # privacy accepted; 'Postuler' sends an application on SuccessFactors (its CV is asked there): NEVER pressed, the
    # account comes with the first application the user sends
    assert st["state"] == "filled" and "makes your account then" in st["message"], st
    assert any("privacy statement" in x for x in st["steps"]) and not any("creating the account" in x for x in st["steps"])
    assert next(iter(load_accounts(tmp_path / "accounts.json").values()))["state"] == "creating"
    window.open("sf2", url, _job(allow_terms=True, accounts_only=True))
    assert _wait(window, "sf2")["state"] == "account_at_first_application"


def test_an_account_form_asking_a_security_question(tmp_path, window):
    """CNRS: the account form asks a security question and its answer (free text): a fixed question, an answer made for
    the website, kept with the password. 'Prénom' whose technical name holds 'Nom' is the first name."""
    from jobfinder.apply import SECURITY_QUESTION, load_accounts
    url = _site(tmp_path)
    (tmp_path / "signup.html").write_text("""<html><body><h1>Créer un compte</h1><form action="code.html" method="get"
onsubmit="if (!this.q.value || !this.a.value || this.fn.value != 'Jean') { document.title = 'REFUSED'; return false; }">
<label for=fn>Prénom</label><input id=fn name=txtPrenomNom><label for=ln>Nom</label><input id=ln name=txtNom>
<label for=em>Adresse e-mail</label><input id=em name=em>
<label for=p1>Mot de passe</label><input id=p1 type=password><label for=p2>Confirmation du mot de passe</label><input id=p2 type=password>
<label for=q>Question de sécurité</label><input id=q name=q><label for=a>Réponse à la question</label><input id=a name=a>
<label><input type=checkbox name=t required> J'ai lu et j'accepte les CGU</label>
<button type=submit>Créer un compte</button></form></body></html>""", encoding="utf-8")
    window.open("q1", url, _job(allow_terms=True))
    st = _wait(window, "q1")
    assert st["state"] == "needs_code", st
    acct = next(iter(load_accounts(tmp_path / "accounts.json").values()))
    assert acct["security_question"] == SECURITY_QUESTION and len(acct["security_answer"]) == 12


def test_a_required_notice_box_and_a_page_saying_an_email_was_sent(tmp_path, window):
    """Inria: the box to tick is 'Merci de prendre connaissance des mentions d'information... puis de cocher la case
    suivante*' (required, worded unlike usual terms). MAIF: after 'Je me crée un espace candidat' the same page says 'Un
    email vous a été envoyé concernant la création de votre compte': the step is done, never pressed again."""
    from jobfinder.apply import load_accounts
    url = _site(tmp_path)
    (tmp_path / "signup.html").write_text("""<html><body><h1>Créer un compte</h1>
<div id=msg></div><form onsubmit="if (!document.getElementById('c').checked) { return false; }
document.getElementById('msg').innerText = 'Un email vous a été envoyé concernant la création de votre compte.';
window.n = (window.n || 0) + 1; document.title = 'sent ' + window.n; return false;">
<label for=ln>Nom*</label><input id=ln><label for=fn>Prénom*</label><input id=fn><label for=em>Courriel*</label><input id=em>
<label for=p1>Mot de passe*</label><input id=p1 type=password><label for=p2>Confirmez*</label><input id=p2 type=password>
<label for=c>Merci de prendre connaissance des mentions d'information ci-dessous puis de cocher la case suivante*</label>
<input type=checkbox id=c><label><input type=checkbox id=nl> Recevoir nos offres</label>
<input type=submit value="Je me crée un espace candidat"></form></body></html>""", encoding="utf-8")
    window.open("i1", url, _job(allow_terms=True))
    st = _wait(window, "i1")
    assert st["state"] == "needs_link", st
    assert next(iter(load_accounts(tmp_path / "accounts.json").values()))["state"] == "waiting_link"


def test_a_page_saying_the_account_is_active(tmp_path, window):
    """CNRS, after its activation link: 'Votre inscription est terminée. Votre compte est actif.' The account is ready
    (a free model had read this page as one asking for a code: a page without any field never is)."""
    from jobfinder.apply import account, load_accounts, site_of
    url = _pages(tmp_path, offer="""<html><body><h1>Vérification de l'adresse de courriel de votre compte</h1>
<h2>Votre inscription est terminée</h2><p>Votre compte est actif.</p><a href="infos.html">Vos informations</a></body></html>""")
    account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    window.open("ok1", url, _job())
    st = _wait(window, "ok1")
    assert st["state"] == "account_ready" and "is active" in st["message"], st
    assert load_accounts(tmp_path / "accounts.json")[site_of(url)]["state"] == "created"


def test_an_offer_page_with_a_sign_in_form_in_its_header(tmp_path, window):
    """Talentsoft: the offer page shows a sign-in form at its top and its own 'Postuler'. Signed in from the header, the
    website shows 'Mon espace' and the offer is lost: the offer's 'Postuler' comes first, and a sign-in that leads
    elsewhere comes back to the offer."""
    from jobfinder.apply import account, set_account, site_of
    url = _pages(tmp_path, offer="""<html><body><form action="espace.html"><label for=e>Identifiant (email)</label>
<input id=e name=e><label for=p>Mot de passe</label><input id=p name=p type=password><input type=submit value="Se connecter">
</form><h1>Ingénieur IA</h1><a href="login.html">Postuler</a></body></html>""",
                 espace="<html><body><h1>Mon espace</h1><a href='logout.html'>Déconnecter</a></body></html>")
    (tmp_path / "login.html").write_text(LOGIN, encoding="utf-8")
    account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    set_account(site_of(url), tmp_path / "accounts.json", state="created")
    window.open("h1", url, _job())
    st = _wait(window, "h1")
    assert st["state"] == "filled", st
    assert st["steps"][0] == "'Postuler'" and any("signing in" in x for x in st["steps"]), st["steps"]


def test_the_cv_chosen_for_the_offer_goes_into_the_cv_field_only(tmp_path, window):
    """The user's request (19 Nov 2026): a CV field the website requires takes the CV chosen for the offer, named
    'CV_<first>_<last>.pdf'; never the letter field; the form is never sent."""
    cv = tmp_path / "2bae9305354979c511eb9168.pdf"
    cv.write_bytes(b"%PDF-1.4 test")
    url = _pages(tmp_path, offer="""<html><body><form onsubmit="location.href='sent.html'; return false;">
<label for=fn>Prénom</label><input id=fn><label for=ln>Nom</label><input id=ln><label for=em>E-mail</label><input id=em>
<label for=cv>CV *</label><input id=cv type=file required onchange="document.title = 'cv:' + this.files[0].name">
<label for=lm>Lettre de motivation (fichier)</label><input id=lm type=file onchange="document.title += ' letter!'">
<button type=submit>Envoyer</button></form></body></html>""")
    window.open("cv1", url, _job(cv_path=str(cv)))
    st = _wait(window, "cv1")
    assert st["state"] == "filled" and "cv" in st["filled"] and "CV attached" in st["steps"], st
    window.open("cv2", url, _job(cv_path=str(cv), dry_run=True))  # a dry run never sends the user's real file anywhere
    st2 = _wait(window, "cv2")
    assert "cv" not in st2.get("filled", []), st2


def test_the_cv_file_name_the_recruiter_sees():
    from jobfinder.apply import cv_filename
    assert cv_filename(form_values(AP)) == "CV_Jean_Dupont.pdf" and cv_filename({}) == "CV.pdf"
    assert cv_filename(form_values({"first_name": "Zineddine", "last_name": "Bouhadjira"})) == "CV_Zineddine_Bouhadjira.pdf"


def test_a_password_the_website_refuses_is_said_and_can_be_replaced(tmp_path, window):
    from jobfinder.apply import account, load_accounts, set_account, site_of
    url = _pages(tmp_path, offer="""<html><body><h1>Connexion</h1><div class=error>Le login et le mot de passe n'ont pas été
reconnus. Impossible de se connecter</div><form onsubmit="return false;"><label for=e>Identifiant (email)</label><input id=e>
<label for=p>Mot de passe</label><input id=p type=password><input type=submit value="Se connecter"></form></body></html>""")
    account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    set_account(site_of(url), tmp_path / "accounts.json", state="created")
    window.open("wp1", url, _job())
    st = _wait(window, "wp1")
    assert st["state"] == "wrong_password" and "box below" in st["message"], st
    assert load_accounts(tmp_path / "accounts.json")[site_of(url)]["state"] == "wrong_password"


def test_a_refused_password_asks_the_website_for_a_new_one(tmp_path, window):
    """Arkéa (20 Nov 2026): the website made its own password; the app's is refused. Its 'Mot de passe perdu' is opened
    once, the account email typed in its form and sent: the website emails a link (pasted in the app, it sets the app's
    password). The header's sign-in form on the same page is never used for it."""
    from jobfinder.apply import account, load_accounts, set_account, site_of
    login = """<form onsubmit="return false;"><label for=hl>Login</label><input id=hl><label for=hp>Password</label>
<input id=hp type=password><input type=submit value="Se connecter"></form>"""
    url = _pages(tmp_path, offer=f"""<html><body><h1>Connexion</h1><div class=error>Le login et le mot de passe n'ont pas été
reconnus. Impossible de se connecter</div>{login}<a href="lost.html">Mot de passe perdu</a></body></html>""",
                 lost=f"""<html><body>{login}<h1>Mot de passe perdu</h1><form onsubmit="location.href='asked.html?' +
encodeURIComponent(document.getElementById('i').value); return false;"><label for=i>Identifiant (email)</label><input id=i>
<input type=submit value="Recevoir mon mot de passe"></form></body></html>""",
                 asked="<html><body><h1>Un email vous a été envoyé</h1></body></html>")
    account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    set_account(site_of(url), tmp_path / "accounts.json", state="created")
    window.open("lp1", url, _job())
    st = _wait(window, "lp1")
    assert st["state"] == "needs_link" and "Link from the email" in st["message"], st
    assert load_accounts(tmp_path / "accounts.json")[site_of(url)]["state"] == "waiting_link"
    assert window.pages["lp1"].url.endswith("asked.html?" + AP["email"].replace("@", "%40")), window.pages["lp1"].url
    window.open("lp2", url, _job(dry_run=True))  # a dry run never asks a real website for anything
    assert _wait(window, "lp2")["state"] != "needs_link"


def test_a_reset_page_sets_the_new_password_only(tmp_path, window):
    """Arkéa (20 Nov 2026): the emailed link opens a page setting a new password, under the website's header sign-in
    (Identifiant + Mot de passe + 'Se connecter'). Only 'Nouveau' and 'Confirmer' take the app's password, and the
    page's own button is pressed, never the header's sign-in."""
    from jobfinder.apply import account, load_accounts, set_account, site_of
    url = _pages(tmp_path, offer="""<html><head><title>Réinitialisation du mot de passe</title></head><body>
<form onsubmit="return false;"><label for=hl>Identifiant (email)</label><input id=hl><label for=hp>Mot de passe</label>
<input id=hp type=password><input type=submit value="Se connecter" onclick="location.href='header.html'"></form>
<h1>Modifier mon mot de passe</h1><form onsubmit="location.href = 'done.html?' + (document.getElementById('n').value ===
document.getElementById('c').value) + (document.getElementById('hp').value === '') + (document.getElementById('hl').value === '');
return false;"><label for=n>Nouveau mot de passe</label><input id=n type=password><label for=c>Confirmer le mot de passe</label>
<input id=c type=password><input type=submit value="Enregistrer mon nouveau mot de passe"></form>
<a href="signup.html">Je me crée un espace candidat</a></body></html>""",
                 done="<html><body><h1>Votre mot de passe a bien été modifié</h1></body></html>")
    acct = account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    set_account(site_of(url), tmp_path / "accounts.json", state="wrong_password")
    from jobfinder import claude_forms  # Sorbonne: its 'create an account' button, remembered, is also on that page
    claude_forms.learn(site_of(url), buttons={"create_account": "Je me crée un espace candidat"})
    window.open("rs1", url, _job())
    st = _wait(window, "rs1")
    assert window.pages["rs1"].url.endswith("done.html?truetruetrue"), (window.pages["rs1"].url, st)
    assert st["state"] == "account_ready" and load_accounts(tmp_path / "accounts.json")[site_of(url)]["password"] == acct["password"]


def test_the_terms_shown_after_the_account_form_are_accepted(tmp_path, window):
    """SNCF (20 Nov 2026): 'Enregistrer' on the account form, then the website's CGU with 'J'accepte': the account exists
    only once accepted. The user allowed terms for their accounts; their data-protection mailbox in the text is never
    taken for 'apply by email'."""
    from jobfinder.apply import load_accounts, site_of
    url = _pages(tmp_path, offer="""<html><body><h1>Créer votre compte</h1><form onsubmit="location.href='cgu.html'; return false;">
<label for=n>NOM*</label><input id=n required><label for=p>Prénom*</label><input id=p required><label for=e>Email*</label>
<input id=e type=email required><label for=e2>Ressaisir l'email*</label><input id=e2 type=email required>
<label for=w>Mot de passe**</label><input id=w type=password required><label for=w2>Ressaisir le mot de passe**</label>
<input id=w2 type=password required><input type=button value=Retour><input type=submit value=Enregistrer></form></body></html>""",
                 cgu="""<html><body><p>En sélectionnant « J'accepte », vous confirmez avoir lu et acceptez l'ensemble des CGU du site.
Contact : donneespersonnellesrecrutement@sncf.fr</p><button onclick="location.href='made.html'">J'accepte</button></body></html>""",
                 made="<html><body><h1>Votre compte a été créé</h1></body></html>")
    window.open("cg1", url, _job(allow_terms=True))
    st = _wait(window, "cg1")
    assert st["state"] == "account_ready", st
    assert load_accounts(tmp_path / "accounts.json")[site_of(url)]["state"] == "created"


def test_an_account_whose_email_is_not_validated_asks_the_email_again_once(tmp_path, window):
    """Amundi (20 Nov 2026): signed in, the website says the email address is not validated yet and its email never came.
    Its 'Envoyer le mail à nouveau' is pressed once and the app waits for the link (never signs in again and again)."""
    from jobfinder.apply import account, load_accounts, set_account, site_of
    url = _pages(tmp_path, offer="""<html><body><h1>Connexion</h1><form onsubmit="location.href='unvalidated.html'; return false;">
<label for=e>Identifiant</label><input id=e><label for=p>Mot de passe</label><input id=p type=password>
<input type=submit value="Se connecter"></form></body></html>""",
                 unvalidated="""<html><body><form onsubmit="return false;"><label for=e>Identifiant</label><input id=e>
<label for=p>Mot de passe</label><input id=p type=password><input type=submit value="Se connecter"></form>
<p>Votre adresse e-mail n'a pas encore été validée. Veuillez cliquer sur le lien contenu dans l'e-mail que nous vous avons envoyé.</p>
<button onclick="document.title = 'sent ' + (+document.title.split(' ')[1] + 1 || 1)">Envoyer le mail à nouveau</button></body></html>""")
    account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    set_account(site_of(url), tmp_path / "accounts.json", state="sent")
    window.open("rv1", url, _job())
    st = _wait(window, "rv1")
    assert st["state"] == "needs_link" and "email asked again" in " ".join(st["steps"]), st
    assert load_accounts(tmp_path / "accounts.json")[site_of(url)]["state"] == "waiting_link"


def test_a_password_refused_inside_a_frame_is_seen(tmp_path, window):
    """SNCF (20 Nov 2026): 'Postuler' opens a pop-up whose sign-in is a frame of another website; the refusal is said
    there. It was not seen: the app pressed 'Postuler' again and said the application did not open."""
    from jobfinder.apply import account, set_account, site_of
    url = _pages(tmp_path, offer="""<html><body><h1>Technicien</h1><button onclick="document.getElementById('pop').innerHTML =
'<iframe src=login3.html style=width:600px;height:400px></iframe>'">Postuler</button><div id=pop></div></body></html>""",
                 login3="""<html><body><h2>Connexion</h2><div id=err></div><form onsubmit="document.getElementById('err').innerText =
'Votre email ou votre mot de passe n\u2019est pas valide. Veuillez recommencer.'; return false;"><label for=e>Email*</label>
<input id=e type=email><label for=p>Mot de passe*</label><input id=p type=password><a href=lost3.html>Mot de passe oublié ?</a>
<button type=submit>S'identifier</button></form></body></html>""",
                 lost3="""<html><body><h2>Mot de passe oublié</h2><form onsubmit="document.body.innerHTML = '<h2>Email envoyé</h2>';
return false;"><label for=m>Email</label><input id=m type=email><button type=submit>Envoyer</button></form></body></html>""")
    account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    set_account(site_of(url), tmp_path / "accounts.json", state="created")
    window.open("fr1", url, _job())
    st = _wait(window, "fr1")
    assert st["state"] == "needs_link", "\n".join(st.get("steps", []))


def test_after_signing_in_the_offer_s_postuler_is_pressed_again(tmp_path, window):
    """CNRS (19 Nov 2026): 'Postuler' -> sign-in -> back to the offer; its 'Postuler' had been pressed before the sign-in
    and was never pressed again. Signed in, it leads to the form."""
    from jobfinder.apply import account, set_account, site_of
    url = _pages(tmp_path, offer="""<html><body><h1>Ingénieur d'études</h1><a href="#" onclick="location.href =
(sessionStorage.getItem('in') ? 'form.html' : 'login2.html'); return false;">Postuler</a></body></html>""",
                 login2="""<html><body><h1>Connexion</h1><form onsubmit="sessionStorage.setItem('in', '1'); location.href='offer.html';
return false;"><label for=e>Adresse e-mail</label><input id=e type=email><label for=p>Mot de passe</label>
<input id=p type=password><button type=submit>Se connecter</button></form></body></html>""")
    account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    set_account(site_of(url), tmp_path / "accounts.json", state="created")
    window.open("rp1", url, _job())
    st = _wait(window, "rp1")
    assert st["state"] == "filled", st
    assert [x for x in st["steps"] if x == "'Postuler'"] == ["'Postuler'", "'Postuler'"], st["steps"]


def test_a_link_from_the_email_sets_the_account_s_password_in_the_window(tmp_path, window):
    """MAIF / Arkéa (19 Nov 2026): the website made its own password and emailed a link to set one. The user pastes the
    link in the app: the window opens it, types the password the app made for the website, and the account is ready."""
    from jobfinder.apply import account, load_accounts, set_account, site_of
    (tmp_path / "offer.html").write_text("<html><body><h1>Offre</h1></body></html>", encoding="utf-8")
    (tmp_path / "reset.html").write_text("""<html><head><title>Réinitialisation du mot de passe</title></head><body>
<h1>Modifier mon mot de passe</h1><form action="done.html"><label for=a>Nouveau mot de passe *</label><input id=a name=a type=password>
<label for=b>Confirmer le mot de passe *</label><input id=b name=b type=password>
<input type=submit value="Enregistrer mon nouveau mot de passe"></form></body></html>""", encoding="utf-8")
    (tmp_path / "done.html").write_text("<html><body><h1>Votre mot de passe a bien été modifié.</h1></body></html>",
                                        encoding="utf-8")
    url = (tmp_path / "offer.html").as_uri()
    acct = account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    set_account(site_of(url), tmp_path / "accounts.json", state="waiting_link")
    window.open("lk0", url, _job())
    _wait(window, "lk0")
    window.open_link("lk0", (tmp_path / "reset.html").as_uri(), _job())
    st = _wait(window, "lk0", busy=("opening", "working"))
    assert st["state"] == "account_ready", st
    a = load_accounts(tmp_path / "accounts.json")[site_of(url)]
    assert a["state"] == "created" and a["password"] == acct["password"]


def test_a_link_whose_click_opens_nothing_is_followed_and_a_menu_join_us_never_pressed(tmp_path, window):
    """Hardis (19 Nov 2026): 'Postuler à l'offre' opens Cornerstone in a new tab the browser held back; the app then pressed
    the menu's 'Join us'. Now the link's address is opened, and a 'Join us' in the website's menu is never an apply button."""
    from jobfinder.apply import apply_rank
    url = _pages(tmp_path, offer="""<html><body><header><nav><button>Join us</button></nav></header><h1>Technicien</h1>
<a href="form.html" target="_blank" onclick="return false;">Postuler à l'offre</a></body></html>""")
    window.open("hd1", url, _job())
    st = _wait(window, "hd1")
    assert st["state"] == "filled" and "'Join us'" not in st["steps"], st
    assert apply_rank({"tag": "button", "text": "Join us", "nav": True}) is None
    assert apply_rank({"tag": "button", "text": "Rejoins-nous !", "nav": False}) == 4


def test_a_403_page_with_a_return_address_means_sign_in_first(tmp_path, window):
    """CNRS (19 Nov 2026): 'Postuler' without being signed in shows /Erreur/Erreur403.aspx?ReturnUrl=...: not a block,
    a sign-in to make."""
    url = _pages(tmp_path, offer="<html><body><h1>Ingénieur</h1><a href='err.html?ReturnUrl=/Candidater'>Postuler</a></body></html>",
                 err="""<html><head><title>Erreur 403</title></head><body><h1>Accès refusé</h1>
<a href="login.html">Se connecter</a></body></html>""")
    (tmp_path / "login.html").write_text(LOGIN, encoding="utf-8")
    window.open("r403", url, _job(dry_run=True))
    st = _wait(window, "r403")
    assert st["state"] == "dry_run" and st["wall"] == "sign_in", st


def test_a_postuler_that_opens_a_menu_is_no_dead_link(tmp_path, window):
    """Alstom (19 Nov 2026): 'Postuler »' links to the home page but opens a menu; the app went to the home page. A link is
    followed by its address only when its click changed nothing on the page."""
    (tmp_path / "home.html").write_text("<html><body><h1>Home</h1></body></html>", encoding="utf-8")
    url = _pages(tmp_path, offer="""<html><body><h1>Ingénieur</h1><a href="home.html" onclick="document.getElementById('m')
.style.display='block'; return false;">Postuler »</a><div id=m style="display:none"><a href="form.html">Apply Now</a></div></body></html>""")
    window.open("al1", url, _job())
    st = _wait(window, "al1")
    assert st["state"] == "filled", st


def test_a_job_title_is_never_the_civility():
    """Siemens (20 Nov 2026): 'Current / Last position title' and 'Job Title' were taken for the civility (M./Mme)."""
    assert field_kind(F("Current / Last position title *")) == "current_position" and field_kind(F("Job Title")) is None
    assert field_kind(F("Titre du poste")) is None
    assert field_kind(F("Title")) == "civility" and field_kind(F("Civilité")) == "civility"


def test_create_an_account_by_typing_the_details_not_with_a_cv(tmp_path, window):
    """Siemens (20 Nov 2026): 'Already registered?' beside 'Create an account using any of the following options: Upload
    file | Self-complete'. Without an account, 'Self-complete' (never the CV upload) opens the account form."""
    url = _pages(tmp_path, offer="""<html><body><h2>Already registered?</h2><form onsubmit="return false;"><label for=e>Email</label>
<input id=e type=email><label for=p>Password</label><input id=p type=password><button type=submit>Login</button></form>
<h2>Create an account using any of the following options:</h2><a href="upload.html">Upload file</a>
<a href="register.html">Self-complete</a></body></html>""",
                 register="""<html><body><form onsubmit="return false;"><label for=f>First Name *</label><input id=f>
<label for=l>Last Name *</label><input id=l><label for=m>Email *</label><input id=m type=email>
<label for=w>Password *</label><input id=w type=password><label for=w2>Confirm password *</label><input id=w2 type=password>
<button type=submit>Continue</button></form></body></html>""")
    window.open("sc1", url, _job(accounts_only=True))
    st = _wait(window, "sc1")
    assert window.pages["sc1"].url.endswith("register.html"), st
    assert "no account on this website yet: opening 'create an account'" in st["steps"], st


def test_a_privacy_statement_before_the_account_form_is_accepted_when_allowed(tmp_path, window):
    """3DS (Taleo, 20 Nov 2026): 'New User' shows the applicants' privacy statement with 'I Accept' / 'I Decline' before
    the account form. Accepted when the user allowed terms; never in a dry run, never 'I Decline'."""
    url = _pages(tmp_path, offer="""<html><body><h2>Privacy statement</h2><p>Please read our privacy policy for applicants: your
personal data is processed for recruitment.</p><input type=button value="I Accept" onclick="location.href='register.html'">
<input type=button value="I Decline" onclick="location.href='declined.html'"></body></html>""",
                 register="""<html><body><h2>Create an account</h2><form onsubmit="return false;"><label for=u>User Name</label>
<input id=u><label for=p>Password</label><input id=p type=password><label for=p2>Re-enter Password</label>
<input id=p2 type=password><label for=e>Email Address</label><input id=e type=email><button type=submit>Register</button>
</form></body></html>""")
    window.open("ps1", url, _job(dry_run=True))
    assert _wait(window, "ps1")["state"] != "dry_run" or not window.pages["ps1"].url.endswith("register.html")
    window.open("ps2", url, _job(dry_run=True, allow_terms=True))
    st = _wait(window, "ps2")
    assert not window.pages["ps2"].url.endswith("register.html"), st  # a dry run accepts nothing
    window.open("ps3", url, _job(allow_terms=True, accounts_only=True))
    st = _wait(window, "ps3")
    assert "the website's terms accepted (you allowed it for your accounts)" in st["steps"], st
    assert "declined" not in window.pages["ps3"].url, st


def test_the_user_s_own_email_is_never_an_offer_s_mailbox():
    """Mitsubishi Electric (20 Nov 2026): signed in, the page shows the account's email beside 'Contact': the app said
    'apply by email: write to <the user's own address>'."""
    from jobfinder.apply import by_email
    scans = [(None, {"buttons": [], "mails": [{"addr": "me@acme.org", "before": "Envoyez votre CV et votre lettre de candidature à"}]})]
    assert by_email(scans) == "me@acme.org" and by_email(scans, ("me@acme.org",)) == ""
    scans[0][1]["buttons"] = [{"text": "Postuler par mail", "href": "mailto:ME@acme.org"}]
    assert by_email(scans, ("me@acme.org",)) == ""


def test_the_questions_many_forms_ask_are_answered_from_the_settings():
    """BlaBlaCar on Lever, 20 Nov 2026: 'What are your salary expectations?', 'Are you based in Paris or ready to
    relocate?', 'Are you fluent in English?' were left empty. The user's answers (Settings) fill them; a question about the
    current salary, travel or another city is never answered for them."""
    from jobfinder.apply import pick_option
    v = form_values({**AP, "salary_expectation": "38000", "availability": "immediately", "english_level": "C2"})
    assert v["salary"] == "38000" and v["availability"] == "Immédiatement" and v["english"] == "Bilingue (C2)"
    assert field_kind(F("What are your salary expectations for this role? (gross annual, EUR)")) == "salary"
    assert field_kind(F("Prétentions salariales")) == "salary" and field_kind(F("Disponibilité")) == "availability"
    assert field_kind(F("Are you fluent in English?")) == "english" and field_kind(F("Niveau d'anglais")) == "english"
    assert field_kind(F("Are you based in Paris or ready to relocate?")) == "relocation"
    assert pick_option(["", "Yes", "No"], "english", v["english"]) == 1
    assert pick_option(["Sélectionner", "Débutant", "Intermédiaire", "Courant", "Bilingue"], "english", v["english"]) == 4
    assert pick_option(["--", "Dans 3 mois", "Immédiate"], "availability", v["availability"]) == 2
    assert pick_option(["--", "30-35 k€", "35-40 k€", "40-45 k€"], "salary", "38000") == 2
    assert pick_option(["", "Oui", "Non"], "relocation", "yes") == 1


def test_an_application_form_s_questions_and_consent_are_filled_when_allowed(tmp_path):
    """The user's request (20 Nov 2026): pressing 'Open and fill' leaves only the CV and the submit button. The required
    consent box is ticked when allowed; a newsletter box never; the current salary and another city never answered.
    (Lever-like markup: each question and its answers in one block.)"""
    from playwright.sync_api import sync_playwright
    from jobfinder.apply import drive
    (tmp_path / "offer.html").write_text("""<html><body><h1>Candidature</h1><form onsubmit="location.href='sent.html'; return false;">
<label for=fn>Prénom</label><input id=fn required><label for=ln>Nom</label><input id=ln required>
<label for=em>E-mail</label><input id=em type=email required>
<label for=sa>What are your salary expectations for this role? *</label><input id=sa required>
<label for=cur>Salaire actuel</label><input id=cur>
<div class=q><div>Are you based in Paris or ready to relocate? *</div><ul><li><label><input type=radio name=reloc value=y required>Yes</label></li>
<li><label><input type=radio name=reloc value=n>No</label></li></ul></div>
<div class=q><div>Are you willing to relocate to Lyon?</div><ul><li><label><input type=radio name=lyon value=y>Yes</label></li>
<li><label><input type=radio name=lyon value=n>No</label></li></ul></div>
<label for=en>Are you fluent in English?</label><select id=en><option></option><option>Yes</option><option>No</option></select>
<label for=av>Disponibilité</label><input id=av>
<label for=cv>CV *</label><input id=cv type=file>
<label><input type=checkbox id=gdpr required> J'accepte la politique de confidentialité et le traitement de mes données *</label>
<label><input type=checkbox id=nl> Je souhaite recevoir la newsletter</label>
<button type=submit>Envoyer</button></form></body></html>""", encoding="utf-8")
    ap = {**AP, "salary_expectation": "38000", "availability": "immediately", "english_level": "C2", "city": "Paris"}
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        page = br.new_page()
        page.goto((tmp_path / "offer.html").as_uri())
        page, st = drive(page, _job(values=form_values(ap), allow_terms=True), accounts_path=tmp_path / "accounts.json")
        got = page.evaluate("""() => { const g = id => document.getElementById(id); return {sa: g('sa').value, cur: g('cur').value,
            reloc: (document.querySelector('[name=reloc]:checked') || {}).value || null,
            lyon: (document.querySelector('[name=lyon]:checked') || {}).value || null, en: g('en').value, av: g('av').value,
            gdpr: g('gdpr').checked, nl: g('nl').checked, url: location.href}; }""")
        br.close()
    assert st["state"] == "filled", st
    assert got["sa"].startswith("38 000") and got["cur"] == "" and got["reloc"] == "y" and got["lyon"] is None, got
    assert got["en"] == "Yes" and got["av"] == "Immédiatement" and got["gdpr"] and not got["nl"], got
    assert "sent.html" not in got["url"]  # never sent


def test_the_position_the_message_and_answer_radios_are_understood():
    """Aubay, Novencia, Neurones (20 Nov 2026): 'Poste recherché *' takes the offer's title, 'Votre message' the letter, and
    radios that hold the answer itself ('M', 'Immédiate') are chosen like a list's options."""
    from jobfinder.apply import pick_option
    assert field_kind(F("Poste recherché *")) == "position" and field_kind(F("Intitulé du poste")) == "position"
    assert field_kind({**F("Votre message"), "tag": "textarea", "type": "textarea"}) == "cover_letter"
    assert field_kind(F("Nom de l'entreprise")) is None  # still never the user's
    assert pick_option(["", "M"], "civility", "M.") == 1 and pick_option(["", "Mme"], "civility", "M.") is None
    assert pick_option(["", "immédiate"], "availability", "Immédiatement") == 1
    assert pick_option(["", "< 3 mois"], "availability", "Immédiatement") is None


def test_an_offer_inside_a_page_wide_form_opens_its_application_never_one_click(tmp_path, window):
    """Mitsubishi Electric (Talentsoft, ASP.NET, 20 Nov 2026): ONE form holds the whole page (header sign-in, search, the
    offer). Its 'Je postule à cette offre' opens the application form; 'Postuler en un clic' would send the stored profile at
    once: never pressed. On the application page itself (the user's details in the same page-wide form), its submit sends:
    never pressed either."""
    head = """<input type=hidden name=__VIEWSTATE value=x><label for=lg>Identifiant</label><input id=lg>
<label for=pw>Mot de passe</label><input id=pw type=password><input type=submit value="Se connecter"
onclick="this.form.action='login.html'"><label for=kw>Mots clés</label><input id=kw>"""
    url = _pages(tmp_path, offer=f"""<html><body><form method=get action=offer.html>{head}<h1>Ingénieur data</h1><p>Description</p>
<input type=submit value="Je postule à cette offre" onclick="this.form.action='apply.html'">
<input type=submit value="Postuler en un clic" onclick="this.form.action='sent.html'"></form></body></html>""",
                 apply=f"""<html><body><form method=get action=sent.html>{head}<h1>Ma candidature</h1><label for=fn>Prénom</label>
<input id=fn><label for=ln>Nom</label><input id=ln><label for=em>E-mail</label><input id=em type=email>
<input type=submit value="Je postule"></form></body></html>""")
    window.open("wp1", url, _job())
    st = _wait(window, "wp1")
    assert st["state"] == "filled" and "apply.html" in window.pages["wp1"].url, (st, window.pages["wp1"].url)
    assert "Postuler en un clic" not in " ".join(st["steps"])


def test_chronopost_s_phone_civility_and_language_lists_are_understood():
    """Chronopost (20 Nov 2026): 'Téléphone portable de préférence' was taken for a referee's field (préférence), the
    civility radios 'Madame*' 'Monsieur*' (named id_civilite) said nothing, the language and level lists stayed empty."""
    from jobfinder.apply import pick_option
    assert field_kind(F("Téléphone*", placeholder="Téléphone portable de préférence", name="tel_portable")) == "phone"
    assert field_kind({**F(""), "type": "radio", "label": "Monsieur*", "group": "", "name": "id_civilite"}) == "civility"
    assert field_kind(F("Nom du référent")) is None and field_kind(F("Référence de l'offre")) is None
    assert field_kind({**F("Langue 1 et niveau", name="id_langue_1"), "tag": "select"}) == "language"
    v = form_values({**AP, "english_level": "C2"})["language"]
    assert pick_option(["", "Allemand", "Anglais", "Arabe"], "language", v) == 2
    assert pick_option(["niveau", "Bilingue", "Courant", "Opérationnel"], "language", v) == 1


def test_a_remembered_button_is_the_form_s_own_when_two_share_its_text():
    """Inria (20 Nov 2026): the header's 'Se connecter' link and the sign-in form's 'Se connecter' button; the remembered
    'Se connecter' found the header link first, which only reloads the page."""
    from jobfinder.apply import _saved_button
    scan = {"buttons": [{"tag": "a", "text": "Se connecter", "href": "", "submits": False},
                        {"tag": "input", "text": "Se connecter", "href": "", "submits": True}]}
    assert _saved_button(scan, {"buttons": {"sign_in": "Se connecter"}}, "sign_in")["tag"] == "input"


def test_a_gender_list_is_the_civility_but_never_a_contract_type():
    """SNCF (20 Nov 2026): the civility list is named 'Genre' (label in another cell) with 'Madame' / 'Monsieur'."""
    from jobfinder.apply import pick_option
    assert field_kind({**F("", name="NoGenre", elid="Genre"), "tag": "select"}) == "civility"
    assert field_kind(F("Genre de contrat")) is None and field_kind(F("Gender")) == "civility"
    assert pick_option(["", "Madame", "Monsieur", "Ne se prononce pas"], "civility", "M.") == 2
    assert pick_option(["", "Femme", "Homme"], "civility", "M.") == 2


def test_signed_in_the_application_the_offer_opens_is_kept_and_its_sections_opened(tmp_path, window):
    """CNRS and SNCF (20 Nov 2026). CNRS: once signed in, 'Postuler' opens the application page; the app went 'back to the
    offer' as after a sign-in that lands on the account's dashboard. SNCF: once signed in, the application shows in a frame
    over the offer, in sections ('Ouvrir Vos informations d'identité'): the app pressed 'Postuler' behind it again."""
    from jobfinder.apply import account, set_account, site_of
    url = _pages(tmp_path, offer="""<html><body><h1>Technicien</h1><button onclick="document.getElementById('pop').innerHTML =
(sessionStorage.getItem('in') ? '<iframe src=sections.html style=width:700px;height:500px></iframe>' :
'<iframe src=login4.html style=width:600px;height:400px></iframe>')">Postuler</button><div id=pop></div></body></html>""",
                 login4="""<html><body><h2>Connexion</h2><form onsubmit="sessionStorage.setItem('in', '1');
location.href='sections.html'; return false;"><label for=e>Email*</label><input id=e type=email><label for=p>Mot de passe*</label>
<input id=p type=password><button type=submit>S'identifier</button></form></body></html>""",
                 sections="""<html><body><h2>Vous candidatez à l'offre suivante</h2><table><tr><td><a href="identity.html"
title="Ouvrir Vos informations d'identité"><img alt="" style="display:inline-block;width:20px;height:20px"></a></td><td><b>Vos informations d'identité</b></td><td>INCOMPLET</td></tr>
<tr><td><a href="cv.html" title="Ouvrir Votre CV"><img alt="" style="display:inline-block;width:20px;height:20px"></a></td><td>Votre CV</td><td>INCOMPLET</td></tr></table></body></html>""",
                 identity="""<html><body><h2>Vous candidatez à l'offre suivante</h2><form onsubmit="return false;">
<label for=n>Nom (*)</label><input id=n><label for=pr>Prénom (*)</label><input id=pr><label for=t>Téléphone (*)</label><input id=t>
<button type=submit>Enregistrer</button></form></body></html>""")
    account(site_of(url), AP["email"], create=True, path=tmp_path / "accounts.json")
    set_account(site_of(url), tmp_path / "accounts.json", state="created")
    window.open("sx1", url, _job())
    st = _wait(window, "sx1")
    assert st["state"] == "filled" and "phone" in st["filled"], st["steps"]
    assert "opening the application's section 'Ouvrir Vos informations d'identité'" in st["steps"], st["steps"]


def test_current_position_location_and_phone_code_are_filled():
    """20 Nov 2026 test on every website: 'Current position *' (26 forms), 'Location (City)*', 'Current Location' and the
    phone's country code were left empty. A company's own location and name stay empty."""
    from jobfinder.apply import pick_option
    v = form_values({**AP, "current_position": "Machine Learning Engineer", "country": "France"})
    assert v["current_position"] == "Machine Learning Engineer" and v["phone_code"] == "+33"
    assert field_kind(F("Current position *")) == "current_position" and field_kind(F("Poste actuel *")) == "current_position"
    assert field_kind(F("Location (City)*")) == "city" and field_kind(F("Current Location")) == "city"
    assert field_kind(F("Company location")) is None and field_kind(F("Nom de l'entreprise actuelle")) is None
    assert field_kind(F("Indicatif du pays/territoire*")) == "phone_code"
    assert pick_option(["", "Allemagne (+49)", "France (+33)", "Guyane française (+594)"], "phone_code", "+33") == 2


def test_combobox_lists_are_chosen_from_their_own_options(tmp_path):
    """Greenhouse (Artefact, 20 Nov 2026): 'Country' and the consents 'Personal Data*' / 'Resume Data*' are comboboxes: a
    list opens on a click. The app typed text, which they do not keep, and its options were read from another list of
    the page (the phone's hidden list of countries). Now: the opened list's option; a consent only when allowed."""
    from playwright.sync_api import sync_playwright
    from jobfinder.apply import drive
    combo = lambda cid, label, opts: f"""<div class=select__container><label id={cid}-l for={cid}>{label}</label>
<input id={cid} role=combobox aria-labelledby={cid}-l aria-controls={cid}-lb required onclick="document.getElementById('{cid}-lb').style.display='block'">
<span class=select__single-value id={cid}-v></span><ul id={cid}-lb role=listbox style="display:none">""" + "".join(
        f"""<li role=option onclick="document.getElementById('{cid}-v').textContent=this.textContent;this.parentNode.style.display='none'">{o}</li>"""
        for o in opts) + "</ul></div>"
    (tmp_path / "offer.html").write_text(f"""<html><body><h1>Apply</h1><form onsubmit="return false;">
<ul role=listbox style="display:none"><li role=option>Afghanistan+93</li><li role=option>France+33</li></ul>
<label for=fn>First Name</label><input id=fn required><label for=ln>Last Name</label><input id=ln required>
<label for=em>Email</label><input id=em type=email required>
{combo("country", "Country*", ["Belgium", "France", "Germany"])}
{combo("q1", "Personal Data*", ["I accept and agree to the privacy policy.", "I do not agree"])}
{combo("q2", "Are you looking for a 6-month internship?*", ["Yes", "No"])}
<label for=cv>Resume/CV*</label><input id=cv type=file><button type=submit>Submit application</button></form></body></html>""",
                                          encoding="utf-8")
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        out = {}
        for allow in (False, True):
            page = br.new_page()
            page.goto((tmp_path / "offer.html").as_uri())
            page, st = drive(page, _job(allow_terms=allow), accounts_path=tmp_path / "accounts.json")
            out[allow] = [page.inner_text(f"#{c}-v") for c in ("country", "q1", "q2")]
        br.close()
    assert out[False] == ["France", "", ""], out
    assert out[True] == ["France", "I accept and agree to the privacy policy.", ""], out


def test_the_offer_s_letter_pdf_goes_into_the_letter_field_and_the_cv_into_the_cv_field(tmp_path, window):
    """CNRS (20 Nov 2026): 'Votre CV *' and 'Votre Lettre de Motivation *' are both files. The CV chosen for the offer and
    the offer's cover letter (its PDF, written in the app) go each into its own field; never sent."""
    cv, letter = tmp_path / "cv.pdf", tmp_path / "letter.pdf"
    cv.write_bytes(b"%PDF-1.4 cv")
    letter.write_bytes(b"%PDF-1.4 letter")
    url = _pages(tmp_path, offer="""<html><body><h1>Formulaire de candidature</h1><form onsubmit="location.href='sent.html'; return false;">
<label for=cv>Ajouter un CV depuis mon ordinateur</label><input id=cv type=file onchange="document.title += ' cv:' + this.files[0].name">
<label for=lm>Ajouter une lettre de motivation depuis mon ordinateur</label>
<input id=lm type=file onchange="document.title += ' lm:' + this.files[0].name"><button type=submit>Envoyer</button></form></body></html>""")
    window.open("lt1", url, _job(cv_path=str(cv), letter_path=str(letter)))
    st = _wait(window, "lt1")
    assert st["state"] == "filled" and set(st["filled"]) >= {"cv", "letter"}, st
    assert "CV attached" in st["steps"] and "cover letter attached" in st["steps"], st["steps"]


def test_greenhouse_questions_algolia():
    """Algolia on Greenhouse (20 Nov 2026): 'What is your preferred first name?' ('preferred' held 'referred': a referee's
    field), 'How did you initially hear about this job?', sponsorship asked in a text box, a required 'Acknowledged'."""
    from jobfinder.apply import _CONSENT, _kinds_said
    assert field_kind(F("What is your preferred first name?*")) == "first_name" and field_kind(F("Referred by")) is None
    assert field_kind(F("How did you initially hear about this job?*")) == "how_heard"
    assert _kinds_said("will you now, or in the future, require sponsorship for employment visa status?", "textarea") == ["sponsorship"]
    assert _kinds_said("tell us about your last project", "textarea") == []
    assert _CONSENT.search("Guidelines for using AI in our interviewing process * Acknowledged")


def test_a_hidden_consent_box_marked_required_by_aria_is_ticked_when_allowed(tmp_path):
    """Orange (Phenom, 20 Nov 2026): '* J'accepte de transmettre mes données de candidature...', the star first, required
    by aria-required, the box itself hidden under a styled one."""
    from playwright.sync_api import sync_playwright
    from jobfinder.apply import drive
    (tmp_path / "offer.html").write_text("""<html><body><h1>Vous postulez</h1><form onsubmit="return false;">
<label for=fn>Prénom</label><input id=fn><label for=ln>Nom</label><input id=ln><label for=em>E-mail</label><input id=em type=email>
<span style="position:relative"><input type=checkbox id=dp aria-required=true aria-labelledby=dp-l
style="position:absolute;opacity:0;width:0;height:0"><span id=dp-l>* J’accepte de transmettre mes données de candidature aux
équipes de recrutement.</span></span><label><input type=checkbox id=nl> Recevoir les offres par e-mail</label>
<label for=cv>CV</label><input id=cv type=file><button type=submit>Postuler</button></form></body></html>""", encoding="utf-8")
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        page = br.new_page()
        page.goto((tmp_path / "offer.html").as_uri())
        page, st = drive(page, _job(allow_terms=True), accounts_path=tmp_path / "accounts.json")
        got = page.evaluate("() => [document.getElementById('dp').checked, document.getElementById('nl').checked]")
        br.close()
    assert st["state"] == "filled" and got == [True, False], (st, got)


def test_a_menu_apply_link_back_to_the_job_list_is_never_pressed(tmp_path, window):
    """Michelin (20 Nov 2026): the menu's 'Apply' links to the job list above the offer (/jobs, the offer being
    /jobs/analyst); the offer's own 'Apply' goes to its application. The app pressed the menu's first, in a loop."""
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "analyst.html").write_text("""<html><body><nav><a href="../jobs">Apply</a></nav><h1>Analyst</h1>
<p>Description</p><a href="../form.html">Apply</a></body></html>""", encoding="utf-8")
    (tmp_path / "jobs.html").write_text("<html><body><h1>All jobs</h1></body></html>", encoding="utf-8")
    _pages(tmp_path)
    window.open("up1", (tmp_path / "jobs" / "analyst.html").as_uri(), _job())
    st = _wait(window, "up1")
    assert st["state"] == "filled" and window.pages["up1"].url.endswith("form.html"), (st, window.pages["up1"].url)
