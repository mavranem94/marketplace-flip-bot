import os
import asyncio
import re
from datetime import datetime
import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright

MAX_LISTINGS = 100
TARGET_LOCATION = st.session_state.get("target_location", "London")
GUMTREE_URL = f"https://www.gumtree.com/search?search_category=for-sale&search_location={TARGET_LOCATION.lower()}"

def estimate_resale_price(title, price):
    if any(k in title.lower() for k in ["sofa", "couch", "armchair"]):
        factor = 1.8
    elif any(k in title.lower() for k in ["iphone", "phone", "macbook", "laptop"]):
        factor = 1.6
    else:
        factor = 1.4
    return int(price * factor)

async def scrape_gumtree_headless(keywords, limit=10, headless=True):
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

        await page.goto(GUMTREE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Scroll to load more
        for _ in range(25):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

        listings = await page.query_selector_all('article[data-q="search-result"]')
        count = 0
        for listing in listings:
            if count >= limit:
                break
            try:
                title = await listing.query_selector_eval('h2[data-q="tile-title"]', 'el => el.innerText')
                price_text = await listing.query_selector_eval('strong[data-q="tile-price"]', 'el => el.innerText')
                link = await listing.query_selector_eval('a[data-q="search-result-anchor"]', 'el => el.href')
            except:
                continue

            nums = re.sub(r"[^0-9]", "", price_text or "")
            if nums == "":
                continue
            price = int(nums)

            resale = estimate_resale_price(title, price)
            margin = (resale - price) / max(price, 1)

            if any(k.lower() in title.lower() for k in keywords):
                results.append({
                    "title": title.strip(),
                    "price": price,
                    "resale_est": resale,
                    "margin": round(margin, 2),
                    "link": link,
                    "scraped_at": datetime.utcnow().isoformat()
                })
                count += 1

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

if st.button("Run Scraper"):
    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    with st.spinner("Scraping Gumtree..."):
        items = asyncio.run(scrape_gumtree_headless(kws, limit=MAX_LISTINGS, headless=True))
        scored = [score_listing(it, min_margin) for it in items]
        if not scored:
            st.warning("No items found — try looser keywords or increase MAX_LISTINGS.")
        else:
            df = pd.DataFrame(scored)
            st.dataframe(df)
            st.session_state["last_scrape"] = df
