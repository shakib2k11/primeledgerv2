# Prime Ledger Agent Guide

This file is the working contract for AI-assisted changes in this repository.

## Mission

Build a trustworthy, tenant-safe operations and accounting system for small businesses. Optimize for correctness, clarity, maintainability, and fast daily workflows. The product is not a generic analytics dashboard and should not accumulate decorative or speculative features.

## Repository map

- `config/`: Django settings, URL configuration, and application entry points
- `core/domain/`: framework-independent contracts and business concepts
- `core/application/`: use cases and orchestration policies
- `core/infrastructure/`: Django ORM repository adapters
- `core/`: current persistence models, delivery forms/views, and inventory primitives; migrate new behavior toward the Clean Architecture packages
- `accounting/`: accounts, fiscal periods, journals, journal lines, and vouchers
- `operations/`: sales and purchases, line items, posting orchestration, API/UI delivery, and exports
- `Dockerfile`, `docker-compose.yml`, `docker/`: reproducible web and PostgreSQL runtime
- `README.md`: product scope, UI direction, architecture, setup, and verification
- `requirements.txt`: Python runtime dependencies

When adding a feature, place business rules in the domain/application layers rather than in a view, serializer, form, or template. Keep cross-cutting services small and explicit.

## Clean Architecture rules

- The dependency direction is delivery -> application -> domain. Infrastructure implements interfaces required by the inner layers.
- Domain code must remain importable without Django, DRF, ReportLab, or PostgreSQL installed.
- Application services accept ports/protocols and plain values where practical; they should be straightforward to unit test without a database.
- Infrastructure adapters may import Django models, settings, transactions, and PostgreSQL-specific behavior.
- Delivery adapters may import application services and serializers/forms, but must not decide accounting, stock, balance, or tenant policy.
- Do not create a second ORM or duplicate persistence entities. Django ORM is the selected persistence technology; keep its use behind infrastructure boundaries as the codebase grows.
- Introduce new boundaries incrementally. A small adapter around an existing model is preferable to a broad speculative rewrite.

## Product assumptions

- The system is multi-tenant with a shared database schema.
- A `Business` is the tenant boundary. Domain records owned by a business must carry that relationship.
- Super Admin creates businesses. Business Admin manages their business. Employees receive limited, role-based access.
- The default market is Bangladesh: BDT, `Asia/Dhaka`, and `dd-MMM-yyyy` display dates.
- English is the implementation language; UI and documents must remain ready for English/Bangla localization.
- The MVP is single-location per business and does not assume VAT/GST unless configured.
- PostgreSQL is the only runtime database. SQLite is permitted only for the explicit test fallback in `manage.py`.

## Security and accounting rules

- Scope every read and write by authenticated business membership. Client-provided tenant IDs are input, not authority.
- Enforce permissions in the backend even when a menu item is hidden in the UI.
- Do not expose another tenant's existence through object lookup, validation, autocomplete, or report totals.
- Use `Decimal` for money and quantities. Do not use floats for financial calculations.
- Journal entries must have equal total debits and credits before posting.
- Draft vouchers can be edited. Posted records require the repository's reversal/period policy; never delete financial history casually.
- Locked fiscal periods reject edits and postings.
- Use database transactions around voucher posting, journal creation, payment allocation, and stock movement creation.
- Add an audit trail before introducing destructive administrative actions.

## Docker workflow

- `docker compose up --build` starts the web container and PostgreSQL with a health-gated dependency.
- The web entrypoint runs migrations and `collectstatic` before Gunicorn.
- Never bake production credentials or a production secret into `Dockerfile` or source. Compose defaults are development-only; use `.env` locally and replace every placeholder with managed secrets in production.
- Preserve the named PostgreSQL volume during normal shutdown. Destructive volume removal requires explicit user approval.
- Validate container changes with `docker compose config` and an image build before declaring them complete.

## Backend conventions

- Use Django models for persistence and explicit domain/service functions for multi-model operations.
- Keep PostgreSQL as the canonical database and test against PostgreSQL for database-specific behavior.
- Keep API routes under `/api/v1/` and return predictable validation errors.
- Filter, paginate, and order list endpoints server-side.
- Validate that related objects belong to the same business before saving.
- Add migrations with every model change and keep migrations reviewable.
- Prefer the Django admin as an internal bootstrap tool, not as the finished product UI.
- PDF generation must be deterministic, include business identity, report period, currency, and generated date, and handle empty results gracefully.

## Frontend conventions

Use React + TypeScript + Vite when the frontend is introduced. Keep a typed API boundary and route-level authorization. Treat the React app as a delivery adapter: it may manage view state, but server-side application policies remain authoritative.

Visual language: warm white background, charcoal typography, quiet slate borders, and one restrained teal accent. Use semantic red/amber/green only for status and pair each with text or an icon. Favor compact tables, clear spacing, strong numeric alignment, and a persistent sidebar over oversized hero areas or card grids.

Every screen needs loading, empty, error, permission-denied, and success states. Forms must preserve entered values on validation errors. Destructive or posting actions require an explicit confirmation and clear result feedback.

Avoid gradients, decorative blobs, excessive rounded rectangles, rainbow dashboards, auto-playing motion, and color-only meaning. Use icons from the selected icon library with accessible labels/tooltips.

## Change protocol

1. Find the nearest model, service, view, component, or test that owns the behavior.
2. State one falsifiable local hypothesis and identify the cheapest check that could disprove it.
3. Make the smallest focused edit. Preserve unrelated user changes.
4. Run a narrow test, check, typecheck, or syntax validation immediately after the first substantive edit.
5. Add or update tests for tenant isolation, permissions, accounting balance, period locking, repository adapters, and boundary values when relevant.
6. Run the broader verification commands before declaring completion.
7. Summarize changed files, behavior, validation results, and any environment limitation.

Do not commit, reset, or create branches unless explicitly requested. Do not change public APIs or rename domain concepts without confirming the impact.

## Definition of done

A feature is complete only when its domain rule, application use case, infrastructure adapter, permissions, API behavior, UI states, report/document output, tests, migrations, and documentation are consistent. A passing happy-path test alone is not enough for financial or tenant-scoped behavior.
