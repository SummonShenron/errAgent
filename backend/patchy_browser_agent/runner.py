import asyncio
import os
from playwright.async_api import async_playwright

BTY_FRONTEND = "https://btyapp.vercel.app/sign-in"
TEST_ADMIN_EMAIL = "jackharper0517@gmail.com"
TEST_ADMIN_PASSWORD = "R1$3ifyouwould!"
TEST_BACKUP_CODE = os.getenv("CLERK_BACKUP_CODE", "tifdav-pehraz-5qatcE")

BROWSERLESS_WS = "wss://production-sfo.browserless.io/chromium/playwright?token=2V8MYAQGdOa2zWT4cdb32601610399d98cfccd929eea9defb"

async def run_browser_and_get_token():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect(BROWSERLESS_WS)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # 1. Load sign-in page
        await page.goto(BTY_FRONTEND, wait_until="domcontentloaded")

        # 2. Fill Email
        email_input = page.locator('input[name="identifier"], input[type="email"], input[id*="identifier"]').first
        await email_input.wait_for(state="visible", timeout=15000)
        await email_input.fill(TEST_ADMIN_EMAIL)

        continue_btn = page.locator('button.cl-formButtonPrimary, button[type="submit"]:has-text("Continue")').first
        await continue_btn.click()

        # 3. Fill Password
        pwd_input = page.locator('input[name="password"], input[type="password"]').first
        await pwd_input.wait_for(state="visible", timeout=15000)
        await pwd_input.fill(TEST_ADMIN_PASSWORD)

        submit_btn = page.locator('button.cl-formButtonPrimary, button[type="submit"]:has-text("Continue")').first
        await submit_btn.click()

        # 4. Handle MFA via Backup Code
        try:
            # Wait briefly to see if an MFA screen appears
            await page.wait_for_selector(
                'input[name="code"], button:has-text("Use backup code"), button:has-text("Use a backup code"), .cl-alternativeMethodsBlockButton',
                timeout=10000
            )

            # Click "Use backup code" if presented with an alternative method list or TOTP screen
            backup_toggle = page.locator(
                'button:has-text("Use backup code"), '
                'button:has-text("Use a backup code"), '
                'a:has-text("Use backup code"), '
                'button:has-text("Use backup"), '
                '.cl-alternativeMethodsBlockButton:has-text("backup")'
            ).first

            if await backup_toggle.is_visible():
                await backup_toggle.click()

            # Input backup code
            code_input = page.locator('input[name="code"], input[type="text"]').first
            await code_input.wait_for(state="visible", timeout=10000)
            await code_input.fill(TEST_BACKUP_CODE)

            # Submit MFA form
            mfa_submit = page.locator('button.cl-formButtonPrimary, button[type="submit"]').first
            await mfa_submit.click()
        except Exception:
            # Bypasses cleanly if MFA is not requested
            pass

        # 5. Wait for Clerk session hydration
        await page.wait_for_function("window.Clerk?.session?.id", timeout=20000)

        # 6. Extract token
        token = await page.evaluate("""
            async () => {
                if (window.__patchy_get_token) {
                    return await window.__patchy_get_token();
                }
                if (window.Clerk?.session) {
                    return await window.Clerk.session.getToken();
                }
                return null;
            }
        """)

        await browser.close()
        return token

if __name__ == "__main__":
    token = asyncio.run(run_browser_and_get_token())
    print("Extracted JWT:", token)