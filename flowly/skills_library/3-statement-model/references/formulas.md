# Formula Reference

**Default to the formulas below** unless the user specifies something different.

---

## The integrating linkages

These are the joints that turn three separate statements into one model. Each must hold every period.

```
Balance sheet identity:   Assets = Liabilities + Equity
Net income handoff:       IS Net Income  →  top of CF Operations
Cash build:               ΔCash = CFO + CFI + CFF
Cash tie-out:             CF Ending Cash = BS Cash (asset)
Monthly vs. annual cash:  Monthly closing cash = Annual closing cash
Retained earnings:        Prior RE + Net Income − Dividends = Ending RE
Equity raise:             Δ Common Stock/APIC (BS) = Equity Issuance (CFF)
Year-0 equity:            Equity Raised (Year 0) = Beginning Equity (Year 1)
```

## Gross profit — start from net revenue

**Use net revenue, never gross revenue**, as the basis for profitability.

```
Gross Profit = Net Revenue − Cost of Revenue
```

| Term | Meaning |
|---|---|
| Gross Revenue | The top line before any deductions |
| Net Revenue | Gross Revenue − Returns − Allowances − Discounts |
| Cost of Revenue | Direct cost of producing what was sold |
| Gross Profit | Net Revenue − Cost of Revenue |

On most published statements "Net Sales" or simply "Revenue" already means net revenue — that is your starting point. Beginning from gross revenue overstates the real top line and distorts every margin built on top of it.

## Margins

```
Gross Margin %   = Gross Profit  / Net Revenue
EBITDA           = EBIT + D&A          (equivalently, Gross Profit − OpEx)
EBITDA Margin %  = EBITDA       / Net Revenue
EBIT Margin %    = EBIT         / Net Revenue
Net Margin %     = Net Income   / Net Revenue
```

## Credit and leverage

```
Total Debt           = Current Portion of Debt + Long-Term Debt
Net Debt             = Total Debt − Cash
Total Debt / EBITDA  = Total Debt / EBITDA
Net Debt / EBITDA    = Net Debt   / EBITDA
Interest Coverage    = EBITDA / Interest Expense
Net Int Exp % Debt   = Net Interest Expense / Long-Term Debt
Debt / Total Cap     = Total Debt / (Total Debt + Total Equity)
Debt / Equity        = Total Debt / Total Equity
Current Ratio        = Total Current Assets / Total Current Liabilities
Quick Ratio          = (Total Current Assets − Inventory) / Total Current Liabilities
```

## Forecasting operating lines (% of net revenue)

Each forecast operating cost is a percentage of net revenue driven from the Assumptions tab:

```
Cost of Revenue = Net Revenue × (Cost of Revenue %)
S&M             = Net Revenue × (S&M %)
G&A             = Net Revenue × (G&A %)
R&D             = Net Revenue × (R&D %)
SBC             = Net Revenue × (SBC %)
```

## Working-capital schedules

Each account rolls forward from its prior balance, with one line acting as the cash plug. The day-count ratios let you forecast the closing balance from an activity driver.

```
ACCOUNTS RECEIVABLE
  Prior AR
  + Revenue            (from IS)
  − Cash Collections   (plug)
  = Ending AR
  DSO = (AR / Revenue) × 365

INVENTORY
  Prior Inventory
  + Purchases          (plug)
  − COGS               (from IS)
  = Ending Inventory
  DIO = (Inventory / COGS) × 365

ACCOUNTS PAYABLE
  Prior AP
  + Purchases          (from the Inventory roll-forward)
  − Cash Payments      (plug)
  = Ending AP
  DPO = (AP / COGS) × 365

Net Working Capital = AR + Inventory − AP
ΔWC                 = Current NWC − Prior NWC
```

## D&A and PP&E roll-forward

Two parallel roll-forwards — gross asset and accumulated depreciation — net to the carrying value.

```
Beginning PP&E (Gross)
+ CapEx
= Ending PP&E (Gross)

Beginning Accumulated Depreciation
+ Depreciation Expense
= Ending Accumulated Depreciation

PP&E (Net) = Ending Gross PP&E − Ending Accumulated Depreciation
```

## Debt schedule

```
Beginning Debt Balance
+ New Borrowings
− Repayments
= Ending Debt Balance

Interest Expense = Average Debt Balance × Interest Rate
  Use the beginning balance to keep it non-circular,
  or iterate on the average if circular references are enabled.
```

## Retained earnings roll-forward

```
Beginning Retained Earnings
+ Net Income                    (from IS)
+ Stock-Based Compensation      (from IS)
− Dividends
= Ending Retained Earnings
```

## Net operating loss (NOL) schedule

The NOL balance accumulates losses and gets drawn down against future profits, subject to the post-2017 federal cap. It also drives a deferred-tax asset on the balance sheet.

```
ROLL-FORWARD
  Beginning NOL Balance   (Year 1 / formation = 0)
  + NOL Generated         (if EBT < 0 → ABS(EBT), else 0)
  − NOL Utilized          (capped by taxable income and the 80% limit)
  = Ending NOL Balance

STARTING BALANCE
  A newly formed or first-modeled entity opens at 0.
  NOL only grows from realized losses (EBT < 0); it is never assumed into existence.

UTILIZATION
  When EBT > 0:
    NOL Available     = Beginning NOL Balance
    Utilization Cap   = EBT × 80%        (post-2017 federal limit)
    NOL Utilized      = MIN(NOL Available, Utilization Cap)
    Taxable Income    = EBT − NOL Utilized
  When EBT ≤ 0:
    NOL Utilized      = 0
    Taxable Income    = 0
    NOL Generated     = ABS(EBT)

TAX
  Taxes Payable = MAX(0, Taxable Income × Tax Rate)
    Taxes never go negative; a loss builds an NOL asset rather than a refund.

DEFERRED TAX ASSET
  DTA (NOL carryforward) = Ending NOL Balance × Tax Rate
  ΔDTA = Current DTA − Prior DTA
    A rising DTA is a non-cash benefit; a falling DTA is a non-cash expense.
```

## Balance sheet layout

```
ASSETS
  Cash                          (from CF ending cash)
  Accounts Receivable           (from WC)
  Inventory                     (from WC)
  Total Current Assets
  PP&E, Net                     (from D&A schedule)
  Deferred Tax Asset — NOL      (from NOL schedule)
  Total Non-Current Assets
  Total Assets

LIABILITIES
  Accounts Payable              (from WC)
  Current Portion of Debt       (from Debt schedule)
  Total Current Liabilities
  Long-Term Debt                (from Debt schedule)
  Total Liabilities

EQUITY
  Common Stock
  Retained Earnings             (from RE schedule)
  Total Equity

CHECK:  Assets − Liabilities − Equity = 0
```

## Cash flow layout

```
OPERATING (CFO)
  Net Income                    (link: IS)
  + D&A                         (link: D&A schedule)
  + Stock-Based Compensation    (link: IS or Assumptions)
  − ΔDTA                        (link: NOL schedule; a rising DTA uses cash)
  − ΔAR                         (link: WC)
  − ΔInventory                  (link: WC)
  + ΔAP                         (link: WC)
  = CFO

INVESTING (CFI)
  − CapEx                       (link: D&A schedule)
  = CFI

FINANCING (CFF)
  + Debt Issuance               (link: Debt schedule)
  − Debt Repayment              (link: Debt schedule)
  + Equity Issuance             (link: BS Common Stock/APIC)
  − Dividends                   (link: RE schedule)
  = CFF

Net Change in Cash = CFO + CFI + CFF
Beginning Cash
+ Net Change in Cash
= Ending Cash                   (link to: BS Cash)
```

## Income statement layout

```
Net Revenue
  Growth %
(−) Cost of Revenue
  % of Net Revenue
──────────────────
Gross Profit  (= Net Revenue − Cost of Revenue)
  Gross Margin %

(−) S&M     ( % of Net Revenue )
(−) G&A     ( % of Net Revenue )
(−) R&D     ( % of Net Revenue )
(−) D&A
(−) SBC     ( % of Net Revenue )
──────────────────
EBIT
  EBIT Margin %

EBITDA
  EBITDA Margin %

(−) Interest Expense
──────────────────
EBT (pre-tax income)
(−) NOL Utilization   (from NOL schedule; reduces taxable income)
──────────────────
Taxable Income
(−) Taxes   (= Taxable Income × Tax Rate)
──────────────────
Net Income
  Net Margin %
```

## Check formulas

Every check below should evaluate to its target; surface failures on the Checks tab.

```
BS balance:           Assets − Liabilities − Equity                    = 0
Cash tie-out:         BS Cash − CF Ending Cash                         = 0
RE roll-forward:      Prior RE + NI + SBC − Dividends − BS RE          = 0
DTA tie-out:          NOL-schedule DTA − BS DTA                        = 0
Equity-raise tie-out: Δ Common Stock/APIC (BS) − Equity Issuance (CFF) = 0
Year-0 equity:        Equity Raised (Year 0) − Beginning Equity (Y1)   = 0
Monthly vs. annual:   Monthly closing cash − Annual closing cash       = 0
NOL utilization cap:  NOL Utilized ≤ EBT × 80%                         = TRUE (post-2017)
NOL non-negative:     Ending NOL Balance ≥ 0                           = TRUE
NOL starting balance: Beginning NOL (Year 1) = 0                       = TRUE (new entity)
NOL accumulation:     NOL rises only when EBT < 0                      = TRUE
```
