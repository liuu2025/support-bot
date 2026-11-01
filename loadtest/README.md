# 压测（Locust）

模拟大量并发客户同时持有 SSE 长连接，外加管理员旁观、恶意客户端和竞争条件探测，
同时校验"快"和"对"：不出现违反政策的退款、不重复退款、护栏仍然生效。

## 1. 起一个假模型服务（不花钱、可复现）

应用本身没有假模型，它只跟 OpenAI 兼容接口说话。压测时把接口指向本目录里的
`mock_openai.py` —— 它实现了 `GET /v1/models` 与 `POST /v1/chat/completions`，
被要求退款时会返回 `issue_refund` 工具调用，因此走的是**真实代码路径**，只有模型是假的。

```bash
python loadtest/mock_openai.py --port 7800
```

然后在 `.env` 里指向它（容器内访问宿主机用 `host.docker.internal`）：

```bash
MODEL_BASE_URL=http://host.docker.internal:7800/v1
MODEL_API_KEY=mock
MODEL_NAME=loadtest-model
```

> 也可以直接在网页「模型设置」里填这组地址，效果一样（地址不做内网/回环限制，
> 本地自建模型照原样填即可）。

## 2. 造合成数据

```bash
python -m scripts.seed_synthetic      # 在 backend/ 目录下执行
```

## 3. 跑压测

```bash
pip install -r loadtest/requirements.txt
locust -f loadtest/locustfile.py --headless -u 200 -r 20 -t 5m --host http://localhost:7101
```

退出时 `slo.py` 会给出判定并设置退出码。自定义指标（请求类型 `SSE`）：
`chat:ttfe`（首事件延迟）、`chat:full_turn`（整轮耗时）、`chat:agent_error`
（出现 `error` 事件的轮次）、`chat:wrong_decision`（与政策矛盾的退款，永远算失败）、
`chat:shed_503`（背压拒绝，过载时属预期）。
