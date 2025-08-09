import os
import asyncio
import re
from datetime import datetime
import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright

MAX_LISTINGS = 100

def estimate_resale_price(title, price):
    if any(k in title.lower() for k in ["sofa", "couch", "armchair"]):
        factor = 1.8
    elif any(k in title.lower() for k in ["iphone", "phone", "macbook", "laptop"]):
        factor = 1.6
    else:
        factor = 1.4
    return int(price * factor)

async def _dismiss_cookies(page):
    selectors = [
        'button:has-text("Accept All")',
        'button:has-text("Accept all")',
        'button:has-text("Accept Cookies")',
        'button:has-text("Accept cookies")',
        'button:has-text("Agree")',
    ]
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(500)
                break
        except Exception:
            continue

async def _scroll_until_end(page, container_selector, limit):
    previous = 0
    while True:
        try:
            load_more = await page.query_selector('button:has-text("Load more")')
            if load_more:
                await load_more.click()
                await page.wait_for_timeout(1200)
        except Exception:
            pass
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)
        listings = await page.query_selector_all(container_selector)
        if len(listings) >= limit or len(listings) == previous:
            break
        previous = len(listings)

async def scrape_gumtree_headless(keywords, location, limit=10, headless=True, debug=False):
    results = []
    async with async_playwright() as p:
        chromium_path = os.environ.get("CHROMIUM_PATH")
        possible_paths = ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]
        if not chromium_path:
            for path in possible_paths:
                if os.path.exists(path):
                    chromium_path = path
                    break

        launch_options = {
            "headless": headless,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process", "--disable-setuid-sandbox"]
        }
        if chromium_path and os.path.exists(chromium_path):
            launch_options["executable_path"] = chromium_path

        browser = await p.chromium.launch(**launch_options)
        context = await browser.new_context()
        page = await context.new_page()

        url_loc = location.lower().replace(" ", "-")
        url = f"https://www.gumtree.com/search?search_category=for-sale&search_location={url_loc}"
        await page.goto(url, wait_until="domcontentloaded")
        await _dismiss_cookies(page)

        container_candidates = [
            "a[data-testid='listing-link']",
            "a.listing-link",
            "article[data-q='search-result']",
            "li[data-q='search-result']",
            "a[href*='/p/']",
        ]
        title_candidates = [
            "h2[data-testid='listing-title']",
            "h2.listing-title",
            "span.listing-title",
            "h2[data-q='tile-title']",
            "h2",
        ]
        price_candidates = [
            "span[data-testid='listing-price']",
            "div[data-testid='listing-price']",
            "span.listing-price",
            "strong[data-q='tile-price']",
            "span[class*=price]",
        ]
        link_candidates = [
            ":scope",
            "a[data-testid='listing-link']",
            "a.listing-link",
            "a[data-q='search-result-anchor']",
            "a[href*='/p/']",
        ]

        container_selector = ",".join(container_candidates)
        await _scroll_until_end(page, container_selector, limit)
        listings = await page.query_selector_all(container_selector)

        debug_records = []
        count = 0
        for listing in listings:
            if count >= limit:
                break
            title = price_text = link = None
            for sel in title_candidates:
                try:
                    el = await listing.query_selector(sel)
                    if el:
                        title = (await el.inner_text()).strip()
                        break
                except Exception:
                    continue
            for sel in price_candidates:
                try:
                    el = await listing.query_selector(sel)
                    if el:
                        price_text = await el.inner_text()
                        break
                except Exception:
                    continue
            link = await listing.get_attribute("href")
            if not link:
                for sel in link_candidates:
                    try:
                        el = await listing.query_selector(sel)
                        if el:
                            link = await el.get_attribute("href")
                            if link:
                                break
                    except Exception:
                        continue
            if not (title and price_text and link):
                continue
            if link.startswith("/"):
                link = "https://www.gumtree.com" + link

            nums = re.sub(r"[^0-9]", "", price_text)
            if not nums:
                continue
            price = int(nums)

            debug_records.append((title, link))

            resale = estimate_resale_price(title, price)
            margin = (resale - price) / max(price, 1)
            if any(k.lower() in title.lower() for k in keywords):
                results.append({
                    "title": title,
                    "price": price,
                    "resale_est": resale,
                    "margin": round(margin, 2),
                    "link": link,
                    "scraped_at": datetime.utcnow().isoformat(),
                })
                count += 1

        if debug:
            st.write(f"Found {len(listings)} listings before filtering")
            for t, l in debug_records[:5]:
                st.write(f"{t} -> {l}")
            if not listings:
                try:
                    first = await page.query_selector(container_candidates[0])
                    snippet = await first.evaluate('el => el.outerHTML') if first else await page.content()
                    st.write(snippet[:1000])
                except Exception:
                    pass
                try:
                    hrefs = await page.eval_on_selector_all('a', 'els => els.map(e => e.href).slice(0,10)')
                    st.write(hrefs)
                except Exception:
                    pass

        await browser.close()
    return results

def score_listing(item, min_margin=0.25):
    item["viable"] = item.get("margin", 0) >= min_margin
    return item

st.set_page_config(page_title="Gumtree Flip Bot", layout="wide")
st.title("🛍️ Gumtree Flip Bot — Streamlit Prototype")

with st.sidebar:
    st.header("Settings")
    keywords = st.text_input("Search keywords (comma separated)", value="bike,iPhone,sofa")
    min_margin = st.slider("Min profit margin", 0.05, 1.0, 0.25)
    target_loc = st.text_input("Target location", value="London")
    debug_mode = st.checkbox("Debug mode")

if st.button("Run Scraper"):
    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    with st.spinner("Scraping Gumtree..."):
        items = asyncio.run(scrape_gumtree_headless(kws, target_loc, limit=MAX_LISTINGS, headless=True, debug=debug_mode))
        scored = [score_listing(it, min_margin) for it in items]
        if not scored:
            st.warning("No items found — try looser keywords or increase MAX_LISTINGS.")
        else:
            df = pd.DataFrame(scored)
            st.dataframe(df)
            st.session_state["last_scrape"] = df
