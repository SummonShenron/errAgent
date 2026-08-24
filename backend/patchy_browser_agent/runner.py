# patchy_browser_agent/runner.py

from playwright.async_api import async_playwright
import asyncio

BTY_FRONTEND = "https://btyapp.vercel.app/admin"
TEST_ADMIN_EMAIL = "jackharper0517@gmail.com"
TEST_ADMIN_PASSWORD = "R1$3ifyouwould!"

BROWSERLESS_WS = "wss://production-sfo.browserless.io/chromium/playwright?token=2V8MYAQGdOa2zWT4cdb32601610399d98cfccd929eea9defb"

async def run_browser_and_get_token():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect(BROWSERLESS_WS)
        page = await browser.new_page()

        # Use DEV frontend, not production
        await page.goto("https://btyapp.vercel.app/admin")

        # Clerk dev login selector
        await page.wait_for_selector('input[name="identifier"]', timeout=30000)
        await page.fill('input[name="identifier"]', TEST_ADMIN_EMAIL)

        await page.fill('input[type="password"]', TEST_ADMIN_PASSWORD)
        await page.click('button:has-text("Sign in")')

        await page.wait_for_load_state("networkidle")

        await page.wait_for_selector("text=Content", timeout=15000)

        token = await page.evaluate(
            "window.__patchy_get_token && window.__patchy_get_token()"
        )

        await browser.close()
        return token



if __name__ == "__main__":
    token = asyncio.run(run_browser_and_get_token())
    print("Extracted JWT:", token)
