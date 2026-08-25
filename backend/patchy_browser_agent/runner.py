import asyncio
import os
import pyotp
from playwright.async_api import async_playwright

BTY_FRONTEND = "https://btyapp.vercel.app/sign-in"
TEST_ADMIN_EMAIL = "jackharper0517@gmail.com"
TEST_ADMIN_PASSWORD = "R1$3ifyouwould!"

# Force string fallbacks to prevent NoneType exceptions
TOTP_SECRET = (os.getenv("CLERK_TOTP_SECRET") or "").strip()
TEST_BACKUP_CODE = (os.getenv("CLERK_BACKUP_CODE") or "").strip()

BROWSERLESS_WS = "wss://production-sfo.browserless.io/chromium/playwright?token=2V8MYAQGdOa2zWT4cdb32601610399d98cfccd929eea9defb"

def generate_mfa_code() -> str:
    """Safely return valid TOTP or Backup Code without blowing up on NoneType."""
    if TOTP_SECRET:
        try:
            return pyotp.TOTP(TOTP_SECRET).now()
        except Exception:
            pass
    return TEST_BACKUP_CODE

async def run_browser_and_get_token() -> str:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect(BROWSERLESS_WS)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # 1. Load sign-in page & fill email
        await page.goto("https://btyapp.vercel.app/admindashboard", wait_until="networkidle")
        
        email_input = page.locator('input[name="identifier"]:visible, input[type="email"]:visible').first
        await email_input.wait_for(state="visible", timeout=15000)
        await email_input.fill(TEST_ADMIN_EMAIL)
        await email_input.press("Enter")

        # 2. Fill password
        pwd_input = page.locator('input[name="password"]:visible, input[type="password"]:visible').first
        await pwd_input.wait_for(state="visible", timeout=15000)
        await pwd_input.fill(TEST_ADMIN_PASSWORD)
        await pwd_input.press("Enter")

        # 3. Handle MFA (TOTP or Backup Code)
        try:
            code_input = page.locator('input[name="code"]:visible, input[type="text"]:visible').first
            await code_input.wait_for(state="visible", timeout=8000)

            code_to_fill = generate_mfa_code()
            if code_to_fill:
                await code_input.fill(code_to_fill)
                await code_input.press("Enter")
        except Exception:
            pass  # MFA skipped or cached

        # 4. Wait for post-login redirect away from /sign-in
        try:
            await page.wait_for_url(lambda url: "sign-in" not in url, timeout=15000)
        except Exception:
            pass

        await page.wait_for_load_state("domcontentloaded")

        # 5. Extract Session Token
        token = None
        for _ in range(12):
            token = await page.evaluate("""
                async () => {
                    if (window.Clerk?.session) {
                        return await window.Clerk.session.getToken();
                    }
                    if (window.__patchy_get_token) {
                        return await window.__patchy_get_token();
                    }
                    try {
                        for (let i = 0; i < localStorage.length; i++) {
                            const key = localStorage.key(i);
                            if (key && (key.includes('clerk') || key.includes('session') || key.includes('jwt'))) {
                                const val = localStorage.getItem(key);
                                if (val && val.startsWith('eyJ')) return val;
                            }
                        }
                    } catch (e) {}
                    return null;
                }
            """)
            if token and isinstance(token, str):
                break
            await asyncio.sleep(1)

        # Fallback to cookies
        if not token:
            raw_cookies = await context.cookies() or []
            for c in raw_cookies:
                if isinstance(c, dict) and (c.get("name") in ["__session", "__clerk_db_jwt"] or "clerk" in c.get("name", "")):
                    token = c.get("value")
                    break

        await browser.close()

        if not token or not isinstance(token, str):
            raise RuntimeError("Browser Agent completed login but failed to retrieve a string JWT token.")

        return token

if __name__ == "__main__":
    token = asyncio.run(run_browser_and_get_token())
    print("Extracted JWT:", token)