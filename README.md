# WebsiteBuilder / Scholar Article Renderer

This project extracts publications from a Google Scholar author profile, saves article metadata to JSON, retrieves BibTeX entries, and renders the first page of available PDFs as PNG previews.

The workflow is split into two scripts:

1. `1_scholar_profile_export.py`  
   Reads a Scholar profile, extracts articles, searches each exact title on Scholar, opens the Cite dialog, retrieves BibTeX, finds PDF links, and writes everything to `scholar_articles_output.json`.

2. `2_render_missing_first_pages.py`  
   Reads `scholar_articles_output.json`, finds entries where `first_page_png` is missing but `pdf_url` exists, downloads/opens the PDF through Chrome, and renders the first page as a PNG.

---

## Requirements

Install Python dependencies:

```powershell
pip install -r requirements.txt