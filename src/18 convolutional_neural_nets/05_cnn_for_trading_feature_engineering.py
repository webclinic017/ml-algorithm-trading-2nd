# CNN for Trading - Part 1: Feature Engineering
# To exploit the grid-like structure of time-series data, we can use CNN architectures for univariate and multivariate
# time series. In the latter case, we consider different time series as channels, similar to the different color signals.
# An alternative approach converts a time series of alpha factors into a two-dimensional format to leverage the ability
# of CNNs to detect local patterns. [Sezer and Ozbayoglu (2018)]
# (https://www.researchgate.net/publication/324802031_Algorithmic_Financial_Trading_with_Deep_Convolutional_Neural_
# Networks_Time_Series_to_Image_Conversion_Approach) propose CNN-TA, which computes 15 technical indicators for different
# intervals and uses hierarchical clustering (see Chapter 13, Data-Driven Risk Factors and Asset Allocation with
# Unsupervised Learning) to locate indicators that behave similarly close to each other in a two-dimensional grid.
# The authors train a CNN similar to the CIFAR-10 example we used earlier to predict whether to buy, hold, or sell an
# asset on a given day. They compare the CNN performance to "buy-and-hold" and other models and find that it outperforms
# all alternatives using daily price series for Dow 30 stocks and the nine most-traded ETFs over the 2007-2017 time period.
#
# The section on *CNN for Trading* consists of three notebooks that experiment with this approach using daily US equity
# price data. They demonstrate
# 1. How to compute relevant financial features
# 2. How to convert a similar set of indicators into image format and cluster them by similarity
# 3. How to train a CNN to predict daily returns and evaluate a simple long-short strategy based on the resulting signals.
#
# ## Creating technical indicators at different intervals
# We first select a universe of the 500 most-traded US stocks from the Quandl Wiki dataset by dollar volume for rolling
# five-year periods for 2007-2017.
# - Our features consist of 15 technical indicators and risk factors that we compute for 15 different intervals and
#   then arrange them in a 15x15 grid.
# - For each indicator, we vary the time period from 6 to 20 to obtain 15 distinct measurements.

# To install `talib` with Python 3.7 follow (https://medium.com/@joelzhang/install-ta-lib-in-python-3-7-51219acacafb).

from talib import (
    RSI,
    BBANDS,
    MACD,
    NATR,
    WILLR,
    WMA,
    EMA,
    SMA,
    CCI,
    CMO,
    MACD,
    PPO,
    ROC,
    ADOSC,
    ADX,
    MOM,
)
import seaborn as sns
import matplotlib.pyplot as plt
from statsmodels.regression.rolling import RollingOLS
import statsmodels.api as sm
import pandas_datareader.data as web
import pandas as pd
import numpy as np
from pathlib import Path
import warnings

idx = pd.IndexSlice
np.random.seed(seed=42)
sns.set_style("whitegrid")
plt.rcParams["figure.dpi"] = 300
plt.rcParams["font.size"] = 14
pd.options.display.float_format = "{:,.2f}".format
warnings.filterwarnings("ignore")

DATA_STORE = "../data/assets.h5"

results_path = Path("../data/ch18", "cnn_for_trading")
mnist_path = results_path / "mnist"
if not mnist_path.exists():
    mnist_path.mkdir(parents=True)

if __name__ == "__main__":
    MONTH = 21
    YEAR = 12 * MONTH

    START = "2000-01-01"
    END = "2017-12-31"

    T = [1, 5, 10, 21, 42, 63]

    ## Loading Quandl Wiki Stock Prices & Meta Data
    adj_ohlcv = ["adj_open", "adj_close", "adj_low", "adj_high", "adj_volume"]
    with pd.HDFStore(DATA_STORE) as store:
        prices = (
            store["quandl/wiki/prices"]
            .loc[idx[START:END, :], adj_ohlcv]
            .rename(columns=lambda x: x.replace("adj_", ""))
            .swaplevel()
            .sort_index()
            .dropna()
        )
        metadata = store["us_equities/stocks"].loc[:, ["marketcap", "sector"]]
    ohlcv = prices.columns.tolist()

    prices.volume /= 1e3
    prices.index.names = ["symbol", "date"]
    metadata.index.name = "symbol"

    ## Rolling universe: pick 500 most-traded stocks
    dollar_vol = prices.close.mul(prices.volume).unstack("symbol").sort_index()
    years = sorted(np.unique([d.year for d in prices.index.get_level_values("date").unique()]))

    train_window = 5  # years
    universe_size = 500

    universe = []
    for i, year in enumerate(years[5:], 5):
        start = str(years[i - 5])
        end = str(years[i])
        most_traded = (
            dollar_vol.loc[start:end, :]
            .dropna(thresh=1000, axis=1)
            .median()
            .nlargest(universe_size)
            .index
        )
        universe.append(prices.loc[idx[most_traded, start:end], :])
    universe = pd.concat(universe)
    universe = universe.loc[~universe.index.duplicated()]
    universe.info(show_counts=True)
    print(universe.groupby("symbol").size().describe())
    universe.to_hdf("../data/18_data.h5", "universe")

    ## Generate Technical Indicators Factors
    T = list(range(6, 21))

    ### Relative Strength Index
    for t in T:
        universe[f"{t:02}_RSI"] = universe.groupby(level="symbol").close.apply(RSI, timeperiod=t)

    ### Williams %R
    for t in T:
        universe[f"{t:02}_WILLR"] = universe.groupby(level="symbol", group_keys=False).apply(
            lambda x: WILLR(x.high, x.low, x.close, timeperiod=t)
        )

    ### Compute Bollinger Bands
    def compute_bb(close, timeperiod):
        high, mid, low = BBANDS(close, timeperiod=timeperiod)
        return pd.DataFrame(
            {f"{timeperiod:02}_BBH": high, f"{timeperiod:02}_BBL": low}, index=close.index
        )

    for t in T:
        bbh, bbl = f"{t:02}_BBH", f"{t:02}_BBL"
        universe = universe.join(
            universe.groupby(level="symbol").close.apply(compute_bb, timeperiod=t)
        )
        universe[bbh] = universe[bbh].sub(universe.close).div(universe[bbh]).apply(np.log1p)
        universe[bbl] = universe.close.sub(universe[bbl]).div(universe.close).apply(np.log1p)

    ### Normalized Average True Range
    for t in T:
        universe[f"{t:02}_NATR"] = universe.groupby(level="symbol", group_keys=False).apply(
            lambda x: NATR(x.high, x.low, x.close, timeperiod=t)
        )

    ### Percentage Price Oscillator
    for t in T:
        universe[f"{t:02}_PPO"] = universe.groupby(level="symbol").close.apply(
            PPO, fastperiod=t, matype=1
        )

    ### Moving Average Convergence/Divergence
    def compute_macd(close, signalperiod):
        macd = MACD(close, signalperiod=signalperiod)[0]
        return (macd - np.mean(macd)) / np.std(macd)

    for t in T:
        universe[f"{t:02}_MACD"] = universe.groupby("symbol", group_keys=False).close.apply(
            compute_macd, signalperiod=t
        )

    ### Momentum
    for t in T:
        universe[f"{t:02}_MOM"] = universe.groupby(level="symbol").close.apply(MOM, timeperiod=t)

    ### Weighted Moving Average
    for t in T:
        universe[f"{t:02}_WMA"] = universe.groupby(level="symbol").close.apply(WMA, timeperiod=t)

    ### Exponential Moving Average
    for t in T:
        universe[f"{t:02}_EMA"] = universe.groupby(level="symbol").close.apply(EMA, timeperiod=t)

    ### Commodity Channel Index
    for t in T:
        universe[f"{t:02}_CCI"] = universe.groupby(level="symbol", group_keys=False).apply(
            lambda x: CCI(x.high, x.low, x.close, timeperiod=t)
        )

    ### Chande Momentum Oscillator
    for t in T:
        universe[f"{t:02}_CMO"] = universe.groupby(level="symbol").close.apply(CMO, timeperiod=t)

    ### Rate of Change
    # Rate of change is a technical indicator that illustrates the speed of price change over a period of time.
    for t in T:
        universe[f"{t:02}_ROC"] = universe.groupby(level="symbol").close.apply(ROC, timeperiod=t)

    ### Chaikin A/D Oscillator
    for t in T:
        universe[f"{t:02}_ADOSC"] = universe.groupby(level="symbol", group_keys=False).apply(
            lambda x: ADOSC(x.high, x.low, x.close, x.volume, fastperiod=t - 3, slowperiod=4 + t)
        )

    ### Average Directional Movement Index
    for t in T:
        universe[f"{t:02}_ADX"] = universe.groupby(level="symbol", group_keys=False).apply(
            lambda x: ADX(x.high, x.low, x.close, timeperiod=t)
        )
    universe.drop(ohlcv, axis=1).to_hdf("../data/18_data.h5", "features")

    ## Compute Historical Returns
    ### Historical Returns
    by_sym = universe.groupby(level="symbol").close
    for t in [1, 5]:
        universe[f"r{t:02}"] = by_sym.pct_change(t)

    ### Remove outliers
    print(universe[[f"r{t:02}" for t in [1, 5]]].describe())
    outliers = universe[universe.r01 > 1].index.get_level_values("symbol").unique()
    print(len(outliers))

    universe = universe.drop(outliers, level="symbol")

    ### Historical return quantiles
    for t in [1, 5]:
        universe[f"r{t:02}dec"] = (
            universe[f"r{t:02}"]
            .groupby(level="date")
            .apply(lambda x: pd.qcut(x, q=10, labels=False, duplicates="drop"))
        )

    ## Rolling Factor Betas
    # We also use five Fama-French risk factors (Fama and French, 2015; see Chapter 4, Financial Feature Engineering
    # – How to Research Alpha Factors). They reflect the sensitivity of a stock's returns to factor consistently
    # demonstrated to impact equity returns.
    #
    # We capture these factors by computing the coefficients of a rolling OLS regression of a stock's daily returns on
    # the returns of portfolios designed to reflect the underlying drivers:
    # - **Equity risk premium**: Value-weighted returns of US stocks minus the 1-month US
    # - **Treasury bill rate**
    # - **Size (SMB)**: Returns of stocks categorized as Small (by market cap) Minus those of Big equities
    # - **Value (HML)**: Returns of stocks with High book-to-market value Minus those with a Low value
    # - **Investment (CMA)**: Returns differences for companies with Conservative investment expenditures Minus those
    #     with Aggressive spending
    # - **Profitability (RMW)**: Similarly, return differences for stocks with Robust profitability Minus that
    #     with a Weak metric.

    factor_data = web.DataReader(
        "F-F_Research_Data_5_Factors_2x3_daily", "famafrench", start=START
    )[0].rename(columns={"Mkt-RF": "Market"})
    factor_data.index.names = ["date"]
    factor_data.info()

    windows = list(range(15, 90, 5))
    print(len(windows))

    # Next, we apply `statsmodels`' `RollingOLS()` to run regressions over windowed periods of different lengths,
    # ranging from 15 to 90 days. We set the `params_only` parameter on the `.fit()` method to speed up computation and
    # capture the coefficients using the `.params` attribute of the fitted factor_model:
    t = 1
    ret = f"r{t:02}"
    factors = ["Market", "SMB", "HML", "RMW", "CMA"]
    windows = list(range(15, 90, 5))
    for window in windows:
        print(window)
        betas = []
        for symbol, data in universe.groupby(level="symbol"):
            model_data = data[[ret]].merge(factor_data, on="date").dropna()
            model_data[ret] -= model_data.RF

            rolling_ols = RollingOLS(
                endog=model_data[ret], exog=sm.add_constant(model_data[factors]), window=window
            )
            factor_model = rolling_ols.fit(params_only=True).params.drop("const", axis=1)
            result = factor_model.assign(symbol=symbol).set_index("symbol", append=True)
            betas.append(result)
        betas = pd.concat(betas).rename(columns=lambda x: f"{window:02}_{x}")
        universe = universe.join(betas)

    ## Compute Forward Returns
    for t in [1, 5]:
        universe[f"r{t:02}_fwd"] = universe.groupby(level="symbol")[f"r{t:02}"].shift(-t)
        universe[f"r{t:02}dec_fwd"] = universe.groupby(level="symbol")[f"r{t:02}dec"].shift(-t)

    ## Store Model Data
    universe = universe.drop(ohlcv, axis=1)
    universe.info(show_counts=True)

    drop_cols = ["r01", "r01dec", "r05", "r05dec"]
    outcomes = universe.filter(like="_fwd").columns
    universe = universe.sort_index()
    with pd.HDFStore("../data/18_data.h5") as store:
        store.put(
            "features",
            universe.drop(drop_cols, axis=1).drop(outcomes, axis=1).loc[idx[:, "2001":], :],
        )
        store.put("targets", universe.loc[idx[:, "2001":], outcomes])
