# 学生互动轮转排队

本项目维护学生互动轮转排队的领域约定、角色边界与样例数据，并提供一套完整的后端实现（仅标准库）：根据班级、历史参与次数、经批准的照顾依据和报名时间生成可解释队列，空出名额时原子递补并记录通知是否送达。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/rotation/`：后端核心——排序策略、SQLite 存储、命令服务、角色视图、HTTP 接口。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动后端 HTTP 服务。
- `tests/`：契约与后端回归测试。

## 后端设计

### 公平排序（可解释）

每个场次的队列按以下规则确定性排序，任何重放结果一致：

**照顾级别（高优先）→ 历史参与次数（少优先）→ 报名时间（早优先）→ 学号（字典序）**

- 照顾依据须“已批准且在有效期内”才参与排序，细节文本不出现在任何视图；
- 历史参与次数 = 已完成场次的签到记录，避免有人连续参加、有人长期排不到；
- 每次递补都把排序理由快照存入 `offer_explanation`，班主任可随时查看“为什么是这个顺序”。

### 命令与幂等

一切变更都是 `POST /commands`，body 为 `{"event_id": "...", "type": "...", "payload": {...}}`。
同一 `event_id` 重放返回首次记录的结果，绝不重复占位（`events` 表唯一约束 +
`entries` 部分唯一索引双保险）；失败结果同样被记录，重放得到同一错误。

| 命令 | 说明 |
| --- | --- |
| `create_student` / `create_session` | 学生与场次主数据（场次含容量、接收班级） |
| `grant_consent` / `withdraw_consent` | 监护授权授予与撤回 |
| `submit_care_grant` / `approve_care_grant` / `revoke_care_grant` | 照顾依据提交、批准、撤回 |
| `register` / `withdraw_entry` | 报名入队 / 临时退出 |
| `transfer` | 跨场转移（保留原始报名时间） |
| `set_capacity` | 临时扩容/缩容 |
| `check_in` / `complete_session` | 签到 / 场次结算 |
| `record_notification_result` | 登记通知送达结果 |

### 原子递补与通知

退出、授权撤回、扩容、跨场转移在同一事务内完成“释放空位 + 按序递补 + 生成通知”，
不会出现空位丢失或重复占位。通知先落库为 `pending`，提交后由网关派发回写
`sent`/`failed`，送达确认登记为 `delivered`/`failed`；进程崩溃遗留的 `pending`
可由 `dispatch_pending()` 补发。班主任在队列视图中能看到每条名额通知的送达状态。

### 确定性规则

- **并列者**：全部因子相同时按学号字典序决胜，理由中标注 `tie_group_size` 与 `tie_broken_by`；
- **监护授权撤回**：排队/占位记录立即移除并递补；已签到记录保留并标记 `flagged_checked_in` 交工作人员跟进；
- **临时扩容**：立即按序补足空位；缩容不得低于已占席位（`capacity_below_occupied`）；
- **跨场转移**：源场次退出与目标场次入队同事务完成，保留原始报名时间，已签到记录不可转移；
- **同时签到**：`BEGIN IMMEDIATE` 串行化，重复签到返回 `already_checked_in`，不产生重复记录；
- **重放**：相同 `event_id` 返回首次结果（`replayed: true`），不占位、不重复递补。

### 隐私裁剪（按角色）

- `teacher`（班主任）：完整队列 + 每位学生的排序理由 + 通知送达状态，**看不到**监护联系方式、照顾依据细节；
- `volunteer`（活动志愿者）：仅签到名单与状态；
- `student`（学生）：只能看自己的位置与理由。

## 运行

```bash
python3 tools/run_server.py [db_path] [port]   # 默认 rotation.db / 8080
```

角色通过请求头标识：`X-Actor-Role: teacher|student|volunteer|guardian|admin`，`X-Actor-Id: <id>`。
查询队列：`GET /sessions/{id}/queue`（按角色返回裁剪视图）。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
