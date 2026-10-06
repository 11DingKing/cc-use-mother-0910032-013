# 场地容量冲突治理

可拆分展厅的场地排期后端。维护空间层级、布局版本、容量/设备条件与转换缓冲，
预约时把活动需求展开为实际占用范围，检测祖先与子区域冲突，并在布局切换、
部分关闭、跨日活动与并发确认时原子更新占用；已完成活动保留原布局快照，
冲突响应返回完整冲突路径与可行替代空间。

## 模型

- **空间层级**（`Space`）：稳定的物理树，如 场馆 › 楼层 › 展厅 › 隔断东/西区。
- **布局版本**（`Layout` / `LayoutZone`）：同一物理空间可有多个版本
  （全开 / 隔断拆分）；每个版本定义可排期区域及其容量、设备和实际物理落点。
  每个空间同时只有一个生效版本。
- **活动需求**（`Event`）：指定区域、人数、必需设备、自定义缓冲；
  状态流转 筹备 → 待确认 → 已排定 → 执行中 → 已结算（可取消）。
- **占用快照**（`OccupancySnapshot`）：确认时按当时生效布局固化，
  含缓冲展开区间与空间祖先链；之后布局切换不影响已排定/已完成活动。

## 冲突规则

- 时间区间左闭右开；占用区间 = 请求区间前后各展开转换/清场缓冲，
  因此跨日活动只是普通长区间。
- 两次占用在时间相交且空间节点互为**祖先或相同**时冲突；
  仅共享祖先的兄弟隔断分区不冲突（避免隔断区域被重复使用）。
- 待确认为软占用（可排队候补），已排定/执行为硬占用；
  确认时持锁重新检测，落败的并发确认得到 409。
- 布局切换需给出转换窗口 `[effective_at - setup_minutes, effective_at]`，
  窗口内目标空间子树上有占用即拒绝；已结算活动不阻挡切换。
- 部分关闭按子树生效：关闭展厅会挡住其下所有隔断分区的预约，
  关闭单个隔断分区不影响兄弟分区。
- 409 响应包含每个冲突的关系（同区域/祖先/子区域/关闭）、
  请求路径、既占用路径、碰撞窗口，以及满足容量与设备的可行替代空间。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/venue_scheduling/`：排期后端
  - `models.py` 领域模型；`engine.py` 层级展开与冲突检测；
    `repository.py` 线程安全仓储（事件版本号 CAS）；
    `service.py` 原子事务与状态流转；`api.py` 标准库 JSON HTTP 服务；
    `seed.py` 可拆分展厅示例数据。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tools/smoke_test.py`：启动 HTTP 服务跑通完整业务链路。
- `tests/`：契约、领域行为（含并发确认）与 HTTP API 回归测试。

## 验证

```bash
# 单元与集成测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# HTTP 端到端冒烟（自动起停服务）
python3 tools/smoke_test.py

# 启动服务（带示例数据）
PYTHONPATH=src python3 -m venue_scheduling.serve --port 8000 --seed
```

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/spaces` | 创建空间节点（parent_id 构成层级） |
| GET | `/api/space-tree` | 空间层级树 |
| POST | `/api/layouts` | 新增布局版本（区域、容量、设备） |
| POST | `/api/layouts/activate` | 布局切换（转换窗口原子检测） |
| POST | `/api/closures` | 部分/整体关闭 |
| POST | `/api/events` | 创建活动（筹备） |
| POST | `/api/events/{id}/submit` | 提交（→待确认，展开占用快照） |
| POST | `/api/events/{id}/confirm` | 确认（→已排定，支持 expected_version CAS） |
| POST | `/api/events/{id}/start` `/settle` `/cancel` | 执行/结算/取消 |
| POST | `/api/availability` | 试排期：占用展开、冲突路径、替代空间 |
