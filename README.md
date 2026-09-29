# ⚗️ 科研配方量纲校验台（dimcheck）

录入**带单位的计算表达式**与变量取值，系统解析括号、幂运算与函数调用，**递归推导量纲**并自动完成单位换算；对**相加异量纲**、**非法幂指数**、**未定义变量**等错误，给出**指向表达式具体片段**的定位信息，并展示 SI 换算结果。可为直接测量输入填写**带单位的标准不确定度**，系统以各独立输入为扰动源做双侧小步长重算，按**一阶平方和（RSS）**给出结果的**合成标准不确定度**，标称值与不确定度随目标单位一起换算。还可指定一个直接输入变量、取值单位、上下界与**带单位的目标结果**，系统覆盖该变量重算下游变量链，以**二分法反求**使主表达式达到目标的变量值。整理已有配方时可提交一组**「旧名→新名」改名映射**（允许两变量交换名称），系统只改写指向用户变量的标识符、保持内置单位 / 常量 / 函数调用原义，预览改名后的完整配方并重新校验。支持校验案例的**保存 / 加载 / 删除**与 **JSON 导出**。

技术栈：Python 3.11+ · FastAPI · SQLite · 原生 HTML/JS/CSS（无前端构建）。

---

## 快速开始

### 方式一：Docker Compose（推荐，一键启动）

```bash
docker compose up --build
```

启动后访问（浏览器直接打开即可，无需额外配置）：

- Web 界面：<http://localhost:8000/>
- API 文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI 描述：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/api/health>

服务固定监听容器内 **8000 端口**，并映射到宿主机 **8000 端口**（Web 页面与 JSON API 同一地址、同一端口）；SQLite 数据存储在命名卷 `dimcheck-data` 中，容器重建不丢失。修改宿主机端口可改 `docker-compose.yml` 中的 `"宿主端口:8000"`。

### 方式二：本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

随后同样访问 <http://localhost:8000/>。仅需 Python 3.11+，无其他系统依赖；可选环境变量见文末。

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

---

## 表达式语法

| 特性 | 示例 |
| --- | --- |
| 四则运算 / 括号 | `(1 + 2) * 3` |
| 幂运算（右结合，也可用 `**`） | `v^2`、`2^3^2 = 512`、`m^-2` |
| 数字带单位 | `9.8 m/s^2`、`1.5e-3 mol`、`2.5 kJ` |
| 隐式乘法 | `2 m`、`kg m/s^2`、`2(3+4)`、`2π r` |
| Unicode 上标 | `m²`、`x⁻¹`、`5 m³` |
| 符号别名 | `× ÷ · −`（等价于 `* / * -`） |
| 函数 | `sqrt(4 m^2)`、`sin(30 deg)`、`ln(e)`、`log(100)`、`abs(-3 m)` |
| 变量 | 变量值本身是表达式，可相互引用：`F = mass * g0` |
| 内置常量 | `pi π e c g0 G kb NA R qe me eps0 mu0` |

**名称解析顺序**：用户变量 > 内置常量 > 内置单位。变量名与单位/常量同名时按变量解析，并返回非致命**警告**（`warnings` 字段），避免静默错误。

**量纲规则**：

- 加 / 减：两边量纲必须一致，否则报 `dim_mismatch`，错误片段指向右操作数；
- 乘 / 除：量纲指数相应加减，数值自动换算到 SI；
- 幂：指数必须**无量纲**（否则 `bad_exponent`，片段指向指数），底数量纲指数乘以指数（允许分数，如 `(4 m^2)^0.5 = 2 m`）；
- `sin / exp / ln` 等函数参数必须无量纲（否则 `dimless_arg`）；`sqrt` 使量纲指数减半；
- 未定义的名称报 `undefined`，片段指向该标识符；变量循环引用报 `var_cycle`。

完整单位列表见 Web 界面右侧「单位 · 常量 · 函数」面板或 `GET /api/units`。注意：摄氏温度 °C 等带偏移的单位不支持，请使用开尔文 K。

---

## 测量不确定度分析

在变量行的 **「u」列**为**部分直接测量输入**填写标准不确定度（带单位，如 `0.01 kg`）；不填的变量视为未提供。变量值仍可写成表达式并相互引用（如 `F = mass * g0`），但**只有定义式不引用其他用户变量的“直接输入”才能作为独立扰动源**——间接变量的不确定度由其依赖的输入经变量链传播，无需也不能单独标注。

后端对每个不确定度做校验：

- 必须是非负有限数值（`uncert_negative`）；
- 量纲必须与对应变量一致（`uncert_dim`），允许不同单位前缀（`2 cm` 对应 `m` 量变量）；
- 只能是“带单位的数值”，不能引用变量（`uncert_ref`），对应的变量必须存在（`uncert_unknown`）；
- 不能标注在间接变量上（`uncert_indirect`）。

计算方法（一阶 GUM 方式，数值实现）：

1. 以每个直接输入 xᵢ 为独立扰动源，步长 h = 10⁻⁶ · max(|xᵢ|, u(xᵢ))——**标称值为零时步长由其不确定度（再退化为绝对小量）托底，保证步长始终有效**；
2. 在 xᵢ ± h 两处对**完整变量链 + 主表达式整体重算**（中心差分），数值灵敏度 ∂f/∂xᵢ = (f(x+h) − f(x−h)) / (2h)；
3. 合成标准不确定度 u_c(y) = √( Σ (∂f/∂xᵢ · u(xᵢ))² )（各输入相互独立）；
4. 选择目标单位时，**标称值与合成标准不确定度按同一换算因子一起换算**，结果表并列显示。

**失败即停止汇总**：标称计算本身失败时不进行不确定度分析；任一输入校验失败、或某一侧扰动重算触发量纲错误 / 定义域错误（如零处 `sqrt` 的负向扰动）/ 非有限值时，响应 `ok=false`，错误中带 `perturbed`（扰动输入名）与 `direction`（`+` / `−`）定位来源，`result.uncertainty` 为 `null`——不会返回貌似有效的不确定度数值。标称值本身仍然正常返回。

请求示例：

```bash
curl -s http://localhost:8000/api/check -H 'Content-Type: application/json' -d '{
  "expression": "0.5*mass*v^2 + mass*g0*h0",
  "variables": {"mass": "2 kg", "v": "10 m/s", "h0": "5 m"},
  "uncertainties": {"mass": "0.01 kg", "v": "0.1 m/s", "h0": "0.02 m"},
  "target_unit": "kJ"
}'
```

响应 `result.uncertainty`：

```json
{
  "method": "central_difference_rss",
  "step_relative": 1e-6,
  "si_value": 2.2659724347122303,
  "si_unit": "m^2·kg·s^-2",
  "target": {"unit": "kJ", "value": 0.0022659724347122304},
  "sources": [
    {"name": "mass", "u_si": 0.01, "step_si": 2e-6,
     "sensitivity": 99.03325, "contribution_si": 0.9903325}
  ]
}
```

`sources` 给出每个扰动源的输入标准不确定度、实际步长、数值灵敏度与单项贡献（前端另显示方差占比）；不确定度为 0 的输入不列入贡献表。

---

## 反求输入变量（目标值反解）

针对**现有的表达式与变量定义**，指定一个**直接输入变量**（定义式不引用其他用户变量，与不确定度分析的“直接输入”口径一致）、它的取值单位、数值上下界与**带单位的目标结果**，系统以候选值覆盖该变量并**重算完整下游变量链与主表达式**，用**二分法**反求使主表达式达到目标的变量值。每次试算完全沿用现有的量纲推导与定义域规则（`sqrt` 负数、除零、非有限值等照常报错）。

**输入约束**：

- `solve_for` 必须是已定义的直接输入变量（`solve_var` / `solve_indirect`）；
- `solve_unit` 必须能解析为有限为正的单位，且与待求变量**同量纲**（`solve_unit_dim`），允许不同前缀（变量是 `m/s` 时可用 `km/h` 给界）；
- `lower` / `upper` 必须是有限数且下界严格小于上界（`solve_bounds`），二者以 `solve_unit` 计；
- `target` 是带单位的目标结果（如 `"200 J"`、`"0.2 kJ"`、`"0.5"`），只允许数值 + 内置单位/常量，不能引用变量（`solve_ref`），且量纲必须与主表达式一致（`solve_target_dim`）。

**求解规则**：

1. 先以原定义式独立确定待求变量的量纲，再把上下界换算到 SI；
2. **端点已达目标时直接返回**（`endpoint` 为 `"lower"` / `"upper"`，`iterations` 为 0）；
3. 否则仅在两端 SI 残差异号时二分；两端残差同号则报 `solve_no_bracket`（区间不包根），不返回解；
4. 收敛条件为 SI 残差不超过 `1e-8 × max(1, |目标 SI 值|)`（随目标尺度缩放，目标为 0 时退化为绝对 1e-8）；
5. 最多二分 **80** 次；超限仍未达容差报 `solve_max_iter`，不返回解；
6. 任何一次端点或中点试算触发量纲 / 定义域错误或非有限结果，立即中止并报 `solve_eval_failed` 类错误（**保留内部错误的 `kind` 与片段位置**，并附 `solve_for`、`solve_at`、`iteration`、`candidate_si` 定位是哪次试算），不返回解。

请求示例（已知质量与目标动能，反求速度；区间用 `km/h`、目标用 `kJ`）：

```bash
curl -s http://localhost:8000/api/solve -H 'Content-Type: application/json' -d '{
  "expression": "0.5*mass*v^2 + mass*g0*h0",
  "variables": {"mass": "2 kg", "v": "1 m/s", "h0": "5 m"},
  "solve_for": "v",
  "solve_unit": "km/h",
  "lower": 0,
  "upper": 100,
  "target": "0.2 kJ"
}'
```

响应（变量的 SI 值与所给单位下的值、实际结果、残差）：

```json
{
  "ok": true,
  "errors": [],
  "warnings": [],
  "result": {
    "variable": "v",
    "variable_si_value": 10.09621222813924,
    "value": 36.34636402130126,
    "unit": "km/h",
    "bounds_given": {"lower": 0.0, "upper": 100.0, "unit": "km/h"},
    "target": {"given": "0.2 kJ", "si_value": 200.0, "variable_unit": "km/h"},
    "actual": {
      "si_value": 200.0000013556283,
      "residual_si": 1.3556283136040292e-06,
      "dimension_str": "L^2·M·T^-2",
      "si_unit": "m^2·kg·s^-2",
      "derived": ["J"],
      "in_given_unit": {"unit": "kJ", "value": 0.20000000135562832}
    },
    "iterations": 22,
    "endpoint": "",
    "tolerance_si": 2e-06,
    "method": "bisection"
  }
}
```

- `variable_si_value` / `value`：求得变量的 SI 值与在 `solve_unit` 下的值（上例约 10.10 m/s ≈ 36.35 km/h）；
- `actual.si_value` / `actual.residual_si`：在该取值下重算主表达式的实际 SI 结果与对目标的残差；
- 当目标写成「数值 + 单位」（如 `"0.2 kJ"`）时，额外给出 `actual.in_given_unit`，回显实际结果在该单位下的值；纯表达式目标（如 `"10 kg*m/s^2"`）不提供此字段。

反求错误同样以 `ok=false` + `errors` 返回（HTTP 状态码仍为 200；请求体缺字段 / 类型错误为 422），错误项带 `kind` 与 `source`（`solve`、`solve_unit`、`solve_target`、`expression`、`var:<名称>`）。例如区间不包根：

```bash
curl -s http://localhost:8000/api/solve -H 'Content-Type: application/json' -d '{
  "expression": "0.5*mass*v^2",
  "variables": {"mass": "2 kg", "v": "1 m/s"},
  "solve_for": "v", "solve_unit": "m/s",
  "lower": 0, "upper": 1, "target": "200 J"
}'
```

```json
{
  "ok": false,
  "errors": [{
    "message": "区间两端残差同号（下端 -200，上端 -199，SI），区间不包含使主表达式达到目标的解；请放宽或调整上下界",
    "start": 0, "end": 3,
    "kind": "solve_no_bracket",
    "source": "solve"
  }],
  "warnings": [],
  "result": null
}
```

---

## 变量改名预览

整理现有配方时，可提交主表达式、变量定义、各直接输入的不确定度与一组**「旧名→新名」映射**，系统返回改名后的完整配方（表达式、变量定义、不确定度键、旧新名称对照）以及改名后配方的完整校验结果。改名只做标识符级文本改写，**数字、单位、函数调用与不确定度取值一字不动**。

**改名规则**：

- 映射**按原名称同时生效**——先解析再统一替换，因此允许两个变量交换名称（`{"a": "b", "b": "a"}`），不会发生链式覆盖；只提交部分变量时，未列入映射的变量保持原名；
- **旧名必须存在**（`rename_missing_old`），新名必须符合现有变量标识符规则：以字母或 `_` 起始，只含字母、数字与下划线（`rename_bad_new`）；
- 改名后变量名**不得重复**（`rename_duplicate`，包括把 A 改成某个未改名变量 B 的名字）；
- 主表达式与各变量定义中**指向用户变量的引用随之改写**（含间接变量链，如 `F = mass*g0` 中的 `mass`）；
- **内置单位、内置常量与函数调用的同名文本保持原含义**：`m`、`g0`、`sqrt(...)` 等不会被改写。

**内置名称遮蔽保护**：若某个新变量名与配方中**未改写的**内置单位 / 常量同名，改名将使该处静默改按变量解析（名称解析顺序为用户变量 > 内置常量 > 内置单位），系统**拒绝整次改名**（`rename_shadow`），错误定位到改写后串中的冲突片段（`source` 为 `expression` / `var:<名称>` / `uncert:<名称>`，并带 `shadowed` 与 `builtin_kind`（`unit` / `constant`）字段），**不输出任何部分配方**。新名虽然与内置单位同名、但配方中并不存在该内置名称引用时允许改名，仅按现有口径给出 `shadow_unit` / `shadow_constant` 警告。函数名不在遮蔽保护范围内（`sqrt(...)` 调用语法永远先按函数解析）。

**不确定度迁移**：不确定度表达式文本原样保留，仅其**键随变量名迁移**（`mass` 的不确定度改名后挂在新名之下）；不确定度表达式中的单位文本同样受遮蔽保护。

**前后两次校验**：改名前先对原配方做一次完整校验，原配方无效时直接返回原错误（错误项带 `stage: "before_rename"`，`source` 仍指向原始来源），不做改名；改名后再对新配方完整校验（含量纲、不确定度、间接变量规则），理论上改名保持语义等价，若仍失败则错误带 `stage: "after_rename"`，同样不产生可用结果（`result` 为 `null`）。现有 `/api/check`、`/api/solve` 与案例接口不受影响。

请求示例（同时改名两个变量，其中 `g0` 是内置常量但仅出现在被改写引用之外）：

```bash
curl -s http://localhost:8000/api/rename -H 'Content-Type: application/json' -d '{
  "expression": "0.5*mass*v^2 + mass*g0*h0",
  "variables": {"mass": "2 kg", "v": "10 m/s", "h0": "5 m"},
  "uncertainties": {"mass": "0.01 kg", "v": "0.1 m/s"},
  "renames": {"mass": "M", "v": "vel"}
}'
```

响应（`result.check` 即改名后配方的完整 `/api/check` 同构结果）：

```json
{
  "ok": true,
  "errors": [],
  "warnings": [],
  "result": {
    "expression": "0.5*M*vel^2 + M*g0*h0",
    "variables": {"M": "2 kg", "vel": "10 m/s", "h0": "5 m"},
    "uncertainties": {"M": "0.01 kg", "vel": "0.1 m/s"},
    "renames": [{"old": "mass", "new": "M"}, {"old": "v", "new": "vel"}],
    "check": { "ok": true, "errors": [], "warnings": [], "result": { "...": "与 /api/check 相同" } }
  }
}
```

交换名称示例（`a`、`b` 互换；引用按原名同时改写）：

```bash
curl -s http://localhost:8000/api/rename -H 'Content-Type: application/json' -d '{
  "expression": "a*b",
  "variables": {"a": "2", "b": "3"},
  "renames": {"a": "b", "b": "a"}
}'
# expression -> "b*a"，variables -> {"b": "2", "a": "3"}
```

遮蔽冲突示例（新名 `g0` 会让主表达式里未改写的内置常量 `g0` 被变量遮蔽，整次拒绝）：

```bash
curl -s http://localhost:8000/api/rename -H 'Content-Type: application/json' -d '{
  "expression": "x*g0",
  "variables": {"x": "1 kg"},
  "renames": {"x": "g0"}
}'
```

```json
{
  "ok": false,
  "errors": [{
    "message": "主表达式中的内置常量「g0」会被改名后的同名变量遮蔽，该处本应指向内置常量，改名将改变配方含义；请更换新变量名",
    "start": 3, "end": 5,
    "kind": "rename_shadow",
    "source": "expression",
    "shadowed": "g0",
    "builtin_kind": "constant"
  }],
  "warnings": [],
  "result": null
}
```

`POST /api/rename` 请求体字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `expression` | string | 主表达式（与 `/api/check` 相同） |
| `variables` | object | 变量名 → 带单位取值表达式 |
| `uncertainties` | object | 变量名 → 带单位的标准不确定度（可空 / 省略） |
| `renames` | object | **旧变量名 → 新变量名**映射（必填，可为空对象 `{}`，空映射返回原配方预览） |

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/check` | 校验表达式，返回量纲、SI 数值、目标单位换算、合成标准不确定度、错误/警告 |
| POST | `/api/rename` | 变量改名预览：按「旧名→新名」映射整体改写配方（支持交换名称），返回改写后配方与改名后校验结果 |
| POST | `/api/solve` | 反求直接输入变量：在给定区间内二分，使主表达式达到带单位的目标结果 |
| GET | `/api/units` | 单位 / 常量 / 函数目录 |
| POST | `/api/cases` | 保存校验案例（含 `uncertainties`） |
| GET | `/api/cases` | 案例列表 |
| GET | `/api/cases/{id}` | 读取案例 |
| DELETE | `/api/cases/{id}` | 删除案例 |
| GET | `/api/export` | 导出全部案例（JSON 附件下载） |
| GET | `/api/health` | 健康检查 |

`POST /api/check` 请求体字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `expression` | string | 主表达式 |
| `variables` | object | 变量名 → 带单位取值表达式 |
| `target_unit` | string \| null | 目标单位（标称值与不确定度一起换算） |
| `uncertainties` | object | 变量名 → 带单位的标准不确定度（仅直接输入，可只填部分变量） |

`POST /api/solve` 请求体字段（字段名、量纲规则与求解/返回细节见上文「反求输入变量」一节）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `expression` | string | 主表达式 |
| `variables` | object | 变量名 → 带单位取值表达式（与 `/api/check` 相同） |
| `solve_for` | string | 待求的直接输入变量名 |
| `solve_unit` | string | 变量取值单位（上下界按此单位给出，须与变量同量纲） |
| `lower` | number | 下界（以 `solve_unit` 计的有限数） |
| `upper` | number | 上界（以 `solve_unit` 计的有限数，须严格大于下界） |
| `target` | string | 带单位的目标结果（须与主表达式同量纲） |

### 示例：校验通过

```bash
curl -s http://localhost:8000/api/check -H 'Content-Type: application/json' -d '{
  "expression": "0.5*mass*v^2 + mass*g0*h0",
  "variables": {"mass": "2 kg", "v": "10 m/s", "h0": "5 m"},
  "target_unit": "kJ"
}'
```

```json
{
  "ok": true,
  "errors": [],
  "warnings": [],
  "result": {
    "si_value": 198.0665,
    "dimension": ["2", "1", "-2", "0", "0", "0", "0"],
    "dimension_names": ["L", "M", "T", "I", "Θ", "N", "J"],
    "dimension_str": "L^2·M·T^-2",
    "si_unit": "m^2·kg·s^-2",
    "derived": ["J"],
    "target": {"unit": "kJ", "value": 0.1980665},
    "uncertainty": null
  }
}
```

未提供任何 `uncertainties` 时 `uncertainty` 为 `null`；提供时结构见上文「测量不确定度分析」。

### 示例：错误定位（相加异量纲）

```bash
curl -s http://localhost:8000/api/check -H 'Content-Type: application/json' -d '{
  "expression": "1 m + 1 s"
}'
```

```json
{
  "ok": false,
  "errors": [{
    "message": "量纲不一致：「L」与「T」不能相加",
    "start": 6, "end": 9,
    "kind": "dim_mismatch",
    "source": "expression"
  }],
  "result": null
}
```

`start` / `end` 是原始表达式字符串中的字符区间（上例即片段 `"1 s"`），`source` 取值：`expression`（主表达式）、`var:<名称>`（某变量的定义式）、`uncert:<名称>`（某变量的不确定度输入）、`target`（目标单位）、`rename:<旧名>`（改名映射自身的问题）。前端据此将出错片段高亮标红。改名接口的错误项另带 `stage`（`before_rename` / `after_rename`）标明来自改名前还是改名后的校验，遮蔽冲突的片段位置相对于改写后的字符串。

### 错误类型（`kind`）

| kind | 含义 |
| --- | --- |
| `syntax` | 语法错误（空表达式、括号不配对、多余内容等） |
| `undefined` | 未定义的变量或单位 |
| `dim_mismatch` | 加减法两边量纲不一致 |
| `bad_exponent` | 幂指数不是无量纲量 |
| `dimless_arg` | 函数参数要求无量纲 |
| `arity` | 函数参数个数错误 |
| `var_error` / `var_cycle` | 变量定义出错 / 循环引用 |
| `target_dim` | 目标单位与结果量纲不一致 |
| `value` | 除零、定义域、非有限值等数值错误 |
| `uncert_negative` | 标准不确定度为负数 |
| `uncert_dim` | 标准不确定度量纲与变量不一致 |
| `uncert_ref` | 不确定度表达式中引用了变量（只允许带单位数值） |
| `uncert_unknown` | 不确定度对应的变量不存在 |
| `uncert_indirect` | 对引用了其他变量的间接变量标注不确定度 |
| `uncert_eval` | 扰动重算后量纲改变或结果非有限 |
| `solve_var` | 未指定或不存在待求变量 |
| `solve_indirect` | 待求变量引用了其他变量，不是直接输入 |
| `solve_unit_dim` / `solve_unit_value` | 变量单位与待求变量量纲不一致 / 单位不是有限为正 |
| `solve_bounds` | 上下界非有限或下界不小于上界 |
| `solve_target_dim` | 目标结果与主表达式量纲不一致 |
| `solve_ref` | 单位 / 目标表达式中引用了变量（只允许带单位数值） |
| `solve_no_bracket` | 区间两端残差同号，不包含达到目标的解 |
| `solve_max_iter` | 二分 80 次仍未达到收敛容差 |
| `solve_eval_failed` | 反求试算失败（量纲 / 定义域 / 非有限值等；错误项保留内部 `kind` 并带试算上下文字段） |
| `rename_missing_old` | 改名映射中的旧名不是当前配方已定义的变量（`source` 为 `rename:<旧名>`） |
| `rename_bad_new` | 新名称不符合变量标识符规则（字母 / `_` 起始，只含字母数字下划线） |
| `rename_duplicate` | 改名后变量名重复（含与未改名变量撞名） |
| `rename_shadow` | 新变量名会遮蔽未改写的内置单位 / 常量引用（带 `shadowed`、`builtin_kind`，定位到冲突片段），整次改名被拒绝 |

扰动类错误（`uncert_eval` 及扰动中暴露的 `value` 等）还带有 `perturbed`（扰动来源变量名）与 `direction`（`+` / `-`，表示正/负向步长）字段，其 `source` 指向真正出错的表达式（主表达式或 `var:<名称>`）；不确定度输入自身的错误 `source` 为 `uncert:<名称>`。

### 保存与导出案例

```bash
# 保存
curl -s -X POST http://localhost:8000/api/cases -H 'Content-Type: application/json' -d '{
  "name": "自由落体", "expression": "0.5*g0*t^2",
  "variables": {"t": "3 s"}, "uncertainties": {"t": "0.01 s"},
  "target_unit": "m"
}'

# 导出全部案例为 JSON 文件
curl -OJ http://localhost:8000/api/export
```

---

## 项目结构

```
├── app/
│   ├── main.py        # FastAPI 应用：API 路由（check / rename / solve / 案例）+ 静态页面
│   ├── engine.py      # 引擎：词法分析 → 递归下降解析 → 量纲/数值求值、双侧扰动 RSS 不确定度、二分反求、变量改名
│   ├── units.py       # 单位 / 常量 / 函数注册表，量纲格式化
│   ├── db.py          # SQLite 案例存储（含 uncertainties 列，旧库自动迁移）
│   └── static/        # 原生前端（index.html / app.js / style.css）
├── tests/             # pytest：引擎单元测试（含 test_uncertainty.py / test_solve.py / test_rename.py）+ API 集成测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt       # 运行依赖（fastapi / uvicorn）
└── requirements-dev.txt   # 测试依赖（pytest / httpx）
```

## 配置与环境变量

无需任何必填配置：默认即在 `0.0.0.0:8000` 提供 Web 页面与 API（同一端口）。Docker Compose 一键构建启动并将宿主机 `8000` 端口映射到容器 `8000`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `./data/app.db`（容器内 `/app/data/app.db`） | SQLite 数据库文件路径；父目录不存在时自动创建 |

如需更换端口：本地运行改 `uvicorn ... --port <端口>`；Docker 改 `docker-compose.yml` 的端口映射（容器内仍监听 8000，`Dockerfile` 的 `EXPOSE` 与健康检查无需改动）。
