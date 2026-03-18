# Comprehensive Trading Performance Analysis

This report provides a comprehensive interpretation of your trading data across multiple time horizons, detailing the structural edge and pair-by-pair performance of your backtested ruleset.

---

## 1. Strategy Identification
First, by cross-referencing your trading logs with your parameter configuration file, it is definitively clear that all of these simulations were executed using the `mean_reversion` strategy parameters. 
- For example, the execution logs for `USD_JPY` show a Hold of **2190**, Stop Loss (SL) of **0.00653**, and Take Profit (TP) of **0.00822**, which exactly matches the `mean_reversion` tuning block for `USD_JPY` in your configuration file.

## 2. Time Horizon Performance (The Big Picture)
You have provided execution data across three distinct backtesting periods: **1-Week** (Mar 1–Mar 8), **1-Month** (Feb 6–Mar 8), and **3-Months** (Dec 8–Mar 8).

The most crucial finding is that the strategy is **highly robust and profitable over the long term (3 months)**, despite experiencing expected normal variance and chop in the short term.

- **3-Month Horizon (Excellent Performance):** Over the 3-month period, every single currency pair was profitable. The strategy generated impressive absolute PnL and extremely high Sharpe ratios (ranging from 1.30 to 4.91). This conclusively proves the strategy has a genuine edge when allowed to play out over a statistically significant number of trades.
- **1-Month Horizon (Solid Performance):** Portfolio performance remained strong. While `EUR_USD` experienced a drawdown period, pairs like `NZD_USD` and `AUD_USD` carried the holisitic portfolio with massive gains.
- **1-Week Horizon (Short-term Variance):** The 1-week period was a mixed bag, which is standard for mean reversion algorithms. `GBP_USD`, `NZD_USD`, and `AUD_USD` were profitable, while the USD-base pairs (`USD_JPY`, `USD_CHF`, `USD_CAD`) and `EUR_USD` took minor losses. This highlights that judging or discontinuing this strategy based on a weekly result will lead to false negatives.

## 3. Standout Performers (3-Month Data)
Over the most reliable 3-month data set, a few pairs stood out as absolute powerhouses:

| Rank | Pair | PnL | Win Rate | Sharpe | Profit Factor | Note |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 🥇 | **`AUD_USD`** | +$7,423.17 | 37.6% | 4.91 | 3.37 | **The undeniable champion.** Interestingly, it achieved massive gains with a relatively low win rate. It wins exceptionally big (Avg win: $405) and loses very little (Avg loss: -$72). |
| 🥈 | **`NZD_USD`** | +$5,242.16 | 67.2% | 3.75 | ~ | **The most consistent pair.** It yielded an incredible balance of high win rate and deep profitability. |
| 🥉 | **`USD_JPY`** & **`USD_CHF`** | ~$4,850.00 | Mixed | ~ | ~ | Both yielded roughly +$4,800 to +$4,900. `USD_CHF` favored a high win rate (67.2%), while `USD_JPY` leaned on a lower win rate (35.7%) but demonstrated excellent risk-reward with a Profit Factor of 2.49. |


## 4. The Weakest Link: `EUR_USD`
While `EUR_USD` was profitable over the 3-month period (+$1,392.84), it severely underperformed relative to the rest of the portfolio.

- It maintains the lowest Profit Factor (**1.27**) and the lowest Sharpe Ratio (**1.30**).
- In the 1-week and 1-month periods, it was actively losing money (-$662 and -$741, respectively).

**Takeaway:** The mean reversion parameters for `EUR_USD` likely require re-optimization, or this specific pair might be trending too strongly during the current macroeconomic period for a strict mean-reversion strategy to excel. You may want to decrease its allocation size or tighten its entrance bounds.

## 5. Mechanics of Your Edge: Aggressive Capital Preservation
Looking closely at the underlying trade logs (specifically tracking `AUD_USD` and `GBP_USD`), your strategy has a hidden mechanistic superpower driving its profitability: **Breakeven Stops (`be_exits`)**.

For example, out of 69 trades taken on `AUD_USD` over 3 months, **36 trades cleanly exited at breakeven.** Because the strategy rapidly trails stops to breakeven, your average losing trade is drastically minimized globally. You are taking small paper cuts (or zero losses) while letting the trades that hit Take Profit (`tp_exits`) cover the spread exponentially. This extreme capital preservation fundamentally protects the strategy during choppy conditions.
