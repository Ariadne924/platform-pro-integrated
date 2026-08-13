# web/ 四页与 sim_platform 源的差异说明（API_DIFF）

四页拷贝自 `sim_platform-main/sim_platform-main/sim_platform-main - CUDA/cryptopaperlab/web/`
（lab.png 一并拷贝，about.html 引用）。按任务书只允许「API 路径 / 字段映射」两类改动；
另按 05 任务书明确指令隐藏 AI 策略工厂入口（Q4D 不做）。全部改动如下，除此之外逐字节一致。

## A. API 路径改动（3 处）—— 避让 exchangia 既有冻结路由

exchangia 自带的 9 个路由模块占用了 `GET /api/factors`、`GET /api/strategies`、
`DELETE /api/factors/{name}`，且 tests/ 冻结了它们的响应形状（如
`tests/test_web_introspect.py:245` 按 `[{name: ...}]` 消费 `GET /api/factors`），
不能改 shape。Starlette 按注册顺序匹配，后挂的同名路径永远轮不到 →
前端这 3 个调用改到无冲突的 `/api/registry/*` 新路径，响应保持 sim 形状。

| # | 文件:行 | 改前 | 改后 | 原因 |
| --- | --- | --- | --- | --- |
| A1 | index.html:1262 | `api('/factors')` | `api('/registry/factors')` | `GET /api/factors` 被 exchangia 冻结路由占用且 shape 不同（tests 冻结） |
| A2 | index.html:1490 | `api('/strategies')` | `api('/registry/strategies')` | `GET /api/strategies` 同上被占用 |
| A3 | bias-control.html:473 | DELETE `` `/api/factors/${id}` `` | DELETE `` `/api/registry/factors/${id}` `` | `DELETE /api/factors/{name}` 被 exchangia 删实例路由占用（先注册先匹配） |

## B. 字段映射改动（0 处）

无。后端按 sim_platform 的响应形状实现，前端读取字段零改动。

## C. 隐藏 AI 策略工厂入口（4 处）—— 05 任务书明确指令

仅加 `style="display:none"`，DOM 与 JS 原样保留，后续恢复入口删一个属性即可。

| # | 文件:行 | 元素 |
| --- | --- | --- |
| C1 | index.html:361 | 顶部导航 `#nav-factory`「策略工厂」按钮 |
| C2 | explorer.html:309 | 顶部导航 `.accent-btn`「⚙ 策略工厂」按钮 |
| C3 | explorer.html:365 | 预览面板 `#btn-factory`「⚙ 策略工厂」按钮 |
| C4 | about.html:106 | 顶部导航「策略工厂」按钮（跳 index.html#factory） |

about.html 正文中介绍策略工厂的文档段落（h3/特性列表）是说明文字而非入口，未动。

## 复算 sha256 的命令

```bash
cd "sim_platform-main/sim_platform-main/sim_platform-main - CUDA/cryptopaperlab/web"
sha256sum index.html explorer.html bias-control.html about.html lab.png
cd ../../../../../web   # 本仓库 web/
sha256sum index.html explorer.html bias-control.html about.html lab.png
# 差异应仅限上文 A/C 七处；diff -u 逐文件比对可直接核对
```
