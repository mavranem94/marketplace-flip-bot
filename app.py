# app.py — Streamlit + Playwright Marketplace Flip Bot (Cloud-ready, login-debug + cookie persistence)

import os
# Ensure Playwright browser is available in the container (harmless if already installed)
os.system("python -m playwright install chromium")

import asyncio
import re
from datetime import datetime
import json
import pandas as pd
import streamlit as st
import openai
from playwright.async_api import async_playwright

# ---------------------
# Configuration & Secrets
# ---------------------
OPENAI_KEY = st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
FB_EMAIL = st.secrets.get("FB_EMAIL") or os.getenv("FB_EMAIL")
FB_PASSWORD = st.secrets.get("FB_PASSWORD") or os.getenv("FB_PASSWORD")

# Defaults (UI can override below)
TARGET_LOCATION = st.session_state.get("target_location", "London")
MIN_PROFIT_MARGIN = st.session_state.get("min_profit_margin", 0.25)
MAX_LISTINGS = 100  # adjustable
STATE_PATH = "fb_state.json"  # cookie/session state (ephemeral on Streamlit Cloud per deploy)

if OPENAI_KEY is None:
    st.warning("Set your OpenAI API key in Streamlit Secrets (OPENAI_API_KEY) before using the app.")
else:
    openai.api_key = OPENAI_KEY

if not FB_EMAIL or not FB_PASSWORD:
    st.info("FB_EMAIL / FB_PASSWORD not set. Marketplace content may be limited when logged out.")

# ---------------------
# Helpers
# ---------------------

def estimate_resale_price(title: str, price: int) -> int:
    """Very rough heuristic. Replace with eBay/Amazon API calls if desired."""
    t = title.lower()
    if any(k in t for k in ["sofa", "couch", "armchair"]):
        factor = 1.8
    elif any(k in t for k in ["iphone", "phone", "macbook", "laptop"]):
        factor = 1.6
    else:
        factor = 1.4
    return max(int(price * factor), price + 1)


async def facebook_login_and_page(playwright, target_location: str, headless: bool = True):
    """Launch Chromium (cloud-friendly), create context (reusing cookies if present), go to Marketplace, handle login.
    Returns (browser, context, page).
    """
    # Find a usable Chromium executable in the container
    chromium_path = os.environ.get("CHROMIUM_PATH")
    if not chromium_path:
        for p in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
            if os.path.exists(p):
                chromium_path = p
                break

    launch_options = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
            "--disable-setuid-sandbox",
        ],
    }
    if chromium_path and os.path.exists(chromium_path):
        launch_options["executable_path"] = chromium_path
    else:
        raise RuntimeError("No valid Chromium executable found in container.")

    browser = await playwright.chromium.launch(**launch_options)

    # Reuse session if we have it
    context_kwargs = {}
    if os.path.exists(STATE_PATH):
        context_kwargs["storage_state"] = STATE_PATH
        st.write("Using saved FB session state.")

    context = await browser.new_context(**context_kwargs)
    page = await context.new_page()

    target_url = f"https://www.facebook.com/marketplace/{target_location.lower()}"
    await page.goto(target_url, wait_until="networkidle")
    st.write("After first goto, URL:", page.url)
    await page.wait_for_timeout(2000)

    # If redirected to login, try login flow explicitly
    if "login" in page.url.lower() or "/login/" in page.url.lower():
        st.info("Redirected to Facebook login page.")
        await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("input[name='email']", timeout=10000)
            if FB_EMAIL and FB_PASSWORD:
                await page.fill("input[name='email']", FB_EMAIL)
                await page.fill("input[name='pass']", FB_PASSWORD)
                await page.click("button[name=login]")
                await page.wait_for_load_state("networkidle")
                st.write("After login submit, URL:", page.url)

                # Check for checkpoint/2FA indications
                current_html = await page.content()
                if any(x in current_html.lower() for x in ["two-factor", "checkpoint", "approve your login"]):
                    st.error("Facebook triggered a security checkpoint/2FA. Manual verification is required on this account.")
                else:
                    # Save session cookies for reuse next time
                    try:
                        await context.storage_state(path=STATE_PATH)
                        st.write("Saved FB session state to fb_state.json")
                    except Exception as e:
                        st.write(f"Could not save session state: {e}")

                    # Navigate back to marketplace now that we (likely) have a session
                    await page.goto(target_url, wait_until="networkidle")
                    st.write("After returning to Marketplace, URL:", page.url)
                    await page.wait_for_timeout(2000)

            else:
                st.error("Login required but FB_EMAIL/FB_PASSWORD not provided in Secrets.")
        except Exception as e:
            st.error(f"Login automation failed: {e}")

    # Light pre-scroll to trigger lazy load
    for _ in range(3):
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)

    return browser, context, page


async def scrape_marketplace_headless(keywords, limit: int = 10, headless: bool = True):
    """Scroll to load results, collect item links, open each item page for stable title/price extraction."""
    results = []
    async with async_playwright() as p:
        # NOTE: Streamlit Cloud cannot show a visible browser; headless=True is effectively required there.
        browser, context, page = await facebook_login_and_page(p, TARGET_LOCATION, headless=headless)
        try:
            # Ensure we’re on the correct location page
            target_url = f"https://www.facebook.com/marketplace/{TARGET_LOCATION.lower()}"
            await page.goto(target_url, wait_until="networkidle")
            await page.wait_for_timeout(1500)

            # Scroll a lot to load many items
            max_scrolls = 20  # raise if needed
            last_height = 0
            for _ in range(max_scrolls):
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1200)
                height = await page.evaluate("document.body.scrollHeight")
                if height == last_height:
                    break
                last_height = height

            # Collect item links (more robust than tile containers)
            anchors = await page.query_selector_all("a[href*='/marketplace/item/']")
            st.write("Found raw item anchors:", len(anchors))
            seen = set()
            count = 0

            for a in anchors:
                if count >= limit:
                    break
                href = await a.get_attribute("href")
                if not href:
                    continue
                if not href.startswith("http"):
                    href = f"https://www.facebook.com{href}"
                if href in seen:
                    continue
                seen.add(href)

                # Open the item page for stable selectors
                item_page = await context.new_page()
                try:
                    await item_page.goto(href, wait_until="networkidle")
                    await item_page.wait_for_timeout(1200)

                    # Title candidates
                    title = None
                    for sel in [
                        "h1",
                        "[data-ad-preview='message']",
                        "div[role='heading']",
                    ]:
                        try:
                            title = await item_page.query_selector_eval(sel, "el => el.innerText")
                            if title and title.strip():
                                break
                        except Exception:
                            pass

                    # Price candidates (GBP and generic)
                    price_text = None
                    for sel in [
                        "span[aria-label*='£']",
                        "div[aria-label*='£'] span",
                        "div[role='main'] span:has-text('£')",
                        "span:has-text('£')",
                    ]:
                        try:
                            price_text = await item_page.query_selector_eval(sel, "el => el.innerText")
                            if price_text and price_text.strip():
                                break
                        except Exception:
                            pass

                    if not title or not price_text:
                        continue

                    nums = re.sub(r"[^0-9]", "", price_text)
                    if not nums:
                        continue
                    price = int(nums)

                    resale = estimate_resale_price(title, price)
                    margin = (resale - price) / max(price, 1)

                    item = {
                        "title": title.strip(),
                        "price": price,
                        "resale_est": resale,
                        "margin": round(margin, 2),
                        "link": href,
                        "scraped_at": datetime.utcnow().isoformat(),
                    }

                    # Case-insensitive partial keyword match
                    if not keywords or any(k.lower() in title.lower() for k in keywords):
                        results.append(item)
                        count += 1

                finally:
                    await item_page.close()

            await browser.close()

            if not results:
                st.info("No items parsed. Try disabling keyword filter, increasing scrolls, or ensure login succeeded.")
            else:
                st.write("Sample parsed titles:", [r["title"] for r in results[:5]])

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
            max_tokens=160,
        )
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"OpenAI error: {e}"


async def send_message_to_seller(page, listing_url, message):
    """Prototype — selectors are fragile and may break as FB changes UI."""
    try:
        if listing_url:
            await page.goto(listing_url, wait_until="networkidle")
            await page.wait_for_timeout(1000)
            await page.click("text=Message")
            await page.wait_for_timeout(600)
            await page.fill("div[contenteditable='true']", message)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(600)
            return True
    except Exception as e:
        st.write(f"Auto-message failed: {e}")
    return False


async def create_resale_listing(page, item, markup=1.25):
    """Prototype — opens create flow and pre-fills fields. May break as FB changes UI.
    IMPORTANT: Posting items you do not own or cannot deliver may violate platform rules and local law.
    """
    try:
        await page.goto("https://www.facebook.com/marketplace/create/item", wait_until="networkidle")
        await page.wait_for_timeout(1000)
        await page.fill("input[aria-label='Title']", item["title"])  # selectors may vary
        price_to_post = int(item["price"] * markup)
        await page.fill("input[aria-label='Price']", str(price_to_post))
        desc = f"Resale: {item['title']}. In good condition. Local pickup in {TARGET_LOCATION}."
        await page.fill("textarea[aria-label='Description']", desc)
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
    keywords_text = st.text_input("Search keywords (comma separated)", value="bike,iPhone,sofa")
    min_margin = st.slider("Min profit margin", 0.05, 1.0, 0.25)
    target_loc = st.text_input("Target location (for URL)", value=TARGET_LOCATION)
    # Debug toggle (note: Streamlit Cloud cannot show non-headless UI; this is mainly for local runs)
    debug_show_browser = st.checkbox("Debug: show browser (non-headless — local only)", value=False)
    st.write("\nFacebook credentials should be stored in Streamlit Secrets for security.")

if st.button("Run Scraper"):
    # Make the sidebar location actually drive the scraper
    TARGET_LOCATION = target_loc
    keywords = [k.strip() for k in keywords_text.split(",") if k.strip()]

    st.info("Running scraper — this may take 15–45s in the cloud.")
    with st.spinner("Scraping Marketplace..."):
        items = asyncio.run(scrape_marketplace_headless(keywords, limit=MAX_LISTINGS, headless=(not debug_show_browser)))
        scored = [score_listing(it, min_margin) for it in items]
        if not scored:
            st.warning("No items found — try broader keywords, increase scrolling, or ensure login succeeded.")
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
                    browser, context, page = await facebook_login_and_page(p, TARGET_LOCATION, headless=True)
                    ok = await send_message_to_seller(page, row.get('link'), msg)
                    await browser.close()
                    return ok
            res = asyncio.run(_send())
            if res:
                st.success("Message sent (or attempted). Check your Facebook account to confirm.")
            else:
                st.error("Auto-message attempt failed. Try manual messaging.")
        except Exception as e:
            st.error(f"Auto-send failed: {e}")

    if st.button("Auto-create resale listing (prototype)"):
        st.info("Opening a browser to pre-fill the Facebook Create flow. This is a fragile prototype and may not submit automatically.")
        try:
            async def _create():
                async with async_playwright() as p:
                    browser, context, page = await facebook_login_and_page(p, TARGET_LOCATION, headless=True)
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
st.caption("Important: Prototype only. Automating Facebook can violate its Terms of Service and risk account restrictions. Use a test account and act lawfully.")
