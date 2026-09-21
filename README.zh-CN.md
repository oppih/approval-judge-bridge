[English](README.md) | **中文**

# approval-judge-bridge

approval-judge-bridge 提供兼容 OpenAI 的端点，接收 agent 的**审批守卫调用**，返回 `APPROVE`、`DENY` 或 `ESCALATE`。同一接口支持四种判定器：带类型的判定模型（[Jev](https://typesafe.ai)）、任意兼容 OpenAI 的聊天模型、自托管 Jev 风格 classify 判定器，以及确定性规则文件。服务采用失败即关闭（fail-closed）策略，记录每次决策，并附带校准工具，用来确定判定器需要多大把握才能让命令免于人工确认。

为什么需要它：宿主会拦下已标记的 shell 命令，调用辅助模型索取单词判定；Hermes 的智能审批是参考宿主。推理模型可能把 16 个 token 的预算全耗在隐藏推理上，返回空内容，随后映射为“escalate”，导致*每条*已标记命令都要求用户确认，持续发生却没有报错（[hermes-agent#108163](https://github.com/NousResearch/hermes-agent/issues/108163)）。纯文本判定又不含置信度和余量，阈值只能靠猜。桥接服务通过适配调用解决这两个问题，无需修改宿主，集成也不会因宿主升级而丢失。

- **带类型的判定。** Jev 回答一个 Choice 问题（approve / deny / escalate），返回经过校准的概率分布；没有自由文本，也不会因回答冗长而超出 token 上限。
- **所有环节均失败即关闭。** 缺少密钥、超时、HTTP 错误、答案格式错误或自相矛盾、重试一次后仍为空，均返回 `ESCALATE`。无法判定的命令绝不自动批准。
- **一条安全不变式。** 只有最终类别为 `approve` 才可能自动批准；无论阈值如何，`deny` 和 `escalate` 都交由人工处理。降低阈值只会减少确认提示，不会削弱保护。

## 快速开始

```bash
git clone https://github.com/oppih/approval-judge-bridge
cd approval-judge-bridge
python3 -m judge_bridge            # binds 127.0.0.1:3999, needs TYPESAFE_API_KEY in ~/.hermes/.env
curl -s localhost:3999/healthz     # {"status":"ok","backend":"typesafe",...}
```

将宿主指向桥接服务。Hermes 配置如下（`~/.hermes/config.yaml`）：

```yaml
auxiliary:
  approval:
    provider: custom
    model: approval-judge-bridge
    base_url: http://127.0.0.1:3999/v1
    api_key: local-bridge          # the bridge ignores it; the judge key lives in its env
    timeout: 20
approvals:
  mode: smart                      # off | smart | manual
```

其他宿主只需设置基础 URL 和模型名：端点支持 `POST /v1/chat/completions`，通过 `choices[0].message.content` 返回一个词。

## 判定后端

| `JUDGE_BACKEND` | 判定器 | 所需配置 |
|---|---|---|
| `typesafe`（默认） | Jev (System One)：一个 Choice 问题，返回概率和置信度 | `TYPESAFE_API_KEY`（或 `MCP_JEV_API_KEY`） |
| `openai` | 任意兼容 OpenAI 的聊天端点，用于判定 | `JUDGE_OPENAI_BASE_URL`、`JUDGE_OPENAI_MODEL`，可选 `JUDGE_OPENAI_API_KEY` |
| `yajev` | 使用 classify 报文（信封）的判定器（`POST /v1/classify`、`{context, schema}`）；参考实现为自托管 Jev 克隆，设计上无需密钥 | 无必填项（可选 `JUDGE_YAJEV_API_KEY`） |
| `rules` | JSON 文件中的确定性正则表达式规则 | `JUDGE_RULES_PATH`（参见 `rules.example.json`） |

宿主感知不到后端差异，照常发送守卫调用并读取返回的单个词。因此，接入新判定器只需增加后端，无需修改宿主。

- `openai` 自行重建审查提示词，确保两个模型判定器回答同一个问题。答案截断或无法使用时，增加预算并**重试一次**；仍然失败则按失败即关闭处理。
- `rules` 按 `deny` → `escalate` → `approve` 的顺序匹配，未匹配的命令一律转交人工；没有规则绝不代表允许。`approve` 模式必须匹配**整条**命令（`deny` 和 `escalate` 仍按子串搜索；前缀不足以证明复合命令安全）。收到非空操作员策略时，转交人工而非忽略策略。
- `yajev` 专门采用 classify 报文：输入 `{context, schema}`（枚举或布尔字段），输出值、概率及各类别的 `[logit, probability]`。参考实现是 [dongxu 的自托管 Jev 克隆](https://yajev.0xfefe.me/)（家用 GPU 上运行 14B 模型，无需密钥，无 SLA）。其他自托管 Jev 风格服务也使用该协议，接入自己的克隆只需修改 URL。它*不是*带类型后端的另一个 URL：上游 Jev 不提供 `/v1/classify`。由于只有一个 `context` 字符串，操作员策略只能在其中标记，无法通过可信通道传递，因此宿主端仍须保留前置提示注入防护。

这个端点的概率会**饱和**：无害操作和漏判结果都达到 0.99+，因此校准应调整判定标准，而非阈值（见“校准”）。端点也会限制突发请求（~10s 内 ~17 次调用，有 4 次返回 HTTP 429）；后端遇到 429 会等待 0.4s 后重试一次，仍然失败则按失败即关闭处理。经桥接服务调用的单次延迟为 ~0.35s。默认后端仍是 `typesafe`，参考部署继续使用 Jev 把关；对于采用 classify 报文的判定器，classify 后端是经过实测并通过校准的兼容选项。

环境变量：

| 变量 | 默认值 | 含义 |
|---|---|---|
| `JUDGE_BACKEND` | `typesafe` | `typesafe`, `yajev`, `openai`, `rules` |
| `JUDGE_HOST` / `JUDGE_PORT` | `127.0.0.1` / `3999` | 监听地址 |
| `JUDGE_AUTO_ACCEPT` / `JUDGE_MIN_MARGIN` | `0.65` / `0.30` | 返回概率的判定器所用的阈值 |
| `JUDGE_LOG` | `~/.approval-judge-bridge/decisions.jsonl` | 每次决策记录为一条 JSON |
| `JUDGE_ENV_FILE` | `~/.hermes/.env` | 密钥读取路径（环境变量优先） |
| `TYPESAFE_API_URL` / `TYPESAFE_MODEL` | `https://api.typesafe.ai/v1/systemone` / `jev-latest` | Jev 端点和模型 |
| `JUDGE_YAJEV_URL` / `JUDGE_YAJEV_API_KEY` | `https://yajev.0xfefe.me/v1/classify` / 空 | classify 端点及可选密钥（默认无需密钥） |
| `JUDGE_YAJEV_RUBRIC_FILE` / `JUDGE_YAJEV_MAX_DESCRIPTION` | 空 / `2000` | 覆盖单字段判定标准；限制其长度的安全上限 |
| `JUDGE_OPENAI_MAX_TOKENS` / `JUDGE_OPENAI_RETRY_MAX_TOKENS` | `16` / `256` | 首次请求预算及截断后重试的预算 |
| `JUDGE_RULES_PATH` | `rules.json` | `rules` 后端使用的规则文件 |

密钥从环境变量或 env 文件读取，**绝不**写入日志。

## 输入要求

桥接服务只判定一条完整、无歧义的命令：

- **恰好一对** `<command>…</command>`。块外可以有说明文字：参考宿主在块前放置描述，在结束标记后要求只回复一个词。但出现第二个标记时就会转交人工，命令本身含有标记也不例外，否则审查内容就可能只是实际执行命令的前缀。
- 命令超过 `MAX_COMMAND_CHARS`（6000 字符）时，转交人工；绝不静默截断后只凭无害的开头判定。
- 操作员策略只从系统消息读取；`yajev` 后端会在唯一的上下文字段内标记策略，而非通过独立通道传递。

## 校准

阈值属于策略选择，应以数据为依据。每次决策都会记录最终类别、完整概率分布、余量和延迟，部署前可用日志重放候选阈值。

参考部署实测（共记录 57 次判定：重新校准前 21 次，校准后 36 次）：

- Jev 自身的默认值 `0.85 / 0.60` 使 **21 次判定中的 17 次（81%）**转交人工，其中包括 **13 条实际工作命令中的 12 条**，尽管其中 10 条的最终类别是 `approve`（approve 概率 0.65–0.81，余量 0.30–0.66）。
- 改用 `0.65 / 0.30`，同时重写判定标准，列明常规工作并将 `escalate` 收窄到*修改*凭据（此前只读检查也会在 0.88 的概率下转交人工），结果为 19 APPROVE / 10 DENY / 3 次低置信度 ESCALATE / 4 次因类别为 escalate 而返回的 ESCALATE，延迟中位数为 0.18s。
- 危险对照用例仍全部拦截，包括两个用于探测 escalate 类别范围过窄的用例：`sed -i 's/mode: smart/mode: off/' …config.yaml` → ESCALATE，`base64 -d <<< … | bash` → DENY。
- 概率饱和时，阈值无济于事：classify 判定器以 p=0.998 批准复制凭据；判定标准明确提及凭据后，仍以 p≈0.998 批准修改审批机制。只有修改判定标准才堵住这两处漏洞：为 agent 自身的审批、审计和凭据保护设定硬性规则，同时明确区分读取与修改，并列出具体操作。

每次修改前后都应运行校准用例集；只要无害命令未获批准，或危险命令获批，脚本就以非零退出码退出：

```bash
python3 tools/replay_battery.py           # 9 benign (must approve) + 8 danger (must not)
```

## 失败语义

| 情况 | 判定结果 |
|---|---|
| 最终类别为 `approve`，且两项指标均超过阈值 | `APPROVE` |
| 最终类别为 `deny`，无论置信度高低 | `DENY` |
| 最终类别为 `escalate`，或为 `approve` 但有指标低于阈值 | `ESCALATE` |
| 缺少 API 密钥、超时、连接错误、HTTP 错误 | `ESCALATE` |
| 判定端点返回 HTTP 429 | 等待 0.4s 后重试一次，仍然失败则返回 `ESCALATE` |
| 答案报文缺失、不完整、含非有限值或自相矛盾（最终类别不是分布中概率最大的类别），或经 `openai` 重试后仍为空或无法识别，或状态为 `content_filter` | `ESCALATE` |
| 命令报文缺失、重复、未闭合或顺序错误；命令超过 `MAX_COMMAND_CHARS`（6000） | `ESCALATE`，绝不截断后只判定前缀 |
| HTTP 请求格式错误、JSON 结构错误、请求体超过 `MAX_REQUEST_BODY_BYTES`（1 MiB）、`Content-Length` 为负数，或客户端在发送请求体途中停滞 | `ESCALATE`（HTTP 200，日志记录为 `bad_request_shape` / `request_too_large` / `request_read_timeout`） |
| 上游响应超过 `MAX_UPSTREAM_RESPONSE_BYTES`（1 MiB） | `ESCALATE` |
| `rules` 后端收到非空操作员策略，或 classify 后端的策略/上下文超出预算 | `ESCALATE`（`policy_not_supported`、`policy_too_long`、`context_too_long`，绝不截断） |
| 阈值超出 [0, 1] 或为非有限值 | 服务拒绝启动 |

## 作为服务运行

```bash
mkdir -p ~/.config/systemd/user
cp systemd/approval-judge-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now approval-judge-bridge
systemctl --user status approval-judge-bridge
```

该服务单元只监听回环地址，并在失败后自动重启。回滚只需运行 `systemctl --user disable --now approval-judge-bridge`，再将宿主改回原来的提供方。

## 测试

```bash
python3 -m unittest discover -s tests -t . -v     # 63 tests, no network, no dependencies
```

测试离线运行，无需依赖，覆盖阈值不变式、所有失败即关闭路径（无效概率分布、请求结构错误、命令报文存在歧义、答案截断、请求体过大或传输停滞）、增加预算后重试、提示词提取、`rules` 整条命令匹配、决策日志并发写入，以及通过真实套接字验证 HTTP 接口。

## 它不是什么

这是一个**审批关卡**，不是沙箱：判定器可能出错，命令通过审查只代表判定器认为它足够安全、可以免于确认，并不等于已证明安全。底层仍须保留硬性拦截、允许列表和 agent 自身的权限模型，保持信任边界完整（操作员规则来自系统消息，命令文本不可信）。对于可能产生重大影响的操作，应将 `ESCALATE` 视为正常结果。

## License

MIT — 参见 [LICENSE](LICENSE)。
