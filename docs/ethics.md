# Collection policy

This project studies the behaviour of large retailers. It is not a load test,
and nothing here justifies being a nuisance to the sites it measures.

## Rules the code enforces

| Rule | Where |
|---|---|
| `robots.txt` fetched per host and honoured before every request | `scrapers/base.py::RobotsCache` |
| 4–6 s delay between requests, with jitter | `config/sites.yaml` → `request_delay_seconds` |
| Exponential backoff on HTTP 429 / 503 | `scrapers/base.py::fetch` |
| Max 3 retries, then give up for the day | `max_retries` |
| Images, fonts and video blocked in the browser scraper | `scrapers/browser.py` |
| 6 s delay for archive.org (a free public good) | `config/sites.yaml` → `wayback` |

## Scope limits

**Public listing pages only.** No accounts are created, nothing is logged into,
no cart or checkout flow is touched, and no personal data of any kind is
collected. The dataset contains product names, prices and public page text.

**Category pages preferred over product pages.** One request to a category
listing returns ~40 products with prices. Visiting 40 individual product pages
would be 40× the load for the same information. The panel scrapes detail pages
only for the small set of products followed longitudinally.

**Bounded volume.** The daily crawl is roughly 300–500 requests across all
sites — comparable to a person browsing for an afternoon, spread over hours.

## What is deliberately *not* done

- No CAPTCHA solving or bot-detection evasion. If a site blocks the scraper,
  that is a legitimate answer and the site is dropped, not worked around.
- No IP rotation or proxy pools to disguise traffic volume.
- No scraping of competitor price-tracking services to shortcut data collection.
- No credential use, no authenticated endpoints, no private APIs.

## Handling the findings

The analysis reports **patterns**, not accusations.

- The MRP audit says sellers *disagree*; it cannot say which one is wrong, and
  the language throughout is "disagrees with the consensus", never "is lying".
- Inflation flags are hypotheses until manually reviewed, and precision is
  reported with a confidence interval rather than asserted.
- Legitimate explanations exist for some flags — bundled accessories, regional
  editions, genuine cost changes — and the manual validation pass exists
  specifically to estimate how often they apply.

Raw HTML archives are kept locally and are **not** committed to the repository:
they are large, they are the retailers' copyrighted page content, and the
analysis does not require redistributing them. `.gitignore` excludes `data/raw/`.

## Sites dropped after testing

`pf doctor` was run against the original targets on 2026-08-15. Three of four
refused to serve a scraper, and all three were **dropped rather than worked
around**:

| Site | Response | Decision |
|---|---|---|
| Flipkart | reCAPTCHA challenge page, served even in place of `robots.txt` | Dropped |
| Croma | Akamai HTTP 403 to any non-browser client, including for `robots.txt` | Dropped |
| Nykaa, Ajio | HTTP 403, same posture | Dropped |
| Reliance Digital | Serves normally; `robots.txt` permits these paths | **Retained** |
| Snapdeal | Serves normally; `robots.txt` permits these paths | **Retained** |
| Vijay Sales, Tata Cliq, Poorvika, JioMart | Reachable, but catalogue never renders | Not usable |
| ShopClues | Reachable, but sells only refurbished/feature phones | Not usable |

Getting past the first three would require CAPTCHA solving or browser
fingerprint evasion. Both are ruled out above, and that rule is worth more than
the data would have been. A block is an answer.

Their historical prices remain reachable through the Internet Archive, which
serves crawlers deliberately and is what `scrapers/wayback.py` is for.

## If you are a retailer reading this

The collection respects robots.txt and stays well below any reasonable rate
limit. If you would prefer this project not to include your site, that is a
legitimate request — the site can be removed from `config/targets.yaml` and its
data deleted.
