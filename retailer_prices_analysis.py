import re
from pathlib import Path
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

OUTPUT = Path("output")
log = []  # data-quality log: one entry per step
LOOKBACK_DAYS = 28   # benchmark from trading standards pricing guidance
TOL = 0.01           # £ tolerance when comparing prices


def record(step, before, after, note):
    log.append({"step": step, "rows_before": before, "rows_after": after, "note": note})

def clean_price(value):
    if pd.isna(value):
        return None
    text = re.sub(r"[£,]|GBP", "", str(value)).strip()
    return float(text) if text else None

def parse_timestamp(value):
    """Handle both 'dd/mm/yyyy hh:mm' and Unix epoch seconds."""
    if pd.isna(value):
        return pd.NaT
    text = str(value).strip()
    if text.isdigit():
        return pd.to_datetime(int(text), unit="s")
    return pd.to_datetime(text, dayfirst=True, errors="coerce")

def load(path):
    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    return df.dropna(axis=1, how="all")

def clean_data(df):
    n = len(df)
    df = df.drop_duplicates()
    record("exact_duplicates", n, len(df), "Removed identical rows")

    df["retailer"] = (df["page_url"]
                      .str.extract(r"www\.retailer([a-e])\.", flags=re.IGNORECASE)[0]
                      .str.upper())

    for col in ["product_id", "product_name"]:
        missing = df[col].isna().sum()
        lookup = df.dropna(subset=[col]).groupby("page_url")[col].first()
        df[col] = df[col].fillna(df["page_url"].map(lookup))
        record(f"fill_{col}", len(df), len(df), f"Filled {missing} missing values from page_url")

    for col in ["selling_price", "reference_price"]:
        df[col] = df[col].apply(clean_price)

    n = len(df)
    df["scrape_date"] = pd.to_datetime(df["scrape_date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["scrape_date"])
    record("missing_date", n, len(df), "Dropped rows with no scrape date")

    n = len(df)
    df = (df.sort_values(["product_id", "scrape_date", "scrape_timestamp"])
            .drop_duplicates(subset=["product_id", "scrape_date"], keep="first"))
    corrected_time_count = df["scrape_timestamp"].astype(str).str.fullmatch(r"\d+").sum()
    df["scrape_timestamp"] = df["scrape_timestamp"].apply(parse_timestamp)
    record("parse_timestamp", len(df), len(df), f"Converted {corrected_time_count} Unix epoch timestamps")
    record("one_row_per_day", n, len(df), "Kept first scrape per product per day")

    return df

def standardise_labels(df):
    label = df["reference_label"].str.lower().str.strip(" :.")
    mapping = {"was": "was", "previously": "was", "orig": "was", "rrp": "rrp"}
    df["claim_type"] = label.map(mapping)
    has_ref = df["reference_price"].notna()
    df.loc[has_ref & df["claim_type"].isna(), "claim_type"] = "unlabelled"
    return df

def find_episodes(df):
    df = df.sort_values(["product_id", "scrape_date"])
    key = df["reference_price"].fillna(-1)
    new_ep = (key != key.groupby(df["product_id"]).shift()) 
    df["episode"] = new_ep.groupby(df["product_id"]).cumsum()
    return df

def fill_reference_price_gaps(df, max_gap=2):
    """Fill short scraping gaps inside a promotion (same reference price either side)."""
    df = df.sort_values(["product_id", "scrape_date"])
    g = df.groupby("product_id")["reference_price"]
    before, after = g.ffill(limit=max_gap), g.bfill(limit=max_gap)
    gap = df["reference_price"].isna() & before.notna() & (before == after)
    df.loc[gap, "reference_price"] = before[gap]
    df["claim_type"] = df.groupby("product_id")["claim_type"].ffill(limit=max_gap).where(
        df["reference_price"].notna(), df["claim_type"])
    record("fill_reference_price_gaps", len(df), len(df), f"Filled {gap.sum()} short gaps inside promotions")
    return df

def assess_episodes(df):
    start_of_data = df["scrape_date"].min()
    rows = []
    for (pid, ep), g in df[df["reference_price"].notna()].groupby(["product_id", "episode"]):
        start, end = g["scrape_date"].min(), g["scrape_date"].max()
        ref = g["reference_price"].iloc[0]
        sale = g["selling_price"].median()

        hist = df[(df["product_id"] == pid)
                  & (df["scrape_date"] < start)
                  & (df["scrape_date"] >= start - pd.Timedelta(days=LOOKBACK_DAYS))
                  & df["reference_price"].isna()]
        days_at_ref = (hist["selling_price"] >= ref - TOL).sum()
        ever_at_ref = (df[(df["product_id"] == pid) & (df["scrape_date"] < start)]
                       ["selling_price"] >= ref - TOL).any()
        typical = hist["selling_price"].median()

        claimed = (ref - sale) / ref * 100
        real = (typical - sale) / typical * 100 if pd.notna(typical) else None
        length = (end - start).days + 1
        full_history = (start - start_of_data).days >= LOOKBACK_DAYS
        is_was = g["claim_type"].iloc[0] in ("was", "unlabelled")

        rows.append({
            "product_id": pid, "product_name": g["product_name"].iloc[0],
            "retailer": g["retailer"].iloc[0], "category": g["category"].dropna().iloc[0] if g["category"].notna().any() else None,
            "claim_type": g["claim_type"].iloc[0], "promotion": g["promotion_name"].dropna().iloc[0] if g["promotion_name"].notna().any() else None,
            "start": start.date(), "end": end.date(), "days_on_promo": length,
            "reference_price": ref, "sale_price": sale,
            "days_at_ref_before": days_at_ref, "claimed_discount_pct": round(claimed, 1),
            "real_discount_pct": round(real, 1) if real is not None else None,
            "full_history": full_history,
            "flag_never_charged": is_was and not ever_at_ref,
            "flag_short_establishment": is_was and full_history and ever_at_ref and days_at_ref < LOOKBACK_DAYS,
            "flag_promo_longer_than_ref": is_was and ever_at_ref and length > days_at_ref,
        })
    ep = pd.DataFrame(rows)
    ep["severity_gap_pts"] = ep["claimed_discount_pct"] - ep["real_discount_pct"]
    ep["any_flag"] = ep[["flag_never_charged", "flag_short_establishment", "flag_promo_longer_than_ref"]].any(axis=1)
    return ep

def build_report(df, episodes, by_retailer, example_id="RET-PROD-001"):
    flags = ["flag_never_charged", "flag_short_establishment", "flag_promo_longer_than_ref"]
    names = {"flag_never_charged": "Reference price never charged",
             "flag_short_establishment": "Higher price held < 28 days",
             "flag_promo_longer_than_ref": "Sale longer than higher price"}

    # 1. How widespread: % of products flagged by retailer
    br = by_retailer.reset_index()
    fig1 = px.bar(br, x="retailer", y="pct_flagged",
                  text=br.apply(lambda r: f"{r.flagged} of {r.products}", axis=1),
                  title="Share of products using 'was'-type prices that were flagged",
                  labels={"pct_flagged": "% of products flagged", "retailer": "Retailer"})
    fig1.update_yaxes(range=[0, 105])

    # 2. Nature: which behaviour, by retailer
    heat = episodes.groupby("retailer")[flags].mean().mul(100).round(0).rename(columns=names)
    fig2 = px.imshow(heat, text_auto=True, color_continuous_scale="Reds",
                     title="Type of behaviour: % of promotions showing each pattern",
                     labels={"x": "Pattern", "y": "Retailer", "color": "%"})

    # 3. Severity: claimed vs real discount
    fig3 = px.scatter(episodes, x="real_discount_pct", y="claimed_discount_pct",
                      color="retailer", hover_data=["product_name", "start", "days_on_promo"],
                      title="Claimed vs real discount (points above the line overstate the saving)",
                      labels={"real_discount_pct": "Real discount vs previous 28 days (%)",
                              "claimed_discount_pct": "Claimed discount (%)"})
    fig3.add_shape(type="line", x0=0, y0=0, x1=60, y1=60, line=dict(dash="dash", color="grey"))

    # 4. Example: one product over time
    ex = df[df["product_id"] == example_id]
    fig4 = go.Figure()
    fig4.add_trace(go.Scatter(x=ex["scrape_date"], y=ex["selling_price"], name="Selling price"))
    fig4.add_trace(go.Scatter(x=ex["scrape_date"], y=ex["reference_price"], name='"Was" price',
                              line=dict(dash="dot")))
    fig4.update_layout(title=f"Example: {ex['product_name'].iloc[0]} (Retailer {ex['retailer'].iloc[0]})",
                       yaxis_title="Price (£)")

    total = df["product_id"].nunique()
    promoted = episodes["product_id"].nunique()
    flagged = episodes.loc[episodes["any_flag"], "product_id"].nunique()
    headline = (f"<h1>Reference pricing: where to look first</h1>"
                f"<p><b>{flagged} of {promoted}</b> products that used a reference price show at least one "
                f"risk pattern ({total} products monitored, 1 Jan – 30 Mar 2024). "
                f"Flags are risk indicators for investigation, not findings of a breach.</p>")

    notes = [
    "Each bar shows the share of a retailer's products using 'was'/reference prices that triggered at least one risk flag. Small counts (e.g. B) mean less certainty.",
    "Which pattern drives the flags. 'Higher price held < 28 days' compares against the 28-day benchmark in trading standards pricing guidance.",
    "Points on the dashed line offer a genuine saving against the previous 28 days' price. Points far above it, especially at 0% real discount, claim savings against a price that was not recently charged.",
    "Typical pattern: the higher price was charged for only ~10 days, the 'was' price was then shown for weeks, and the 'sale' price became the permanent price.",
    ]
    figs = [fig1, fig2, fig3, fig4]
    parts = [headline]
    for i, (fig, note) in enumerate(zip(figs, notes)):
        parts.append(fig.to_html(full_html=False, include_plotlyjs=(i == 0)))
        parts.append(f"<p style='color:#444;margin:0 0 40px'>{note}</p>")
    (OUTPUT / "report.html").write_text("<html><body style='font-family:sans-serif;max-width:1000px;margin:auto'>"
                                         + "".join(parts) + "</body></html>", encoding="utf-8")

def main():
    OUTPUT.mkdir(exist_ok=True)
    df = clean_data(load("reference_pricing_data.csv"))
    df = fill_reference_price_gaps(standardise_labels(df))
    df.to_csv(OUTPUT / "clean_prices.csv", index=False)
    print("Rows after cleaning:", len(df))

    episodes = assess_episodes(find_episodes(df))
    episodes.to_csv(OUTPUT / "promotion_episodes.csv", index=False)
    
    was_eps = episodes[episodes["claim_type"].isin(["was", "unlabelled"])]
    products = was_eps.groupby(["retailer", "product_id"])["any_flag"].any().reset_index()

    products = episodes.groupby(["retailer", "product_id"])["any_flag"].any().reset_index()
    by_retailer = products.groupby("retailer")["any_flag"].agg(products="count", flagged="sum")
    by_retailer["pct_flagged"] = (by_retailer["flagged"] / by_retailer["products"] * 100).round(0)
    by_retailer.to_csv(OUTPUT / "retailer_summary.csv")

    pd.DataFrame(log).to_csv(OUTPUT / "data_quality_log.csv", index=False)
    print("Promotion episodes:", len(episodes))
    print(by_retailer)
    
    build_report(df, was_eps, by_retailer)

if __name__ == "__main__":
    main()
