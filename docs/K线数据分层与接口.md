# K 线数据分层与接口

K 线后端采用 Bronze → Silver → Gold 单向依赖。HTTP、因子和策略调用方不得
自行复制重采样或质量判断逻辑；统一通过
`superplatform.data.kline_layers.KlineLayerPipeline.load()` 获取数据。

## 分层职责

### Bronze：采集入口

- 当前物理实现是 DuckDB 中按 Provider 隔离的 `pv_*` 缓存表；
- 保留 Provider 原生周期和字段，不执行重采样；
- K 线当前原生周期为 `1m` 和 `1d`；
- API 返回原生 `timestamp` 和缓存字段；
- 原始 Binance Vision ZIP/CSV 仍由采集器管理，后续可在不改变上层接口的
  前提下增加不可变归档和 checksum。

### Silver：标准化与质量层

- 只消费 Bronze；
- 统一 UTC、紧凑 symbol 和规范字段；
- 生成 `open_time`、右开区间的 `close_time` 和 `is_closed`；
- 缺失值保留为 `null`，不使用零填充；
- 生成记录级 `quality_flags`：`missing_field`、`invalid_ohlc`、
  `non_positive_price`、`negative_value`、`incomplete`；
- 当前提供 `1m` 和 `1d` 原生周期。

### Gold：研究和图表层

- 只消费 Silver；
- 生成 `5m/15m/30m/1h/4h/1d/1w` 研究 K 线；
- 聚合口径固定为 open=first、high=max、low=min、close=last，数量字段=sum；
- 全为空的数量字段保持 `null`，不会被错误聚合成零；
- Silver 源 K 线的质量标记会继承到对应 Gold K 线，聚合不会掩盖源异常；
- 响应 `transformations` 记录实际血缘，例如 `resample:1m->4h`；
- K 线页面和四类策略的中低频行情输入应使用 Gold。

## HTTP 接口

```http
GET /api/v1/market/klines
```

必填参数：

- `exchange`：例如 `binance`；
- `market_type`：`spot`、`perpetual` 或 `coin_futures`；
- `symbol`：`BTCUSDT` 或兼容形式 `BTC/USDT`；
- `frequency`：`1m/5m/15m/30m/1h/4h/1d/1w`。

可选参数：

- `layer`：`bronze/silver/gold`，默认 `silver`；
- `start/end`：ISO-8601，省略时读取最近数据；
- `limit`：1～5000，接口通过 `has_more` 表示是否还有数据，不做均匀抽点。
- `ma`：`all` 或逗号分隔的均线 key；均线统一由后端计算。

接口严格匹配 `exchange + market_type + kline` Provider，不允许跨市场静默回退。
`1w` 以 UTC 周一 00:00 为固定边界；改变查询起点不会改变同一周的 OHLCV。
显式 `end` 落在聚合桶中间时，Gold 不返回该历史残桶，避免把不完整棒误标为
已闭合研究数据。

示例：

```http
GET /api/v1/market/klines?exchange=binance&market_type=perpetual&symbol=BTCUSDT&frequency=4h&layer=gold&start=2025-01-01T00:00:00Z&end=2026-01-01T00:00:00Z&limit=3000
```

响应元数据包含：

```json
{
  "exchange": "binance",
  "market_type": "perpetual",
  "symbol": "BTCUSDT",
  "frequency": "4h",
  "source_frequency": "1m",
  "provider_id": "binance-perp-kline",
  "source": "provider_cache",
  "data_layer": "gold",
  "transformations": [
    "utc_normalization",
    "canonical_fields",
    "close_state",
    "quality_flags",
    "resample:1m->4h"
  ],
  "quality_summary": {
    "flagged_bars": 0,
    "incomplete_bars": 0
  }
}
```

## 兼容策略

旧版 `/api/market/klines` 暂时保留，供尚未迁移的调用方使用。首页 K 线已经
切换到 Gold 接口。新代码不得继续依赖旧接口，待调用方全部迁移后再单独安排
废弃周期。

## 当前边界

- Bronze 目前是 Provider 原生缓存，并非完整不可变 raw payload 数据湖；
- `received_at`、源文件 checksum、ingestion run ID 尚未进入现有 Provider 表；
- Gold 当前按请求可重建，尚未物化为独立表；
- DuckDB 仍采用单进程单写者约束。

这些边界不影响当前 K 线接口的分层语义；后续可替换各层物理实现，而保持
`KlineLayerPipeline.load()` 和 `/api/v1/market/klines` 接口稳定。
