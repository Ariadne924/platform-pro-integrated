# BLOCKED_01.md — 01 阶段受阻项

## 1. 验收子项「永续 earliest ≤ 2019-09-26」源端不可达

任务书假设币安 UM 永续数据自 2019-09-25(永续上线日)起可取。但本机唯
一可达的源(data.binance.vision;fapi/api 直连 000 超时)上,**UM 永续
所有归档家族自 2019-12-31 起才存在**,2019-09~2019-12-30 一段没有任何
归档。curl 取证(2026-08-13,HEAD 与 GET 一致):

```
GET 404  data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2019-09-08.zip
GET 404  data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2019-09-26.zip
GET 404  data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2019-12-16.zip
         … 2019-12-17 ~ 2019-12-30 逐日全部 404 …
GET 404  data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2019-12-30.zip
GET 206  data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2019-12-31.zip
         (下载验证:首行 open_time 1577750400000 = 2019-12-31T00:00:00Z,恰 1440 行)
GET 404  data/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2019-09-26.zip
GET 404  data/futures/um/daily/klines/BTCUSDT/1d/BTCUSDT-1d-2019-12-30.zip
GET 404  data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2019-09-26.zip
GET 206  data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2019-12-31.zip
GET 404  data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2019-09.zip (10/11/12 月同 404)
GET 404  data/futures/um/monthly/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2019-09.zip
GET 404  data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2019-09.zip
         (fundingRate 月归档自 2020-01 起;OI metrics 日归档自 2020-09-01 起)
```

结论:小集永续 kline earliest = **2019-12-31**(源端最早),funding
earliest = 2020-01-01,OI earliest = 2020-09-01(BTC)/2021-12-01(ETH)。
这是源端/网络限制,不是机制缺陷:机制(钳位、empty_range 书签、增量)
已用可得数据全部验证。

注意(如实说明):永续 2019-09-25→源端起点 的前缀已按机制记为
「已验证为空」书签(empty_ranges,对 vision 源而言属实——该段归档不
存在)。若未来网络恢复、想走 REST 补 2019-09~12 的真实永续数据,需先
清掉这些书签再跑标准 provider,否则 before-段会被跳过:

```sql
DELETE FROM empty_ranges
WHERE data_type LIKE 'pv_binance_perp%' AND start_ts < '2019-12-31';
```

未造假凑验收;除该子项外,01 其余验收全部达标(见 PROGRESS_01.md)。

## 2. 其余非阻塞限制(记录在案,不阻塞交付)

- fundingRate 只有月归档 → 当月 funding 数据次月 1 日随月归档发布补齐
  (小集 funding latest = 2026-07-31 16:00Z,属源端固有滞后)。
- 日归档 T+1 发布,且 UM 偶发滞后更久(实测 2026-08-12 的 UM 日 kline/
  metrics 当日 404)→ kline/OI latest 落后 1~2 天,下次运行自动补。
- `superplatform validate --input *.csv` 在 pandas 3.0.5 下读 CSV 时间戳
  列为 Arrow 字符串,detect_missing 报 TypeError——00 搬运的既有问题
  (parquet 输入正常),非 01 地界改动,未修。
