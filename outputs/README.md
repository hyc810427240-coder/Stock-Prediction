# Single Stock Quant Prototype

This prototype tests next-session stock signals for one A-share stock.

## Usage

```powershell
python .\stock_quant_app.py 城地香江 --out .
python .\stock_quant_app.py 603887 --out .
```

## Web App

```powershell
python .\web_app.py
```

Open:

```text
http://127.0.0.1:5000
```

The script writes:

- `*_quant_report_*.md`: model result and operation suggestion.
- `*_features_*.csv`: engineered K-line, indicator, fundamental, and target data.

## Data

- BaoStock: daily K-line and financial statement data.
- Eastmoney: fallback daily K-line, turnover-rate enrichment, and news search.

## Models

The target return is:

```text
(t+2 open / t+1 open - 1) * 100%
```

Implemented model groups:

- Multi-factor logistic/ridge model.
- LightGBM classifier/regressor.
- Mean-reversion rule model.
- Trend-following rule model.
- Oversold-rebound rule model.

The app reports historical signal win rate, historical signal average return, latest predicted win probability, latest predicted return, and a final action suggestion.
