# Traffic Rules & Fines Management System

Responsive (mobile + desktop) Flask + SQLite app. Officers issue challans from a phone; the admin dashboard updates live (Server-Sent Events).

## Run
    pip install -r requirements.txt
    python app.py
Open the printed URL. Phones on the same Wi-Fi/hotspot can use the LAN URL. Set `SECRET_KEY` in production.

Demo logins: `officer / officer123`, `admin / admin123` (change these). Public page: `/lookup`.

## Test
    python -m unittest discover -s tests

## Features
Login + roles, challan entry with server-side fines (8 violations, +50% repeat offence), printable receipt, Paid/Pending tracking, vehicle search with totals, live dashboard, audit log (admin), CSRF protection, public fine lookup.

## Edit fines
`RULES` dict at the top of `app.py`.

## Not built yet
Online payments, SMS, photo evidence, offline queue/PWA install, Postgres/Docker, maps.
