# 场地容量冲突治理

本项目维护场地容量冲突治理的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖活动统筹员、讲解员、学校联系人、场馆管理员，并明确空间层级占用、布局版本切换、缓冲时间冲突、替代空间解释等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/venue_capacity/`：后端服务（空间层级、布局版本、原子预约）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动本地 JSON API。
- `tools/demo.py`：无服务器的占用展开与冲突演示。
- `tests/`：契约完整性与服务行为回归测试。

## 后端模型

- **空间层级**：空间组成树（如 综合展厅 → 东区/西区）。预约确认时把占用展开为子树下的全部叶子空间；祖先与子区域的叶子集合相交即互斥，从模型上杜绝隔断区域被重复排期。
- **布局版本**：布局不可变版本化，修改产生新版本。预约在提交时钉住版本、确认时留存快照；已结算活动永远保留原布局。
- **转换缓冲**：同一物理区域上先后两个预约布局不同（`layout_id` 不同）时，较晚一方必须留出其布局的 `conversion_minutes` 转换时间，否则报“布局转换缓冲不足”。
- **部分关闭**：`Closure` 按同样规则展开叶子，与占用中的预约严格冲突；只关闭子区域不影响兄弟区域。
- **跨日活动**：时间一律使用绝对时间戳（ISO 8601），跨日自然成立。
- **原子确认**：确认、取消、关闭都在同一把锁内完成“检测 + 写占用”，并发确认同一区域时只有一个成功，其余收到冲突响应。
- **冲突响应**：`ConflictError` 携带冲突路径（如 `市民文化中心/综合展厅/东区`）、重叠的子区域、原因，以及按容量升序的可行替代空间。

## 状态机

筹备 → 待确认 → 已排定 → 执行中 → 已结算；筹备/待确认/已排定可取消为已取消并释放占用。

## API

启动：`python3 tools/run_server.py --port 8080`（加载演示数据）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /spaces`、`GET /spaces`、`GET /spaces/{id}` | 维护与查询空间层级 |
| `GET /spaces/{id}/occupancy?start=&end=` | 空间（含子孙区域）在时段内的占用 |
| `POST /layouts`、`POST /layouts/{id}/versions` | 登记布局、修改布局（产生新版本） |
| `POST /closures`、`DELETE /closures/{id}` | 部分关闭与解除 |
| `POST /bookings`（可带 `"submit": true`） | 创建预约 |
| `POST /bookings/{id}/submit|confirm|start|settle|cancel` | 状态流转；确认原子检测冲突 |
| `GET /bookings/{id}` | 预约详情（含布局快照与占用展开） |
| `GET /alternatives?capacity=&start=&end=&equipment=` | 可行替代空间 |

冲突时确认接口返回 `409`，响应体含 `conflicts`（冲突路径、重叠子区域、原因）与 `alternatives`（可行替代空间）。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

演示：`python3 tools/demo.py`
