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
        logger.info("[pentest] Connecting to Browserless...")
        browser = await pw.chromium.connect(BROWSERLESS_WS)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Step 1: Initial Navigation
        logger.info(f"[pentest] Navigating to target: {BTY_FRONTEND}")
        await page.goto(BTY_FRONTEND, wait_until="networkidle")
        logger.info(f"[pentest] Landed on URL: {page.url}")

        # Step 2: Identifier Input (Email)
        logger.info("[pentest] Locating email/identifier input field...")
        email_selector = 'input[name="identifier"], input[name="emailAddress"], input[type="email"], input[type="text"]'
        try:
            email_input = page.locator(email_selector).first
            await email_input.wait_for(state="visible", timeout=12000)
            await email_input.fill(TEST_ADMIN_EMAIL)
            logger.info("[pentest] Filled email address.")

            # Click primary submission button
            submit_btn = page.locator('button.cl-formButtonPrimary, button[type="submit"], button:has-text("Continue"), button:has-text("Next")').first
            if await submit_btn.is_visible():
                await submit_btn.click()
                logger.info("[pentest] Clicked email submit button.")
            else:
                await email_input.press("Enter")
                logger.info("[pentest] Pressed Enter on email field.")
        except Exception as e:
            logger.error(f"[pentest] Failed during Email step: {e}")
            await browser.close()
            raise

        # Step 3: Password Input
        logger.info("[pentest] Waiting for password input field...")
        pwd_selector = 'input[name="password"], input[type="password"]'
        try:
            pwd_input = page.locator(pwd_selector).first
            await pwd_input.wait_for(state="visible", timeout=12000)
            await pwd_input.fill(TEST_ADMIN_PASSWORD)
            logger.info("[pentest] Filled password.")

            submit_pwd = page.locator('button.cl-formButtonPrimary, button[type="submit"], button:has-text("Continue"), button:has-text("Sign in")').first
            if await submit_pwd.is_visible():
                await submit_pwd.click()
                logger.info("[pentest] Clicked password submit button.")
            else:
                await pwd_input.press("Enter")
                logger.info("[pentest] Pressed Enter on password field.")
        except Exception as e:
            logger.error(f"[pentest] Failed during Password step: {e}")
            await browser.close()
            raise

        # Step 4: Handle Optional MFA Prompt
        logger.info("[pentest] Checking for potential MFA step...")
        try:
            code_selector = 'input[name="code"], input[autocomplete="one-time-code"]'
            code_input = page.locator(code_selector).first
            await code_input.wait_for(state="visible", timeout=5000)
            
            logger.info("[pentest] MFA prompt detected. Generating TOTP/Backup Code...")
            code_to_fill = generate_mfa_code()
            if code_to_fill:
                await code_input.fill(code_to_fill)
                submit_mfa = page.locator('button.cl-formButtonPrimary, button[type="submit"], button:has-text("Continue")').first
                if await submit_mfa.is_visible():
                    await submit_mfa.click()
                else:
                    await code_input.press("Enter")
                logger.info("[pentest] Submitted MFA code.")
        except Exception:
            logger.info("[pentest] No MFA prompt appeared (or step skipped).")

        # Step 5: Wait for Navigation/Auth Completion
        logger.info("[pentest] Waiting for auth completion and session hydration...")
        await page.wait_for_timeout(3000)
        logger.info(f"[pentest] Current post-login URL: {page.url}")

        # Step 6: Token Extraction Strategy
        token = None

        # Strategy A: Window Clerk Object
        try:
            token = await page.evaluate("""
                async () => {
                    if (window.Clerk && window.Clerk.session) {
                        return await window.Clerk.session.getToken();
                    }
                    return null;
                }
            """)
            if token:
                logger.info("[pentest] Successfully extracted JWT via window.Clerk.session!")
        except Exception as ex:
            logger.warning(f"[pentest] window.Clerk evaluation failed: {ex}")

        # Strategy B: Context Session Cookies Fallback
        if not token:
            logger.info("[pentest] Checking browser context cookies for Clerk session...")
            cookies = await context.cookies()
            for c in cookies:
                if c.get("name") in ["__session", "__clerk_db_jwt"]:
                    token = c.get("value")
                    logger.info(f"[pentest] Found valid token in cookie: {c.get('name')}")
                    break

        await browser.close()

        if not token or not isinstance(token, str):
            raise RuntimeError(f"Authentication failed. Post-login URL was '{page.url}', but no valid JWT token was captured.")

        logger.info(f"[pentest] Token acquired successfully (Length: {len(token)}).")
        return token

if __name__ == "__main__":
    token = asyncio.run(run_browser_and_get_token())
    print("Extracted JWT:", token)