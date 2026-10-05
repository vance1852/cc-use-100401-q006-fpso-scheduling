# 深水油田协同运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理深水油田生产物流、油藏证据评估、关键装备质量和储运提油编排。平台把生产节点、输送通道、原油批次、储罐与加工批次、提油轮与靠泊窗口、油藏方案、分析决定、装备观测、权限和审计事件持久化到 SQLite，供海上平台、浮式生产储卸装置、油藏团队、装备保障、调度和审计人员协作使用。

## 目录

- src/production_flow/：生产节点、输送通道、原油批次、外输申请、分配和情景分析；
- src/reservoir_assurance/：油藏项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/equipment_quality/：装备批次、传感观测、质量分析、账号权限和审批；
- src/lifting_orchestration/：储罐兼容性、质量界限、提油轮适配、靠泊窗口、海况数据版本与加工速率的计划比较，租约预留、平台与船方双确认封存，部分装船、降产、临时换罐、质量降级、航次取消与候补推进；
- fixtures/：离线验收使用的评估协议与结构化观测；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m production_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m reservoir_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m equipment_quality.acceptance
    PYTHONPATH=src python3 -m lifting_orchestration.acceptance --workspace .

四条命令会在临时 SQLite 数据库中完成生产流转、油藏证据评估、装备质量和储运提油编排流程，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m production_flow.api --database production-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m reservoir_assurance.api --database reservoir-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m equipment_quality.api --database equipment-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m lifting_orchestration.api --database lifting-orchestration.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 储运与提油编排

计划（plans）组织储罐兼容性与罐底余量、可用容积、加工批次速率与含水率、提油轮载货区间与适装品位、靠泊窗口与极限波高、海况数据版本和质量界限；引擎按小时模拟罐容平衡、溢流/断油风险、装船可作业小时并给出可比分数与降产/换罐建议。

生命周期：`draft`（可比较）→ `reserved`（租约在 TTL 内独占窗口/船舶并按小时桶占用罐容）→ 平台方与船方分别 `confirm` → `sealed`。并发封存依靠 `BEGIN IMMEDIATE` 与独占桶部分唯一索引保证最多一个成功。

封存后的修订只影响生效小时之后的未完成部分：部分装船（`partial-loading`，不能超过当时海况与速率下的物理上限）、降产（`production-cut`）、临时换罐（`tank-switch`）、质量降级（`quality-downgrade`）和航次取消（`cancel`）。每次修订和租约事件都返回 `capacity_explanation`，逐小时桶列出释放（released）与占用（occupied）、按资源汇总的净变化以及修订后的容量视图。航次取消后候补按冻结优先级 `(priority, entry_id)` 推进，自动跳过不适装或不适配的候补，首位可行者取得剩余窗口租约。
