# BLOCKED.md — superplatform_G1 交付受阻项汇总

各阶段明细见同目录 `BLOCKED_01.md` ~ `BLOCKED_05.md`（02/04/05 为「无」或仅记录在案）。真正影响验收的只有 1 项：

## 1.（01）永续 earliest ≤ 2019-09-26 源端不可达 —— 唯一未达验收子项

任务书假设币安 UM 永续数据自 2019-09-25 起可取。本机唯一可达源 data.binance.vision（fapi/api 直连超时）上，UM 永续所有归档家族自 **2019-12-31** 才存在，2019-09~12-30 无任何归档（总控独立复核：月归档 2019-09/12 均 404、2020-01 起 206）。实际回填：永续 kline earliest=2019-12-31（源端最早）、funding 2020-01-01、OI 2020-09-01(BTC)/2021-12-01(ETH)。机制（钳位/empty_range 书签/增量）已用可得数据全部验证，未造假。若未来 REST 可通，按 `BLOCKED_01.md` 的 SQL 清书签后可补 2019-09~12 真实数据。完整 curl 取证见 `BLOCKED_01.md`。

## 2.（03）testnet「有 key 能连」只验到鉴权层

本机无真实 testnet key；dummy key 下签名请求到达服务端收到 `-2014 API-key format invalid`（网络通、凭据无效）。无 key 报错退出（exit=1、不降级模拟盘）已验收。另：`BinanceBroker` 行情适配器指向生产 fapi（本机超时），有 key 后仍需把行情源切到 testnet 域。见 `BLOCKED_03.md`。

## 3. 记录在案（不阻塞）

- （01）fundingRate 仅月归档、日归档 T+1 发布 → funding/OI/kline latest 滞后 1 天~1 月属源端固有；`validate --input *.csv` 在 pandas 3 下的既有 TypeError（parquet 正常，00 搬运件既有，未修）。
- （04）研究池 4 个标的源端 Invalid symbol 已如实剔除；decorator 因子无 impl 路径时 full_sample 检查如实 BLOCKED。
- （05）sim 有而本平台无的端点如实 503/501（signals/cleaning/backtest-factor/pystrategies）；相关性矩阵默认集收窄为已评估因子；无常驻因子最新值（时序图按需真算）。见 `BLOCKED_05.md`。
