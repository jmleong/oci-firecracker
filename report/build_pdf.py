#!/usr/bin/env python3
"""
build_pdf.py — render the Markdown report into a styled dark-theme PDF.

Usage: build_pdf.py <report.md> <out.pdf>

Converts the Markdown (tables, image, blockquotes) to HTML, wraps it in a
dark stylesheet, colors the winner tags (green = OCI / E6 wins, amber = AWS / E5),
embeds the chart PNG inline, and renders with WeasyPrint.
"""
import sys, os, re, base64
import markdown
from weasyprint import HTML

def main():
    md_path, out_pdf = sys.argv[1], sys.argv[2]
    base = os.path.dirname(os.path.abspath(md_path))
    text = open(md_path, encoding="utf-8").read()

    # inline any local images as base64 data URIs
    def embed(m):
        alt, src = m.group(1), m.group(2)
        p = os.path.join(base, src)
        if os.path.exists(p):
            b64 = base64.b64encode(open(p, "rb").read()).decode()
            return f"![{alt}](data:image/png;base64,{b64})"
        return m.group(0)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", embed, text)

    html_body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])

    # colorize winner tags inside table cells
    html_body = re.sub(r"\((E6\.Ax|E6\.256|OCI)\)", r'<span class="win">(\1)</span>', html_body)
    html_body = re.sub(r"\((E5|AWS)\)", r'<span class="lose">(\1)</span>', html_body)

    css = """
    @page { size: A4; margin: 1.4cm; background: #0d1117; }
    html, body { background: #0d1117; color: #c9d1d9;
      font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
      font-size: 10.5px; line-height: 1.5; }
    h1 { color: #ffffff; font-size: 22px; margin: 0 0 4px; line-height: 1.2; }
    h2 { color: #ffffff; font-size: 15px; margin: 22px 0 8px;
      border-bottom: 1px solid #30363d; padding-bottom: 5px; }
    h3 { color: #e6edf3; font-size: 12.5px; margin: 16px 0 6px; }
    p { margin: 8px 0; }
    a { color: #58a6ff; text-decoration: none; }
    strong { color: #f0f6fc; }
    em { color: #8b949e; }
    blockquote { border-left: 3px solid #1f6feb; background: #161b22;
      margin: 10px 0; padding: 8px 12px; color: #adbac7; border-radius: 4px; }
    table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 8.6px; }
    th { background: #161b22; color: #8b949e; text-transform: uppercase;
      font-size: 7.6px; letter-spacing: .3px; text-align: left;
      padding: 6px 8px; border-bottom: 1px solid #30363d; }
    td { padding: 5px 8px; border-bottom: 1px solid #21262d; vertical-align: top; }
    tr:nth-child(even) td { background: #0f141b; }
    td strong { color: #58a6ff; }
    .win  { color: #3fb950; font-weight: 600; }
    .lose { color: #d29922; font-weight: 600; }
    img { max-width: 100%; display: block; margin: 12px auto;
      background: #fff; border-radius: 6px; padding: 6px; }
    code { background: #161b22; padding: 1px 4px; border-radius: 3px;
      font-family: "SF Mono", Consolas, monospace; font-size: 9px; color: #79c0ff; }
    pre { background: #161b22; padding: 10px; border-radius: 6px; overflow-x: auto; }
    """
    full = (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<style>{css}</style></head><body>{html_body}</body></html>")
    HTML(string=full, base_url=base).write_pdf(out_pdf)
    print("wrote", out_pdf)

if __name__ == "__main__":
    main()
