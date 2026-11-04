# Refund & Dispute Loss Calculator — product facts

## What it does
- Parses uploaded or pasted Stripe refunds and disputes CSV export data (up to 2 MB or 20,000 rows per file).
- Computes total financial loss, fee leakage (processing and dispute fees), and total refunded amounts.
- Identifies and lists repeat customers (customers with two or more refunds, disputes, or both) and flags specific customer behaviors.
- Breaks down losses by country and by individual customer.
- Provides a built-in demo/sample dataset option for testing.
- Allows downloading the customer loss report as a CSV file.
- Interface language: English.

## What it does NOT do
- No integrations or live API connections with Stripe or any other payment processor.
- No real-time data synchronization.
- No automated analysis or webhook processing.
- No AI or predictive modeling features.
- No user accounts, authentication, or login system.
- No persistent storage or database; all data is processed in memory for the current request only.
- No payment gateway, paid plans, or pricing/payment pages.

## Pricing and offer
- There is no paid plan, payment page, subscription, or checkout mechanism in the code. The product is entirely free to use as-is.

## Good fit
This product is a good fit for a solo founder or finance operator who wants to quickly analyze a Stripe CSV export locally in their browser to understand total losses from refunds and disputes without connecting live credentials.
