import argparse
import asyncio
import hashlib
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import fitz  # PyMuPDF
from playwright.async_api import async_playwright


# ------------------------------------------------------------
# Filename / JSON helpers
# ------------------------------------------------------------

def safe_filename(text: str, max_len: int = 120) -> str:
    text = text.strip()
    text = re.sub(r"[^\w\s\-().]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._-")
    return (text or "article")[:max_len]


def stable_article_id(title: str, url: str = "") -> str:
    raw = f"{title}|{url}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:10]


def make_png_path(preview_dir: Path, title: str, article_url: str = "") -> Path:
    article_id = stable_article_id(title, article_url)
    name = safe_filename(title)
    return preview_dir / f"{name}_{article_id}_page1.png"


def looks_like_valid_url(url: str) -> bool:
    if not url:
        return False

    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and isinstance(data.get("articles"), list):
        return data, data["articles"]

    if isinstance(data, list):
        wrapper = {
            "updated_at": "",
            "count": len(data),
            "articles": data,
        }
        return wrapper, data

    raise ValueError("Input JSON must be a list or a dict containing an 'articles' list.")


def save_json_atomic(path: Path, data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["count"] = len(data.get("articles", []))

    tmp_path = path.with_suffix(path.suffix + ".tmp")

    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    tmp_path.replace(path)


# ------------------------------------------------------------
# PDF rendering
# ------------------------------------------------------------

def render_first_page(pdf_path: Path, png_path: Path, zoom: float = 2.0) -> bool:
    try:
        doc = fitz.open(pdf_path)

        if len(doc) == 0:
            print("    empty PDF")
            doc.close()
            return False

        page = doc[0]
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)

        png_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(png_path))

        doc.close()

        return png_path.exists() and png_path.stat().st_size > 0

    except Exception as e:
        print(f"    render error: {e}")
        return False


def needs_png(article: dict) -> bool:
    first_page_png = article.get("first_page_png", "").strip()
    pdf_url = article.get("pdf_url", "").strip()

    if not looks_like_valid_url(pdf_url):
        return False

    if not first_page_png:
        return True

    png_path = Path(first_page_png)

    if not png_path.exists():
        return True

    if png_path.stat().st_size == 0:
        return True

    return False


# ------------------------------------------------------------
# Browser download helpers
# ------------------------------------------------------------

async def browser_download_pdf(
    context,
    pdf_url: str,
    output_pdf_path: Path,
    referer: str = "",
) -> bool:
    """
    First attempt: download via Playwright's browser context request.
    This reuses the connected Chrome context cookies/session.
    """

    headers = {
        "Accept": "application/pdf,text/html,*/*",
    }

    if referer:
        headers["Referer"] = referer

    try:
        response = await context.request.get(
            pdf_url,
            headers=headers,
            timeout=90_000,
            max_redirects=10,
        )

        if not response.ok:
            print(f"    browser request failed: HTTP {response.status}")
            return await browser_navigation_download_pdf(
                context=context,
                pdf_url=pdf_url,
                output_pdf_path=output_pdf_path,
            )

        data = await response.body()

        if not data.startswith(b"%PDF"):
            content_type = response.headers.get("content-type", "")
            print(f"    browser request did not return PDF, content-type={content_type}")

            return await browser_navigation_download_pdf(
                context=context,
                pdf_url=pdf_url,
                output_pdf_path=output_pdf_path,
            )

        output_pdf_path.write_bytes(data)

        return output_pdf_path.exists() and output_pdf_path.stat().st_size > 0

    except Exception as e:
        print(f"    browser request error: {e}")

        return await browser_navigation_download_pdf(
            context=context,
            pdf_url=pdf_url,
            output_pdf_path=output_pdf_path,
        )


async def click_chrome_pdf_viewer_download_button(page) -> bool:
    """
    Tries to click the Download button in Chrome's built-in PDF viewer.
    Chrome's PDF viewer uses shadow DOM, so we try CSS and JS fallbacks.
    """

    selectors = [
        "pdf-viewer #download",
        "pdf-viewer cr-icon-button#download",
        "pdf-viewer viewer-toolbar #download",
        "pdf-viewer viewer-download-controls #download",
        "viewer-toolbar #download",
        "cr-icon-button#download",
        "#download",
        "[aria-label='Download']",
        "[title='Download']",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0:
                await locator.first.click(timeout=3000)
                return True
        except Exception:
            pass

    js = """
    () => {
        function walkShadow(node, out) {
            if (!node) return;

            out.push(node);

            if (node.shadowRoot) {
                walkShadow(node.shadowRoot, out);
            }

            const children = node.querySelectorAll ? node.querySelectorAll("*") : [];

            for (const child of children) {
                walkShadow(child, out);
            }
        }

        const roots = [];
        walkShadow(document, roots);

        for (const root of roots) {
            if (!root.querySelector) continue;

            const candidates = [
                root.querySelector("#download"),
                root.querySelector("cr-icon-button#download"),
                root.querySelector("[aria-label='Download']"),
                root.querySelector("[title='Download']"),
                root.querySelector("[aria-label='Scarica']"),
                root.querySelector("[title='Scarica']")
            ];

            for (const el of candidates) {
                if (el) {
                    el.click();
                    return true;
                }
            }
        }

        return false;
    }
    """

    try:
        return bool(await page.evaluate(js))
    except Exception:
        return False


async def download_from_visible_pdf_viewer(
    context,
    page,
    output_pdf_path: Path,
    tmp_download_dir: Path,
) -> bool:
    """
    Final fallback.

    Assumes the PDF is visible in Chrome's PDF viewer.
    It tries to click Chrome's PDF-viewer Download button and save the file
    into tmp_download_dir. If automatic clicking fails, it lets you manually
    click Download, then scans the temp folder for a PDF.
    """

    tmp_download_dir.mkdir(parents=True, exist_ok=True)

    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send(
            "Page.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(tmp_download_dir.resolve()),
            },
        )
    except Exception as e:
        print(f"    could not set Chrome download folder through CDP: {e}")

    def find_downloaded_pdf() -> Path | None:
        candidates = []

        for p in tmp_download_dir.glob("*"):
            if not p.is_file():
                continue

            if p.suffix.lower() != ".pdf":
                continue

            if p.stat().st_size <= 0:
                continue

            candidates.append(p)

        if not candidates:
            return None

        candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        return candidates[0]

    print("    trying Chrome PDF viewer download-button fallback...")

    try:
        async with page.expect_download(timeout=8000) as download_info:
            clicked = await click_chrome_pdf_viewer_download_button(page)

            if not clicked:
                raise RuntimeError("Could not click PDF viewer download button automatically.")

        download = await download_info.value
        await download.save_as(str(output_pdf_path))

        if output_pdf_path.exists() and output_pdf_path.stat().st_size > 0:
            if output_pdf_path.read_bytes()[:4] == b"%PDF":
                print("    downloaded PDF through Chrome PDF viewer")
                return True

        print("    downloaded file does not look like a PDF")
        return False

    except Exception as e:
        print(f"    automatic PDF-viewer download failed: {e}")

    await page.wait_for_timeout(3000)

    downloaded = find_downloaded_pdf()

    if downloaded is not None:
        shutil.copyfile(downloaded, output_pdf_path)

        if output_pdf_path.exists() and output_pdf_path.read_bytes()[:4] == b"%PDF":
            print(f"    found downloaded PDF in temp folder: {downloaded}")
            return True

    print()
    print("    Automatic download did not work.")
    print("    In the opened Chrome tab, click the PDF viewer Download button manually.")
    print("    It should download into this temporary folder:")
    print(f"      {tmp_download_dir}")
    input("    After the PDF has downloaded, press ENTER here... ")

    downloaded = find_downloaded_pdf()

    if downloaded is None:
        print("    no PDF file found in the temporary download folder")
        return False

    shutil.copyfile(downloaded, output_pdf_path)

    if output_pdf_path.exists() and output_pdf_path.stat().st_size > 0:
        if output_pdf_path.read_bytes()[:4] == b"%PDF":
            print(f"    manually downloaded PDF found: {downloaded}")
            return True

    print("    downloaded file does not look like a valid PDF")
    return False


async def browser_navigation_download_pdf(
    context,
    pdf_url: str,
    output_pdf_path: Path,
) -> bool:
    """
    Fallback chain:

    1. Open the PDF URL in a real Chrome tab.
    2. Try to read the raw response body.
    3. If there is a login/cookie/consent page, let user fix it.
    4. Reload and retry response body.
    5. If PDF is visible in Chrome's PDF viewer, use the viewer Download button.
    6. If automatic download fails, let user manually click Download.
    """

    page = await context.new_page()

    try:
        print("    trying real browser navigation fallback...")

        response = await page.goto(
            pdf_url,
            wait_until="domcontentloaded",
            timeout=90_000,
        )

        await page.wait_for_timeout(4000)

        if response is not None and response.ok:
            try:
                data = await response.body()

                if data.startswith(b"%PDF"):
                    output_pdf_path.write_bytes(data)
                    return output_pdf_path.exists() and output_pdf_path.stat().st_size > 0

                content_type = response.headers.get("content-type", "")
                print(f"    navigation did not return raw PDF bytes, content-type={content_type}")

            except Exception as e:
                print(f"    could not read navigation response body: {e}")

        elif response is not None:
            print(f"    navigation failed: HTTP {response.status}")
        else:
            print("    navigation produced no response")

        print()
        print("    Opened the page in Chrome.")
        print("    If there is a login/cookie/consent page, fix it manually.")
        print("    If the PDF becomes visible in Chrome's PDF viewer, this script can download it.")
        input("    When the PDF is visible in Chrome, press ENTER here... ")

        await page.wait_for_timeout(2000)

        try:
            response = await page.reload(
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            await page.wait_for_timeout(3000)

            if response is not None and response.ok:
                data = await response.body()

                if data.startswith(b"%PDF"):
                    output_pdf_path.write_bytes(data)
                    return output_pdf_path.exists() and output_pdf_path.stat().st_size > 0

                print("    reload still did not return raw PDF bytes")
            else:
                print("    reload did not produce a valid PDF response")

        except Exception as e:
            print(f"    reload/read retry failed: {e}")

        with tempfile.TemporaryDirectory() as tmp_download_dir:
            ok = await download_from_visible_pdf_viewer(
                context=context,
                page=page,
                output_pdf_path=output_pdf_path,
                tmp_download_dir=Path(tmp_download_dir),
            )

            if ok:
                return True

        print("    PDF viewer download fallback failed")
        return False

    except Exception as e:
        print(f"    navigation download error: {e}")
        return False

    finally:
        await page.close()


# ------------------------------------------------------------
# Article processing
# ------------------------------------------------------------

async def process_article(
    context,
    article: dict,
    preview_dir: Path,
    zoom: float,
) -> bool:
    title = article.get("title", "").strip() or "Untitled article"
    article_url = article.get("url", "").strip()
    pdf_url = article.get("pdf_url", "").strip()

    png_path = make_png_path(preview_dir, title, article_url)

    print(f"\nProcessing: {title}")
    print(f"  PDF: {pdf_url}")
    print(f"  PNG: {png_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_pdf_path = Path(tmpdir) / f"{stable_article_id(title, article_url)}.pdf"

        ok = await browser_download_pdf(
            context=context,
            pdf_url=pdf_url,
            output_pdf_path=tmp_pdf_path,
            referer=article_url,
        )

        if not ok:
            print("  result: failed to download PDF")
            return False

        ok = render_first_page(
            pdf_path=tmp_pdf_path,
            png_path=png_path,
            zoom=zoom,
        )

        if not ok:
            print("  result: failed to render first page")
            return False

    article["first_page_png"] = str(png_path)
    print("  result: OK")
    return True


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

async def async_main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "input_json",
        help="Path to scholar_articles_output.json",
    )

    parser.add_argument(
        "--preview-dir",
        default="paper_first_pages",
        help="Folder where first-page PNG previews will be saved.",
    )

    parser.add_argument(
        "--zoom",
        type=float,
        default=2.0,
        help="PDF render zoom. 2.0 is usually good quality.",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Delay in seconds between PDFs.",
    )

    parser.add_argument(
        "--cdp",
        default="http://127.0.0.1:9222",
        help="Chrome DevTools URL for your running Chrome.",
    )

    args = parser.parse_args()

    input_path = Path(args.input_json)
    preview_dir = Path(args.preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)

    data, articles = load_json(input_path)
    targets = [a for a in articles if needs_png(a)]

    print(f"Loaded {len(articles)} articles.")
    print(f"Articles missing first-page PNG but with valid pdf_url: {len(targets)}")

    if not targets:
        return

    async with async_playwright() as p:
        print(f"Connecting to Chrome at {args.cdp}")

        try:
            browser = await p.chromium.connect_over_cdp(args.cdp)
        except Exception as e:
            raise RuntimeError(
                "\nCould not connect to Chrome DevTools at http://127.0.0.1:9222.\n\n"
                "Start Chrome first with:\n\n"
                "  Stop-Process -Name chrome -Force -ErrorAction SilentlyContinue\n\n"
                '  & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `\n'
                "    --remote-debugging-port=9222 `\n"
                "    --remote-allow-origins=* `\n"
                '    --user-data-dir="C:\\Code\\papers_renderer\\chrome_debug_profile"\n\n'
                "Then verify this works:\n\n"
                "  Invoke-WebRequest http://127.0.0.1:9222/json/version\n"
            ) from e

        if not browser.contexts:
            raise RuntimeError(
                "Connected to Chrome, but no browser context was found. "
                "Make sure Chrome was started with --remote-debugging-port=9222."
            )

        context = browser.contexts[0]

        success = 0
        failed = 0

        for idx, article in enumerate(targets, start=1):
            print(f"\n[{idx}/{len(targets)}]")

            ok = await process_article(
                context=context,
                article=article,
                preview_dir=preview_dir,
                zoom=args.zoom,
            )

            if ok:
                success += 1
            else:
                failed += 1

            save_json_atomic(input_path, data)
            await asyncio.sleep(args.delay)

        print("\nDone.")
        print(f"Rendered successfully: {success}")
        print(f"Failed: {failed}")
        print(f"Updated JSON: {input_path}")
        print(f"PNG folder: {preview_dir}")

        await browser.close()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()