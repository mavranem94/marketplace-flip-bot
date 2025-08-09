# app.py — Streamlit + Playwright Gumtree Flip Bot (Cloud-ready prototype)

import os
import shutil
os.system("python -m playwright install chromium")

import asyncio
import re
from datetime import datetime
import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright

# ---------------------
# Configuration
# ---------------------
TARGET_LOCATION = st.session_state.get("target_location", "London")
MIN_PROFIT_MARGIN = st.session_state.get("min_profit_margin", 0.25)
MAX_LISTINGS = 50
GUMTREE_URL = f"https://www.gumtree.com/search?search_category=for-sale&search_location={TARGET_LOCATION}"

# ---------------------
# Helper functions
# ---------------------

def get_chromium_path():
    possible_paths = [
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome"
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return shutil.which("chromium") or shutil.which("google-chrome")

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
        chromium_path = get_chromium_path()
        launch_options = {
            "headless": headless,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--disable-setuid-sandbox"
            ]
        }
        if chromium_path:
            launch_options["executable_path"] = chromium_path

        browser = await p.chromium.launch(**launch_options)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(GUMTREE_URL, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Scroll to load more listings
        for _ in range(8):
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(1000)

        listings = await page.query_selector_all("li[data-q='search-result']")
        count = 0
        for listing in listings:
            if count >= limit:
                break
            try:
                title = await listing.query_selector_eval("a > h2", "el => el.innerText")
                price_text = await listing.query_selector_eval("strong[data-q='price']", "el => el.innerText")
                nums = re.sub(r"[^0-9]", "", price_text)
                if not nums:
                    continue
                price = int(nums)
                link = await listing.query_selector_eval("a", "el => el.href")

                resale = estimate_resale_price(title, price)
                margin = (resale - price) / max(price, 1)
                if any(k.lower() in title.lower() for k in keywords):
                    results.append({
                        "title": title,
                        "price": price,
                        "resale_est": resale,
                        "margin": round(margin, 2),
                        "link": link,
                        "scraped_at": datetime.utcnow().isoformat()
                    })
                    count += 1
            except:
                continue

        await browser.close()
    return results

def score_listing(item, min_margin=MIN_PROFIT_MARGIN):
    item["viable"] = item.get("margin", 0) >= min_margin
    return item

# ---------------------
# Streamlit UI
# ---------------------
st.set_page_config(page_title="Gumtree Flip Bot", layout="wide")
st.title("📦 Gumtree Flip Bot — Streamlit Cloud Prototype")

with st.sidebar:
    st.header("Settings")
    keywords = st.text_input("Search keywords (comma separated)", value="bike,iPhone,sofa")
    min_margin = st.slider("Min profit margin", 0.05, 1.0, 0.25)
    target_loc = st.text_input("Target location", value="London")

if st.button("Run Scraper"):
    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    st.info("Running scraper — this may take 10–20s in the cloud.")
    with st.spinner("Scraping Gumtree..."):
        items = asyncio.run(scrape_gumtree_headless(kws, limit=MAX_LISTINGS, headless=True))
        scored = [score_listing(it, min_margin) for it in items]
        if not scored:
            st.warning("No items found — try looser keywords or increase MAX_LISTINGS.")
        else:
            df = pd.DataFrame(scored)
            st.dataframe(df)
            st.session_state["last_scrape"] = df
