import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error, parse, request


DEFAULT_ORDER_NUMBER = "ALS01481544388"
DEFAULT_REQUEST_FILE = "tmp/[3656] request_onetouch-partner.alibaba.com_message.txt"
DEFAULT_RESPONSE_FILE = "tmp/[3656] response_onetouch-partner.alibaba.com_message.txt"
DEFAULT_WEBHOOK_URL = (
    "https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook"
    "?key=3OeXQfxTesRYhrHFn8IvM4Q8egvx1oal4aVhfBJ8jfo5xvh1CpJ2zkEsW5aGLcGnrg4djwooLpMmLZrtqQBopCZqxIhPzEww4SrZTkDDtUza"
)


LOGGER = logging.getLogger("onetouch_to_wedoc")


BASE_SCHEMA_DEFINITION: Dict[str, Dict[str, Any]] = {
    "flosGy": {"label": "订单号", "source_field": "orderNumber", "type": "text", "empty": ""},
    "f3Vstd": {"label": "下单账号", "source_field": "sellerLoginId", "type": "text", "empty": ""},
    "fyNfPg": {"label": "用户名称", "source_field": "customerName", "type": "text", "empty": ""},
    "f6KONI": {"label": "电话", "source_field": "mobileNo", "type": "text", "empty": ""},
    "fE2nO0": {"label": "邮箱", "source_field": "email", "type": "text", "empty": ""},
    "faGx63": {
        "label": "方案类型",
        "source_field": "solutionName",
        "type": "select",
        "empty": [],
    },
}

REQUIRED_EXTRACTED_FIELDS = [
    "email",
    "mobileNo",
    "sellerLoginId",
    "customerName",
    "solutionName",
    "orderNumber",
]


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_response_body(response_text: str) -> Dict[str, Any]:
    body_start = response_text.find("{")
    if body_start < 0:
        raise ValueError("响应文件中未找到 JSON 包体")
    return json.loads(response_text[body_start:])


def parse_request_order_number(request_text: str) -> Optional[str]:
    first_line = request_text.splitlines()[0] if request_text else ""
    parts = first_line.split(" ")
    if len(parts) < 2:
        return None

    parsed_url = parse.urlsplit(parts[1])
    query = parse.parse_qs(parsed_url.query)
    json_param = query.get("json", [None])[0]
    if json_param:
        try:
            request_json = json.loads(json_param)
            return request_json.get("generalSearchField")
        except json.JSONDecodeError:
            LOGGER.warning("请求报文 json 参数解析失败，将回退到字符串检索")

    marker = '"generalSearchField":"'
    start = request_text.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = request_text.find('"', start)
    if end < 0:
        return None
    return request_text[start:end]


def find_order_record(payload: Dict[str, Any], order_number: str) -> Optional[Dict[str, Any]]:
    data_list = payload.get("data", {}).get("dataList", [])
    for item in data_list:
        if item.get("orderNumber") == order_number:
            return item
    return None


def extract_fields(record: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    contact = record.get("consignorAddress", {}).get("contact", {})
    solution_name = "半托管" if "半托管" in str(record.get("solutionName", "")) else "非半托管"

    extracted = {
        "email": str(contact.get("email") or ""),
        "mobileNo": str(contact.get("mobileNo") or ""),
        "sellerLoginId": str(record.get("sellerLoginId") or ""),
        "customerName": str(record.get("customerName") or ""),
        "solutionName": solution_name,
        "orderNumber": str(record.get("orderNumber") or ""),
    }

    missing = [field for field in REQUIRED_EXTRACTED_FIELDS if not extracted.get(field)]
    return extracted, missing


def build_schema_definition(extra_empty_fields: List[str]) -> Dict[str, Dict[str, Any]]:
    schema_definition = dict(BASE_SCHEMA_DEFINITION)
    for field_id in extra_empty_fields:
        schema_definition[field_id] = {
            "label": f"额外空字段:{field_id}",
            "source_field": None,
            "type": "text",
            "empty": "",
        }
    return schema_definition


def build_add_record_values(
    extracted: Dict[str, str],
    schema_definition: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    values: Dict[str, Any] = {}

    for field_id, field_config in schema_definition.items():
        source_field = field_config["source_field"]
        if source_field is None:
            values[field_id] = field_config["empty"]
            continue

        raw_value = extracted.get(source_field, "")
        if field_id == "faGx63":
            values[field_id] = [{"text": raw_value}] if raw_value else []
        else:
            values[field_id] = raw_value if raw_value else field_config["empty"]

    return values


def build_preview_payload(
    values: Dict[str, Any],
    schema_definition: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "schema": schema_definition,
        "add_records": [
            {
                "values": values,
            }
        ],
    }


def build_webhook_payload(values: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "add_records": [
            {
                "values": values,
            }
        ]
    }


def send_webhook(webhook_url: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    with request.urlopen(req, timeout=timeout) as resp:
        response_text = resp.read().decode("utf-8")

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        return {"raw_response": response_text}


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def process(args: argparse.Namespace) -> int:
    base_dir = Path(args.base_dir).resolve()
    request_file = base_dir / args.request_file
    response_file = base_dir / args.response_file

    request_text = read_text(request_file)
    response_text = read_text(response_file)

    request_order_number = parse_request_order_number(request_text)
    if request_order_number:
        LOGGER.info("请求报文中的查询单号: %s", request_order_number)
    else:
        LOGGER.warning("请求报文中未解析到查询单号，将直接使用入参单号: %s", args.order_number)

    response_payload = parse_response_body(response_text)
    target_record = find_order_record(response_payload, args.order_number)
    if not target_record:
        LOGGER.error("未在接口响应中找到单号 %s 对应的数据", args.order_number)
        return 1

    extracted, missing_fields = extract_fields(target_record)
    if missing_fields:
        LOGGER.error("单号 %s 缺少必要字段: %s", args.order_number, ", ".join(missing_fields))
    else:
        LOGGER.info("单号 %s 的必要字段提取完整", args.order_number)

    extra_empty_fields = [field_id.strip() for field_id in args.extra_empty_field_ids if field_id.strip()]
    if extra_empty_fields:
        LOGGER.info("将显式补空以下额外字段: %s", ", ".join(extra_empty_fields))
    else:
        LOGGER.warning("未提供额外空字段 schema，仅发送已确认映射字段")

    schema_definition = build_schema_definition(extra_empty_fields)
    values = build_add_record_values(extracted, schema_definition)
    preview_payload = build_preview_payload(values, schema_definition)
    webhook_payload = build_webhook_payload(values)

    preview_path = base_dir / args.preview_file
    webhook_payload_path = base_dir / args.webhook_payload_file
    write_json(preview_path, preview_payload)
    write_json(webhook_payload_path, webhook_payload)

    LOGGER.info("预览 payload 已写入: %s", preview_path)
    LOGGER.info("发送 payload 已写入: %s", webhook_payload_path)

    result: Dict[str, Any] = {
        "order_number": args.order_number,
        "request_order_number": request_order_number,
        "missing_fields": missing_fields,
        "extracted": extracted,
        "preview_payload_file": str(preview_path),
        "webhook_payload_file": str(webhook_payload_path),
        "sent": False,
    }

    if args.send:
        try:
            webhook_response = send_webhook(args.webhook_url, webhook_payload, args.timeout)
            result["sent"] = True
            result["webhook_response"] = webhook_response
            LOGGER.info("Webhook 发送完成")
        except error.HTTPError as exc:
            error_text = exc.read().decode("utf-8", errors="replace")
            LOGGER.error("Webhook HTTP 错误: %s %s", exc.code, error_text)
            result["webhook_error"] = {"status": exc.code, "body": error_text}
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Webhook 发送失败: %s", exc)
            result["webhook_error"] = {"message": str(exc)}
    else:
        LOGGER.info("当前为 dry-run，未实际发送 webhook")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not missing_fields else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从 Onetouch 响应提取订单数据并写入企业微信智能表格 webhook。")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--request-file", default=DEFAULT_REQUEST_FILE)
    parser.add_argument("--response-file", default=DEFAULT_RESPONSE_FILE)
    parser.add_argument("--order-number", default=DEFAULT_ORDER_NUMBER)
    parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL)
    parser.add_argument("--preview-file", default="wedoc_payload_preview.json")
    parser.add_argument("--webhook-payload-file", default="wedoc_webhook_payload.json")
    parser.add_argument("--log-file", default="logs/onetouch_to_wedoc.log")
    parser.add_argument(
        "--extra-empty-field-id",
        dest="extra_empty_field_ids",
        action="append",
        default=[],
        help="需要显式置空的额外字段 ID，可重复传入多次",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--send", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(Path(args.base_dir) / args.log_file)
    return process(args)


if __name__ == "__main__":
    raise SystemExit(main())
