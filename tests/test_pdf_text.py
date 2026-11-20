"""The PDF library, on a real PDF.

Every other PDF test hands the code `b"%PDF-1.4 pretend…"` and a stubbed
extractor, so they would pass with no PDF library installed at all. These build a
small but valid PDF — one line of Helvetica per page — and read it with whatever
`services/books.py` and `services/learn.py` import. They are what says that
swapping the library (PyPDF2 → pypdf) still reads text, still finds a passage that
runs across a page break, and still names an unreadable file as one.
"""

from services import books, learn


def tiny_pdf(*pages: str) -> bytes:
    """A valid PDF with one line of text per page. No parentheses in the text."""
    count = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref)
    return bytes(out)


BOOK = tiny_pdf("I must not fear. Fear is the mind-killer.",
                "Fear is the little-death that brings total obliteration.")


def test_learn_pdf_reads_the_text_of_every_page():
    text, err = learn.extract_pdf(BOOK)

    assert err is None
    assert "Fear is the mind-killer" in text
    assert "total obliteration" in text


def test_a_quote_is_found_across_a_page_break():
    quote, err = books.extract_quote_from_pdf(BOOK, "Fear is the mind-killer",
                                              "brings total obliteration")

    assert err is None
    assert quote == ("Fear is the mind-killer. Fear is the little-death that "
                     "brings total obliteration")


def test_a_passage_that_is_not_there_is_named():
    quote, err = books.extract_quote_from_pdf(BOOK, "the spice must flow", "obliteration")

    assert quote is None
    assert "Begin text not found" in err


def test_an_unreadable_pdf_is_reported_as_one():
    """The library's own error class, caught by name: with the wrong one, this
    would fall through to the generic "PDF extraction error"."""
    quote, err = books.extract_quote_from_pdf(b"this is not a pdf", "begin", "end")

    assert quote is None
    assert err.startswith("Could not read PDF")
