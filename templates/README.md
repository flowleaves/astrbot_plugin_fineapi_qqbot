# 星芽 · 模型状态文转图模板

文件：`model_status.html`

AstrBot 的自定义文转图模板只允许三个 Jinja2 变量：

- `text`
- `version`
- `shiki_runtime`

本模板只使用 `{{ text | safe }}` 和 `{{ version }}`，不存在 `title`、`models` 等自定义变量。
模型状态数据全部通过 `text` 传入，再由页面内 JavaScript 解析成卡片。

预览：[`../docs/model-status-preview.png`](../docs/model-status-preview.png)

## 传入格式

推荐在 Markdown 前放一行 JSON 注释：

```text
<!--XINGYA_MODEL_STATUS:{"title":"模型状态","subtitle":"近 1 小时 · 调用正确率","window_text":"18:44 → 19:44","query":"gemini","matched":9,"shown":9,"total_calls":1377,"overall_rate":0.984,"generated_at":"19:44","models":[{"name":"gemini-3.8-flash","calls":512,"rate":1.0,"status":"ok","segments":["ok","ok"],"latency_ms":640}]}-->

**gemini · 近 1 小时**

| 模型 | 调用 | 正确率 | 状态 |
|---|---:|---:|---|
| gemini-3.8-flash | 512 | 100.00% | 稳定 |
```

### `models` 字段

| 字段 | 说明 |
|---|---|
| `name` | 模型名 |
| `calls` | 近 1 小时调用数 |
| `rate` | 0-1 的成功率；`null` 表示数据不足 |
| `status` | `ok` / `warn` / `bad` / `na` |
| `status_text` | 可选，默认「稳定 / 波动 / 异常 / 数据不足」 |
| `segments` | 可选，30 段状态条，元素为 `ok/warn/bad/na` |
| `latency_ms` | 可选，平均延迟 |

`status` 与阈值建议：

| 条件 | status | 文案 |
|---|---|---|
| `calls < 5` | `na` | 数据不足 |
| `rate >= 0.95` | `ok` | 稳定 |
| `0.80 <= rate < 0.95` | `warn` | 波动 |
| `rate < 0.80` | `bad` | 异常 |

## 两种解析方式

1. **优先读 JSON 注释**：有 `<!--XINGYA_MODEL_STATUS:{...}-->` 时，直接按 `models` 渲染，
   可以使用精确的 `segments` 和 `latency_ms`。
2. **没有 JSON 时读 Markdown 表格**：模板会自动寻找带「模型 / 调用 / 正确率 / 状态」表头的表格，
   把表格行转换成卡片；状态条按成功率自动生成。

如果两种方式都找不到模型数据，模板会回退为普通 Markdown 渲染（使用 `text`）。

## 使用

把 `model_status.html` 全文粘贴到 AstrBot 的文转图模板配置里，
然后插件侧用类似下面的方式生成 `text` 并调用 `text_to_image` / `html_render`。

```python
text = "<!--XINGYA_MODEL_STATUS:" + json.dumps(data, ensure_ascii=False) + "-->\n"
text += "| 模型 | 调用 | 正确率 | 状态 |\n|---|---:|---:|---|\n"
# ... 追加表格行

url = await self.text_to_image(text)
yield event.image_result(url)
```

> 注意：AstrBot 的模板校验不允许自定义 Jinja2 变量，所以不要把数据直接写进
> `{{ models }}`、`{{ title }}` 之类的变量。
