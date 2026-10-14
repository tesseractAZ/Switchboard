"""The manual's typefaces: pinned in the .docx, checked in the PDF build.

    python3 -m pytest switchboard/tests/test_docs_fonts.py

pandoc 3.7 (Ubuntu 26.04) changed its template to Aptos and dropped bold
headings; the first manual built there shipped plain serif headings and
proportional inline code while every check passed.
"""
import io
import re
import zipfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
build = SourceFileLoader("build_docs_docx", str(REPO / "scripts" / "build-docs-docx.py")).load_module()

# The shape pandoc 3.7 / 3.10 emits (abridged).
THEME = ('<a:theme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Aptos Display"/>'
         '</a:majorFont><a:minorFont><a:latin typeface="Aptos"/></a:minorFont></a:fontScheme></a:theme>')
STYLES = (
    '<w:styles>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:rPr><w:rFonts w:asciiTheme="majorHAnsi"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:rPr><w:rFonts w:asciiTheme="majorHAnsi"/>'
    '<w:color w:val="0F4761"/><w:sz w:val="40"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:pPr/></w:style>'
    '<w:style w:type="character" w:styleId="VerbatimChar"><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/></w:rPr></w:style>'
    '</w:styles>')


def _docx(tmp: Path, theme=THEME, styles=STYLES) -> Path:
    p = tmp / "t.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/theme/theme1.xml", theme)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/document.xml", "<w:document/>")
    return p


def _read(p: Path, name: str) -> str:
    with zipfile.ZipFile(p) as z:
        return z.read(name).decode()


def _style(xml: str, sid: str) -> str:
    return re.search(rf'w:styleId="{sid}".*?</w:style>', xml, re.S).group(0)


def test_the_theme_is_pinned_and_headings_are_bold(tmp_path):
    p = _docx(tmp_path)
    build.pin_fonts(p)
    theme = _read(p, "word/theme/theme1.xml")
    assert '<a:majorFont><a:latin typeface="Calibri"/>' in theme
    assert '<a:minorFont><a:latin typeface="Cambria"/>' in theme
    styles = _read(p, "word/styles.xml")
    for sid in ("Title", "Heading1", "Heading2"):
        assert "<w:b/>" in _style(styles, sid), sid
    assert "<w:b/>" not in _style(styles, "VerbatimChar")
    assert _read(p, "word/document.xml") == "<w:document/>"      # the rest is untouched
    build.pin_fonts(p)                                           # idempotent
    assert _style(_read(p, "word/styles.xml"), "Heading1").count("<w:b/>") == 1


def test_a_changed_template_fails_the_build_instead_of_unpinning(tmp_path):
    for theme, styles in ((THEME.replace("<a:minorFont>", "<a:otherFont>"), STYLES),
                          (THEME, STYLES.replace('w:styleId="Heading1"', 'w:styleId="Heading9x"'))):
        p = _docx(tmp_path, theme, styles)
        try:
            build.pin_fonts(p)
        except RuntimeError:
            continue
        raise AssertionError("pin_fonts accepted a template it cannot pin")


def test_the_docs_workflow_checks_the_typefaces_by_role_and_by_embedding():
    doc = yaml.safe_load((REPO / ".github" / "workflows" / "docs.yml").read_text())
    steps = {s.get("name"): s for s in doc["jobs"]["docs"]["steps"]}
    fonts = steps["Fonts for the PDF"]["run"]
    for want in ("fonts-crosextra-carlito", "fonts-crosextra-caladea", "fonts-dejavu",
                 "role Calibri Carlito", "role Cambria Caladea", 'role Consolas "DejaVu Sans Mono"',
                 "%{spacing}"):
        assert want in fonts, want
    names = [s.get("name") for s in doc["jobs"]["docs"]["steps"]]
    assert names.index("Fonts for the PDF") < names.index("Build .pdf (render the .docx)") \
        < names.index("Check the PDF's typefaces")
    check = steps["Check the PDF's typefaces"]["run"]
    for want in ('"Carlito|Carlito-Regular"', '"Carlito-Bold"', '"Caladea|Caladea-Regular"', '"DejaVuSansMono"'):
        assert want in check, want
    upload = [s for s in doc["jobs"]["docs"]["steps"] if "upload-artifact" in str(s.get("uses", ""))]
    assert upload and upload[0].get("if") == "always()"
