"""document_read / document_info — extraction for each format + path scoping + chunking."""

import asyncio
import zipfile

import pytest

from agent import doc_tools

MINI_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 44 >> stream
BT /F1 12 Tf 20 100 Td (Hello PDF world) Tj ET
endstream endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
xref
0 6
0000000000 65535 f 
trailer << /Root 1 0 R /Size 6 >>
startxref
0
%%EOF
"""

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _docx(path):
    doc = f'''<w:document xmlns:w="{W}"><w:body>
      <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Invoice 42</w:t></w:r></w:p>
      <w:p><w:r><w:t>Total due: </w:t></w:r><w:r><w:t>$100</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Qty</w:t></w:r></w:p></w:tc></w:tr>
             <w:tr><w:tc><w:p><w:r><w:t>Widget</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>3</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>'''
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)


def _xlsx(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{S}" xmlns:r="{RL}"><sheets><sheet name="Sales" sheetId="1" r:id="rId1"/><sheet name="Notes" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PR}"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{S}"><si><t>Region</t></si><si><t>Amount</t></si><si><t>North, "East"</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row><row r="2"><c r="A2" t="s"><v>2</v></c><c r="C2"><v>12.5</v></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>South</t></is></c><c r="B3"><v>7</v></c></row></sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData></worksheet>')


def _pptx(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ppt/slides/slide2.xml", f'<p:sld xmlns:p="x" xmlns:a="{A}"><a:p><a:r><a:t>Second slide</a:t></a:r></a:p></p:sld>')
        z.writestr("ppt/slides/slide1.xml", f'<p:sld xmlns:p="x" xmlns:a="{A}"><a:p><a:r><a:t>Title</a:t></a:r><a:r><a:t> here</a:t></a:r></a:p><a:p><a:r><a:t>Bullet</a:t></a:r></a:p></p:sld>')


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "work"; root.mkdir()
    (root / "a.pdf").write_bytes(MINI_PDF)
    _docx(root / "b.docx"); _xlsx(root / "c.xlsx"); _pptx(root / "d.pptx")
    (root / "e.csv").write_text("name,qty\nbolt,4\nnut,9\n")
    (root / "f.md").write_text("# Notes\nplain text here\n")
    (root / "g.exe").write_bytes(b"MZ")
    media = tmp_path / "channels" / "acct" / "media"; media.mkdir(parents=True)
    (media / "sent.csv").write_text("x,y\n1,2\n")
    outside = tmp_path / "secret.txt"; outside.write_text("nope")
    return {"root": root, "media": media, "outside": outside, "tools": doc_tools.make_doc_tools([root, tmp_path / "channels"])}


def _read(tools, **kw):
    return asyncio.run(tools["document_read"]["fn"](**kw))


def test_each_format_extracts_text(docs):
    t = docs["tools"]
    assert "Hello PDF world" in _read(t, path="a.pdf") and "page 1" in _read(t, path="a.pdf")
    out = _read(t, path="b.docx")
    assert "# Invoice 42" in out and "Total due: $100" in out and "Widget | 3" in out
    out = _read(t, path="c.xlsx")
    assert "=== sheet: Sales ===" in out and "Region,Amount" in out and '"North, ""East""",,12.5' in out and "South,7" in out
    assert "=== sheet: Notes ===" in out
    only = _read(t, path="c.xlsx", sheet="Notes")
    assert "Sales" not in only.split("\n", 1)[1]
    out = _read(t, path="d.pptx")
    assert out.index("Title here") < out.index("Second slide") and "Bullet" in out
    assert "bolt,4" in _read(t, path="e.csv")
    assert "plain text here" in _read(t, path="f.md")
    assert _read(t, path="g.exe").startswith("error: unsupported file type .exe")


def test_scoping_allows_media_root_and_refuses_outside(docs):
    t = docs["tools"]
    assert "1,2" in _read(t, path=str(docs["media"] / "sent.csv"))
    assert _read(t, path=str(docs["outside"])).startswith("error:")
    assert _read(t, path="../secret.txt").startswith("error:")
    assert _read(t, path="missing.pdf").startswith("error:")


def test_chunking_and_offset(docs):
    big = docs["root"] / "big.txt"; big.write_text("x" * 3000)
    t = docs["tools"]
    first = _read(t, path="big.txt", max_chars=1000)
    assert "chars 0-1000 of 3000" in first and "offset=1000" in first
    second = _read(t, path="big.txt", max_chars=1000, offset=2000)
    assert "chars 2000-3000 of 3000" in second and "truncated" not in second


def test_document_info(docs):
    info = asyncio.run(docs["tools"]["document_info"]["fn"](path="c.xlsx"))
    assert "type: xlsx" in info and "sheets: ['Sales', 'Notes']" in info and "supported: True" in info
    info = asyncio.run(docs["tools"]["document_info"]["fn"](path="a.pdf"))
    assert "pages: 1" in info
