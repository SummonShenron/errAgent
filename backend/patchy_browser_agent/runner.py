import asyncio
from playwright.async_api import async_playwright

BTY_FRONTEND = "https://btyapp.vercel.app/admin"
TEST_ADMIN_EMAIL = "jackharper0517@gmail.com"
TEST_ADMIN_PASSWORD = "R1$3ifyouwould!"
GOOGLE_PHONE_LAST_2_DIGITS = "90"

BROWSERLESS_WS = "wss://production-sfo.browserless.io/chromium/playwright?token=2V8MYAQGdOa2zWT4cdb32601610399d98cfccd929eea9defb"

async def run_browser_and_get_token():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect(BROWSERLESS_WS)
        context = await browser.new_context(
            java_script_enabled=True,
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        # 1. Load sign-in page
        await page.goto("https://btyapp.vercel.app/sign-in", wait_until="domcontentloaded")

        # 2. Resilient selector for Clerk's Google button
        google_btn = page.locator(
            'button[data-provider="google"], '
            'button:has-text("Google"), '
            'button:has-text("Continue with Google"), '
            '.cl-socialButtonsBlockButton__google, '
            'button[aria-label*="Google"]'
        ).first

        await google_btn.wait_for(state="visible", timeout=30000)

        # 3. Click Google button and capture popup
        async with context.expect_page() as popup_info:
            await google_btn.click()
        google = await popup_info.value

        # 4. Fill Google email
        await google.wait_for_selector('input[type="email"]', timeout=30000)
        await google.fill('input[type="email"]', TEST_ADMIN_EMAIL)
        await google.click('button:has-text("Next")')

        # 5. Fill Google password
        await google.wait_for_selector('input[type="password"]', timeout=30000)
        await google.fill('input[type="password"]', TEST_ADMIN_PASSWORD)
        await google.click('button:has-text("Next")')

        # Handle optional prompts
        try:
            await google.click('button:has-text("Yes")', timeout=5000)
        except Exception:
            pass

        try:
            await google.wait_for_selector('input[type="tel"]', timeout=5000)
            await google.fill('input[type="tel"]', GOOGLE_PHONE_LAST_2_DIGITS)
            await google.click('button:has-text("Next")')
        except Exception:
            pass

        # 6. Wait for redirect and Clerk session hydration
        await page.wait_for_load_state("networkidle")
        await page.wait_for_function("window.Clerk?.session?.id", timeout=20000)
        await page.wait_for_selector("text=Content", timeout=20000)

        # Extract JWT
        token = await page.evaluate(
            "window.__patchy_get_token && window.__patchy_get_token()"
        )

        await browser.close()
        return token

if __name__ == "__main__":
    token = asyncio.run(run_browser_and_get_token())
    print("Extracted JWT:", token)