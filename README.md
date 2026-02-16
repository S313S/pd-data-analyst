# 拼多多商品内容生成 MVP

一个最小可运行工具，支持以下流程：

1. 输入拼多多商品链接（或微信分享文本中的链接）
2. 自动抓取：
   - 标题
   - 主图（URL）
   - 视频（URL）
3. AI 输出：
   - 卖点拆解
   - 30 秒带货脚本
   - 小红书版本改写

## 快速启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
streamlit run app.py
```

## 可选：配置 AI

如果不配置 AI Key，系统会使用本地模板兜底输出。

```bash
export OPENAI_API_KEY=your_key
export OPENAI_MODEL=gpt-4o-mini
```

## 可选：观察 Playwright 可视化窗口

默认使用无头模式。若想看到浏览器窗口：

```bash
export PLAYWRIGHT_HEADLESS=0
```

## 抓取策略

- 第一层：`requests + BeautifulSoup` 静态抓取（同时尝试原始分享页与 `goods_id` 规范化链接）
- 第二层（自动兜底）：当标题/主图/视频缺失时，触发 `Playwright` 动态渲染抓取并合并结果
- 动态抓取会额外监听网络请求与接口 JSON，提高视频链接命中率
- 支持可选 Cookie 输入（用于 `needs_login=1` 的分享链接）
- 默认先登录后抓取：点击“开始生成”会自动打开浏览器并加载目标链接
- 若检测到未登录，需在浏览器完成登录，并勾选“登录状态：我已完成登录”后再次点击“开始生成”
- 不使用登录等待倒计时策略，改为人工确认登录
- 登录会话会保存到 `.playwright_storage_state.json`，避免重复扫码
- 每次抓取使用独立浏览器会话（避免线程冲突），但登录态会自动复用

## 说明与限制

- 这是 MVP 版本，动态渲染可提升命中率，但仍可能受登录态、风控或页面策略影响。
- 若没安装浏览器内核（`playwright install chromium`），系统会回退为静态抓取。
- Playwright 默认以无头模式运行，你在桌面上看不到浏览器窗口，这是正常行为。
- 如果标题显示“拼多多商城”，通常是链接被重定向到首页。建议使用包含 `goods_id` 的商品分享链接。
- 建议后续升级：
  - 增加代理池和重试
  - 增加商品详情 API/合作渠道接入
  - 增加结果缓存与任务队列
