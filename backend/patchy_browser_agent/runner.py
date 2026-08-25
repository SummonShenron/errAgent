import asyncio
import logging
import os
import pyotp
from playwright.async_api import async_playwright

logger = logging.getLogger("errAgent Logger")

BTY_FRONTEND = "https://btyapp.vercel.app/admin"
TEST_ADMIN_EMAIL = "jackharper0517@gmail.com"
TEST_ADMIN_PASSWORD = "R1$3ifyouwould!"

TOTP_SECRET = (os.getenv("CLERK_TOTP_SECRET") or "").strip()
TEST_BACKUP_CODE = (os.getenv("CLERK_BACKUP_CODE") or "").strip()

BROWSERLESS_WS = "wss://production-sfo.browserless.io/chromium/playwright?token=2V8MYAQGdOa2zWT4cdb32601610399d98cfccd929eea9defb"

def generate_mfa_code() -> str:
    if TOTP_SECRET:
        try:
            return pyotp.TOTP(TOTP_SECRET).now()
        except Exception:
            pass
    return TEST_BACKUP_CODE

async def run_browser_and_get_token() -> str:
    async with async_playwright() as pw:
        logger.info("[pentest] Connecting to Browserless and navigating to /admin...")
        browser = await pw.chromium.connect(BROWSERLESS_WS)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # 1. Load Admin / Sign-in route
        await page.goto(BTY_FRONTEND, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # 2. Fill Email
        email_input = page.locator('input[name="identifier"]:visible, input[type="email"]:visible').first
        await email_input.wait_for(state="visible", timeout=15000)
        await email_input.fill(TEST_ADMIN_EMAIL)
        
        # Click primary submit button explicitly
        submit_btn = page.locator('button.cl-formButtonPrimary:visible, button[type="submit"]:visible').first
        if await submit_btn.is_visible():
            await submit_btn.click()
        else:
            await email_input.press("Enter")

        # 3. Fill Password
        pwd_input = page.locator('input[name="password"]:visible, input[type="password"]:visible').first
        await pwd_input.wait_for(state="visible", timeout=15000)
        await pwd_input.fill(TEST_ADMIN_PASSWORD)
        
        submit_pwd = page.locator('button.cl-formButtonPrimary:visible, button[type="submit"]:visible').first
        if await submit_pwd.is_visible():
            await submit_pwd.click()
        else:
            await pwd_input.press("Enter")

        # 4. Handle MFA if prompted
        try:
            code_input = page.locator('input[name="code"]:visible, input[type="text"]:visible').first
            await code_input.wait_for(state="visible", timeout=6000)

            code_to_fill = generate_mfa_code()
            if code_to_fill:
                await code_input.fill(code_to_fill)
                submit_mfa = page.locator('button.cl-formButtonPrimary:visible, button[type="submit"]:visible').first
                if await submit_mfa.is_visible():
                    await submit_mfa.click()
                else:
                    await code_input.press("Enter")
        except Exception:
            pass

        # 5. Wait for Clerk Session Hydration
        logger.info("[pentest] Waiting for active Clerk session hydration...")
        try:
            await page.wait_for_function("() => window.Clerk && window.Clerk.session !== null", timeout=15000)
        except Exception:
            logger.warning("[pentest] Timeout waiting for window.Clerk.session predicate.")

        # 6. Extract Session JWT directly from hydrated Clerk object
        token = await page.evaluate("""
            async () => {
                if (window.Clerk && window.Clerk.session) {
                    return await window.Clerk.session.getToken();
                }
                return null;
            }
        """)

        await browser.close()

        if not token or not isinstance(token, str):
            raise RuntimeError("Authentication failed: Clerk session did not return a valid JWT token.")

        logger.info(f"[pentest] Successfully acquired Clerk JWT token (Length: {len(token)}).")
        return token

if __name__ == "__main__":
    token = asyncio.run(run_browser_and_get_token())
    print("Extracted JWT:", token)