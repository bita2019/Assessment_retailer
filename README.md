# Reference pricing analysis

# Findings

- 20 of 21 products using "was"-type reference prices show at least one risk pattern, across all five retailers.
- Main patterns: higher price held < 28 days before the sale, and sales lasting longer than the higher price.
- Most severe: three promotions (Retailers C, D, E) claimed 29–50% off where the real saving vs the previous 28 days was ~0%.
- Largest overstatement: Retailer B, ~57% claimed vs ~19% real.

# How to run

pip install -r requirements.txt
python retailer_prices_analysis.py # outputs go to output/
Open output/report.html in a browser.

# Approach

Clean → group daily rows into promotion episodes → flag each episode → summarise by retailer → report.

# Flags

- Reference price never charged before the promotion
- Higher price held < 28 days (benchmark from trading standards pricing guidance)
- Promotion longer than the period at the higher price
- Severity: claimed discount minus real discount (vs median price in previous 28 days)

# Assumptions

- "Was", "Previously", "Orig." = retailer's own previous price; RRP excluded (manufacturer price).
- Reference price with no label treated as "was".
- Reference price missing ≤ 2 days with the same price either side = scraping gap, filled.
- Promotions starting before 29 Jan have < 28 days history, so short-establishment is not flagged.
- One scrape per product per day; earliest kept.

# Data quality

See output/data_quality_log.csv (duplicates, filled IDs, price formats, Unix timestamps, gaps).

# Limitations

- No sales volumes, so only duration can be tested, not how many sales were made at the higher price.
- 90 days only; small product counts per retailer (e.g. B has 2).
- Flags are risk indicators, not proof of a breach.
- Synthetic data.

# Next steps

Streamlit dashboard; daily scheduled run on AWS; tests for each flag rule.
