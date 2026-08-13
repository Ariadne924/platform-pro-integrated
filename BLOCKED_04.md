# BLOCKED_04.md — 04 阶段受阻项

无。

记录在案（非阻塞，详见 PROGRESS_04.md「已知限制」）：

- 研究池 4 个标的（PEPEUSDT/SHIBUSDT/FLOKIUSDT/BONKUSDT）在数据源端
  Invalid symbol，评级/指标逐 symbol 剔除并如实告警（metrics status=PARTIAL），
  属源端符号有效性，非机制缺陷。
- config 通道（decorator）因子无实现文件路径，full_sample 静态扫描对其如实
  BLOCKED（无证据不判 PASS），评级/指标不受影响。
- 评级窗口（近 N 天）需要最近期的 sub-daily K 线，未覆盖区间由 CachingProvider
  走 vision 归档按需补齐（机制同 01/03，非直连 API）；首次全池评级约 1~2 分钟，
  此后落缓存。
