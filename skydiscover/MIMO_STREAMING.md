# MIMO 流式输出改动说明（`stream: true`）

- **日期**：2026-10-17
- **基线提交**：`423cde8`（分支 `backup/before-mimo-stream`）
- **目标模型**：`mimo-v2.6-flash`（OpenAI 兼容端点 `http://192.168.1.19:4000/v1`）

---

## 1. 改动目的

让配置里的 `stream: true` 真正生效：LLM 请求以流式（SSE）方式发送，
客户端逐 chunk 接收并拼成完整文本后返回。

这样做的原因是：mimo 生成长回复时，非流式请求在网关侧长时间没有数据返回，
容易触发网关的 120 秒空闲超时；流式下连接持续有数据，可以避免这类超时。

同时明确要求：**不在控制台打印任何流式增量**。实时 chunk 只累积在内存里，
对上层调用方和日志的表现与原来的非流式模式完全一致。

改动保持**向后兼容**：不配置 `stream` 时仍走原来的非流式路径。

---

## 2. 改动位置一览

| 文件 | 位置 | 作用 |
|---|---|---|
| `skydiscover/optimize/config.py` | `LLMModelConfig`，L196-L200 | 新增 `stream` 配置字段，避免加载配置时报“未知字段” |
| `skydiscover/optimize/config.py` | `LLMConfig.__post_init__().shared_config`，L268-L271 | 让 `llm:` 层的 `stream` 下发给各模型 |
| `skydiscover/optimize/llm/openai.py` | `OpenAILLM.__init__`，L68 | 从模型配置读取 `stream` |
| `skydiscover/optimize/llm/openai.py` | `_generate_text`，L162-L166 | 流式开启时给请求参数加 `stream: true` |
| `skydiscover/optimize/llm/openai.py` | `_call_api`，L236-L243 | 流式请求走专用分支 |
| `skydiscover/optimize/llm/openai.py` | 新增 `_stream_chat_completions`，L255-L271 | 静默消费流式响应并拼接文本 |
| `tests/config/test_llm_params.py` | L177-L235 | 新增 5 个测试覆盖配置传播、参数注入、静默拼接 |

---

## 3. 改前 / 改后源码

### 3.1 `skydiscover/optimize/config.py`：`LLMModelConfig` 新增字段

**改前**

```python
    # Request parameters
    timeout: Optional[int] = None
    retries: Optional[int] = None
    retry_delay: Optional[int] = None

    # Reasoning parameters
    reasoning_effort: Optional[str] = None
```

**改后**

```python
    # Request parameters
    timeout: Optional[int] = None
    retries: Optional[int] = None
    retry_delay: Optional[int] = None
    # Stream the Chat Completions response. Chunks are accumulated silently;
    # nothing is printed to the console.
    stream: Optional[bool] = None

    # Reasoning parameters
    reasoning_effort: Optional[str] = None
```

> 说明：`LLMConfig` 继承自 `LLMModelConfig`，所以 `llm:` 顶层也能写 `stream`。
> 加载配置时 `_reject_unknown_keys` 不会再把它判为非法键。

### 3.2 `skydiscover/optimize/config.py`：`llm:` 层参数下发

**改前**

```python
        shared_config = {
            "api_base": self.api_base,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "retries": self.retries,
            "retry_delay": self.retry_delay,
            "reasoning_effort": self.reasoning_effort,
        }
        self.update_model_params(shared_config)
```

**改后**

```python
        shared_config = {
            "api_base": self.api_base,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "retries": self.retries,
            "retry_delay": self.retry_delay,
            "reasoning_effort": self.reasoning_effort,
            "stream": self.stream,
        }
        self.update_model_params(shared_config)
```

> `update_model_params` 只在模型字段为 `None` 时填充，因此**模型级配置优先于 `llm:` 级配置**。

### 3.3 `skydiscover/optimize/llm/openai.py`：`__init__` 读取配置

**改前**

```python
        self.api_key = model_cfg.api_key
        self.reasoning_effort = getattr(model_cfg, "reasoning_effort", None)
```

**改后**

```python
        self.api_key = model_cfg.api_key
        self.reasoning_effort = getattr(model_cfg, "reasoning_effort", None)
        self.stream = getattr(model_cfg, "stream", None)
```

### 3.4 `skydiscover/optimize/llm/openai.py`：`_generate_text` 注入请求参数

**改前**

```python
        # Add response_format if requested (e.g. {"type": "json_object"})
        response_format = kwargs.get("response_format")
```

**改后**

```python
        # Stream responses when enabled; chunks are accumulated silently in
        # _stream_chat_completions (no console output).
        if kwargs.get("stream", self.stream):
            params["stream"] = True

        # Add response_format if requested (e.g. {"type": "json_object"})
        response_format = kwargs.get("response_format")
```

> 单次调用也可以用 `generate(..., stream=False)` 临时关闭，或 `stream=True` 临时开启。

### 3.5 `skydiscover/optimize/llm/openai.py`：`_call_api` 分流

**改前**

```python
    async def _call_api(self, params: Dict[str, Any]) -> str:
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                None, lambda: self.client.chat.completions.create(**params)
            )
            return response.choices[0].message.content
```

**改后**

```python
    async def _call_api(self, params: Dict[str, Any]) -> str:
        loop = asyncio.get_running_loop()
        try:
            if params.get("stream"):
                return await loop.run_in_executor(
                    None, lambda: self._stream_chat_completions(params)
                )
            response = await loop.run_in_executor(
                None, lambda: self.client.chat.completions.create(**params)
            )
            return response.choices[0].message.content
```

### 3.6 `skydiscover/optimize/llm/openai.py`：新增静默流式消费方法

**改前**：不存在该方法。

**改后**（位于 `_call_api` 与 `_call_api_via_responses` 之间）

```python
    def _stream_chat_completions(self, params: Dict[str, Any]) -> str:
        """Consume a streaming Chat Completions response and return the full text.

        Runs in a worker thread via ``run_in_executor``. Chunks are only
        accumulated into ``parts`` — nothing is printed or logged per chunk, so
        console output is identical to the non-streaming path.
        """
        parts: List[str] = []
        stream = self.client.chat.completions.create(**params)
        for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            content = getattr(choices[0].delta, "content", None)
            if content:
                parts.append(content)
        return "".join(parts)
```

---

## 4. 如何使用

### 4.1 配置文件（当前 CCA5 的用法）

在 `benchmarks/CCA5/config.yaml` 的模型条目下加一行：

```yaml
llm:
  models:
    - name: "mimo-v2.6-flash"
      weight: 1.0
      api_base: "http://192.168.1.19:4000/v1"
      api_key: "sk-..."
      stream: true
```

### 4.2 也可以写在 `llm:` 顶层

对该层所有模型生效：

```yaml
llm:
  stream: true
  models:
    - name: "mimo-v2.6-flash"
      weight: 1.0
      api_base: "http://192.168.1.19:4000/v1"
      api_key: "sk-..."
```

模型条目里显式写了值（`true` 或 `false`）时以模型条目为准。

### 4.3 保持默认（不改行为）

不写 `stream` 或写 `stream: false`，仍走原来的非流式请求。

### 4.4 运行 CCA5

容器内（与之前实验一致）：

```bash
docker exec -it quic_sky /bin/bash
cd /workspace/skydiscover

uv run skydiscover optimize benchmarks/CCA5/initial_program.py \
  benchmarks/CCA5/evaluator/evaluator.py \
  -c benchmarks/CCA5/config.yaml
```

启动阶段如果配置有误，会立即报 `Unrecognised key(s)`；现在 `stream` 已合法，不会再报。

---

## 5. 行为与注意事项

1. **控制台零输出**：每个 chunk 只追加到 `parts`，没有 `print`，也没有逐 chunk 日志；
   最终返回的 `LLMResponse.text` 与非流式模式完全相同。
2. **重试/超时逻辑不变**：仍由 `_generate_text` 的 `retries`/`retry_delay`/`asyncio.wait_for`
   控制，超时按整个响应的总时长计算。
3. **只影响 Chat Completions 路径**：Responses API 回退路径和图像生成路径没有改动。
4. **网关需支持标准 SSE 流式**。当前端点 `192.168.1.19:4000` 为 OpenAI 兼容网关；
   代码没有发送 `stream_options` 等扩展参数，兼容性最好。
5. **mimo thinking 的已知问题仍然存在**：流式解决的是网关空闲超时，
   不能解决“思考内容吃光 `max_tokens` 导致正文为空”的问题。此前实验结论是
   mimo 需设置 `reasoning_effort: "none"` 才能稳定跑 CCA5；本改动不会改变这一点。
6. **usage-only chunk 已处理**：`choices` 为空的 chunk 会被跳过。

---

## 6. 验证情况

### 6.1 已执行的独立验证

由于 `.venv` 和 `quic_sky` 容器内都没有 pytest，使用临时脚本（未加入仓库）
`/tmp/verify_stream.py` 做了等价验证，结果：

```text
[config] model=mimo-v2.6-flash stream=True api_base=http://192.168.1.19:4000/v1
[stream] text='Hello, world' stdout='' stderr=''
[generate] text='ok' stream_param=True
ALL OK
```

覆盖了：

- 真实 `benchmarks/CCA5/config.yaml` 能正常加载，`stream=True` 落到模型配置；
- 多个 chunk 正确拼接为 `Hello, world`，且 stdout/stderr 均为空；
- `generate()` 发出的请求参数中包含 `stream=True`。

### 6.2 新增单元测试

在 `tests/config/test_llm_params.py` 中新增：

- `test_stream_param_included_when_enabled`
- `test_stream_param_absent_by_default`
- `test_stream_chunks_are_concatenated_silently`
- `test_llm_level_stream_propagates_to_models`
- `test_model_level_stream_is_not_overwritten`

环境具备 pytest 时运行：

```bash
cd /workspace/skydiscover
uv run pytest tests/config/test_llm_params.py -q
```

---

## 7. 回退方式

只回退本次代码改动（保留基线提交）：

```bash
cd /home/cll/cca-sky/skydiscover
git diff 423cde8 -- \
  skydiscover/optimize/config.py \
  skydiscover/optimize/llm/openai.py \
  tests/config/test_llm_params.py
```

把单个文件恢复到基线：

```bash
git checkout 423cde8 -- skydiscover/optimize/llm/openai.py
```

彻底丢弃所有未提交改动：

```bash
git checkout backup/before-mimo-stream
git reset --hard 423cde8
```

---

## 8. 相关提交与分支

| 项目 | 值 |
|---|---|
| 基线分支 | `backup/before-mimo-stream` |
| 基线提交 | `423cde8` `chore: snapshot before adding mimo streaming support` |
| 原始分支 | `main`（未改动，未推送） |
| 本次改动 | 工作区未提交（3 个文件） |
