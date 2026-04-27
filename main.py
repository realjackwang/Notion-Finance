import os
import requests
import json
import time
import base64
import datetime
import chinese_calendar as calendar
import holidays
from notion_client import Client
from sm_crypto.sm4 import CryptSM4
from dotenv import load_dotenv

load_dotenv()

# 从环境变量获取密钥
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
ASSETS_DB_ID = os.environ.get("DATABASE_ID_ASSETS")
HISTORY_DB_ID = os.environ.get("DATABASE_ID_HISTORY")
TRANSACTION_DB_ID = os.environ.get("DATABASE_ID_TRANSACTIONS")
AUTO_INVEST_LOG_DB_ID = os.environ.get("DATABASE_ID_DCA_LOGS")

# Notion 仪表盘块 ID
TOTAL_ASSET_BLOCK_ID = os.environ.get("NOTION_BLOCK_TOTAL_ASSET_ID")
CUMULATIVE_PROFIT_BLOCK_ID = os.environ.get("NOTION_BLOCK_CUMULATIVE_PROFIT_ID")
DAILY_PROFIT_BLOCK_ID = os.environ.get("NOTION_BLOCK_DAILY_PROFIT_ID")
MAX_DRAWDOWN_BLOCK_ID = os.environ.get("NOTION_BLOCK_MAX_DRAWDOWN_ID")
ANNUAL_RETURN_BLOCK_ID = os.environ.get("NOTION_BLOCK_ANNUAL_RETURN_ID")

# 招商银行 API 鉴权常量
CMB_AUTH_APP_ID = "FinProd"
CMB_AUTH_KEY = os.environ.get("CMB_AUTH_KEY")
CMB_VALUE_APP_ID = "LB50.22_CFWebUI"

# 调试配置：可跳过的步骤（用逗号分隔）
DEBUG_SKIP = set(os.environ.get("DEBUG_SKIP", "").lower().split(",")) if os.environ.get("DEBUG_SKIP") else set()
DEBUG_FORCE_DCA = os.environ.get("DEBUG_FORCE_DCA", "").lower() == "true"

notion = Client(auth=NOTION_TOKEN)
DIRECT_REQUEST_SESSION = requests.Session()
DIRECT_REQUEST_SESSION.trust_env = False


def log_section(title):
    print()
    print(f"===== {title} =====")


def log_info(message):
    print(f"ℹ️ | {message}")


def log_success(message):
    print(f"✅ | {message}")


def log_skip(message):
    print(f"⏭️ | {message}")


def log_wait(message):
    print(f"⏳ | {message}")


def log_warn(message):
    print(f"⚠️ | {message}")


def log_error(message):
    print(f"❌ | {message}")


def log_debug(message):
    print(f"🧪 | {message}")


def get_database_data_source_id(database_id):
    db_info = notion.databases.retrieve(database_id=database_id)
    return db_info.get("data_sources", [{}])[0].get("id")


def query_all_data_source_rows(data_source_id, filter=None):
    results = []
    next_cursor = None

    while True:
        query_kwargs = {"data_source_id": data_source_id}
        if filter is not None:
            query_kwargs["filter"] = filter
        if next_cursor:
            query_kwargs["start_cursor"] = next_cursor

        response = notion.data_sources.query(**query_kwargs)
        results.extend(response.get("results", []))

        if not response.get("has_more"):
            break

        next_cursor = response.get("next_cursor")

    return results


def get_title_text(page, property_name):
    title_items = page.get("properties", {}).get(property_name, {}).get("title", [])
    if not title_items:
        return ""
    return "".join(item.get("plain_text", "") for item in title_items)


def get_property_date(page, property_name):
    date_value = (
        page.get("properties", {})
        .get(property_name, {})
        .get("date")
    )
    if not date_value:
        return None

    return (
        date_value.get("start")
    )


def update_page_date_property(page_id, property_name, date_value):
    notion.pages.update(
        page_id=page_id,
        properties={property_name: {"date": {"start": date_value}}}
    )


def get_latest_transaction_date(asset_page_id):
    if not TRANSACTION_DB_ID:
        return None

    db_info = notion.databases.retrieve(database_id=TRANSACTION_DB_ID)
    ds_id = db_info.get("data_sources", [{}])[0].get("id")

    response = notion.data_sources.query(
        data_source_id=ds_id,
        filter={
            "property": "所属资产",
            "relation": {"contains": asset_page_id}
        },
        sorts=[{"property": "交易日期", "direction": "descending"}],
        page_size=1,
    )

    results = response.get("results", [])
    if not results:
        return None

    return get_property_date(results[0], "交易日期")


def get_checkbox_value(page, property_name):
    return (
        page.get("properties", {})
        .get(property_name, {})
        .get("checkbox", False)
    )


def get_number_value(page, property_name, default=None):
    value = (
        page.get("properties", {})
        .get(property_name, {})
        .get("number")
    )
    return default if value is None else value


def numbers_equal(left, right, precision=8):
    if left is None or right is None:
        return left is right
    return round(float(left) - float(right), precision) == 0


def normalize_month_day_to_date(month_day_text, today=None):
    if not month_day_text:
        return None

    today = today or datetime.date.today()
    try:
        month, day = [int(part) for part in month_day_text.split("-", 1)]
        candidate = datetime.date(today.year, month, day)
    except (ValueError, TypeError):
        return None

    # 年初查看上一年末净值时，接口通常不带年份，这里做一个保守回拨。
    if candidate > today + datetime.timedelta(days=31):
        candidate = datetime.date(today.year - 1, month, day)

    return candidate.strftime("%Y-%m-%d")


def normalize_chinese_date(date_text):
    if not date_text:
        return None

    cleaned = (
        str(date_text)
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .strip()
    )
    try:
        return datetime.datetime.strptime(cleaned, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def is_market_trading_day(day, market, holiday_calendars):
    if day.weekday() >= 5:
        return False

    if market == "A股":
        return calendar.is_workday(day)

    market_holidays = holiday_calendars.get(market)
    if market_holidays and day in market_holidays:
        return False

    return True


def http_request(method, url, **kwargs):
    try:
        return requests.request(method, url, **kwargs)
    except requests.exceptions.ProxyError as e:
        log_warn(f"检测到代理错误，改为直连重试: {e}")
        return DIRECT_REQUEST_SESSION.request(method, url, **kwargs)
    except requests.exceptions.SSLError as e:
        log_warn(f"检测到 SSL 错误，改为直连重试: {e}")
        return DIRECT_REQUEST_SESSION.request(method, url, **kwargs)

def generate_cmb_signature(timespan):
    try:
        if not CMB_AUTH_KEY:
            log_warn("未配置 CMB_AUTH_KEY，无法生成招商银行接口签名。")
            return None
        message = f"{CMB_AUTH_APP_ID}|{timespan}"
        key = CMB_AUTH_KEY.encode('utf-8')
        crypt = CryptSM4()
        crypt.set_key(key, 0)
        encrypted = crypt.crypt_ecb(message.encode('utf-8'))
        return base64.b64encode(encrypted).decode('utf-8')
    except Exception as e:
        log_error(f"生成签名出错: {e}")
        return None


def generate_cmb_signature_for_app(app_id, timespan):
    try:
        if not CMB_AUTH_KEY:
            log_warn("未配置 CMB_AUTH_KEY，无法生成招商银行接口签名。")
            return None
        message = f"{app_id}|{timespan}"
        key = CMB_AUTH_KEY.encode('utf-8')
        crypt = CryptSM4()
        crypt.set_key(key, 0)
        encrypted = crypt.crypt_ecb(message.encode('utf-8'))
        return base64.b64encode(encrypted).decode('utf-8')
    except Exception as e:
        log_error(f"生成签名出错: {e}")
        return None

def get_tiantian_fund_snapshot(fund_code):
    """天天基金获取净值和净值日期"""
    url = f"http://fundgz.1234567.com.cn/js/{fund_code}.js"
    try:
        response = http_request("GET", url, timeout=10)
        content = response.text
        json_str = content[content.find('{'):content.rfind('}')+1]
        data = json.loads(json_str)
        price = data.get("dwjz")
        nav_date = data.get("jzrq")
        return {
            "price": float(price) if price not in (None, "") else None,
            "nav_date": nav_date,
        }
    except Exception as e:
        log_warn(f"备用接口(天天基金)获取 {fund_code} 出错: {e}")
        return {"price": None, "nav_date": None}

def get_fund_info(fund_code):
    url = f"https://danjuanfunds.com/djapi/fund/{fund_code}"
    headers = {
        'accept': 'application/json, text/plain, */*',
        'referer': f'https://danjuanfunds.com/funding/{fund_code}',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
    }

    result = {"price": None, "drawdown": None, "nav_date": None}

    try:
        response = http_request("GET", url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json().get("data", {})
        base_data = data.get("sec_header_base_data", [])

        for item in base_data:
            if item.get("data_name") == "最新净值":
                val = item.get("data_value_number")
                if val is not None and val != "":
                    result["price"] = float(val)
                result["nav_date"] = normalize_month_day_to_date(item.get("data_extend"))
            elif item.get("data_name") == "最大回撤":
                val = item.get("data_value_number")
                if val is not None and val != "":
                    result["drawdown"] = float(val)
    except Exception as e:
        log_warn(f"主接口(雪球基金)请求 {fund_code} 出错: {e}")

    # 兜底逻辑：主接口未拿到净值时，才启用天天基金备用接口。
    if result["price"] is None:
        log_wait(f"雪球基金未返回净值，尝试使用天天基金备用接口获取 {fund_code}...")
        tiantian_snapshot = get_tiantian_fund_snapshot(fund_code)
        result["price"] = tiantian_snapshot.get("price")
        result["nav_date"] = tiantian_snapshot.get("nav_date")

    return result


def get_fund_price_by_date(fund_code, trade_date):
    url = f"https://danjuanfunds.com/djapi/fund/growth/{fund_code}?day=ty"
    headers = {
        "accept": "application/json, text/plain, */*",
        "referer": f"https://danjuanfunds.com/funding/{fund_code}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    }

    try:
        response = http_request("GET", url, headers=headers, timeout=10)
        response.raise_for_status()
        rows = (response.json().get("data") or {}).get("fund_nav_growth") or []
        for row in rows:
            if row.get("date") == trade_date:
                nav = row.get("nav")
                return {
                    "price": float(nav) if nav not in (None, "") else None,
                    "nav_date": trade_date,
                }
        return {"price": None, "nav_date": None}
    except Exception as e:
        log_warn(f"按日期获取基金 {fund_code} 净值出错: {e}")
        return {"price": None, "nav_date": None}

def get_cmb_wealth_price(product_code):
    url = 'https://finprod.paas.cmbchina.com/api/prod/queryProdList'
    timespan = str(int(time.time() * 1000)) 
    signature = generate_cmb_signature(timespan)
    if not signature: return None
    
    headers = {
        'accept': 'application/json',
        'appid': CMB_AUTH_APP_ID,
        'content-type': 'application/json;charset=UTF-8',
        'origin': 'https://finprod.paas.cmbchina.com',  
        'referer': 'https://finprod.paas.cmbchina.com/', 
        'signature': signature,
        'timespan': timespan,
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    payload = {
        "keyWords": product_code,
        "type": "PN",
        "isOwn": "A",
        "isPublic": "Z",
        "status": "0",
        "pageNO": 1,
        "pageSize": 50,
        "crossFinance": "Z",
        "riskLevel": "",
        "obligate": "",
    }
    
    result = {"price": None, "nav_date": None}

    try:
        response = http_request("POST", url, headers=headers, json=payload, timeout=10)
        data = response.json()
        code = data.get("code")
        items = (data.get("data") or {}).get("list") or []

        if code == "SUC0000" and items:
            first_item = items[0]
            nav = first_item.get("dnvval")
            if nav is not None and nav != "":
                result["price"] = float(nav)
            result["nav_date"] = normalize_chinese_date(first_item.get("zprfDat"))
            result["saa_code"] = first_item.get("saacod")

        return result
    except Exception as e:
        log_warn(f"获取理财 {product_code} 接口调用出错: {e}")
        return result


def get_cmb_wealth_price_by_date(product_code, saa_code, trade_date):
    url = "https://cfweb.paas.cmbchina.com/api/ProductValue/getSAValueByPageOrDate"
    timespan = str(int(time.time() * 1000))
    signature = generate_cmb_signature_for_app(CMB_VALUE_APP_ID, timespan)
    if not signature:
        return {"price": None, "nav_date": None}

    headers = {
        "accept": "application/json",
        "appid": CMB_VALUE_APP_ID,
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://cfweb.paas.cmbchina.com",
        "referer": "https://cfweb.paas.cmbchina.com/personal/default",
        "signature": signature,
        "timespan": timespan,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    }
    payload = {
        "saaCod": saa_code,
        "funCod": product_code,
        "pageIndex": 1,
        "pageSize": 10,
        "startDate": "",
        "endDate": "",
    }

    try:
        response = http_request("POST", url, headers=headers, json=payload, timeout=10)
        data = response.json()
        rows = ((data.get("body") or {}).get("data")) or []
        if not rows:
            return {"price": None, "nav_date": None}

        target_date = trade_date.replace("-", "")
        for row in rows:
            if row.get("znavDat") == target_date:
                nav_val = row.get("znavVal")
                return {
                    "price": float(nav_val) if nav_val not in (None, "") else None,
                    "nav_date": trade_date,
                }

        return {"price": None, "nav_date": None}
    except Exception as e:
        log_warn(f"按日期获取理财 {product_code} 净值出错: {e}")
        return {"price": None, "nav_date": None}

def auto_record_investments():
    if not AUTO_INVEST_LOG_DB_ID:
        log_warn("未配置 DATABASE_ID_DCA_LOGS，跳过自动定投日志写入。")
        return

    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    log_section(f"自动定投日志 | {today}")

    world_holidays = {
        "美股": holidays.US(years=today.year),
        "港股": holidays.HK(years=today.year),
        "日股": holidays.JP(years=today.year)
    }

    try:
        ds_id = get_database_data_source_id(ASSETS_DB_ID)
        auto_invest_log_ds_id = get_database_data_source_id(AUTO_INVEST_LOG_DB_ID)

        assets = query_all_data_source_rows(
            ds_id,
            filter={"property": "自动定投", "checkbox": {"equals": True}}
        )
        existing_log_rows = query_all_data_source_rows(
            auto_invest_log_ds_id,
            filter={"property": "交易日期", "date": {"equals": today_str}}
        )
        existing_log_keys = set()
        for row in existing_log_rows:
            relations = row.get("properties", {}).get("所属资产", {}).get("relation", [])
            if not relations:
                continue
            existing_log_keys.add((relations[0]["id"], get_property_date(row, "交易日期")))

        for page in assets:
            p = page.get("properties", {})
            asset_name_prop = p.get("资产名称", {}).get("title", [])
            asset_name = asset_name_prop[0]["text"]["content"] if asset_name_prop else "未知资产"

            amount = p.get("定投金额", {}).get("number") or 0
            fee_rate = p.get("手续费", {}).get("number") or 0
            if amount <= 0:
                continue

            market_prop = p.get("交易日历", {}).get("select", {})
            market = market_prop.get("name", "A股") if market_prop else "A股"

            if not DEBUG_FORCE_DCA and not is_market_trading_day(today, market, world_holidays):
                holiday_name = world_holidays.get(market, {}).get(today)
                if holiday_name:
                    log_skip(f"[{asset_name}] 今日为{market}法定节假日 ({holiday_name})，休市跳过。")
                else:
                    log_skip(f"[{asset_name}] 今日为{market}非交易日，跳过。")
                continue
            if DEBUG_FORCE_DCA and not is_market_trading_day(today, market, world_holidays):
                log_debug(f"[{asset_name}] 今日虽为{market}非交易日，但已启用 DEBUG_FORCE_DCA。")

            log_key = (page["id"], today_str)
            if log_key in existing_log_keys:
                log_skip(f"[{asset_name}] 已存在定投日志 | 日期: {today_str}")
                continue

            notion.pages.create(
                parent={"database_id": AUTO_INVEST_LOG_DB_ID},
                properties={
                    "名称": {
                        "title": [
                            {
                                "type": "text",
                                "text": {"content": f"{asset_name} {today_str}"}
                            }
                        ]
                    },
                    "所属资产": {"relation": [{"id": page["id"]}]},
                    "交易日期": {"date": {"start": today_str}},
                    "发生金额": {"number": amount},
                    "手续费": {"number": fee_rate},
                    "确认份额": {"checkbox": False},
                }
            )
            actual_amount = amount * (1 - fee_rate)
            if fee_rate > 0:
                log_success(f"[{asset_name}] 已写入定投日志 | 市场: {market} | 定投金额: {amount} | 手续费: {fee_rate}% | 实际买入: {round(actual_amount, 2)}")
            else:
                log_success(f"[{asset_name}] 已写入定投日志 | 市场: {market} | 金额: {amount}")

            time.sleep(0.5)

    except Exception as e:
        log_error(f"自动定投执行出错: {e}")


def process_pending_auto_invest_logs(asset_pages):
    if not AUTO_INVEST_LOG_DB_ID:
        return

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    log_section("待确认定投处理")
    auto_invest_log_ds_id = get_database_data_source_id(AUTO_INVEST_LOG_DB_ID)
    pending_logs = query_all_data_source_rows(
        auto_invest_log_ds_id,
        filter={"property": "确认份额", "checkbox": {"equals": False}}
    )
    asset_map = {page["id"]: page for page in asset_pages}

    for log_page in pending_logs:
        try:
            relations = log_page.get("properties", {}).get("所属资产", {}).get("relation", [])
            if not relations:
                log_warn("存在缺少所属资产的定投日志，已跳过。")
                continue

            asset_page_id = relations[0]["id"]
            asset_page = asset_map.get(asset_page_id)
            if not asset_page:
                asset_page = notion.pages.retrieve(page_id=asset_page_id)
                asset_map[asset_page_id] = asset_page

            asset_name = get_title_text(asset_page, "资产名称") or "未知资产"
            trade_date = get_property_date(log_page, "交易日期")
            nav_date = get_property_date(asset_page, "净值日期")
            asset_type = (
                asset_page.get("properties", {})
                .get("资产分类", {})
                .get("select", {})
                .get("name", "")
            )
            code_prop = asset_page.get("properties", {}).get("产品代码", {}).get("rich_text", [])
            product_code = code_prop[0]["text"]["content"] if code_prop else None

            if not trade_date:
                log_warn(f"[{asset_name}] 定投日志缺少交易日期，已跳过。")
                continue

            confirm_price = None
            confirm_nav_date = None

            if "理财" in asset_type and product_code:
                wealth_info = get_cmb_wealth_price(product_code)
                saa_code = wealth_info.get("saa_code")
                if not saa_code:
                    log_warn(f"[{asset_name}] 未获取到理财 saa_code，无法按日期确认。")
                    continue
                history_info = get_cmb_wealth_price_by_date(product_code, saa_code, trade_date)
                confirm_price = history_info.get("price")
                confirm_nav_date = history_info.get("nav_date")
            elif "基金" in asset_type and product_code:
                history_info = get_fund_price_by_date(product_code, trade_date)
                confirm_price = history_info.get("price")
                confirm_nav_date = history_info.get("nav_date")
            else:
                if nav_date and nav_date >= trade_date:
                    confirm_price = get_number_value(asset_page, "当前净值")
                    confirm_nav_date = nav_date

            if not confirm_nav_date or confirm_nav_date < trade_date or not confirm_price:
                log_wait(f"[{asset_name}] 交易日 {trade_date} 仍未拿到可确认净值（当前净值日期: {nav_date or '无'}），继续等待。")
                continue

            amount = get_number_value(log_page, "发生金额", 0.0) or 0.0
            fee_rate = get_number_value(log_page, "手续费", 0.0) or 0.0

            actual_amount = amount * (1 - fee_rate)
            confirmed_shares = round(actual_amount / confirm_price, 2)

            notion.pages.create(
                parent={"database_id": TRANSACTION_DB_ID},
                properties={
                    "交易日期": {"date": {"start": trade_date}},
                    "所属资产": {"relation": [{"id": asset_page_id}]},
                    "交易类型": {"select": {"name": "买入"}},
                    "发生金额": {"number": amount},
                    "成交份额": {"number": confirmed_shares}
                }
            )
            notion.pages.update(
                page_id=log_page["id"],
                properties={
                    "成交净值": {"number": confirm_price},
                    "成交份额": {"number": confirmed_shares},
                    "确认份额": {"checkbox": True},
                    "写入日期": {"date": {"start": today_str}},
                }
            )
            log_success(f"[{asset_name}] 已确认定投 | 交易日: {trade_date} | 成交净值: {confirm_price} | 成交份额: {confirmed_shares}")
        except Exception as e:
            log_warn(f"定投日志处理失败，已跳过: {e}")


def update_average_cost(asset_page_id, asset_name):
    if not TRANSACTION_DB_ID: return

    try:
        latest_transaction_date = get_latest_transaction_date(asset_page_id)
        if not latest_transaction_date:
            log_skip(f"[{asset_name}] 无交易记录，跳过成本重算")
            return

        asset_page = notion.pages.retrieve(page_id=asset_page_id)
        last_cost_update_date = get_property_date(asset_page, "持仓成本更新日期")
        if last_cost_update_date and latest_transaction_date <= last_cost_update_date:
            log_skip(f"[{asset_name}] 无新交易，跳过成本重算")
            return

        db_info = notion.databases.retrieve(database_id=TRANSACTION_DB_ID)
        ds_id = db_info.get("data_sources", [{}])[0].get("id")

        query_payload = {
            "data_source_id": ds_id,
            "filter": {
                "property": "所属资产", 
                "relation": {"contains": asset_page_id}
            }
        }
        
        transactions = query_all_data_source_rows(
            ds_id,
            filter=query_payload["filter"]
        )
        
        def get_date(tx):
            return tx.get("properties", {}).get("交易日期", {}).get("date", {}).get("start", "1970-01-01")
        
        transactions.sort(key=get_date)

        total_shares = 0.0
        avg_cost = 0.0
        
        for tx in transactions:
            p = tx.get("properties", {})
            tx_type = p.get("交易类型", {}).get("select", {}).get("name", "")
            amount = p.get("发生金额", {}).get("number") or 0.0
            shares = p.get("成交份额", {}).get("number") or 0.0
            
            if tx_type == "买入":
                new_shares = total_shares + shares
                if new_shares > 0:
                    avg_cost = (total_shares * avg_cost + amount) / new_shares
                total_shares = new_shares
            elif tx_type == "卖出":
                total_shares += shares 
                if total_shares <= 0:
                    total_shares = 0.0
                    avg_cost = 0.0

        notion.pages.update(
            page_id=asset_page_id,
            properties={
                "持仓成本": {"number": round(avg_cost, 4)},
                "持仓成本更新日期": {"date": {"start": latest_transaction_date}}
            }
        )
        log_success(f"[{asset_name}] 成本单价重算成功 | 成本: {round(avg_cost, 4)} | 更新日期: {latest_transaction_date}")
        
    except Exception as e:
        log_error(f"[{asset_name}] 成本计算失败: {str(e)}")

def get_yesterday_profit(today=None):
    if not HISTORY_DB_ID: return 0.0
    today = today or datetime.date.today()
    try:
        db_info = notion.databases.retrieve(database_id=HISTORY_DB_ID)
        ds_id = db_info.get("data_sources", [{}])[0].get("id")
        
        results = query_all_data_source_rows(ds_id)
        
        if not results: return 0.0
        
        def get_history_date(page):
            return page.get("properties", {}).get("日期", {}).get("date", {}).get("start", "1970-01-01")
        
        history_before_today = [
            page for page in results
            if get_history_date(page) < today.strftime("%Y-%m-%d")
        ]

        if not history_before_today:
            return 0.0

        history_before_today.sort(key=get_history_date, reverse=True)
        props = history_before_today[0].get("properties", {})
        
        last_profit = props.get("累计总盈亏", {}).get("number")
        if last_profit is None:
            last_val = props.get("总市值", {}).get("number") or 0.0
            last_cost = props.get("总本金", {}).get("number") or 0.0
            last_profit = last_val - last_cost
            
        return last_profit
    except Exception as e:
        log_warn(f"读取昨日快照失败: {e}")
        return 0.0


def upsert_history_snapshot(snapshot_date, total_val, total_cost, total_profit, daily_profit, weighted_annual_return):
    db_info = notion.databases.retrieve(database_id=HISTORY_DB_ID)
    ds_id = db_info.get("data_sources", [{}])[0].get("id")

    existing_rows = query_all_data_source_rows(
        ds_id,
        filter={"property": "日期", "date": {"equals": snapshot_date}}
    )

    properties = {
        "日期": {"date": {"start": snapshot_date}},
        "总市值": {"number": round(total_val, 2)},
        "总本金": {"number": round(total_cost, 2)},
        "累计总盈亏": {"number": round(total_profit, 2)},
        "当日盈亏": {"number": round(daily_profit, 2)},
        "加权年化收益率": {"number": weighted_annual_return}
    }

    if existing_rows:
        notion.pages.update(page_id=existing_rows[0]["id"], properties=properties)
    else:
        notion.pages.create(
            parent={"database_id": HISTORY_DB_ID},
            properties=properties
        )

def format_currency(amount, decimals=2, show_sign=False):
    sign = ""
    if show_sign:
        sign = "+" if amount >= 0 else "-"
    elif amount < 0:
        sign = "-"

    amount_text = f"{abs(amount):,.{decimals}f}" if decimals > 0 else f"{abs(amount):,.0f}"
    return f"{sign}¥{amount_text}"

def format_percent(value):
    return f"{value:+.2f}%"

def get_profit_color(value):
    if value > 0:
        return "green"
    if value < 0:
        return "red"
    return "default"

def get_drawdown_color(value):
    return "red" if value <= 0 else "red"

def update_text_block(block_id, text, color="default"):
    if not block_id:
        return

    try:
        block = notion.blocks.retrieve(block_id=block_id)
        block_type = block.get("type", "paragraph")
        payload = {
            block_type: {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": text},
                        "annotations": {
                            "bold": True,
                            "italic": False,
                            "strikethrough": False,
                            "underline": False,
                            "code": False,
                            "color": color,
                        },
                    }
                ]
            }
        }
        notion.blocks.update(block_id=block_id, **payload)
        log_success(f"更新仪表盘块成功 [{block_id}]: {text}")
    except Exception as e:
        log_warn(f"更新仪表盘块失败 [{block_id}]: {e}")

def update_dashboard_blocks(total_val, total_profit, daily_profit, total_cost, weighted_max_drawdown_pct, weighted_annual_return):
    profit_base = total_cost if total_cost > 0 else total_val
    cumulative_rate = total_profit / profit_base * 100 if profit_base > 0 else 0.0
    daily_rate = daily_profit / profit_base * 100 if profit_base > 0 else 0.0
    max_drawdown_amount = total_val * weighted_max_drawdown_pct / 100 if total_val > 0 else 0.0

    update_text_block(
        TOTAL_ASSET_BLOCK_ID,
        f"{format_currency(total_val, decimals=0)} 总资产",
        "blue",
    )
    update_text_block(
        CUMULATIVE_PROFIT_BLOCK_ID,
        f"{format_currency(total_profit, decimals=2, show_sign=True)} 累计盈亏（{format_percent(cumulative_rate)}）",
        get_profit_color(total_profit),
    )
    update_text_block(
        DAILY_PROFIT_BLOCK_ID,
        f"{format_currency(daily_profit, decimals=2, show_sign=True)} 今日盈亏（{format_percent(daily_rate)}）",
        get_profit_color(daily_profit),
    )
    update_text_block(
        MAX_DRAWDOWN_BLOCK_ID,
        f"{format_currency(max_drawdown_amount, decimals=0, show_sign=True)} 最大回撤（{format_percent(weighted_max_drawdown_pct)}）",
        get_drawdown_color(weighted_max_drawdown_pct),
    )
    update_text_block(
        ANNUAL_RETURN_BLOCK_ID,
        f"{format_percent(weighted_annual_return * 100)} 年化收益率",
        get_profit_color(weighted_annual_return),
    )

def update_notion():
    data_source_id = get_database_data_source_id(ASSETS_DB_ID)
    log_section("资产净值更新")
    
    assets = query_all_data_source_rows(data_source_id)
    
    for page in assets:
        props = page.get("properties", {})
        asset_name_prop = props.get("资产名称", {}).get("title", [])
        asset_name = asset_name_prop[0]["text"]["content"] if asset_name_prop else "未知资产"
        last_nav_update_date = get_property_date(page, "净值日期")
        current_nav_value = get_number_value(page, "当前净值")
        current_drawdown_value = get_number_value(page, "最大回撤")

        try:
            code_prop = props.get("产品代码", {}).get("rich_text", [])
            asset_type = props.get("资产分类", {}).get("select", {}).get("name", "")
            
            if not code_prop:
                continue
            fund_code = code_prop[0]["text"]["content"]
            
            if "price" in DEBUG_SKIP:
                log_debug(f"跳过 {asset_name} 的净值更新")
            else:
                if "基金" in asset_type:
                    # 只调用一次合并接口！
                    fund_info = get_fund_info(fund_code)
                    price = fund_info.get("price")
                    drawdown = fund_info.get("drawdown")
                    nav_date = fund_info.get("nav_date")
                    no_price_change = numbers_equal(price, current_nav_value)
                    no_drawdown_change = drawdown is None or numbers_equal(drawdown, current_drawdown_value)
                    same_nav_date = nav_date == last_nav_update_date

                    if same_nav_date and no_price_change and no_drawdown_change:
                        log_skip(f"基金 {fund_code} ({asset_name}) 净值/回撤未变化 | 净值日期: {nav_date or '无'}")
                    else:
                        update_properties = {}

                        if price is not None and not no_price_change:
                            update_properties["当前净值"] = {"number": price}

                        if drawdown is not None and not no_drawdown_change:
                            update_properties["最大回撤"] = {"number": drawdown}

                        if nav_date and nav_date != last_nav_update_date:
                            update_properties["净值日期"] = {"date": {"start": nav_date}}

                        if update_properties:
                            notion.pages.update(page_id=page["id"], properties=update_properties)
                            if "当前净值" in update_properties:
                                log_success(f"已更新基金 {fund_code} ({asset_name}) | 净值: {price}")
                            if "最大回撤" in update_properties:
                                log_success(f"已更新基金 {fund_code} ({asset_name}) 最大回撤: {round(drawdown * 100, 2)}%")

                elif "理财" in asset_type:
                    wealth_info = get_cmb_wealth_price(fund_code)
                    price = wealth_info.get("price")
                    nav_date = wealth_info.get("nav_date")
                    no_price_change = numbers_equal(price, current_nav_value)
                    same_nav_date = nav_date == last_nav_update_date

                    if price and same_nav_date and no_price_change:
                        log_skip(f"理财 {fund_code} ({asset_name}) 净值未变化 | 净值日期: {nav_date or '无'}")
                    elif price:
                        update_properties = {}
                        if not no_price_change:
                            update_properties["当前净值"] = {"number": price}
                        if nav_date and nav_date != last_nav_update_date:
                            update_properties["净值日期"] = {"date": {"start": nav_date}}
                        if update_properties:
                            notion.pages.update(
                                page_id=page["id"],
                                properties=update_properties
                            )
                            log_success(f"已更新理财 {fund_code} ({asset_name}) | 净值: {price} | 净值日期: {nav_date or '未返回'}")
                else:
                    log_info(f"[{asset_name}] 分类为 '{asset_type}'，未包含'基金'或'理财'，跳过净值更新")
        except Exception as e:
            log_warn(f"[{asset_name}] 更新失败，已跳过该资产: {e}")
        time.sleep(0.5)

    refreshed_assets = query_all_data_source_rows(data_source_id)
    process_pending_auto_invest_logs(refreshed_assets)

    log_section("持仓成本重算")
    assets_for_cost = query_all_data_source_rows(data_source_id)
    for page in assets_for_cost:
        props = page.get("properties", {})
        asset_name_prop = props.get("资产名称", {}).get("title", [])
        asset_name = asset_name_prop[0]["text"]["content"] if asset_name_prop else "未知资产"
        if "cost" in DEBUG_SKIP:
            log_debug(f"跳过 {asset_name} 的成本重算")
        else:
            update_average_cost(page["id"], asset_name)
        time.sleep(0.5)

    log_section("组合快照")
    log_wait("等待 Notion 重新计算总市值与盈亏...")
    time.sleep(5) 
    
    updated_assets = query_all_data_source_rows(data_source_id)
    total_val, total_cost, total_profit = 0.0, 0.0, 0.0
    weighted_annual_return = 0.0
    weighted_max_drawdown = 0.0

    for page in updated_assets:
        p = page.get("properties", {})
        current_val = p.get("当前市值", {}).get("formula", {}).get("number") or 0
        annual_return = p.get("年化收益率", {}).get("formula", {}).get("number") or 0
        max_drawdown = p.get("最大回撤", {}).get("number") or 0
        
        total_val += current_val
        total_cost += p.get("总买入金额", {}).get("formula", {}).get("number") or 0
        total_profit += p.get("累计总盈亏", {}).get("formula", {}).get("number") or 0
        
        weighted_annual_return += annual_return * current_val
        weighted_max_drawdown += abs(max_drawdown) * current_val

    weighted_annual_return = round(weighted_annual_return / total_val, 4) if total_val > 0 else 0.0
    weighted_max_drawdown_pct = round(-(weighted_max_drawdown / total_val) * 100, 2) if total_val > 0 else 0.0

    snapshot_date = time.strftime("%Y-%m-%d")
    yesterday_profit = get_yesterday_profit(today=datetime.datetime.strptime(snapshot_date, "%Y-%m-%d").date())
    daily_profit = total_profit - yesterday_profit

    upsert_history_snapshot(
        snapshot_date=snapshot_date,
        total_val=total_val,
        total_cost=total_cost,
        total_profit=total_profit,
        daily_profit=daily_profit,
        weighted_annual_return=weighted_annual_return,
    )
    update_dashboard_blocks(
        total_val,
        total_profit,
        daily_profit,
        total_cost,
        weighted_max_drawdown_pct,
        weighted_annual_return,
    )
    max_drawdown_amount = total_val * weighted_max_drawdown_pct / 100 if total_val > 0 else 0.0
    log_success(
        f"快照完成 | 总市值: {round(total_val, 2)} | 累计总盈亏: {round(total_profit, 2)} | 当日盈亏: {round(daily_profit, 2)} | 加权年化收益率: {round(weighted_annual_return * 100, 2)}% | 加权最大回撤: {round(max_drawdown_amount, 2)} ({weighted_max_drawdown_pct}%)"
    )

if __name__ == "__main__":
    # 运行模式：
    # morning -> 仅写入定投待确认日志
    # evening -> 处理待确认定投 + 净值/成本/快照全流程
    # all     -> morning + evening（本地手动联调）
    run_mode = (os.environ.get("RUN_MODE") or "all").strip().lower()

    log_section(f"任务启动 | 模式: {run_mode}")

    if run_mode == "morning":
        auto_record_investments()
    elif run_mode == "evening":
        update_notion()
    elif run_mode == "all":
        auto_record_investments()
        update_notion()
    else:
        log_warn(f"未知 RUN_MODE={run_mode}，按 all 执行")
        auto_record_investments()
        update_notion()
