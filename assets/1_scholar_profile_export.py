import asyncio
import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from urllib.parse import quote_plus
import difflib
import os
import hashlib
from pathlib import Path

import fitz  # PyMuPDF


@dataclass
class Article:
    title: str
    precise_date: str
    venue: str
    year: str
    url: str
    bibtex: str
    pdf_url: str = ""
    first_page_png: str = ""

def safe_filename(text: str, max_len: int = 120) -> str:
    text = text.strip()
    text = re.sub(r"[^\w\s\-().]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._-")

    if not text:
        text = "article"

    return text[:max_len]


def stable_article_id(title: str, url: str = "") -> str:
    raw = f"{title}|{url}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:10]


def make_png_path(preview_dir: str, title: str, url: str = "") -> str:
    Path(preview_dir).mkdir(parents=True, exist_ok=True)

    article_id = stable_article_id(title, url)
    name = safe_filename(title)

    return str(Path(preview_dir) / f"{name}_{article_id}_page1.png")

async def download_pdf_with_context(context, pdf_url: str, output_pdf_path: str) -> bool:
    """
    Downloads a PDF through Playwright's browser context, preserving cookies/session.

    Returns True only if the result looks like a real PDF.
    """
    try:
        response = await context.request.get(
            pdf_url,
            timeout=60000,
            headers={
                "Accept": "application/pdf,text/html,*/*",
            },
        )

        if not response.ok:
            print(f"  PDF: download failed with HTTP {response.status}")
            return False

        data = await response.body()

        if not data.startswith(b"%PDF"):
            content_type = response.headers.get("content-type", "")
            print(f"  PDF: downloaded content is not a PDF, content-type={content_type}")
            return False

        with open(output_pdf_path, "wb") as f:
            f.write(data)

        return True

    except Exception as e:
        print(f"  PDF: download error: {e}")
        return False


def render_first_pdf_page_to_png(pdf_path: str, png_path: str, zoom: float = 2.0) -> bool:
    """
    Renders the first PDF page to PNG using PyMuPDF.
    zoom=2.0 gives a decent high-resolution preview.
    """
    try:
        doc = fitz.open(pdf_path)

        if len(doc) == 0:
            print("  PDF: empty PDF")
            doc.close()
            return False

        page = doc[0]
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)

        pix.save(png_path)
        doc.close()

        return True

    except Exception as e:
        print(f"  PDF: render error: {e}")
        return False

async def get_pdf_url_from_search_result(result, base_url: str) -> str:
    """
    Tries to find the visible [PDF] link on the right side of a Scholar result.
    """
    candidates = result.locator("div.gs_or_ggsm a")

    for i in range(await candidates.count()):
        link = candidates.nth(i)
        text = ""
        href = ""

        try:
            text = (await link.inner_text()).strip()
            href = await link.get_attribute("href") or ""
        except Exception:
            continue

        if not href:
            continue

        lower_text = text.lower()
        lower_href = href.lower()

        if "pdf" in lower_text or ".pdf" in lower_href:
            return urljoin(base_url, href)

    # Fallback: scan all links inside the result.
    all_links = result.locator("a")

    for i in range(await all_links.count()):
        link = all_links.nth(i)

        try:
            href = await link.get_attribute("href") or ""
        except Exception:
            continue

        if ".pdf" in href.lower():
            return urljoin(base_url, href)

    return ""

async def save_first_page_preview_from_pdf_url(
    context,
    pdf_url: str,
    title: str,
    article_url: str,
    preview_dir: str,
) -> str:
    """
    Downloads the PDF and saves a first-page PNG preview.

    Returns the PNG path, or "" if unavailable.
    """
    if not pdf_url:
        return ""

    png_path = make_png_path(preview_dir, title, article_url)

    if os.path.exists(png_path) and os.path.getsize(png_path) > 0:
        print(f"  PDF: preview already exists: {png_path}")
        return png_path

    tmp_pdf_path = str(Path(preview_dir) / f"__tmp_{stable_article_id(title, article_url)}.pdf")

    print(f"  PDF: downloading {pdf_url}")

    ok = await download_pdf_with_context(context, pdf_url, tmp_pdf_path)

    if not ok:
        try:
            if os.path.exists(tmp_pdf_path):
                os.remove(tmp_pdf_path)
        except Exception:
            pass
        return ""

    print(f"  PDF: rendering first page to {png_path}")

    rendered = render_first_pdf_page_to_png(tmp_pdf_path, png_path)

    try:
        if os.path.exists(tmp_pdf_path):
            os.remove(tmp_pdf_path)
    except Exception:
        pass

    if rendered:
        return png_path

    return ""

def add_query_params(url: str, **params: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query.update({k: v for k, v in params.items() if v is not None})
    return urlunparse(parsed._replace(query=urlencode(query)))


def parse_sort_date(date_text: str, fallback_year: str = "") -> datetime:
    """
    Google Scholar dates can be:
    - 2024/7/15
    - 2024/7
    - 2024
    - July 2024
    - 2024-07-15
    """
    candidates = [
        "%Y/%m/%d",
        "%Y/%m",
        "%Y",
        "%B %Y",
        "%b %Y",
        "%Y-%m-%d",
    ]

    text = (date_text or "").strip()
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    year_match = re.search(r"\b(19|20)\d{2}\b", text or fallback_year)
    if year_match:
        return datetime(int(year_match.group(0)), 1, 1)

    return datetime(1, 1, 1)


def choose_venue(fields: dict) -> str:
    """
    Scholar article pages expose metadata as field/value rows.
    Venue can appear under different names depending on the paper.
    """
    preferred_keys = [
        "Journal",
        "Conference",
        "Book",
        "Source",
        "Proceedings",
        "Publication",
        "Publisher",
    ]

    for key in preferred_keys:
        if key in fields and fields[key].strip():
            return fields[key].strip()

    return ""


async def detect_block_or_captcha(page) -> bool:
    """
    Returns True if Scholar seems blocked.
    Does not raise immediately, so the caller can allow manual solving.
    """
    url = page.url.lower()

    try:
        body_text = (await page.locator("body").inner_text(timeout=5000)).lower()
    except Exception:
        body_text = ""

    blocked_markers = [
        "our systems have detected unusual traffic",
        "to continue, please type the characters",
        "please show you're not a robot",
        "recaptcha",
        "captcha",
    ]

    return "google.com/sorry" in url or any(marker in body_text for marker in blocked_markers)

async def wait_for_manual_unblock(page) -> None:
    print("\nGoogle Scholar is showing a CAPTCHA / anti-bot page.")
    print("Please solve it manually in the opened browser window.")
    print("After the Scholar profile page is visible, press ENTER here to continue.")
    input()

    await page.wait_for_load_state("domcontentloaded")

    if await detect_block_or_captcha(page):
        raise RuntimeError(
            "Scholar still appears blocked after manual intervention. "
            "Try again later, increase delay_seconds, or use your normal Chrome profile."
        )

async def expand_all_publications(page, delay_seconds: float) -> None:
    """
    Clicks 'Show more' until all publications are visible.
    """
    while True:
        more = page.locator("#gsc_bpf_more")

        if await more.count() == 0:
            return

        try:
            disabled = await more.get_attribute("disabled")
            aria_disabled = await more.get_attribute("aria-disabled")
            class_name = await more.get_attribute("class") or ""

            if disabled is not None or aria_disabled == "true" or "disabled" in class_name:
                return

            await more.click()
            await page.wait_for_timeout(int(delay_seconds * 1000))
        except PlaywrightTimeoutError:
            return
        except Exception:
            return


async def collect_article_links(page, base_url: str) -> list[dict]:
    """
    Collect article titles, years, and detail URLs from the profile table.
    """
    rows = page.locator("tr.gsc_a_tr")
    total = await rows.count()

    articles = []

    for i in range(total):
        row = rows.nth(i)
        title_link = row.locator("a.gsc_a_at")

        if await title_link.count() == 0:
            continue

        title = (await title_link.inner_text()).strip()
        href = await title_link.get_attribute("href")
        year = ""

        year_locator = row.locator(".gsc_a_y span")
        if await year_locator.count() > 0:
            year = (await year_locator.inner_text()).strip()

        if not href:
            continue

        article_url = urljoin(base_url, href)
        articles.append(
            {
                "title_from_profile": title,
                "year_from_profile": year,
                "url": article_url,
            }
        )

    return articles


async def extract_fields_from_article_page(page) -> dict:
    """
    Extract fields from a Google Scholar citation details page.

    Typical selectors:
    - .gsc_oci_field
    - .gsc_oci_value
    """
    fields = {}

    field_nodes = page.locator(".gsc_oci_field")
    value_nodes = page.locator(".gsc_oci_value")

    n = min(await field_nodes.count(), await value_nodes.count())

    for i in range(n):
        key = (await field_nodes.nth(i).inner_text()).strip()
        value = (await value_nodes.nth(i).inner_text()).strip()
        if key:
            fields[key] = value

    return fields


async def extract_title_from_article_page(page, fallback: str) -> str:
    title_candidates = [
        "#gsc_oci_title",
        "#gsc_oci_title a",
        "div#gsc_oci_title",
    ]

    for selector in title_candidates:
        locator = page.locator(selector)
        if await locator.count() > 0:
            text = (await locator.first.inner_text()).strip()
            if text:
                return text

    return fallback


async def extract_bibtex_and_pdf_by_scholar_search(
    context,
    title: str,
    delay_seconds: float,
) -> tuple[str, str]:
    """
    Searches exact title on Scholar, finds matching result, extracts:
    - BibTeX from Cite -> BibTeX
    - PDF URL from the [PDF] link on the result
    """
    search_url = scholar_search_url_for_title(title)
    page = await context.new_page()

    try:
        print(f"  Scholar search: {title}")

        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(int(delay_seconds * 1000))

        if await detect_block_or_captcha(page):
            await wait_for_manual_unblock(page, search_url)

        results = page.locator(".gs_r.gs_or.gs_scl")
        n = await results.count()

        if n == 0:
            print("  Scholar search: no results found")
            return "", ""

        best_idx = -1
        best_score = 0.0

        for i in range(min(n, 5)):
            result = results.nth(i)
            result_title = await get_search_result_title(result)
            score = title_similarity(title, result_title)

            print(f"    candidate {i + 1}: score={score:.3f} | {result_title}")

            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx < 0 or best_score < 0.65:
            print(f"  Scholar search: no close enough title match, best score={best_score:.3f}")
            return "", ""

        best_result = results.nth(best_idx)

        pdf_url = await get_pdf_url_from_search_result(best_result, page.url)

        if pdf_url:
            print(f"  PDF URL found: {pdf_url}")
        else:
            print("  PDF URL not found")

        cite_button = best_result.locator("a", has_text=re.compile(r"^Cite$", re.I))

        if await cite_button.count() == 0:
            cite_button = best_result.locator("a.gs_or_cit")

        if await cite_button.count() == 0:
            print("  BibTeX: Cite button not found")
            return "", pdf_url

        await cite_button.first.click()
        await page.wait_for_timeout(int(delay_seconds * 1000))

        try:
            await page.locator("#gs_cit").wait_for(timeout=15000)
        except Exception:
            pass

        bibtex_link = page.locator("#gs_cit a", has_text=re.compile(r"BibTeX", re.I))

        if await bibtex_link.count() == 0:
            bibtex_link = page.locator("a", has_text=re.compile(r"BibTeX", re.I))

        if await bibtex_link.count() == 0:
            print("  BibTeX: BibTeX link not found")
            return "", pdf_url

        href = await bibtex_link.first.get_attribute("href")

        if not href:
            print("  BibTeX: BibTeX link has no href")
            return "", pdf_url

        bib_url = urljoin(page.url, href)

        bib_page = await context.new_page()

        try:
            await bib_page.goto(bib_url, wait_until="domcontentloaded", timeout=60000)
            await bib_page.wait_for_timeout(int(delay_seconds * 1000))

            if await detect_block_or_captcha(bib_page):
                await wait_for_manual_unblock(bib_page, bib_url)

            pre = bib_page.locator("pre")

            if await pre.count() > 0:
                text = (await pre.first.inner_text()).strip()
            else:
                text = (await bib_page.locator("body").inner_text()).strip()

            if text.startswith("@"):
                return text, pdf_url

            at_pos = text.find("@")
            if at_pos >= 0:
                return text[at_pos:].strip(), pdf_url

            print("  BibTeX: BibTeX page opened but no BibTeX entry was found")
            return text, pdf_url

        finally:
            await bib_page.close()

    except Exception as e:
        print(f"  Scholar search/BibTeX/PDF ERROR: {e}")
        return "", ""

    finally:
        await page.close()

async def scrape_article(context, article_info: dict, delay_seconds: float, preview_dir: str) -> Article:
    page = await context.new_page()

    try:
        await page.goto(article_info["url"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(int(delay_seconds * 1000))
        if await detect_block_or_captcha(page):
            await wait_for_manual_unblock(page)

        title = await extract_title_from_article_page(
            page,
            fallback=article_info["title_from_profile"],
        )

        fields = await extract_fields_from_article_page(page)

        precise_date = (
            fields.get("Publication date")
            or fields.get("Date")
            or article_info.get("year_from_profile", "")
        )

        venue = choose_venue(fields)
        year = article_info.get("year_from_profile", "")

        if not year:
            year_match = re.search(r"\b(19|20)\d{2}\b", precise_date or "")
            if year_match:
                year = year_match.group(0)

        bibtex, pdf_url = await extract_bibtex_and_pdf_by_scholar_search(
            context=context,
            title=title,
            delay_seconds=delay_seconds,
        )

        first_page_png = ""

        if pdf_url:
            first_page_png = await save_first_page_preview_from_pdf_url(
                context=context,
                pdf_url=pdf_url,
                title=title,
                article_url=article_info["url"],
                preview_dir=preview_dir,
            )

        return Article(
            title=title,
            precise_date=precise_date,
            venue=venue,
            year=year,
            url=article_info["url"],
            bibtex=bibtex,
            pdf_url=pdf_url,
            first_page_png=first_page_png,
        )

    finally:
        await page.close()

def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def load_existing_results(output_path: str) -> list[dict]:
    if not output_path:
        return []

    try:
        with open(output_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return data

        if isinstance(data, dict) and "articles" in data:
            return data["articles"]

        return []

    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        print(f"WARNING: Existing output file is not valid JSON: {output_path}")
        return []


def save_results(output_path: str, results: list[Article]) -> None:
    if not output_path:
        return

    serializable = [asdict(a) if isinstance(a, Article) else a for a in results]

    serializable.sort(
        key=lambda a: parse_sort_date(
            a.get("precise_date", ""),
            a.get("year", ""),
        ),
        reverse=True,
    )

    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(serializable),
        "articles": serializable,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(serializable)} articles to {output_path}")


def build_existing_index(existing_results: list[dict]) -> dict[str, dict]:
    """
    Index previous output by article URL and normalized title.
    URL is preferred, title is fallback.
    """
    index = {}

    for item in existing_results:
        url = item.get("url", "").strip()
        title = normalize_title(item.get("title", ""))

        if url:
            index[f"url:{url}"] = item

        if title:
            index[f"title:{title}"] = item

    return index


def article_already_done(article_info: dict, existing_index: dict[str, dict]) -> Optional[dict]:
    url = article_info.get("url", "").strip()
    title = normalize_title(article_info.get("title_from_profile", ""))

    candidate = None

    if url and f"url:{url}" in existing_index:
        candidate = existing_index[f"url:{url}"]
    elif title and f"title:{title}" in existing_index:
        candidate = existing_index[f"title:{title}"]

    if candidate is None:
        return None

    bibtex = candidate.get("bibtex", "").strip()
    first_page_png = candidate.get("first_page_png", "").strip()

    has_png = first_page_png and os.path.exists(first_page_png) and os.path.getsize(first_page_png) > 0

    # Skip only if both BibTeX and the first-page PNG exist.
    if bibtex and has_png:
        return candidate

    return None

def upsert_article(results: list[Article], new_article: Article) -> list[Article]:
    new_url = new_article.url.strip()
    new_title = normalize_title(new_article.title)

    for i, old in enumerate(results):
        old_url = old.url.strip()
        old_title = normalize_title(old.title)

        same_url = new_url and old_url and new_url == old_url
        same_title = new_title and old_title and new_title == old_title

        if same_url or same_title:
            results[i] = new_article
            return results

    results.append(new_article)
    return results

def scholar_search_url_for_title(title: str) -> str:
    query = quote_plus(f'"{title}"')
    return f"https://scholar.google.com/scholar?hl=en&q={query}"


def title_similarity(a: str, b: str) -> float:
    a = normalize_title(a)
    b = normalize_title(b)
    return difflib.SequenceMatcher(None, a, b).ratio()


async def get_search_result_title(result) -> str:
    title_locator = result.locator("h3.gs_rt")
    if await title_locator.count() == 0:
        return ""

    text = (await title_locator.first.inner_text()).strip()

    # Scholar sometimes prefixes with [PDF], [HTML], [CITATION], etc.
    text = re.sub(r"^\[[^\]]+\]\s*", "", text).strip()
    return text


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scholar_profile_export.py scholar_config.json")
        sys.exit(1)

    config_path = sys.argv[1]

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    pdf_preview_dir = config.get("pdf_preview_dir", "paper_first_pages")
    Path(pdf_preview_dir).mkdir(parents=True, exist_ok=True)    

    output_json = config.get("output_json", "scholar_articles_output.json")

    existing_raw = load_existing_results(output_json)
    existing_index = build_existing_index(existing_raw)

    results = []
    for item in existing_raw:
        results.append(
            Article(
                title=item.get("title", ""),
                precise_date=item.get("precise_date", ""),
                venue=item.get("venue", ""),
                year=item.get("year", ""),
                url=item.get("url", ""),
                bibtex=item.get("bibtex", ""),
                pdf_url=item.get("pdf_url", ""),
                first_page_png=item.get("first_page_png", ""),
            )
        )

    print(f"Loaded {len(results)} existing articles from {output_json}")

    scholar_url = config["scholar_url"]
    delay_seconds = float(config.get("delay_seconds", 2.0))

    # Force English labels so fields like "Publication date" and "Journal" are predictable.
    scholar_url = add_query_params(scholar_url, hl="en")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=r"C:\Code\papers_renderer\chrome_scholar_profile",
            channel="chrome",  # use installed Google Chrome instead of Playwright Chromium
            headless=False,
            slow_mo=800,
            viewport={"width": 1400, "height": 1000},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        print(f"Opening profile: {scholar_url}")
        await page.goto(scholar_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(int(delay_seconds * 1000))
        if await detect_block_or_captcha(page):
            await wait_for_manual_unblock(page)

        print("Expanding all publications...")
        await expand_all_publications(page, delay_seconds)
        if await detect_block_or_captcha(page):
            await wait_for_manual_unblock(page)

        article_links = await collect_article_links(page, scholar_url)
        print(f"Found {len(article_links)} publications.\n")

        for idx, article_info in enumerate(article_links, start=1):
            title = article_info["title_from_profile"]

            existing_item = article_already_done(article_info, existing_index)

            if existing_item is not None:
                print(f"[{idx}/{len(article_links)}] SKIP already done: {title}")
                continue

            print(f"[{idx}/{len(article_links)}] Reading/repairing: {title}")

            try:
                article = await scrape_article(
                    context=context,
                    article_info=article_info,
                    delay_seconds=delay_seconds,
                    preview_dir=pdf_preview_dir,
                )

                results = upsert_article(results, article)

                existing_index[f"url:{article.url}"] = asdict(article)
                existing_index[f"title:{normalize_title(article.title)}"] = asdict(article)

                save_results(output_json, results)

            except Exception as e:
                print(f"  ERROR: {e}")
                save_results(output_json, results)

        await context.close()

    results.sort(
        key=lambda a: parse_sort_date(a.precise_date, a.year),
        reverse=True,
    )

    save_results(output_json, results)

    print("\n" + "=" * 100)
    print("PUBLICATIONS FROM MOST RECENT TO OLDEST")
    print("=" * 100)

    for i, article in enumerate(results, start=1):
        print(f"\n{i}. {article.title}")
        print(f"Date:  {article.precise_date}")
        print(f"Venue: {article.venue}")
        print(f"URL:   {article.url}")

        if article.bibtex:
            print("BibTeX:")
            print(article.bibtex)
        else:
            print("BibTeX: not available on the Scholar article page")

    # Also print machine-readable JSON at the end.
    print("\n" + "=" * 100)
    print("JSON OUTPUT")
    print("=" * 100)
    print(json.dumps([asdict(a) for a in results], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())