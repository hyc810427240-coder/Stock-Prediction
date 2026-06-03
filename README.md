# A-Share Single Stock Quant Web App

本项目是一个 A 股单股次日量化建议网页原型。

## 功能

- 输入股票名称或 6 位股票代码。
- 自动获取近一年行情、财报、资讯/公告数据。
- 计算 KDJ、MACD、BOLL、换手率、量价、筹码近似等指标。
- 使用多因子、LightGBM、均值回归、趋势跟踪、超跌反弹模型输出预测胜率和收益。
- 展示操作建议、支撑位、压力位、止损参考。
- 展示重要讯息、估值指标、市值指标和多指标财报图表。

## 本地运行

```powershell
pip install -r requirements.txt
python .\outputs\web_app.py
```

打开：

```text
http://127.0.0.1:5000
```

## 命令行报告

```powershell
python .\outputs\stock_quant_app.py 城地香江 --out .\outputs
python .\outputs\stock_quant_app.py 603887 --out .\outputs
```

## 说明

模型结果用于研究和交易前筛选，不构成投资建议。免费数据源可能存在延迟、限流或字段缺失，实盘前需要进一步校验。
