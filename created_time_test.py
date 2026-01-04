import sys
import requests
import re
import json
from datetime import datetime

# Selenium imports (used if available)
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except Exception:
    SELENIUM_AVAILABLE = False

# Put the Reel URL here (manually). You can also pass it as the first CLI arg.
REEL_URL = "https://www.instagram.com/reel/DS5BbPrDBVs/?igsh=MTd5M25rdHR0M3FqYg=="
if len(sys.argv) > 1:
    REEL_URL = sys.argv[1]


def try_extract_json_pattern(html, pattern):
    m = re.search(pattern, html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def find_timestamp_in_html(html):
    # 1) window._sharedData = {...};</script>
    jd = try_extract_json_pattern(html, r"window\._sharedData\s*=\s*({.*?})\s*;</script>")
    if jd:
        # try known locations
        try:
            # GraphQL entry points
            entry = jd.get('entry_data', {})
            for k in ('PostPage', 'ReelPage'):
                if k in entry and entry[k]:
                    obj = entry[k][0]
                    # drill for taken_at_timestamp
                    for v in ('taken_at_timestamp', 'taken_at'):
                        ts = None
                        if isinstance(obj, dict):
                            ts = obj.get(v) or obj.get('graphql', {}).get('shortcode_media', {}).get(v)
                        if isinstance(ts, (int, float)):
                            return int(ts)
        except Exception:
            pass

    # 2) window.__additionalDataLoaded(..., {...});
    jd = try_extract_json_pattern(html, r"window\.__additionalDataLoaded\([^,]+,\s*({.*?})\);")
    if jd:
        try:
            # nested structures may contain shortcode_media
            def walk(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k == 'taken_at_timestamp' and isinstance(v, (int, float)):
                            return int(v)
                        res = walk(v)
                        if res:
                            return res
                elif isinstance(obj, list):
                    for it in obj:
                        res = walk(it)
                        if res:
                            return res
                return None
            ts = walk(jd)
            if ts:
                return ts
        except Exception:
            pass
import sys
from datetime import datetime

# Selenium imports
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except Exception:
    SELENIUM_AVAILABLE = False

# Usage: created_time_test.py <url>
REEL_URL = None
if len(sys.argv) > 1:
    REEL_URL = sys.argv[1]


def main():
    if not REEL_URL:
        print('Usage: created_time_test.py <instagram_reel_url>')
        return
    if not SELENIUM_AVAILABLE:
        print('Selenium or webdriver-manager not available in the environment.')
        print('Install them with: pip install selenium webdriver-manager')
        return

    print('REEL URL:', REEL_URL)
    try:
        options = webdriver.ChromeOptions()
        # visible browser by default; add headless if desired later
        options.add_argument('--no-sandbox')
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.get(REEL_URL)

        # wait for a <time> element to appear
        try:
            el = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, 'time'))
            )
        except Exception:
            el = None

        page_html = driver.page_source
        # save rendered page for inspection
        try:
            with open('storage/v3/created_time_page.html', 'w', encoding='utf-8') as fh:
                fh.write(page_html)
            print('Saved rendered page to storage/v3/created_time_page.html')
        except Exception as e:
            print('Failed to save rendered page:', type(e).__name__, e)

        if el:
            time_attr = el.get_attribute('datetime')
            if time_attr:
                print('Datetime extrait:', time_attr)
                dt = datetime.fromisoformat(time_attr.replace('Z', '+00:00'))
                print('Date de création:', dt.strftime('%Y-%m-%d %H:%M:%S UTC'))
                driver.quit()
                return

        # if no <time> found or no datetime attribute
        print('Aucun <time datetime> trouvé dans le DOM rendu.')
        driver.quit()
    except Exception as e:
        print('Erreur lors de l\'extraction via Selenium:', type(e).__name__, e)


if __name__ == '__main__':
    main()
                # dig for graphql.shortcode_media.taken_at_timestamp


def get_reel_created_timestamp(url: str, *, use_selenium: bool = True, timeout: float = 10.0) -> int | None:
    """Return the Unix timestamp (seconds since epoch) when the reel was created, or None.

    Strategy:
    - Try a plain HTTP GET and extract known JSON patterns from the HTML.
    - If that fails and Selenium is available and allowed, render the page and try again.
    """
    if not url:
        return None

    headers = {"User-Agent": "snap-bot/1.0 (+https://example)"}
    try:
        print(f"[CTT] HTTP fetch url={url} timeout={timeout}", flush=True)
        r = requests.get(url, headers=headers, timeout=float(timeout))
        print(f"[CTT] HTTP status={getattr(r, 'status_code', None)} len={len(getattr(r, 'text', '') or '')}", flush=True)
        if r.status_code == 200:
            html = r.text
            ts = find_timestamp_in_html(html)
            print(f"[CTT] find_timestamp_in_html -> {ts}", flush=True)
            if isinstance(ts, (int, float)):
                return int(ts)
    except Exception as e:
        print(f"[CTT] HTTP fetch failed: {type(e).__name__}: {e}", flush=True)
        # network or parsing error; fallthrough to selenium if allowed
        pass

    if use_selenium and SELENIUM_AVAILABLE:
        try:
            options = webdriver.ChromeOptions()
            options.add_argument('--no-sandbox')
            # headless by default for programmatic use
            options.add_argument('--headless=new')
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            try:
                print(f"[CTT] Selenium fetching url={url}", flush=True)
                driver.get(url)
                # wait briefly for dynamic content to load
                try:
                    WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.TAG_NAME, 'time')))
                except Exception:
                    pass
                page_html = driver.page_source
                print(f"[CTT] Selenium page len={len(page_html)}", flush=True)
                # first try to parse <time datetime>
                try:
                    el = driver.find_element(By.TAG_NAME, 'time')
                    dt = el.get_attribute('datetime')
                    print(f"[CTT] Selenium <time> datetime={dt}", flush=True)
                    if dt:
                        try:
                            d = datetime.fromisoformat(dt.replace('Z', '+00:00'))
                            return int(d.timestamp())
                        except Exception as e:
                            print(f"[CTT] datetime parse failed: {type(e).__name__}:{e}", flush=True)
                            pass
                except Exception:
                    pass

                ts = find_timestamp_in_html(page_html)
                print(f"[CTT] Selenium find_timestamp_in_html -> {ts}", flush=True)
                if isinstance(ts, (int, float)):
                    return int(ts)
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
        except Exception:
            pass

    return None


def get_reel_age_seconds(url: str, *, use_selenium: bool = True, timeout: float = 10.0) -> int | None:
    """Return age in seconds (now - created_ts) or None if unknown."""
    try:
        ts = get_reel_created_timestamp(url, use_selenium=use_selenium, timeout=timeout)
        if ts is None:
            return None
        now_s = int(__import__('time').time())
        age = max(0, int(now_s - int(ts)))
        return age
    except Exception:
        return None
