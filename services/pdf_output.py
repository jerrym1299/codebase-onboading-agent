from pathlib import Path

import markdown as md
from fpdf import FPDF
from fpdf.fonts import FontFace, TextStyle


_BLACK = "#000000"

_MONOCHROME_TAG_STYLES = {
    "a": FontFace(color=_BLACK, emphasis="UNDERLINE"),
    "h1": TextStyle(color=_BLACK, b_margin=0.4, font_size_pt=24, t_margin=5 + 834 / 900),
    "h2": TextStyle(color=_BLACK, b_margin=0.4, font_size_pt=18, t_margin=5 + 453 / 900),
    "h3": TextStyle(color=_BLACK, b_margin=0.4, font_size_pt=14, t_margin=5 + 199 / 900),
    "h4": TextStyle(color=_BLACK, b_margin=0.4, font_size_pt=12, t_margin=5 + 72 / 900),
    "h5": TextStyle(color=_BLACK, b_margin=0.4, font_size_pt=10, t_margin=5 - 55 / 900),
    "h6": TextStyle(color=_BLACK, b_margin=0.4, font_size_pt=8, t_margin=5 - 182 / 900),
    "blockquote": TextStyle(color=_BLACK, t_margin=3, b_margin=3),
}


def write_pdf(text: str, output_path: str | Path) -> Path:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 8, text)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out))
    return out


def write_markdown_pdf(markdown_text: str, output_path: str | Path) -> Path:
    html = md.markdown(markdown_text, extensions=["fenced_code", "tables"])

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.set_text_color(0, 0, 0)
    pdf.write_html(html, tag_styles=_MONOCHROME_TAG_STYLES)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out))
    return out
