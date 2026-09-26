"""
generate_inventory.py
Builds fact_inventory_snapshot: month end stock by store and SKU, consistent with fact_sales.

Logic (per store x SKU, month by month):
  opening        = previous month closing
  units_sold_net = sales minus returns from fact_sales (returns go back into stock)
  units_received = initial allocation at launch, then monthly replenishment up to a
                   target stock level while the SKU is live; never less than needed
                   to cover that month's sales (so stock never goes negative)
  transferred_out= leftover stock sent back to the warehouse 3 months after end of life
  closing        = opening + received - sold_net - transferred_out
"""
import numpy as np
import pandas as pd

SRC = "/mnt/user-data/uploads/luxury_retail_star_schema.xlsx"
rng = np.random.default_rng(42)

sales = pd.read_excel(SRC, sheet_name="fact_sales")
prod = pd.read_excel(SRC, sheet_name="dim_product")
store = pd.read_excel(SRC, sheet_name="dim_store")

months = pd.period_range("2021-01", "2025-12", freq="M")
m_index = {p: i for i, p in enumerate(months)}
n_m = len(months)

# Net units sold per store x SKU x month
sales["period"] = sales["transaction_date"].dt.to_period("M")
units = (sales.groupby(["store_key", "product_key", "period"])["quantity"].sum())

# Assumptions (documented in README)
MIN_STOCK = {"Premium (5k+)": 1, "Core (1k–5k)": 1, "Entry (<1k)": 2}   # presentation stock
EXTRA_RANGE_PROB = 0.08          # chance a SKU is ranged in a store where it never sold
AFTER_EOL_MONTHS = 3             # months leftover stock stays before transfer out
store_cover = {k: rng.uniform(1.0, 2.5) for k in store.store_key}  # months of cover target per store

rows = []
for _, p in prod.iterrows():
    pk = p.product_key
    launch = p.launch_date.to_period("M")
    eol = None if pd.isna(p.end_of_life_date) else p.end_of_life_date.to_period("M")
    first_m = max(launch, months[0])
    if first_m > months[-1]:
        continue
    last_live = months[-1] if eol is None else min(eol, months[-1])
    live_months = max(1, (last_live - first_m).n + 1)

    sold_stores = set(units.xs(pk, level="product_key").index.get_level_values("store_key")) \
        if pk in units.index.get_level_values("product_key") else set()
    for sk in store.store_key:
        if sk not in sold_stores and rng.random() > EXTRA_RANGE_PROB:
            continue
        s_units = units.loc[(sk, pk)] if (sk, pk) in units.index.droplevel("period") else pd.Series(dtype=float)
        avg_monthly = s_units.clip(lower=0).sum() / live_months
        min_stock = MIN_STOCK[p.price_band]
        target = max(min_stock, int(np.ceil(avg_monthly * store_cover[sk] * rng.uniform(0.8, 1.3))))

        closing = 0
        for per in months[m_index[first_m]:]:
            opening = closing
            sold = int(s_units.get(per, 0))
            live = eol is None or per <= eol
            received = 0
            if live:
                received = max(0, target - (opening - sold))
            received = max(received, sold - opening)        # cover sales, no negative stock
            out = 0
            if eol is not None and per == eol + AFTER_EOL_MONTHS:
                out = opening + received - sold
            closing = opening + received - sold - out
            if opening == 0 and received == 0 and closing == 0 and not live:
                break                                       # stop once cleared after EOL
            me = per.to_timestamp(how="end").normalize()
            rows.append((int(me.strftime("%Y%m%d")), me, sk, pk, opening, received, sold, out, closing,
                         round(closing * p.standard_cost_usd, 2), closing * p.retail_price_usd))

inv = pd.DataFrame(rows, columns=["date_key", "snapshot_date", "store_key", "product_key",
                                  "opening_units", "units_received", "units_sold_net",
                                  "units_transferred_out", "on_hand_units",
                                  "on_hand_cost_usd", "on_hand_retail_usd"])
inv.to_csv("fact_inventory_snapshot.csv", index=False)
print(inv.shape)
