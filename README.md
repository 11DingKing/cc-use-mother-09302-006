# 学生互动轮转排队后端

木偶互动课每轮接待名额有限。本服务用**事件溯源 + 确定性公平排序**生成可解释队列，
支持空缺席位的原子递补、通知送达回执、监护授权撤回、临时扩容、跨场转移、
并发/重复签到的确定处理，并按角色裁剪敏感信息。相同命令携带相同幂等键重放，
不会重复占位。

## 目录

- `domain/contract.json`：领域角色、状态、不变量与样例。
- `src/domain_contract/`：契约读取与确定性校验（既有）。
- `src/rotation_queue/`：轮转排队后端。
  - `events.py`：不可变领域事件。
  - `ranking.py`：纯函数、可解释的公平排序键。
  - `model.py`：事件投影（`State`）与命令处理；所有业务规则集中于此。
  - `store.py`：SQLite 事件存储，单事务追加 + 命令级幂等。
  - `service.py`：应用服务，编排命令与读取视图。
  - `views.py`：读取模型与基于角色的隐私裁剪。
  - `api.py` / `server.py`：标准库 HTTP 接口与启动入口。
- `tests/`：排序单测、应用流程、HTTP 集成、持久化与并发压力测试。
- `tools/check_contract.py`：契约命令行检查。

## 排序规则（全部可解释）

排序键按优先级升序比较，任一步都记录到 `reason_text` / `factors`：

1. **监护授权有效**——授权撤回者立即移出所有未关闭场次，不参与排序。
2. **班级内历史参与次数升序**——参加得越少越靠前，从根上避免"有人连续参加、
   有人几周排不到"。历史次数在**签到完成**后才增长。
3. **经批准且在有效期内的照顾依据**——医疗类 > 无障碍类 > 其他 > 无；
   只比较经批准的依据，不接受自报。
4. **报名时间升序**——先报先得；跨场转入者沿用原报名时刻，等待时长不打折。
5. **学生 ID 决胜**——业务因素完全相同的并列者，以学生 ID 做最终决胜，
   结果与执行节点、时钟、字典遍历顺序无关。

## 关键不变量

- **原子递补**：取消、授权撤回、跨场转出手产生的空缺，与候补晋升、递补通知
  在**同一个事务的同一批事件**内完成，不会出现"空位无人补"或"一个名额补两人"。
- **临时扩容**：只能增不能减；扩容后立即按同一排序键批量递补。
- **跨场转移**：仅限同班场次；接受转移时源场原子递补，目标场以原报名时刻入队；
  待处理申请不可重复发起，已决定申请不可重复接受。
- **同时签到**：重复签到返回 `409 already_checked_in`，历史次数只增一次；
  候补者不能签到。
- **重放幂等**：写请求携带 `Idempotency-Key`，同键重放返回首次结果（`replayed=true`）
  且不追加任何事件；业务唯一约束（如重复报名）即使换键也返回冲突。
- **隐私裁剪**：
  - 班主任可见排序理由、因素编码标签、名额与通知送达状态；**不可见**监护人身份/
    联系方式、照顾依据证据编号与医疗细节。
  - 志愿者仅可见签到名单与签到状态。
  - 学生仅可见本人记录。

## 运行

```bash
python3 -m rotation_queue.server --db data/queue.db --port 8080
# 需让 src 可被导入：PYTHONPATH=src 或 pip install -e .
```

测试 / 编译 / 契约检查：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```

## HTTP 接口

身份用请求头表达（生产应替换为真实身份提供方签发的令牌）：

- `X-Role: teacher | volunteer | student`
- 学生角色附带 `X-Student-Id`，只能操作本人数据。
- 写请求可附带 `Idempotency-Key`。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/classes` | 教师 | 建班 |
| POST | `/api/students` | 教师 | 录入学生 |
| POST | `/api/sessions` | 教师 | 排场次（容量） |
| POST | `/api/accommodations` | 教师 | 批准照顾依据（医疗/无障碍/其他，可设有效期） |
| POST | `/api/consents/grant` `/api/consents/withdraw` | 教师 | 监护授权授予/撤回 |
| POST | `/api/sessions/{id}/signups` | 教师/学生 | 报名，可带 `enqueued_at` |
| DELETE | `/api/sessions/{id}/signups/{student}` | 教师/学生 | 临时退出，触发原子递补 |
| GET | `/api/sessions/{id}/preview` | 教师 | 发布前实时排序预览 |
| POST | `/api/sessions/{id}/roster/publish` | 教师 | 冻结名单，生成入选/候补通知 |
| POST | `/api/sessions/{id}/capacity` | 教师 | 临时扩容，批量递补 |
| POST | `/api/sessions/{id}/attendance` | 教师/志愿者 | 签到 |
| POST | `/api/sessions/{id}/notifications` | 教师 | 上报通知送达回执 |
| POST | `/api/transfers/offers` `/accept` `/decline` | 教师发起；学生接受/拒绝 | 跨场转移 |
| POST | `/api/sessions/{id}/close` | 教师 | 关闭场次 |
| GET | `/api/sessions/{id}` | 按角色裁剪 | 教师全量理由 / 志愿者签到 / 学生本人 |
| GET | `/api/sessions/{id}/overview` | 教师/志愿者 | 不含人名与理由的计数概览 |

错误响应统一为 `{"error": {"code": ..., "message": ...}}`，
常用码：`409 already_signed_up / already_checked_in / offer_decided`、
`403 forbidden / not_offer_owner`、`422 no_consent / not_on_roster / bad_capacity`。

## 事件与确定性

所有状态变更都是不可变事件（`events` 表，全局自增 `sequence`）。
读取侧完全由重放事件重建；关闭进程后重新打开同一数据库，队列结果逐字节一致。
命令处理在 `BEGIN IMMEDIATE` 事务内"重放 → 校验 → 追加"，
并发命令由 SQLite 写锁串行化，业务约束兜底，因此并发递补/签到不会超员或重复。
