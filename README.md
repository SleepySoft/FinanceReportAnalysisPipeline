# FinanceReportAnalysisPipeline

A-share annual report data processing pipeline (for further anaysis)


## 处理流水线

**文本优先原则**

本系统采用 TXT-first 架构。所有输入源首先被转换为统一的标准文本对象，后续清洗、章节切分、价值分类、字段抽取和 AI 路由均优先基于标准文本对象执行。

PDF 坐标、页面版式、OCR 和图像信息不作为主流程依赖，只在文本缺失、文本质量不足、表格结构无法恢复、字段冲突或需要原文复核时作为 fallback 或增强资源使用。

因此，系统资源索引以文本 span 为主定位方式，以页码、bbox、HTML 节点等作为可选辅助定位方式。

```
TXT / PDF / HTML 年报
        |
        v
标准文本生成层
- TXT 直接接入
- HTML 正文抽取
- PDF 原生文本抽取
- OCR 识别兜底
- 统一生成标准文本对象
- 文本质量评估
        |
        v
文本清洗
- 检查编码和乱码
- 删除无用文字
    页眉，页脚，页码
    目录页码线
    连续空行
- 规范标点和空白字符
- 质量校验
        |
        v
标准章节切分层
- 目录识别
- 标题正则匹配
- 页码定位
- 多版本标题别名匹配
- 章节切分
- 更新资源索引（表格等等）
- 质量校验
        |
        v
价值分类层
- 强结构化高价值块: 财务指标表、利润表、资产负债表、现金流量表、前十大股东、分红方案
- 半结构化高价值块: 管理层讨论、主营业务、风险因素、重大事项、环保处罚、诉讼仲裁
- 弱价值但可索引:块 公司治理流程、董事履历、会议召开情况、制度性描述
- 低价值/噪声块: 页眉页脚、目录页码、重复声明、空白页、广告图片说明、免责声明重复段
        |
        v
字段抽取层
- 规则字段（正则抽取/表格抽取）
    company_name: 公司名称
    stock_code: 股票代码
    report_year: 报告期年份
    revenue: 营业收入
    net_profit_parent: 归母净利润
    total_assets: 总资产
    net_assets_parent: 归母净资产
    eps_basic: 基本每股收益
    roe_weighted: 加权平均净资产收益率
    audit_opinion: 审计意见类型
    accounting_firm: 会计师事务所
    top10_shareholders: 前十名股东
    cash_dividend_plan: 现金分红方案
- 表格字段（？）
    报表数据（TXT怎么办？）
- AI 字段（语义级抽取）
    business_model_summary: 主营业务模式
    core_competitiveness: 核心竞争力
    major_risks: 主要风险
    industry_trend: 行业趋势
    management_analysis: 管理层经营分析
    future_strategy: 未来发展战略
    abnormal_events: 异常事项
    是否提到增长或萎缩
    .
    .
    .
    .
        |
        v
结构化存储
- JSON
- Markdown
```


## 文件结构（概念）

```
processed/
  2024_600519/
    meta.json
    sections/
      01_重要提示_目录_释义.txt
      02_公司简介和主要财务指标.txt
      03_管理层讨论与分析.txt
      04_公司治理.txt
      05_环境和社会责任.txt
      06_重要事项.txt
      07_股份变动及股东情况.txt
      08_债券相关情况.txt
      09_财务报告.txt
      99_低价值与噪声.txt

    tables/
      main_financial_indicators.csv
      balance_sheet.csv
      income_statement.csv
      cash_flow_statement.csv
      top10_shareholders.csv

    extracted/
      rule_fields.json
      ai_fields.json
      final_fields.json

    qa/
      section_detect_report.json
      token_estimate.json
      warnings.json

      
    intermediate/
      standard_text.json
      chunks.json
      resource_index.json
      table_candidates.json
```


## 鲁棒性

#### 别名

由于章节标题不一定一致，所以需要别名词典。

```python
# ---------- 仅示例 ----------
SECTION_ALIASES = {
    "重要提示_目录_释义": [
        "重要提示",
        "目录",
        "释义"
    ],
    "公司简介和主要财务指标": [
        "公司简介和主要财务指标",
        "公司基本情况",
        "主要会计数据和财务指标"
    ],
    "管理层讨论与分析": [
        "管理层讨论与分析",
        "经营情况讨论与分析",
        "董事会报告",
        "报告期内公司所处行业情况"
    ],
    "公司治理": [
        "公司治理",
        "公司治理情况"
    ],
    "环境和社会责任": [
        "环境和社会责任",
        "环境与社会责任",
        "社会责任",
        "环境保护相关情况"
    ],
    "重要事项": [
        "重要事项",
        "重大事项"
    ],
    "股份变动及股东情况": [
        "股份变动及股东情况",
        "股份变动和股东情况",
        "股东和实际控制人情况"
    ],
    "债券相关情况": [
        "债券相关情况",
        "公司债券相关情况"
    ],
    "财务报告": [
        "财务报告",
        "审计报告",
        "财务报表附注"
    ]
}
```


#### 错误检测和Fallback

+ 定义每个步骤可接受及停止条件，报告所有问题。



