"""The page is served under a strict content-security policy and wired together by element ids.
Both break silently in a browser (a blocked font, a script that looks for an id that was renamed), so check them here."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "xray" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")

# Elements the script creates itself, so they are not in the HTML file.
CREATED_BY_SCRIPT = {"report-title"}
# The only web address allowed inside the files: an XML namespace, which is a name and fetches nothing.
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}


def test_every_id_the_script_looks_up_exists_in_the_page():
    wanted = set(re.findall(r"""\$\("#([\w-]+)"\)""", JS)) - CREATED_BY_SCRIPT
    present = set(re.findall(r'\bid="([\w-]+)"', HTML))
    assert not wanted - present, f"app.js looks for ids that index.html does not have: {sorted(wanted - present)}"


def test_ids_in_the_page_are_unique():
    ids = re.findall(r'\bid="([\w-]+)"', HTML)
    assert len(ids) == len(set(ids)), f"duplicate ids: {sorted({i for i in ids if ids.count(i) > 1})}"


def test_in_page_links_point_at_real_sections():
    ids = set(re.findall(r'\bid="([\w-]+)"', HTML))
    targets = set(re.findall(r'href="#([\w-]+)"', HTML))
    assert not targets - ids, f"links to sections that do not exist: {sorted(targets - ids)}"


def test_nothing_is_loaded_from_another_origin():
    # default-src 'none' in the policy means an external font, image or script is blocked without any visible error.
    for name, text in (("index.html", HTML), ("app.js", JS), ("app.css", CSS)):
        found = set(re.findall(r"https?://[^\s\"')>]+", text)) - ALLOWED_URLS
        assert not found, f"{name} refers to other origins, which the content-security policy blocks: {sorted(found)}"
    assert "@import" not in CSS and "@font-face" not in CSS


def test_no_inline_script_and_no_inline_event_handlers():
    assert not re.search(r"<script(?![^>]*\bsrc=)", HTML), "an inline <script> is blocked by script-src 'self'"
    assert not re.search(r"""\son[a-z]+\s*=\s*["']""", HTML), "inline event handlers are blocked by script-src 'self'"


def test_every_css_class_the_script_uses_has_a_style_rule():
    # A restyle can drop a class the script still puts on elements; nothing errors, the text just loses its style.
    used = {name for group in re.findall(r'class: "([^"]*)"', JS) for name in group.split()}
    missing = {name for name in used if not re.search(rf"\.{re.escape(name)}\b", CSS)}
    assert not missing, f"app.js uses classes that app.css does not style: {sorted(missing)}"
