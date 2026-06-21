#!/usr/bin/env python3
"""
Script to download PDFs from scholar_articles_output.json, rename them as year_short_title.pdf,
and add file info and summary field.
Uses Playwright to connect to Chrome for downloading restricted URLs.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import unquote
from datetime import datetime

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


def get_filename_from_url(url):
    """Extract a safe filename from URL."""
    url_decoded = unquote(url)
    path_parts = url_decoded.split('/')
    
    for part in reversed(path_parts):
        if '.pdf' in part:
            filename = re.sub(r'[^\w\-\.]', '_', part)
            if not filename.endswith('.pdf'):
                filename += '.pdf'
            return filename
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return f"paper_{timestamp}.pdf"


def sanitize_filename(filename):
    """Make filename safe for filesystem."""
    safe = re.sub(r'[<>:"/\\|?*]', '_', filename)
    if len(safe) > 200:
        name, ext = os.path.splitext(safe)
        safe = name[:200 - len(ext)] + ext
    return safe


def get_short_title(title):
    """Create a short title from the full title."""
    # Remove special characters and limit length
    short = re.sub(r'[^\w\s\-]', '', title)
    short = re.sub(r'\s+', '_', short)
    if len(short) > 50:
        short = short[:50].rstrip('_')
    return short.lower()


def is_restricted_url(url):
    """Check if URL requires authentication."""
    restricted_domains = [
        'sciencedirect.com',
        'ieeexplore.ieee.org',
        'dl.acm.org',
        'springer.com',
        'wiley.com',
        'tandfonline.com'
    ]
    url_lower = url.lower()
    return any(domain in url_lower for domain in restricted_domains)


async def download_pdf_with_playwright(context, pdf_url, output_path):
    """Download PDF using Playwright browser automation."""
    page = await context.new_page()
    
    try:
        # Set up download directory
        cdp = await context.new_cdp_session(page)
        tmp_dir = output_path.parent / '.tmp_downloads'
        tmp_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            await cdp.send(
                "Page.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(tmp_dir.resolve()),
                },
            )
        except Exception as e:
            print(f"    Could not set download directory: {e}")
        
        # Navigate to the URL
        response = await page.goto(pdf_url, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(3000)
        
        if response and response.ok:
            try:
                data = await response.body()
                if data.startswith(b"%PDF"):
                    output_path.write_bytes(data)
                    print("    Downloaded via response body")
                    return True
            except Exception as e:
                print(f"    Could not read response body: {e}")
        
        # Try to click download button if PDF is visible
        try:
            async with page.expect_download(timeout=10_000) as download_info:
                await page.wait_for_timeout(2000)
                # Try common download button selectors
                download_selectors = [
                    'button[aria-label*="download"]',
                    'a[href*=".pdf"]',
                    '.download-button',
                    '#downloadButton'
                ]
                
                for selector in download_selectors:
                    try:
                        elements = await page.query_selector_all(selector)
                        if elements:
                            await elements[0].click()
                            break
                    except Exception:
                        continue
                
                download = await download_info.value
                await download.save_as(str(output_path))
                
                if output_path.exists() and output_path.stat().st_size > 0:
                    print("    Downloaded via browser download")
                    return True
        except Exception as e:
            print(f"    Download button click failed: {e}")
        
        # Check temp directory for downloaded file
        def find_downloaded_pdf():
            candidates = []
            for p in tmp_dir.glob("*"):
                if p.is_file() and p.suffix.lower() == ".pdf" and p.stat().st_size > 0:
                    candidates.append(p)
            if not candidates:
                return None
            candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            return candidates[0]
        
        downloaded = find_downloaded_pdf()
        if downloaded:
            import shutil
            shutil.copyfile(downloaded, output_path)
            print("    Downloaded via temp folder")
            return True
        
        # Manual intervention required
        print()
        print("    Opened the page in Chrome.")
        print("    If there is a login/cookie/consent page, fix it manually.")
        print("    When the PDF is visible, click the download button.")
        input("    Press ENTER after downloading... ")
        
        downloaded = find_downloaded_pdf()
        if downloaded:
            import shutil
            shutil.copyfile(downloaded, output_path)
            return True
        
        print("    No PDF file found in temp folder")
        return False
        
    except Exception as e:
        print(f"    Browser download failed: {e}")
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


def main():
    script_dir = Path(__file__).parent.parent
    json_path = script_dir / 'assets' / 'scholar_articles_output.json'
    pdf_dir = script_dir / 'assets' / 'pdf'
    
    pdf_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Loading {json_path}...")
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    articles = data.get('articles', [])
    updated_count = 0
    
    if HAS_PLAYWRIGHT:
        import asyncio
        
        async def process_all():
            nonlocal updated_count
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                    context = browser.contexts[0]
                except Exception:
                    print("Could not connect to Chrome DevTools. Starting new browser...")
                    browser = await p.chromium.launch(headless=False)
                    context = browser.contexts[0] if browser.contexts else await browser.new_context()
                
                for i, article in enumerate(articles):
                    pdf_url = article.get('pdf_url')
                    
                    # Skip if already successfully processed
                    if article.get('pdf_downloaded') is True:
                        print(f"[{i+1}/{len(articles)}] Already downloaded: {article.get('title', 'Unknown')}")
                        continue
                    
                    if not pdf_url:
                        print(f"[{i+1}/{len(articles)}] No PDF URL found for: {article.get('title', 'Unknown')}")
                        continue
                    
                    # Generate new filename: year_short_title.pdf
                    year = article.get('year', datetime.now().strftime('%Y'))
                    short_title = get_short_title(article.get('title', 'unknown'))
                    safe_filename = sanitize_filename(f"{year}_{short_title}.pdf")
                    
                    pdf_path = pdf_dir / safe_filename
                    
                    if not pdf_path.exists():
                        print(f"[{i+1}/{len(articles)}] Downloading: {article.get('title', 'Unknown')}")
                        
                        if is_restricted_url(pdf_url):
                            print(f"  Restricted URL, using browser...")
                            result = await download_pdf_with_playwright(context, pdf_url, pdf_path)
                        else:
                            # Try direct download first
                            try:
                                import requests
                                response = requests.get(pdf_url, timeout=60)
                                if response.status_code == 200 and response.content.startswith(b"%PDF"):
                                    pdf_path.write_bytes(response.content)
                                    result = True
                                else:
                                    print(f"  Direct download failed, using browser...")
                                    result = await download_pdf_with_playwright(context, pdf_url, pdf_path)
                            except Exception as e:
                                print(f"  Download error: {e}, using browser...")
                                result = await download_pdf_with_playwright(context, pdf_url, pdf_path)
                        
                        if result:
                            print(f"  ✓ Saved to: {safe_filename}")
                            article['pdf_downloaded'] = True
                        else:
                            print(f"  ✗ Failed to download")
                            article['pdf_downloaded'] = False
                    else:
                        print(f"[{i+1}/{len(articles)}] Already exists: {safe_filename}")
                        article['pdf_downloaded'] = True
                    
                    # Add file info
                    article['pdf_file'] = {
                        'filename': safe_filename,
                        'path': str(pdf_path.relative_to(script_dir)).replace('\\', '/')
                    }
                    
                    # Add empty summary field
                    if 'summary' not in article:
                        article['summary'] = ''
                    
                    updated_count += 1
                
                await browser.close()
        
        asyncio.run(process_all())
    else:
        print("Playwright is required but not installed.")
        print("Install with: pip install playwright && playwright install chromium")
        return
    
    output_json_path = script_dir / 'assets' / 'scholar_articles_output_updated.json'
    with open(output_json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*60}")
    print(f"Processed {updated_count} articles")
    print(f"Updated JSON saved to: {output_json_path}")
    print(f"PDFs directory: {pdf_dir}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
