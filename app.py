# app.py
# Streamlit + Playwright Marketplace Flip Bot (Cloud-ready prototype)

import os
os.system("python -m playwright install chromium")

import asyncio
import os
import re
from datetime import datetime
import pandas as pd
import streamlit as st
import openai
from playwright.async_api import async_playwright

# ---------------------
# Configuration
# ---------------------
OPENAI_KEY = st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
FB_EMAIL = st.secrets.get("FB_EMAIL") or os.getenv("FB_EMAIL")
FB_PASSWORD = st.secrets.get("FB_PASSWORD") or os.getenv("FB_PASSWORD")
TARGET_LOCATION = st.session_state.get("target_location", "London")
MIN_PROFIT_MARGIN = st.session_state.get("min_profit_margin", 0.25)
MAX_LISTINGS = 15
FACEBOOK_MARKETPLACE_URL = f"https://www.facebook.com/marketplace/{TARGET_LOCATION.lower()}"

if OPENAI_KEY is None:
    st.warning("Set your OpenAI API key in Streamlit Secrets (OPENAI_API_KEY) before using the app.")
else:
    openai.api_key = OPENAI_KEY

# ---------------------
# Helper functions
# ---------------------

def estimate_resale_price(title, price):
    """A placeholder function. Replace with calls to eBay/Amazon APIs or heuristics."""
    # naive heuristic: assume 1.5x resale for electronics, 1.8x for furniture if price < 500
    if any(k in title.lower() for k in ["sofa", "couch", "armchair"]):
        factor = 1.8
    elif any(k in title.lower() for k in ["iphone", "phone", "macbook", "laptop"]):
        factor = 1.6
    else:
        factor = 1.4
    return int(price * factor)


async def facebook_login_and_page(playwright, headless=True):
    """Launch browser, navigate to Facebook Marketplace (target location) and log in using secrets. Returns logged-in page."""
    # Attempt to use system-installed Chromium path if available
    chromium_path = os.environ.get("CHROMIUM_PATH")
    if not chromium_path:
        # Try common install locations in container environments
        possible_paths = [
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/usr/bin/google-chrome"
        ]
        for path in possible_paths:
            if os.path.exists(path):
                chromium_path = path
                break

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
    if chromium_path and os.path.exists(chromium_path):
        launch_options["executable_path"] = chromium_path
    else:
        raise RuntimeError(f"No valid Chromium executable found. Tried paths: {possible_paths} and CHROMIUM_PATH env.")

    try:
        browser = await playwright.chromium.launch(**launch_options)
    except Exception as e:
        raise RuntimeError(f"Failed to launch Chromium. Checked path: {chromium_path}. Error: {type(e).__name__}: {e}")

    context = await browser.new_context()
    page = await context.new_page()

    # Go directly to Facebook Marketplace in the target location
    target_url = f"https://www.facebook.com/marketplace/{TARGET_LOCATION.lower()}"
    await page.goto(target_url, wait_until="networkidle")
    await page.wait_for_timeout(8000)  # Give extra time for listings to load

    # If a login is required, attempt it and then reload the target Marketplace page
    try:
        if "login" in page.url.lower():
            if FB_EMAIL and FB_PASSWORD:
                await page.fill("input[name='email']", FB_EMAIL)
                await page.fill("input[name='pass']", FB_PASSWORD)
                await page.click("button[name=login]")
                await page.wait_for_timeout(5000)
                await page.goto(target_url, wait_until="networkidle")
                await page.wait_for_timeout(8000)
            else:
                st.info("No Facebook credentials found in secrets; please login manually in the opened browser window.")
    except Exception as e:
        st.error(f"Login attempt failed: {e}")

    return browser, context, page


async def scrape_marketplace_headless(keywords, limit=10, headless=True):
    """Scrapes the Marketplace listing tiles and returns a list of dicts. This is a best-effort prototype — selectors may break."""
    results = []
    async with async_playwright() as p:
        browser, context, page = await facebook_login_and_page(p, headless=headless)
        try:
            await page.goto(FACEBOOK_MARKETPLACE_URL, wait_until="networkidle")
            await page.wait_for_timeout(3000)

            # Find article tiles
            tiles = await page.query_selector_all("[role='article']")
            count = 0
            for tile in tiles:
                if count >= limit:
                    break
                try:
                    title = await tile.query_selector_eval("h2", "el=>el.innerText")
                except Exception:
                    try:
                        title = await tile.query_selector_eval("span", "el=>el.innerText")
                    except Exception:
                        continue

                # crude price extraction
                try:
                    price_text = await tile.query_selector_eval("[aria-label*='£']", "el=>el.innerText")
                except Exception:
                    try:
                        price_text = await tile.query_selector_eval("span", "el=>el.innerText")
                    except Exception:
                        price_text = ""

                nums = re.sub(r"[^0-9]", "", price_text or "")
                if nums == "":
                    continue
                price = int(nums)

                link = None
                try:
                    ah = await tile.query_selector("a")
                    link = await ah.get_attribute("href")
                except Exception:
                    link = None

                resale = estimate_resale_price(title, price)
                margin = (resale - price) / max(price, 1)

                item = {
                    "title": title,
                    "price": price,
                    "resale_est": resale,
                    "margin": round(margin, 2),
                    "link": link,
                    "scraped_at": datetime.utcnow().isoformat()
                }

                # simple keyword filter
                if any(k.lower() in title.lower() for k in keywords):
                    results.append(item)
                    count += 1

            await browser.close()
        except Exception as e:
            await browser.close()
            st.error(f"Scraping failed: {e}")
    return results


def score_listing(item, min_margin=MIN_PROFIT_MARGIN):
    item["viable"] = item.get("margin", 0) >= min_margin
    return item


def openai_generate_message(title, price):
    if OPENAI_KEY is None:
        return "OpenAI API key not configured."
    prompt = (
        f"You are a polite buyer on Facebook Marketplace. The item is titled '{title}', listed at £{price}. "
        "Write a friendly short message asking if the item is still available and offering a reasonable lower price. Keep it human and concise."
    )
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=160
        )
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"OpenAI error: {e}"


async def send_message_to_seller(page, listing_url, message):
    """Prototype: open the listing and use Messenger button to send message. Real selectors depend on FB's current DOM."""
    try:
        if listing_url:
            await page.goto(listing_url, wait_until="networkidle")
            await page.wait_for_timeout(2000)
            # This code is fragile — fb changes often. It's a **prototype** only.
            # Example: click message/contact button and fill the composer.
            await page.click("text=Message")
            await page.wait_for_timeout(1000)
            await page.fill("div[contenteditable='true']", message)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(1000)
            return True
    except Exception as e:
        st.write(f"Auto-message failed: {e}")
    return False


async def create_resale_listing(page, item, markup=1.25):
    """Prototype stub — opens 'Sell' flow and fills certain fields. VERY fragile and may not work reliably.
    IMPORTANT: Posting items you do not own or cannot deliver may violate platform rules and local law.
    """
    try:
        await page.goto("https://www.facebook.com/marketplace/create/item", wait_until="networkidle")
        await page.wait_for_timeout(2000)
        # Attempt to fill title, price, description
        await page.fill("input[aria-label='Title']", item["title"])
        price_to_post = int(item["price"] * markup)
        await page.fill("input[aria-label='Price']", str(price_to_post))
        desc = f"Resale: {item['title']}. In good condition. Local pickup in {TARGET_LOCATION}."
        await page.fill("textarea[aria-label='Description']", desc)
        # Not uploading photos in this prototype
        # Submit (this will likely change) — commented out for safety
        # await page.click("text=Next")
        # await page.wait_for_timeout(1000)
        return True
    except Exception as e:
        st.write(f"Create listing failed: {e}")
    return False


# ---------------------
# Streamlit UI
# ---------------------
st.set_page_config(page_title="Marketplace Flip Bot", layout="wide")
st.title("🛍️ Marketplace Flip Bot — Streamlit Cloud Prototype")

with st.sidebar:
    st.header("Settings")
    keywords = st.text_input("Search keywords (comma separated)", value="bike,iPhone,sofa")
    min_margin = st.slider("Min profit margin", 0.05, 1.0, 0.25)
    target_loc = st.text_input("Target location (for URL)", value="London")
    st.write("\nFacebook credentials should be stored in Streamlit Secrets for security.")

if st.button("Run Scraper"):
    kws = [k.strip() for k in keywords.split(",") if k.strip()]
    st.info("Running scraper — this may take 10–20s in the cloud.")
    with st.spinner("Scraping Marketplace..."):
        items = asyncio.run(scrape_marketplace_headless(kws, limit=MAX_LISTINGS, headless=True))
        scored = [score_listing(it, min_margin) for it in items]
        if not scored:
            st.warning("No items found — try looser keywords or increase MAX_LISTINGS.")
        else:
            df = pd.DataFrame(scored)
            st.dataframe(df)
            st.session_state["last_scrape"] = df


st.markdown("---")

if st.session_state.get("last_scrape") is not None:
    df = st.session_state["last_scrape"]
    st.subheader("Actions")
    idx = st.number_input("Choose row index to act on (from dataframe)", min_value=0, max_value=max(0, len(df)-1), value=0)
    row = df.iloc[int(idx)].to_dict()

    st.markdown(f"**Selected:** {row['title']} — £{row['price']} — est resale £{row['resale_est']} ({row['margin']*100:.0f}% margin)")

    if st.button("Draft negotiation message"):
        msg = openai_generate_message(row['title'], row['price'])
        st.text_area("Suggested message", value=msg, height=160)

    if st.button("Auto-send message (prototype)"):
        st.info("Attempting to open a browser, log in, and send the message. This may fail or trigger platform protections.")
        msg = openai_generate_message(row['title'], row['price'])
        try:
            async def _send():
                async with async_playwright() as p:
                    browser, context, page = await facebook_login_and_page(p, headless=True)
                    ok = await send_message_to_seller(page, row.get('link'), msg)
                    await browser.close()
                    return ok
            res = asyncio.run(_send())
            if res:
                st.success("Message sent (or attempted). Check your Facebook account to confirm.)")
            else:
                st.error("Auto-message attempt failed. Check logs or try manual messaging.")
        except Exception as e:
            st.error(f"Auto-send failed: {e}")

    if st.button("Auto-create resale listing (prototype)"):
        st.info("Opening a browser to pre-fill the Facebook Create flow. This is a fragile prototype and may not submit automatically.")
        try:
            async def _create():
                async with async_playwright() as p:
                    browser, context, page = await facebook_login_and_page(p, headless=True)
                    ok = await create_resale_listing(page, row, markup=1.3)
                    await browser.close()
                    return ok
            res = asyncio.run(_create())
            if res:
                st.success("Create flow opened and pre-filled (if selectors matched). Review on FB before submitting.")
            else:
                st.error("Create flow failed. This is expected sometimes.")
        except Exception as e:
            st.error(f"Auto-create failed: {e}")

st.markdown("---")
st.caption("Important: This project is a technical prototype. Using automated accounts to message or post at scale may violate platform terms of service and risk account suspension.")
