# astrbot_plugin_tweet

从 `nonebot_tweet` 迁移到 AstrBot 的插件版本。

功能：
- 自动识别 `x.com / twitter.com` 推文链接
- 从指定 RSSHub 实例拉取推文文本、图片、视频
- 默认模式下使用 AstrBot 内置 LLM 接口自动翻译推文
- 支持 `c/content`（仅媒体）与 `o/origin`（仅原文）前缀
- 自动识别并拉取 `booth.pm` 商品名称与图片

## 触发方式
- 直接发送推文链接：`https://x.com/<user>/status/<id>`
- 仅媒体：`c https://x.com/<user>/status/<id>` 或 `content https://...`
- 仅原文：`o https://x.com/<user>/status/<id>` 或 `origin https://...`
- 发送 BOOTH 链接：`https://booth.pm/xx/items/<id>`

## 配置项
通过 `_conf_schema.json` 在 AstrBot 管理面板配置：
- `rsshub_base_url`: RSSHub 基础地址（默认 `https://rsshub.app/twitter/user/`）
- `rsshub_query_param`: 附加查询参数
- `translate_enabled`: 是否启用翻译
- `translate_target_language`: 翻译目标语言
- `translate_provider_id`: 翻译使用的 provider（留空则跟随当前会话）
- `translate_fallback_provider_ids`: 翻译失败时按顺序重试的 provider 列表
- `detect_language_before_translate`: 翻译前语言识别（优先 Google 免 Key，失败回退 LLM）
- `booth_locale`: BOOTH 接口语言区域
- `request_timeout_sec`: 网络请求超时秒数

## 说明
- 翻译调用使用 AstrBot 的 `context.llm_generate()` 内置接口实现。
- 当 `translate_provider_id` 为空时，会自动使用当前会话的聊天模型。
- 当 `translate_fallback_provider_ids` 为空且 `translate_provider_id` 也为空时，会自动继承当前会话配置中的 `provider_settings.fallback_chat_models`。
- 当主翻译模型请求异常、返回错误响应，或返回空文本时，插件会按 fallback 顺序继续重试。
