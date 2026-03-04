# 前端需求研发文档：<需求名称>

## 1. 文档元信息

1. 文档 ID：`DOC-001`
2. 文档版本：`v1`
3. 当前状态：`Planned | In Progress | Done（YYYY-MM-DD）`
4. 负责人：`TODO:`
5. 创建日期：`YYYY-MM-DD`
6. 关联需求/工单：`TODO:`
7. 下游 AI 用途：`代码生成 | 测试生成 | 联调脚本生成 | 评审`

## 2. 业务背景与目标

1. 业务背景：`TODO:`
2. 核心痛点：`TODO:`
3. 目标用户：`TODO:`
4. 成功指标（可量化）：`TODO:`

## 3. 业务理解确认

1. 需求理解复述：`TODO:`
2. 需求方确认结论：`TODO:`
3. 关键共识：`TODO:`

## 4. 范围与非范围

| REQ-ID | 需求描述 | 范围类型 | 优先级 | 备注 |
| --- | --- | --- | --- | --- |
| REQ-001 | TODO | In Scope | P0 | TODO |
| REQ-999 | TODO | Out of Scope | P2 | TODO |

## 5. 用户角色与使用场景

1. 角色清单：`TODO:`
2. 主场景流程：`TODO:`
3. 异常场景流程：`TODO:`

## 6. 页面流程与交互说明

| FLOW-ID | 页面/入口 | 前置条件 | 用户动作 | 系统响应 | 异常分支 |
| --- | --- | --- | --- | --- | --- |
| FLOW-001 | TODO | TODO | TODO | TODO | TODO |

## 7. 列表与按钮定义

| BTN-ID | 按钮/操作 | 位置 | 显示条件 | 禁用条件 | 权限 | 点击动作 | 副作用 | 失败反馈 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTN-001 | TODO | TODO | TODO | TODO | TODO | TODO | TODO | TODO |

## 8. 接口与字段映射

### 8.1 接口清单

| API-ID | 接口 | 方法 | 用途 | 负责人 | 文档位置 |
| --- | --- | --- | --- | --- | --- |
| API-001 | TODO | TODO | TODO | TODO | TODO |

### 8.2 字段映射

| API-ID | 字段名 | 类型 | 必填 | 枚举/范围 | 业务含义 | 前端用途 | 空值语义 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| API-001 | TODO | string/int/boolean/... | true/false | TODO | TODO | TODO | TODO |

## 9. 字段展示与校验规则

| FIELD-ID | 字段名 | 展示组件 | 展示格式 | 校验规则 | 异常处理 | 脱敏/权限 |
| --- | --- | --- | --- | --- | --- | --- |
| FIELD-001 | TODO | TODO | TODO | TODO | TODO | TODO |

## 10. 异常处理与边界条件

| ERR-ID | 场景 | 触发条件 | 处理策略 | 用户提示 | 可恢复性 |
| --- | --- | --- | --- | --- | --- |
| ERR-001 | TODO | TODO | TODO | TODO | YES/NO |

## 11. 非功能要求

1. 性能目标：`TODO:`
2. 安全与合规：`TODO:`
3. 可观测性（日志/埋点/告警）：`TODO:`
4. 兼容性：`TODO:`

## 12. 详细实施阶段

### 12.1 实施总览

1. 总里程碑：`TODO:`
2. 阶段依赖关系：`TODO:`

### 12.2 阶段一：开发准备

1. 分支与发布策略：`TODO:`
2. 配置与环境准备：`TODO:`
3. 接口契约冻结点：`TODO:`
4. 阶段门禁：`TODO:`

### 12.3 阶段二：前端实现任务拆解

| TASK-ID | 任务类型 | 负责人 | 依赖任务 | 输入 | 输出 | 完成定义 |
| --- | --- | --- | --- | --- | --- | --- |
| TASK-001 | 页面开发 | TODO | 无 | REQ-001 | 页面代码 | 构建通过 + 自测通过 |
| TASK-002 | 组件开发 | TODO | TASK-001 | REQ-001 | 组件代码 | 单测通过 |
| TASK-003 | 状态管理 | TODO | TASK-001 | API-001 | 状态流实现 | 集成测试通过 |

1. 阶段门禁：`TODO:`

### 12.4 阶段三：接口联调

1. 联调顺序：`TODO:`
2. 联调检查项：`TODO:`
3. 失败重试与降级策略：`TODO:`
4. 阶段门禁：`TODO:`

### 12.5 阶段四：测试与修复

1. 冒烟测试：`TODO:`
2. 功能与边界测试：`TODO:`
3. 回归测试：`TODO:`
4. 缺陷分级与修复 SLA：`TODO:`
5. 阶段门禁：`TODO:`

### 12.6 阶段五：发布与验证

1. 灰度步骤：`TODO:`
2. 监控指标与阈值：`TODO:`
3. 放量条件：`TODO:`
4. 阶段门禁：`TODO:`

### 12.7 阶段六：回滚与补偿

1. 回滚触发条件：`TODO:`
2. 回滚步骤：`TODO:`
3. 数据补偿方案：`TODO:`
4. 阶段门禁：`TODO:`

### 12.8 阶段验收门禁

1. 阶段门禁总表：`TODO:`

## 13. 验收标准

| TEST-ID | 需求项ID | 前置条件 | 操作步骤 | 预期结果 | 覆盖接口/字段 |
| --- | --- | --- | --- | --- | --- |
| TEST-001 | REQ-001 | TODO | TODO | TODO | API-001/FIELD-001 |

## 14. 风险与回滚方案

| RISK-ID | 风险描述 | 影响范围 | 触发条件 | 应对策略 | 状态 |
| --- | --- | --- | --- | --- | --- |
| RISK-001 | TODO | TODO | TODO | TODO | Open |

## 15. 待确认问题（必须答复）

| Q-ID | 问题描述 | 阻断级别 | 关联对象 | 责任人 | 截止时间 |
| --- | --- | --- | --- | --- | --- |
| Q-001 | TODO | BLOCKER/NON-BLOCKER | REQ-001/API-001/TASK-001 | TODO | YYYY-MM-DD |

## 16. 变更记录

1. `YYYY-MM-DD`：初始化文档模板。

### 16.1 机器可解析附录（强制）

```json
{
  "metadata": {
    "doc_id": "DOC-001",
    "version": "v1",
    "status": "Planned",
    "target_ai_usage": ["代码生成", "测试生成"]
  },
  "requirements": [
    {
      "req_id": "REQ-001",
      "title": "TODO",
      "scope": "in_scope",
      "priority": "P0",
      "related_flows": ["FLOW-001"],
      "related_apis": ["API-001"],
      "related_tests": ["TEST-001"]
    }
  ],
  "apis": [
    {
      "api_id": "API-001",
      "path": "TODO",
      "method": "GET",
      "fields": [
        {
          "field_id": "FIELD-001",
          "name": "TODO",
          "type": "string",
          "required": true,
          "enum": [],
          "nullable": false
        }
      ]
    }
  ],
  "tasks": [
    {
      "task_id": "TASK-001",
      "type": "页面开发",
      "depends_on": [],
      "input": ["REQ-001"],
      "output": ["页面代码"],
      "done_definition": ["构建通过", "自测通过"]
    }
  ],
  "tests": [
    {
      "test_id": "TEST-001",
      "req_id": "REQ-001",
      "steps": ["TODO"],
      "assertions": ["TODO"]
    }
  ],
  "risks": [
    {
      "risk_id": "RISK-001",
      "level": "high",
      "status": "open",
      "mitigation": "TODO"
    }
  ],
  "open_questions": [
    {
      "q_id": "Q-001",
      "blocking_level": "BLOCKER",
      "question": "TODO"
    }
  ]
}
```
