# patchy_browser_agent/runner.py

from playwright.async_api import async_playwright
import asyncio

BTY_FRONTEND = "https://www.btyfitness.app/admin"
TEST_ADMIN_EMAIL = "jackharper0517@gmail.com"
TEST_ADMIN_PASSWORD = "R1$3ifyouwould!"

async def run_browser_and_get_token():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        # 1. Load admin page
        await page.goto(BTY_FRONTEND)

        # 2. Wait for Clerk sign-in UI
        await page.wait_for_selector('input[type="email"]')

        # 3. Fill Clerk login form
        await page.fill('input[type="email"]', TEST_ADMIN_EMAIL)
        await page.fill('input[type="password"]', TEST_ADMIN_PASSWORD)
        await page.click('button:has-text("Sign in")')

        # Wait for redirect + admin page load
        await page.wait_for_load_state("networkidle")

        # Wait for something that ACTUALLY exists on your admin dashboard
        # "Content" is guaranteed to appear in your AdminDashboard UI
        await page.wait_for_selector("text=Content", timeout=15000)

        # Extract JWT using injected helper
        token = await page.evaluate(
            "window.__patchy_get_token && window.__patchy_get_token()"
        )

        # Guard against missing token
        if not token:
            raise RuntimeError("Browser Agent: Clerk token helper returned None")

        await browser.close()
        return token


if __name__ == "__main__":
    token = asyncio.run(run_browser_and_get_token())
    print("Extracted JWT:", token)
