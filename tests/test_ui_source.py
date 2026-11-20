"""Two flaws found by driving the app in a browser (19 Nov 2026), kept from coming back."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_icons_keep_their_font():
    """The app's typeface rule covers every element; icon elements are the exception. An alert's icon
    (st.warning(..., icon=':material/...:')) is a 'stAlertDynamicIcon' element: without the exception its name is printed
    as text ('assignment_turned_in') over the message."""
    css = (ROOT / "assets" / "navy.css").read_text(encoding="utf-8")
    rule = re.search(r"([^{}]*)\{\s*(?:/\*.*?\*/\s*)?font-family:\s*\"Material Symbols Rounded\"", css, re.S)
    assert rule and "DynamicIcon" in rule.group(1) and "stIconMaterial" in rule.group(1)


def test_the_email_code_box_shows_as_soon_as_the_window_asks():
    """The application window works in its own thread; only the part refreshed every 2 s (_apply_status, a fragment)
    sees it ask for the email code: the box to type it must be drawn there, not in the part drawn once at the click."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fns = {n.name: ast.get_source_segment((ROOT / "app.py").read_text(encoding="utf-8"), n)
           for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "Code received by email" in fns["_apply_status"] and "Send the code" in fns["_apply_status"]
    assert "Code received by email" not in fns["_apply_controls"]
    assert "st.fragment(run_every=2)(_apply_status)" in fns["_apply_controls"]


def test_a_password_the_website_emailed_can_be_typed_in_the_app():
    """MAIF, Arkéa (19 Nov 2026): the website made the password and emailed it. The box to type it is in the part that
    follows the window, and the password is kept with the account."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_apply_status")
    assert "Password from the email" in fn and "set_account" in fn and "wrong_password" in fn
    assert "Link from the email" in fn and "open_link" in fn  # or the email's link, opened in the app's window


def test_the_window_gets_the_offer_s_title_letter_and_the_user_s_answers():
    """20 Nov 2026 (the user's request: only the CV and the submit button are left): the window gets the offer's title
    ('Poste recherché'), its cover letter PDF (a letter file field), and Settings ask the answers many forms require."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name: ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert '"position"' in fns["_apply_job"] and '"letter_path": _apply_letter_pdf(uid)' in fns["_apply_job"]
    for key in ("salary_expectation", "availability", "english_level", "current_position"):
        assert f'ap["{key}"]' in src, key
