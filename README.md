# Prime Ledger

Prime Ledger is a multi-tenant business operations and accounting platform for small retail shops, wholesalers, and service businesses in Bangladesh. It is designed for owners and business administrators who need a clear daily operating picture without the complexity of a large ERP.

## Product direction

The product should make these daily questions easy to answer:

- What was sold, purchased, received, paid, or returned today?
- Which products are available, moving slowly, or below reorder level?
- Which customers owe money and which supplier balances are due?
- Is the business cash-positive and profitable for the selected period?
- Which employee actions require review?

The first release is single-location per business, BDT-first, Bangladesh timezone, and English/Bangla-ready. VAT/GST is optional and must not be assumed in calculations when disabled.

## Current foundation

- Shared-schema tenancy through `Business` ownership on domain records
- Super Admin-created businesses with Business Admin and Employee memberships
- Custom business roles with menu/feature permission keys
- Customers, suppliers, products, services, SKU/barcode and stock movements
- Accounting accounts, fiscal periods, balanced journal entries and vouchers
- Super Admin-managed chart-of-accounts templates with a seeded 43-account default
- Draft and posted sales/purchases with product or service line items
- Fixed-amount or percentage sale discounts with net invoice and journal totals
- Atomic sales/purchase posting into journals, vouchers, stock movements, weighted-average cost of goods sold, and money receipts for immediately paid sales
- Filtered sales/purchase registers and deterministic CSV/PDF exports
- Tenant-scoped contact, posted invoice, money receipt, and arbitrary date-range account activity reports with CSV/PDF exports
- Printable sales invoices and money receipts linked to immutable posted records
- English/Bangla-ready business locale, BDT default currency and Bangladesh timezone
- PostgreSQL-first runtime configuration through `DATABASE_URL`
- Clean Architecture boundary packages for domain contracts, application services, and Django ORM repositories

## Application architecture

- **Backend:** Django modular monolith with Django REST Framework, organized using Clean Architecture
- **Database:** PostgreSQL is the application database. SQLite is used only by the test command when no test database is configured.
- **Frontend:** Responsive server-rendered Django interface; the versioned REST API remains a typed-client boundary for a later React delivery adapter
- **Documents:** ReportLab-based server-side PDF generation; CSV export for tabular reports
- **API shape:** `/api/v1/...`, explicit business context, pagination, filtering, stable error responses
- **Deployment:** Docker Compose for the application and PostgreSQL; production can move the same containers to a managed platform

### Dependency direction

```text
Delivery adapters (Django views, DRF, React)
			-> Application use cases and policies
			-> Domain contracts, value rules, and business invariants
			<- Infrastructure adapters (Django ORM, PostgreSQL, PDF, external services)
```

The domain layer must not import Django, DRF, ReportLab, or database-specific code. The application layer coordinates use cases through ports/protocols. Infrastructure implements those ports with the Django ORM and PostgreSQL. Delivery code translates HTTP/UI input into application commands and renders results; it does not own accounting or tenant rules.

The existing Django models are the persistence adapter for the current foundation. New behavior should be added through application services and repository interfaces; existing delivery code can be migrated incrementally without a disruptive rewrite. Keep the backend and frontend separately deployable, but avoid premature microservices.

## UI and visual direction

The interface should feel calm, precise, and executive-friendly: dense enough for repeated work, but never visually noisy.

- Use a restrained palette: warm white surfaces, charcoal text, muted slate borders, and one controlled teal accent for primary actions and positive states.
- Use semantic status colors sparingly: red for blocking risk, amber for attention, green for confirmed success. Never use color as the only status signal.
- Prefer a modern sans-serif with strong numerals and Bangla fallback support. Use typography hierarchy instead of decorative color or oversized headings.
- Use a persistent compact sidebar, a clear business switcher, a period/date filter, and a small number of high-value dashboard metrics.
- Use tables for operational data, with sticky headers, predictable column alignment, server-side filtering, and responsive horizontal scrolling on small screens.
- Use cards only for summary metrics, individual records, and dialogs. Do not put cards inside cards or turn every page section into a floating panel.
- Use familiar icons for actions, with tooltips for unfamiliar icons. Keep text labels for important business actions such as Post, Reverse, Receive Payment, and Export PDF.
- Support keyboard navigation, visible focus states, readable contrast, loading states, empty states, and confirmation for irreversible actions.
- Show Bengali labels/documents only when the business locale requests them; keep internal identifiers, API fields, and permission keys in English.
- Do not use gradients, decorative blobs, excessive rounded controls, animated charts, or a dark-mode-first visual language.

Recommended primary navigation: Overview, Sales, Purchases, Inventory, Customers, Suppliers, Accounting, Reports, and Settings. Hide inaccessible menu items rather than displaying dead ends, while preserving direct-URL authorization on the backend.

## Setup

### Docker (recommended)

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The web application is available at `http://127.0.0.1:8000/` and PostgreSQL data is persisted in the `postgres_data` Docker volume. Create the platform Super Admin in another terminal:

```powershell
docker compose exec web python manage.py createsuperuser
```

Sign in to Prime Ledger and open **Businesses** in the Administration navigation. From there the Super Admin can create a tenant, edit its identity/settings, assign the first Business Admin, and follow the onboarding checklist for fiscal periods, contacts, and catalog items. A new business automatically receives the active default chart of accounts. These routes are protected by backend superuser checks and are not available to ordinary business members.

The Super Admin maintains reusable charts under **Administration → Account templates**. Template changes affect future applications only; each application copies accounts into the tenant's own ledger. For a business created before this feature, select the business, open **Accounting → Chart of accounts**, and choose **Apply template**.

Stop the stack with `docker compose down`. Do not use `docker compose down -v` unless the local database may be discarded.

### Local development with PostgreSQL

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:DATABASE_URL = "postgresql://primeledger:primeledger@localhost:5432/primeledger"
python manage.py makemigrations core accounting operations
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/admin/` for the initial management surface and `http://127.0.0.1:8000/api/health/` for the health endpoint.

## MVP scope

### Implemented

- Tenant setup by Super Admin
- Business Admin, Employee, and custom role permissions
- Products, services, controlled inventory units, SKU/barcode, pricing, and reorder levels
- Customer and supplier records with opening balances
- Draft and posted sales and purchases with split product/service lines
- Sale-level fixed or percentage discounts shown on drafts, invoices, registers, and exports
- Stock inflow/outflow and movement history
- Double-entry journal posting with editable drafts and locked fiscal periods
- A default small-business chart managed by the Super Admin and copied into each new tenant
- Stable posting roles for receivables, payables, inventory, revenue, cash/bank, capital, and cost of goods sold
- Moving weighted-average inventory valuation and automatic COGS/Inventory journal lines on product sales
- Operational dashboard plus stock, sales, and purchase registers
- Sales/purchase document PDF, register PDF, and CSV export
- Customer/supplier directory, posted invoice register, and receipt-voucher register
- Individual printable sales invoices and money receipts
- Automatic immutable money receipts when a sale is posted to an account mapped as Cash, Bank, or Mobile Financial Services
- Partial and final customer-payment allocation from posted credit invoices, with automatic journal, receipt voucher, and money receipt generation
- Partial and final supplier-payment allocation from posted Accounts Payable purchases, with automatic journal and printable payment voucher generation
- Dedicated **Payments** workspace for open customer receivables, supplier payables, and recent settlement activity
- Invoice-level receivable/payable set-off for contacts classified as Customer & Supplier, with multi-document allocation, automatic contra journal/voucher, and a printable statement
- Dedicated operating-expense workflow for rent, employee salary, utilities, professional fees, and contingent expenditure, with paid-now/pay-later posting, partial payable settlement, expense/payment vouchers, and CSV/PDF registers

Sales and purchase posting is transactional and idempotent. A successful post creates the balanced journal entry, immutable voucher, and applicable stock movements together. An immediately paid sale—identified by selecting a debit account mapped as Cash, Bank, or Mobile Financial Services—also creates one immutable money receipt with the net sale amount. A credit sale posted to Accounts Receivable exposes **Receive payment**: each partial or final collection is allocated to that invoice, debits the selected funds account, credits Accounts Receivable, and creates an immutable receipt voucher and money receipt atomically. A purchase posted to Accounts Payable exposes **Pay supplier**: each allocation debits Accounts Payable, credits the selected Cash, Bank, or Mobile Financial Services account, and creates an immutable printable payment voucher. Overpayments, future dates, locked periods, cross-tenant accounts, and duplicate request-key conflicts are rejected before side effects. Sales may apply one fixed-amount or percentage discount; receivable, revenue, voucher, invoice, receipt, and report totals use the net amount after discount. Product sales debit Cost of Goods Sold and credit Inventory using the moving weighted-average cost of stock on hand, so a commercial discount never changes inventory cost; service lines do not affect stock or COGS. A failed validation—including an excessive discount, insufficient sale stock, missing required posting roles, or a locked period—rolls back every side effect. Draft sales and purchases in open periods can be deleted after explicit confirmation; posted or locked-period documents cannot be deleted, and a deleted draft's automatic number is never reused.

When the same contact is classified as **Customer & Supplier** and has both open balances, use **Payments → Mutual balances** to allocate one or more sales against one or more purchases. Posting debits Accounts Payable and credits Accounts Receivable by equal amounts, creates an immutable contra voucher and printable statement, and updates both documents without moving cash. Unbalanced or excessive allocations, future dates, locked periods, and cross-tenant documents are rejected atomically.

The **Expenses** workspace recognizes operating costs directly against an active expense account. **Paid now** debits the expense and credits Cash, Bank, or Mobile Financial Services. **Pay later** debits the expense and credits Accounts Payable; subsequent partial or final payments debit that liability and credit the selected funds account without recognizing the expense twice. Employees can be maintained as Employee contacts, while landlords and other vendors can remain suppliers. Every posting creates an immutable journal and voucher, respects period locks and tenant boundaries, and appears in the filtered expense register and furnished PDF/CSV exports.

### Remaining MVP work

- Sales and purchase returns
- Expenses and one payment split across multiple invoices
- Controlled reversal workflow for posted transactions
- Cashflow, profit-and-loss, and balance-sheet reports
- Optional VAT/GST configuration and calculations

### Later phase

- Email and WhatsApp document delivery
- Attachments and receipt storage
- Batch/lot and expiry automation
- Multiple branches, warehouses, and cash counters
- Full POS hardware polish and offline mode
- Advanced tax configuration and external integrations

## Non-negotiable data rules

1. Every business-owned query must be scoped by the authenticated business context. Never trust a business ID supplied only by the client.
2. A user may access a business only through an active membership or Super Admin status.
3. Posted journal entries must balance. Drafts may be edited; posted entries are corrected through controlled reversal or period rules.
4. Locked fiscal periods reject new postings and edits to posted financial records.
5. Voucher, journal, and stock operations should be transactional and idempotent where retries are possible.
6. Financial amounts use `Decimal`, never floating point. Display currency and dates according to the business settings.
7. Reports must state their date range, business, currency, and whether tax is included.
8. PostgreSQL is the production and container database. Do not add SQLite-specific behavior, SQL, or assumptions.

## AI implementation workflow

Before editing, inspect the nearest owning module and state a local hypothesis about the behavior being changed. Make the smallest coherent change, then run the narrowest relevant check before exploring unrelated code.

For feature work, follow this order: data model and migration, service/domain rule, API serializer/view, permission test, UI state and form, report/document output, then documentation. Prefer existing Django and frontend patterns over new abstractions.

Every new user-facing workflow should include validation, permission denial behavior, loading state, empty state, success feedback, and a focused test. Do not silently broaden scope into unrelated refactors.

## Verification

```powershell
python manage.py check
python manage.py makemigrations --check
python manage.py test
```

For frontend work, also run the project formatter, typecheck, unit tests, and a production build. For report changes, test both a populated report and an empty date range. For Docker changes, run `docker compose config` and build the web image.

## Implemented API foundation

The existing contacts, catalog, inventory units, stock, accounts, fiscal periods, journals, and vouchers are available under `/api/v1/`. Every request must authenticate and provide the selected tenant using the `X-Business-ID` header (or `business` query parameter). An inaccessible tenant produces the same not-found response as an unknown tenant.

Inventory units are controlled master data. The Super Admin maintains the default catalog under **Administration → Default units**. The initial catalog contains piece, kilogram, gram, litre, millilitre, metre, centimetre, box, pack, dozen, pair, hour, and day; each entry can be renamed, deactivated, or supplemented by the Super Admin. Each business can optionally inherit those read-only defaults and can create its own units under **Inventory units**. Products reference an available unit rather than storing open text. The product API preserves a concise code contract such as `"unit": "kilogram"`; unknown, inactive, or cross-tenant unit codes are rejected. A business cannot disable default inheritance while one of its products still references a default unit.

### Default chart of accounts

The seeded **Small Business — Trading & Services** template contains 43 accounts across assets, liabilities, equity, income, cost of sales, and operating expenses. The four VAT/TDS control accounts are copied inactive so tax accounting is never enabled by assumption. A Super Admin can create templates, choose the active default, and add or edit template accounts under **Administration → Account templates**.

Each new business receives a private copy of the current default template. Existing businesses can apply any active template from **Accounting → Chart of accounts → Apply template**. Application is idempotent: compatible account codes and existing posting-role mappings are reused, missing accounts are created, and any account-type or posting-role conflict rolls back the entire application. Editing a template later does not silently rewrite a business ledger.

Posting roles are stable machine-readable mappings rather than hard-coded account numbers. The default template maps Cash, Bank, Mobile Financial Services, Accounts Receivable, Inventory, Accounts Payable, Owner's Capital, Retained Earnings, Product Sales, Service Income, and Cost of Goods Sold. A Business Admin can add business-specific accounts and can assign an unused compatible posting role where automation requires it.

Business Admins receive all permissions for their business. Employee roles use these explicit permission keys:

- `contacts.view`, `contacts.manage`
- `inventory.view`, `inventory.manage`
- `accounting.view`, `accounting.manage`, `accounting.post`
- `sales.view`, `sales.manage`, `sales.post`
- `purchases.view`, `purchases.manage`, `purchases.post`

Journal entries are created as drafts with nested lines and posted through `POST /api/v1/journal-entries/{id}/post/`. Sales and purchases use `/api/v1/sales/` and `/api/v1/purchases/`; each supports nested lines and a `POST /{id}/post/` action. Sales accept `discount_type` as `none`, `fixed`, or `percentage` plus a decimal `discount_value`; `subtotal`, `discount_amount`, and `total` are calculated read-only fields. Purchases reject discount values. Posted documents expose `paid_amount`, `balance_due`, and `payment_status`; sales include immutable `payments`, while purchases include immutable `supplier_payments`. Immediate sales expose `money_receipt_number`. Credit invoices accept `POST /api/v1/sales/{id}/receive-payment/`, and payable purchases accept `POST /api/v1/purchases/{id}/pay-supplier/`; both accept `payment_account`, `amount`, `payment_date`, optional `notes`, and an optional reusable `idempotency_key`. Posting is atomic and idempotent, rejects invalid stock or locked periods, and posted financial history cannot be edited or deleted. Stock movements are append-only through the API. The server-rendered interface uses the same tenant and permission policy.

Sales, purchases, customer receipts, supplier payments, and stock movements receive an immutable tenant-wide automatic number when first saved. The format is `YYNNNNNN`: the transaction year followed by a six-digit sequence, such as `26000001`. These records share the same business/year counter so a number identifies exactly one operational record; each business has an independent counter, and the sequence restarts at `000001` in January. The two-digit year format supports transaction years 2000–2099. Client-supplied numbers are ignored, while the optional stock movement source reference remains available separately. Register screens, search, APIs, CSV exports, PDFs, journals, and vouchers use the generated number.

Balance set-offs are listed, retrieved, and created at `/api/v1/balance-setoffs/`. Creation accepts a `party`, `setoff_date`, equal `sale_allocations` and `purchase_allocations` arrays of `{document_id, amount}`, optional `notes`, and an optional reusable `idempotency_key`.

Expenses are listed, retrieved, and posted at `/api/v1/expenses/`. Creation accepts `expense_date`, `expense_account`, optional `payee`, `settlement` (`paid` or `payable`), an immediate `payment_account` when applicable, `amount`, `description`, optional `external_reference`, and an optional idempotency key. Outstanding expense payables accept `POST /api/v1/expenses/{id}/pay/` with `payment_date`, `payment_account`, `amount`, optional `notes`, and an optional idempotency key.

Returns, multi-invoice payment splitting, advanced financial reports, and the optional React client remain roadmap work rather than claims about this foundation.

## Current user interface

The authenticated Django interface is the operational delivery surface for the implemented foundation. It includes a responsive permission-aware navigation shell, business switching, overview metrics, searchable and paginated contacts/catalog tables, append-only stock movement entry, sales and purchase workflows, and accounting workflows for chart templates, tenant accounts, editable fiscal periods, draft journals, posting, and vouchers. The top-level **Payments** workspace lists open customer receivables and supplier payables with direct **Receive** and **Pay** actions plus recent settlement history. The **Reports** area provides contact directories with opening and reporting-date closing balances, posted invoice and money receipt registers, and a complete account activity/trial balance for any selected date range; each tabular report supports CSV and furnished PDF export. Accounts Receivable collections produce receipt PDFs; Accounts Payable settlements produce printable payment vouchers. Open period boundaries may be changed only when they remain non-overlapping and include every existing entry; locked boundaries remain protected until the period is reopened.

Every select list in the authenticated application is progressively enhanced as a searchable autocomplete combobox. It preserves the native select as the submitted field, supports keyboard and pointer operation, and also applies to line-item selectors added dynamically after page load. If JavaScript is unavailable, the original native select remains usable.

Sales and purchases are available at `/sales/` and `/purchases/`. Users can filter registers by state and date, save editable automatically numbered drafts, add split lines, review posting accounts, confirm posting, follow the resulting journal, and export documents or filtered registers as PDF/CSV. The stock movement register also supports number/source search, date and direction filters, and PDF/CSV export.

The visual system intentionally uses warm neutral surfaces, charcoal typography, quiet borders, and one restrained teal action color. Financial and stock states use semantic color together with explicit text. Posting and fiscal-period state changes require confirmation, and inaccessible workflows are omitted from navigation while direct URLs remain protected by backend permissions.
