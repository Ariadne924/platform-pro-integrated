# BLOCKED_03.md — 03 核心运行时

## 1. testnet「有 key 能连」只验到鉴权层，全链路未验

- 本机**没有** Binance USDT-M testnet API key（任务书也未提供）。已验到的边界：
  - `curl https://testnet.binancefuture.com/fapi/v1/ping` → **200**（0.58s，
    testnet 域名本机可达，与 fapi/api 生产域被墙不同）；
  - 环境变量注入 dummy key 后 `build_broker()` 成功构建 `BinanceBroker`
    （key 仅从环境变量读，构造期无网络请求）；
  - `broker._fetch_account()` 到达 testnet 服务端并收到
    `ClientError (401, -2014, 'API-key format invalid.')`——网络通、TLS 通、
    签名路由正确，只差有效凭据。
- 缺 key 路径已按验收通过：`live --broker binance-testnet` 无环境变量时
  exit=1 报错退出，不静默降级模拟盘。
- **另一个已知障碍**（即使有 key）：`BinanceBroker` 的行情适配器指向生产公共
  API（fapi.binance.com，见 network/brokers/binance.py 模块 docstring），本机
  生产域直连超时（见 BLOCKED_01.md），live 的 `_hook_data` 拉行情会失败。要让
  testnet 全链路在本机跑通，需要把行情源也切到 testnet 域（或可用镜像）——
  属于网络环境限制 + 后续阶段的适配决策，本阶段未改 broker 源码语义。
- 解除条件：用户提供 testnet key 且行情源指向可达端点后，跑
  `superplatform live --strategy DEM-001 --broker binance-testnet --ticks 5`
  即可终验。

## 2. 其他

无。
