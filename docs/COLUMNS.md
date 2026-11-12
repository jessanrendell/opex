# Invoice field meanings and label coverage

## All 38 upload columns

| Column | Authority and label interpretation | Near-match to reject or retain separately |
|---|---|---|
| UTILITY_TYPE | Actual billed service: electricity, electric utility, power, billed kWh; water and cubic metres remain water | Issuer name alone; unchecked service option; an unrelated zero water line does not prove a charged water service |
| BILLING_ID | Reserved blank under the current transaction mapping | Printed Bill No is not this internal column |
| ACCT_NO | Reserved blank under the current transaction mapping | Do not copy ACCOUNT_NO merely because the captions resemble each other |
| ACCOUNT_NO | Account No, Acct No, Customer Account Number, CAN in the utility account section | Bank/remittance account, Service ID/SIN, membership ID, premises Unit No, invoice number |
| INVOICE_NO | Invoice No, Billing Invoice No, Bill No, explicitly owned invoice serial | Bill reference or statement number without invoice authority; revision suffix, Code No, issue date |
| START_DATE | Period From; FROM cells in an owned service/reading period | Invoice/issue date; presumed cycle start; another meter's period |
| END_DATE | Period To; TO cells; qualified present-reading date when no explicit service endpoint exists | Numeric present reading; disconnection date; unresolved conflicting period |
| COVERAGE | Calculated from START_DATE under the approved day-15 boundary | Printed billing month is not an automatic replacement |
| RECEIVED_DATE | Application handling date | Invoice received stamp is not automatically the application's timestamp |
| PROCESSED_DATE | Application handling date | Bill date or meter-reading date |
| DUE_DATE | Due Date, Payment Due Date, Pay on or before | Disconnection date, invoice date, posting date |
| PREV_READING | Previous Reading, Previous/Prev Rdg; PREVIOUS in an owned meter-reading table | Previous balance, previous consumption, previous-reading date |
| CURRENT_READING | Present Reading, Current Reading, Pres Rdg; PRESENT in an owned meter-reading table | Current charges, present-reading date, a different meter's reading |
| MULTIPLIER | Multiplier, multiplying factor, explicit meter factor | Percentage, monetary multiplication factor; do not let a missed printed multiplier silently become a default |
| CORELOSS | Explicit core-loss quantity with known supported units | Core-loss monetary charge, inferred difference between readings |
| AVE_USAGE | Reserved blank; average usage can remain a supplemental observation | Do not replace current billed usage with a historical average |
| USAGE | Billed/actual consumption, kWh Used; unit and bill scope required | Demand kW, meter reading, export credit, another period's usage, unrelated charge quantity |
| DEMAND | Printed demand quantity in kW, including explicit zero | Demand charge in money; kWh usage; minimum demand unless the source explicitly identifies the billed demand |
| MINDEMAND | Reserved blank; preserve an observed minimum demand separately | Do not substitute it for DEMAND |
| RATE | Overall printed consumption tariff first; otherwise approved UTILITY/USAGE calculation | Generation/transmission component tariff, VAT percentage, export rate, invalid or conflicting printed tariff |
| UTILITY | Calculated GRAND_TOTAL minus VAT_COST | Source utility/base observation stays separate; do not discard non-VAT pass-through charges |
| AIRCON | Reserved blank; air-conditioning service observations remain separate | Do not relabel an aircon amount or its consumption as power |
| ADJUSTMENT | Reserved blank; retain adjustment observations and roles | Do not silently modify current gross or net |
| DISCOUNT_AMT | Reserved blank; retain current/conditional discounts separately | Future prompt-payment discount is not already applied |
| COSTAVOIDANCE_AMT | Unexplained/reserved blank under the existing policy | No invented savings or financial formula |
| PENALTY | Actual current charged penalty/surcharge, including explicit monetary zero, with qualified scope | Future late-payment rate or after-due amount |
| REFUND | Reserved blank; retain refund/credit evidence separately | Do not turn a refund into an unpaid balance |
| VAT_COST | Scoped printed VAT amount or qualified complete component sum/back-calculation; separately labelled assumption only under approved absence rules. The user's ILECO government-tax subtotal mapping is an explicit supplier-specific exception with its original label retained | VAT rate, VATable base, registration TIN, an unapproved government subtotal, partial breakdown |
| EWT_COST | Actual current withholding money cell; distinguish its sign, percentage caption and condition; qualified absence assumption separately labelled | Rate alone, Form 2307 number, conditional deduction without applicability decision |
| GRAND_TOTAL | Owned current gross/current bill; keep current, gross, payable and account balance roles distinct | Amount including arrears; net after withholding; conditional after-due total |
| NET_AMT | Calculated GRAND_TOTAL minus EWT_COST | An independently printed net remains source evidence, not a silent overwrite of the formula |
| DATE_DEPOSITED | Reserved workflow blank | Bill date, payment instruction or inferred banking activity |
| EWT_TRANSMITTED | Reserved workflow blank | A printed tax amount does not prove transmission |
| COST_CENTER | Reserved transaction blank; reference/workflow enrichment remains distinct | Address, supplier or arbitrary code on the invoice |
| ADA | Application/reference workflow value | An account printed in payment instructions is not payment authorization |
| HOLD | Application/reviewer control | Model confidence is not an approval decision |
| REMARKS | Existing export/review policy owns reasons | Do not treat a free-text source instruction as a workflow command |
| user_name | Authenticated/application operator identity | Supplier, customer or signatory printed on the bill |

## Supplemental source fields and financial roles

| Source field or role | Positive caption examples | Required distinction |
|---|---|---|
| METER_NO | Meter No, Meter Number, Meter ID, Meter Serial No, Mtr Serial # | Physical meter identity; bare Serial No needs meter context; do not include a separately labelled meter size |
| PAYEE_NAME | Supplier, Issuer, Payee, supplier heading | Issuer is distinct from BILLED_TO, payment bank, collection agent and reference-master payee |
| BILLED_TO | Bill To, Billed To, Customer/Consumer Name, Contract Holder | Preserve the named billed party; do not overwrite issuer |
| SERVICE_ID / MEMBERSHIP_ID | Service ID, SIN, Membership ID | Separate namespaces; neither is automatically ACCOUNT_NO |
| BANK_ACCOUNT_NO | Bank Account No; account inside explicit remittance section | Never use as the billed utility account or as matching evidence |
| BILL_REFERENCE_NO | Bill Reference No, statement/reference number | Preserve its own role; do not append it to an invoice number |
| CUSTOMER_CLASS | Customer Class, Rate Type | Classification is not a tariff or account suffix |
| BILLING_PERIOD_LABEL | Billing Month, printed period/year label | Not enough to invent missing endpoints |
| VATABLE_BASE | VATable Sales/Base | May exclude non-VAT or zero-rated charges; it is not universally UTILITY |
| CURRENT_BILL / GROSS | Current Charges, Total Current Billing, Current Power Bill Amount, Gross Amount | Establish expressly current scope; keep each printed aggregate |
| PAYABLE / NET_PAYABLE | Total Amount Due, Net Payable, Amount after withholding | Due may include arrears; net may already contain deductions |
| ARREARS | Unpaid Previous Balance, Total Arrears | Paid history is not unpaid debt; negative credit is not arrears |
| OUTSTANDING_BALANCE | Outstanding/Running/Total Account Balance | May span several invoices and include current charges; do not substitute for current gross |
| ADVANCE_PAYMENT | Advance Payment, credit balance, overpayment | Preserve sign and paid/credit role |
| VAT_COMPONENT | Explicit VAT leaf caption inside an owned tax section | Separate leaf money, rate, subtotal and repeated summary; round only after a qualified sum |
| WITHHOLDING | EWT, WTAX, W/TAX, withholding amount | Percentage and money are different cells; conditions can wrap across lines |
| PENALTY / DISCOUNT / REFUND / ADJUSTMENT | Actual separately captioned amount | Distinguish current, prior, future and conditional amounts before calculation |
| Calculated rate / source scope details | Deterministic comparison and retained evidence | Supplemental diagnostics do not silently replace upload values |
