"""Prompts for LLM-based invoice parsing."""

import json

# JSON Schema definition for strict output format
INVOICE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {
            "type": ["string", "null"],
            "description": "发票号码，通常为8-20位数字"
        },
        "invoice_code": {
            "type": ["string", "null"],
            "description": "发票代码，通常为10-12位数字（如有）"
        },
        "issue_date": {
            "type": ["string", "null"],
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            "description": "开票日期，格式必须为YYYY-MM-DD"
        },
        "buyer_name": {
            "type": ["string", "null"],
            "description": "购买方名称（公司全称）"
        },
        "buyer_tax_id": {
            "type": ["string", "null"],
            "pattern": "^[A-Z0-9]{15,20}$",
            "description": "购买方纳税人识别号，15-20位字母数字"
        },
        "seller_name": {
            "type": ["string", "null"],
            "description": "销售方名称（公司全称）"
        },
        "seller_tax_id": {
            "type": ["string", "null"],
            "pattern": "^[A-Z0-9]{15,20}$",
            "description": "销售方纳税人识别号，15-20位字母数字"
        },
        "item_name": {
            "type": ["string", "null"],
            "description": "项目名称/货物名称，如有类别标记(*类别*商品)则保留"
        },
        "specification": {
            "type": ["string", "null"],
            "description": "规格型号，项目名称右侧的规格型号列内容，不要混入项目名称"
        },
        "unit": {
            "type": ["string", "null"],
            "description": "单位，如个、台、件、项、小时"
        },
        "quantity": {
            "type": ["string", "null"],
            "description": "数量，只返回数字，不要带单位"
        },
        "unit_price": {
            "type": ["string", "null"],
            "description": "单价，只返回数字，不要带货币符号或逗号"
        },
        "total_with_tax": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d{1,2})?$",
            "description": "价税合计金额，纯数字不含货币符号"
        },
        "amount": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d{1,2})?$",
            "description": "金额（不含税），纯数字"
        },
        "tax_amount": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d{1,2})?$",
            "description": "税额，纯数字（免税发票返回\"0\"）"
        },
        "tax_rate": {
            "type": ["string", "null"],
            "description": "税率，如\"6%\"、\"13%\"、\"免税\""
        }
    },
    "required": [
        "invoice_number", "invoice_code", "issue_date",
        "buyer_name", "buyer_tax_id", "seller_name", "seller_tax_id",
        "item_name", "specification", "unit", "quantity", "unit_price",
        "total_with_tax", "amount", "tax_amount", "tax_rate"
    ],
    "additionalProperties": False
}

# Required fields list for validation
REQUIRED_FIELDS = list(INVOICE_JSON_SCHEMA["required"])

# Vision-based system prompt (for direct image analysis)
INVOICE_VISION_SYSTEM_PROMPT = """你是一个专业的中国发票信息提取助手。
你必须严格按照JSON Schema格式返回结果，无法识别的字段返回null。
只返回JSON对象，不要包含任何其他文字、解释或markdown代码块标记。"""

# Build field descriptions for the prompt
_field_descriptions = json.dumps(
    {k: v["description"] for k, v in INVOICE_JSON_SCHEMA["properties"].items()},
    ensure_ascii=False,
    indent=2
)

# Vision-based extraction prompt (for direct image analysis)
INVOICE_VISION_PROMPT = f"""请分析这张中国发票图片，提取发票信息。

## 输出格式要求（必须严格遵守）

返回一个JSON对象，必须包含以下17个字段（不能多也不能少）：
{_field_descriptions}

字段类型规则：
- 所有字段值必须是 string 或 null（数值也用字符串表示）
- 日期格式必须是 YYYY-MM-DD
- 金额字段仅包含数字和小数点
- 税率字段格式如 \"6%\"、\"13%\" 或 \"免税\"
- 不能返回空字符串，用 null 表示缺失

## 购买方与销售方识别规则（最重要）

根据标签文字识别，不要依赖位置：
- 购买方 = 标注为"购买方"、"购方"、"购货单位"的区域
- 销售方 = 标注为"销售方"、"销方"、"销货单位"的区域
- 销售方区域通常还有"开票人"、"收款人"、"复核"等字样

中国发票格式说明：
- 标准增值税发票：购买方在左上，销售方在左下
- 电子普通发票：购买方在上部，销售方在底部（价税合计下方）
- 全电发票：购买方和销售方可能左右并排

## 字段提取规则

1. invoice_number: 查找"发票号码"标签后的数字
2. invoice_code: 查找"发票代码"标签后的数字（如无则返回null）
3. issue_date: 查找"开票日期"，转换为YYYY-MM-DD格式
4. buyer_name/seller_name: 查找"名称"标签后的公司全称
5. buyer_tax_id/seller_tax_id: 查找"纳税人识别号"后的15-20位编码
6. item_name: 只查找商品明细表“项目名称/货物或应税劳务名称”列，不要把后面的列拼进来
7. specification: 只查找同一商品行“规格型号”列；没有内容返回null
8. unit: 只查找“单位”列；没有内容返回null
9. quantity: 只查找“数量”列，只返回数字；没有内容返回null
10. unit_price: 只查找“单价”列，只返回数字；没有内容返回null
11. total_with_tax: 查找“价税合计”或“小写”金额，只返回数字
12. amount: 查找“金额”或“合计”行的不含税金额
13. tax_amount: 查找“税额”，免税发票返回"0"
14. tax_rate: 查找“税率/征收率”列的值，如"6%"、"13%"、"免税"

## 明细表拆分规则
- 明细表一行通常按“项目名称 | 规格型号 | 单位 | 数量 | 单价 | 金额 | 税率 | 税额”排列。
- 必须根据表头和同一行的列边界拆分字段，不能把整行文字放入 item_name。
- 如果有多行商品，只返回第一条商品行；每个字段只保留对应列的内容。

## 数据清洗规则

- 金额字段去除¥、￥、$、逗号，只保留数字和小数点
- 日期统一转换为YYYY-MM-DD
- 无法识别的字段返回null，不要猜测

## 铁路电子客票自动路由

如果票面标题包含“铁路电子客票”或“电子客票号”，不要按商品明细表处理：
- item_name 返回“铁路客运服务”；票面写有“退票费”或“改签费”时分别返回“铁路退票费”或“铁路改签费”。unit 返回“张”；quantity 返回“1”；unit_price 和 total_with_tax 读取“票价”。
- specification 用“；”拼接车次、出发站→到达站、乘车日期和开车时间、席别、车厢座位、乘车人、电子客票号。
- 购买方只读取底部“购买方名称”“统一社会信用代码”；发票号码绝不能用电子客票号或证件号码代替。
- 票面没有明确税额、税率或销售方全称时返回 null；只出现“中国铁路祝您旅途愉快”时 seller_name 返回“中国铁路”。

请直接返回JSON对象："""


# Railway e-tickets have neither a VAT item table nor the usual buyer/seller
# layout.  They still share the same storage schema, so this template maps the
# journey into item_name/specification instead of treating passenger data as a
# malformed goods row.
RAILWAY_TICKET_VISION_SYSTEM_PROMPT = """你是中国铁路电子客票发票信息提取助手。
这类票据的版式与增值税发票不同。只提取图片中明确出现的信息，不要根据常识补造税率、税额或销售方全称。
你必须只返回JSON对象，不要包含解释或Markdown。"""

RAILWAY_TICKET_VISION_PROMPT = f"""请按“铁路电子客票”模板分析这张电子发票（铁路电子客票）图片。

返回一个JSON对象，必须包含以下17个字段（不能多也不能少）：
{_field_descriptions}

所有字段值必须是 string 或 null；日期用 YYYY-MM-DD；金额只保留数字和小数点；不能返回空字符串。

## 铁路电子客票字段映射

1. invoice_number：读取“发票号码”。不要读取“电子客票号”。
2. issue_date：读取右上角“开票日期”。不要误用乘车日期。
3. buyer_name / buyer_tax_id：只读取底部“购买方名称”“统一社会信用代码”。这两个字段可能在同一行，必须分开。
4. seller_name：仅当票面明确写有销售方名称时填写；如果只出现“中国铁路祝您旅途愉快”，返回“中国铁路”。seller_tax_id 无明确值时返回 null。
5. item_name：普通乘车票返回“铁路客运服务”；票面明确写“退票费”或“改签费”时分别返回“铁路退票费”或“铁路改签费”。
6. specification：把下列明确可读信息用“；”拼接：车次、出发站→到达站、乘车日期和开车时间、席别、车厢座位、乘车人、电子客票号。示例：
   "G872；杭州东→南京南；2026-08-29 15:43；二等座；14车06A号；乘车人徐孟键；电子客票号9004..."
   缺少的片段不要编造。
7. unit 固定为“张”，quantity 固定为“1”，unit_price 与 total_with_tax 都读取“票价”。
8. amount、tax_amount、tax_rate：票面未明确展示时必须返回 null。
9. 发票号码、电子客票号、证件号码、车次、车厢座位不能互相替代。

请直接返回JSON对象："""
