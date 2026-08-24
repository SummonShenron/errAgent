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
        context = await browser.new_context(
            java_script_enabled=True,
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()
        # Load your dev frontend
        await page.goto("https://btyapp.vercel.app/sign-in", wait_until="networkidle")

        # Click "Sign in with Google"
        await page.wait_for_selector('button[data-provider="google"]', timeout=30000)
        async with context.expect_page() as popup_info:
            await page.click('button[data-provider="google"]')
        google = await popup_info.value

        # Fill Google email
        await google.wait_for_selector('input[type="email"]', timeout=30000)
        await google.fill('input[type="email"]', TEST_ADMIN_EMAIL)
        await google.click('button:has-text("Next")')

        # Fill Google password
        await google.wait_for_selector('input[type="password"]', timeout=30000)
        await google.fill('input[type="password"]', TEST_ADMIN_PASSWORD)
        await google.click('button:has-text("Next")')

        # Handle "This browser is not secure"
        try:
            await google.click('button:has-text("Yes")', timeout=5000)
        except:
            pass

        # Handle "Verify it’s you" (if triggered)
        try:
            await google.wait_for_selector('input[type="tel"]', timeout=5000)
            await google.fill('input[type="tel"]', GOOGLE_PHONE_LAST_2_DIGITS)
            await google.click('button:has-text("Next")')
        except:
            pass

        # Wait for redirect back to Clerk
        await page.wait_for_load_state("networkidle")

        # Wait for Clerk session to exist
        await page.wait_for_function("window.Clerk?.session?.id", timeout=20000)

        # Wait for admin dashboard
        await page.wait_for_selector("text=Content", timeout=20000)

        # Extract JWT
        token = await page.evaluate(
            "window.__patchy_get_token && window.__patchy_get_token()"
        )

        await browser.close()
        return token
GOOGLE_PHONE_LAST_2_DIGITS = "90"  # Replace with the last 2 digits of your Google phone number if needed



if __name__ == "__main__":
    token = asyncio.run(run_browser_and_get_token())
    print("Extracted JWT:", token)
