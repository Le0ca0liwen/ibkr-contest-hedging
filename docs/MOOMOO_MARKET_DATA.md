> **Paper execution integration:** The collector now streams quote/book events and reads only US quote entitlements plus security metadata. Data can be used by the IBKR paper executor only after all live-quality checks pass: verified LV permissions, matching derivative metadata, at least two subsequent book pushes in the current connection, fresh provider price and book receipt, and regular trading hours. The identical provider feeds optimization, risk, margin-preview rechecks, limit repricing and execution. Moomoo has no trade context. Old diagnostic-only descriptions below are superseded. US book exchange timestamps are unavailable, so both push receipt and a fresh independent provider price are required; sparse quotes may block trading.

# Moomoo market data only

Moomoo is a candidate replacement quote source, not a replacement broker.
IBKR remains the only execution adapter and source of positions, NAV, margin,
fills, and official statements. No moomoo order, account, or trade context is
created by this integration. Never unlock moomoo trading or provide a trading
password to this application. Do not install the broad trading-capable AI/MCP
integration for this task.

## Verified public coverage; account-specific coverage still unverified

- US stocks/ETFs: free during promotion, Nasdaq Basic + TotalView + NYSE ArcaBook.
  This is not a guarantee of consolidated best bid/ask across every venue.
- US options: Level 1 free for accounts meeting the documented threshold
  (positive total assets or US positions); otherwise an OPRA quote subscription
  may be necessary. App permissions do not establish API permissions.
- SDK: moomoo-api 10.11.7108, corresponding OpenD minimum version 10.11.7108.
  Use a separate Python environment to isolate SDK dependencies from trading.
- These policies can change. Verify entitlements in the logged-in OpenD account;
  no subscription or deposit is purchased automatically.

Sources inspected October 6, 2026:

- https://openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html
- https://openapi.moomoo.com/moomoo-api-doc/en/qa/quote.html
- https://www.moomoo.com/download/OpenAPI
- https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-stock-quote.html
- https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-order-book.html

## Operator setup

1. Download **Moomoo OpenD for Mac** from the official download page. OpenD is
   distinct from the ordinary moomoo desktop app. Use the compatible version.
2. Log in yourself and complete any API questionnaire/agreements. Keep your
   password and verification codes out of this repository and chat.
3. Bind OpenD to localhost (127.0.0.1), normally port 11111. Keep trade unlocking
   disabled. There is no reason to enable trading for this diagnostic.
4. Check API rights for US stocks and US options/OPRA. Do not assume that quotes
   visible in the app prove that the API has the same rights.
5. Run the quote-only probe during the regular options session. For this practice:

```sh
.runtime/moomoo-env/bin/python ops/probe_moomoo.py --code US.BA --code US.BA261016C195000 --code US.BA261016P195000
```

The option codes are diagnostic candidates. Before controller integration,
verify them against provider chain metadata and IBKR-qualified contracts:
underlying, expiry, strike, right, currency, multiplier, and adjusted status.
Future rounds supply their own codes; the probe contains no contest dates.

The report preserves quote timestamps, top-of-book sizes, raw Greeks and IV;
IV is reported by moomoo in percent. It does not label pulled quotes as fresh
live data merely because the request succeeded. SDK installation uses the
separate ignored `.runtime/moomoo-env` environment; its dependency lock is
`requirements.moomoo.lock`.

## Before switching the controller

### First connection test: October 6, 2026, 6:43 p.m. Eastern

The quote-only probe connected successfully and subscribed to BA stock and both
mandatory October 16 $195 options. Both options returned bid/ask books, IV,
Greeks, and a 100-share multiplier. Raw results are stored locally in
`reports/moomoo-market-data-probe.json`. No trade context was created and no
orders were submitted. The test occurred after the regular options session:
option quote times were around 3:55–3:59 p.m.; US book receive timestamps were
empty. This verifies data access, not intraday live freshness or latency.
Execution remains paused; the provider is still a standalone diagnostic.

The standalone diagnostic and persistent 30-second collector are implemented.
The service supervises the isolated quote-only worker when the local code list
exists, reads its atomic archive into the dashboard and monitoring risk engine,
and displays all available quotes under Positions & risk. Raw records are saved
in daily-rotated data/moomoo/raw-YYYYMMDD.jsonl files (capped at 750 MB/day).
*Superseded (Oct 2026):* with `market_data_provider: moomoo`, verified live
Moomoo quotes feed the optimizer, risk gate and IBKR execution; see the banner
above and the contest checklist in CONTEST_RESTRICTIONS.md.

Before execution integration, first demonstrate rights and two-sided quotes for
BA and all mandatory/candidate options. Then implement streaming quote/book
callbacks off the IBKR event loop, explicit entitlement provenance, separate
provider/receipt timestamps, local Greek recalculation with validated units,
contract crosswalks, stale-data/reconnect gates, and archive provenance.

Pulling cached books cannot demonstrate freshness; documented book receive
timestamps have limited coverage. Assess US timestamp availability and push
semantics before permitting execution. Compare prices/spreads and disconnect
behavior in shadow mode; report measured latency and missing coverage rather
than claiming an optimized score. With a small BA universe, streaming should
avoid repeated HTTP/pull overhead, but actual latency is not yet measured.

No moomoo trades are permitted at any stage. The instructor's revised score
formula also remains pending, so the existing paper execution stays paused.
