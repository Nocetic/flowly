# Sourcing Financials From SEC Filings

**Read this only when** the template needs public-company data taken straight from regulatory filings (10-K, 10-Q). If the data is handed to you or comes from another source, you can ignore this file.

---

Pulling historicals from a company's own filings is the most defensible source you have. The flow below takes you from "which filing" to "data sitting in the model."

## 1 · Find the filing

- Search SEC EDGAR: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=[TICKER]&type=10-K`
- Swap `type=10-K` for `type=10-Q` when you need a quarter rather than a full year.

## 2 · Pin down the reporting currency

Don't assume dollars. Confirm the currency before extracting anything:
- the cover page and statement headers (e.g. "in thousands of U.S. dollars");
- Note 1, the summary of significant accounting policies.

| What you see | Currency |
|---|---|
| $, USD | US Dollar |
| €, EUR | Euro |
| £, GBP | British Pound |
| ¥, JPY | Japanese Yen |
| ¥, CNY, RMB | Chinese Yuan |
| CHF | Swiss Franc |
| CAD, C$ | Canadian Dollar |

Set the model currency to match the filing and note it on the Assumptions tab.

## 3 · Open the financial statements

- **10-K:** the statements live under **Item 8**. **10-Q:** under **Item 1**.
- The four things you want:
  - Consolidated Statements of Operations (the Income Statement),
  - Consolidated Balance Sheets,
  - Consolidated Statements of Cash Flows,
  - the Notes (for schedule-level detail).

## 4 · Map filing lines to model lines

**Income Statement** — from the Statements of Operations

| Filing line | Model line |
|---|---|
| Net revenues / Net sales | Revenue |
| Cost of goods sold | COGS |
| Selling, general & administrative | SG&A |
| Depreciation and amortization | D&A |
| Interest expense, net | Interest Expense |
| Income tax expense | Taxes |
| Net income | Net Income |

**Balance Sheet** — from the Consolidated Balance Sheets

| Filing line | Model line |
|---|---|
| Cash and cash equivalents | Cash |
| Accounts receivable, net | AR |
| Inventories | Inventory |
| Property, plant & equipment, net | PP&E (Net) |
| Total assets | Total Assets |
| Accounts payable | AP |
| Short-term debt / current portion of LT debt | Current Debt |
| Long-term debt | LT Debt |
| Retained earnings | Retained Earnings |
| Total stockholders' equity | Total Equity |

**Cash Flow Statement** — from the Statements of Cash Flows

| Filing line | Model line |
|---|---|
| Net income | Net Income |
| Depreciation and amortization | D&A |
| Changes in accounts receivable | ΔAR |
| Changes in inventories | ΔInventory |
| Changes in accounts payable | ΔAP |
| Capital expenditures | CapEx |
| Proceeds from issuance of common stock | Equity Issuance |
| Proceeds from / repayments of debt | Debt activity |
| Dividends paid | Dividends |

## 5 · Mine the notes for schedule detail

The face of the statements rarely gives you enough to build the schedules. Go to the Notes:
- **Debt note** → maturity ladder, interest rates, covenants.
- **PP&E note** → gross PP&E, accumulated depreciation, useful lives.
- **Revenue note** → segment and geographic splits.
- **Lease note** → operating vs. finance lease obligations.

## 6 · Get enough history

Aim for at least three historical years:
- a 10-K gives three years of IS and CF but only two years of BS;
- for the third BS year, pull from the prior year's 10-K;
- use 10-Qs when you need quarterly granularity.

## Pre-flight checklist

- [ ] Reporting currency and scale (thousands / millions) identified
- [ ] 3 years of historical Income Statement
- [ ] 3 years of historical Cash Flow Statement
- [ ] 3 years of historical Balance Sheet
- [ ] IS Net Income = CF opening Net Income, each year
- [ ] BS Cash = CF Ending Cash, each year
- [ ] Debt maturity ladder pulled from the notes
- [ ] D&A detail or useful-life assumptions captured
- [ ] Non-recurring / one-time items flagged for normalization

## Filing quirks and how to deal with them

| Quirk | What to do |
|---|---|
| D&A buried inside COGS / SG&A | Take the D&A figure from the Cash Flow Statement |
| Large "Other" line items | Check the notes for the breakdown |
| Restatements | Use the restated numbers and note it in Assumptions |
| Fiscal year ≠ calendar year | Label with the fiscal year-end (e.g. FYE Jan 2025) |
| Non-USD reporting | Switch the model currency to match the filing |
