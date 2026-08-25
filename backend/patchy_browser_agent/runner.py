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
        
        # Inject realistic browser fingerprints to reduce Google anti-bot triggers
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/Chicago",
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

        # 3. Handle Popup vs. In-Tab Redirect
        auth_page = page
        try:
            async with context.expect_page(timeout=5000) as popup_info:
                await google_btn.click()
            auth_page = await popup_info.value
        except Exception:
            pass

        # 4. Diagnostic URL check & Email fill
        await auth_page.wait_for_load_state("domcontentloaded")
        current_url = auth_page.url

        try:
            email_input = auth_page.locator('input[type="email"]')
            await email_input.wait_for(state="visible", timeout=15000)
            await email_input.fill(TEST_ADMIN_EMAIL)
            await auth_page.click('button:has-text("Next")')
        except Exception as err:
            title = await auth_page.title()
            raise RuntimeError(
                f"Google blocked the automated login on page '{title}' (URL: {current_url}). "
                "Google OAuth disallows headless automation on standard production logins."
            ) from err

        # 5. Fill Google password
        await auth_page.wait_for_selector('input[type="password"]', timeout=30000)
        await auth_page.fill('input[type="password"]', TEST_ADMIN_PASSWORD)
        await auth_page.click('button:has-text("Next")')

        # Handle security prompts
        try:
            await auth_page.click('button:has-text("Yes")', timeout=5000)
        except Exception:
            pass

        try:
            await auth_page.wait_for_selector('input[type="tel"]', timeout=5000)
            await auth_page.fill('input[type="tel"]', GOOGLE_PHONE_LAST_2_DIGITS)
            await auth_page.click('button:has-text("Next")')
        except Exception:
            pass

        # 6. Wait for redirect and Clerk session hydration
        await page.wait_for_load_state("networkidle")
        await page.wait_for_function("window.Clerk?.session?.id", timeout=20000)
        await page.wait_for_selector("text=Content", timeout=20000)

        token = await page.evaluate(
            "window.__patchy_get_token && window.__patchy_get_token()"
        )

        await browser.close()
        return token

if __name__ == "__main__":
    token = asyncio.run(run_browser_and_get_token())
    print("Extracted JWT:", token)