import os
import logging
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, request, abort, Response
from wechatpy import parse_message, create_reply
from wechatpy.crypto import WeChatCrypto
from wechatpy.exceptions import InvalidSignatureException, InvalidAppIdException
from openai import AzureOpenAI
import pymysql
import requests
import threading

# 全局内存缓存去重集合（用于文本消息和客服事件消息去重）
processed_msg_ids = set()

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# 微信参数（请根据实际情况修改）
WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "FR7jRcTe8XuERyaDlAE8x1O")
WECHAT_AES_KEY = os.getenv("WECHAT_ENCODING_AES_KEY", "5Q23GoLfUradvWbxWoaNEXJ3OU1Q2js88THWoufo9M7")
WECHAT_APPID = os.getenv("WECHAT_APP_ID", "ww26ba103d4950e0cb")
WECHAT_CORPSECRET = os.getenv("WECHAT_CORPSECRET", "zUShp6BmRYb8O8o9INoTP0R2141RRh2Jdky5Q2R56A0")

# Azure OpenAI 配置（新版 SDK）
AZURE_API_KEY = os.getenv("AZURE_API_KEY_GPT4", "de7dd2fbb8404f08ad04ac22d515df87")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT_GPT4", "https://edgenesis-openai-sc-01.openai.azure.com/")
AZURE_API_VERSION = os.getenv("AZURE_API_VERSION_GPT4", "2024-08-01-preview")
AZURE_OPENAI_ENGINE = os.getenv("AZURE_DEPLOYMENT_GPT4", "gpt-4o")

client = AzureOpenAI(
    api_key=AZURE_API_KEY,
    api_version=AZURE_API_VERSION,
    azure_endpoint=AZURE_ENDPOINT
)

# 数据库配置（请根据实际情况修改）
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "wechat_user")
DB_PASS = os.getenv("DB_PASSWORD", "Hello@edgenesis123")
DB_NAME = os.getenv("DB_NAME", "wechat_service")
DB_PORT = int(os.getenv("DB_PORT", 3306))

app = Flask(__name__)

def send_kf_message(open_kf_id, external_userid, msgtype, content):
    try:
        access_token = get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={access_token}"
        payload = {
            "touser": external_userid,
            "open_kfid": open_kf_id,
            "msgtype": msgtype,
        }
        if msgtype == "text":
            payload["text"] = {"content": content}
        r = requests.post(url, json=payload, timeout=5)
        data = r.json()
        logging.debug(f"send_kf_message 返回: {data}")
        return data
    except Exception as e:
        logging.error(f"调用 send_kf_message 出错: {e}")
        return None

def get_access_token():
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={WECHAT_APPID}&corpsecret={WECHAT_CORPSECRET}"
    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        if data.get("errcode") == 0:
            token = data.get("access_token")
            logging.debug(f"获取 access_token 成功: {token}")
            return token
        else:
            raise Exception(f"获取 access_token 失败: {data.get('errmsg')}")
    except Exception as e:
        logging.error(f"获取 access_token 出错: {e}")
        raise

def get_customer_service_message(token, open_kf_id):
    try:
        access_token = get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/sync_msg?access_token={access_token}"
        payload = {"token": token, "open_kfid": open_kf_id}
        r = requests.post(url, json=payload, timeout=5)
        data = r.json()
        logging.debug(f"客服消息接口返回: {data}")
        if data.get("errcode") == 0 and data.get("msg_list"):
            sorted_messages = sorted(data["msg_list"], key=lambda m: m.get("send_time", 0), reverse=True)
            return sorted_messages[0]
        else:
            logging.error("客服消息接口返回空消息")
            return None
    except Exception as e:
        logging.error(f"获取客服消息失败: {e}")
        return None

def process_message(decrypted_xml):
    # 解析消息
    try:
        msg = parse_message(decrypted_xml)
    except Exception as e:
        logging.error(f"解析 XML 失败: {e}")
        return

    try:
        xml_root = ET.fromstring(decrypted_xml)
        msg_type = xml_root.findtext("MsgType")
        event = xml_root.findtext("Event")
        logging.debug(f"解析 XML 后提取: MsgType={msg_type}, Event={event}")
    except Exception as e:
        logging.error(f"提取消息类型失败: {e}")
        return

    # 初始化变量
    user_message = ""
    user_id = None       # 用于回复的用户 openid
    cs_info = None       # 客服事件消息内容
    unique_msg_id = None # 用于去重的唯一消息标识

    if msg_type == "text":
        msg_id = getattr(msg, "MsgId", None)
        if msg_id:
            if msg_id in processed_msg_ids:
                logging.debug("该文本消息已处理，跳过")
                return
            else:
                processed_msg_ids.add(msg_id)
        user_message = msg.content.strip()
        user_id = msg.source  # 用户 openid
    elif msg_type == "event" and event == "kf_msg_or_event":
        token_val = xml_root.findtext("Token")
        open_kf_id = xml_root.findtext("OpenKfId")
        logging.debug(f"收到客服事件消息，Token={token_val}, OpenKfId={open_kf_id}")
        cs_info = get_customer_service_message(token_val, open_kf_id)
        if cs_info:
            user_message = cs_info.get("text", {}).get("content", "")
            user_id = cs_info.get("external_userid", "unknown")
            send_time = cs_info.get("send_time", "")
            unique_msg_id = f"{user_id}-{send_time}"
            if unique_msg_id in processed_msg_ids:
                logging.debug("该客服事件消息已处理，跳过")
                return
            else:
                processed_msg_ids.add(unique_msg_id)
            logging.debug(f"客服消息文本: {user_message}, 用户ID: {user_id}")
        else:
            logging.error("未能获取客服消息")
            return
    else:
        logging.debug(f"非文本/客服事件消息类型: {msg_type}，跳过处理")
        return

    logging.debug(f"最终用户文本消息: {user_message}")

    # 保存用户消息到数据库
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASS,
            database=DB_NAME,
            port=DB_PORT,
            charset="utf8mb4"
        )
        cur = conn.cursor()
        final_user_id = user_id if user_id else (getattr(msg, "source", "unknown"))
        cur.execute(
            "INSERT INTO chat_records (`chat_time`, `user_id`, `message`) VALUES (%s, %s, %s)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), final_user_id, user_message)
        )
        conn.commit()
        cur.close()
        conn.close()
        logging.debug("聊天记录保存成功")
    except Exception as db_err:
        logging.error(f"数据库保存错误: {db_err}")

    # 调用 Azure OpenAI API生成回复
    reply_content = "抱歉，目前无法处理您的消息。"
    prompt_messages = [
    {
        "role": "system",
        "content": (
            "# 新文蓄电池AI客服执行协议\n\n"
            "## 核心指令\n"
            "你作为分类应答机器人，必须严格按照以下流程工作：\n"
            "1. 接收到用户输入文本后，根据内容判断用户的问题类型，用户问题一共分为五类，分别是订单，物流，产品，售后，技术\n"
            "2. 如果用户问题无法匹配到以上无类，则将其归为“其他”类。\n"
            "3. 每个回复应包含两个部分：\n"
            "   - 固定的模板回复部分：这部分内容会根据问题类型固定生成。\n"
            "   - 基于知识库的智能生成回复：根据问题的具体内容，自动从知识库提取信息生成智能回复。\n\n"
            "## 固定模板回复部分\n"
            "json{\n"
            '  "订单问题": {"emoji": "📦", "响应模板": "订单特工组已接收您的指令！核查专员正在调取全链路数据，保证在太阳下山前通过专属通道给您明确答复！🌱", "触发关键词": ["订单号", "发货时间", "付款异常", "修改订单"]},\n'
            '  "物流问题": {"emoji": "🚛", "响应模板": "物流守护者已上线！运输情况已由特勤组接手，最迟24小时内推送最新进展路线图~ 💨", "触发关键词": ["物流延迟", "单号查询", "货物破损", "路线变更"]},\n'
            '  "产品问题": {"emoji": "🔋", "响应模板": "技术中心已启动参数解析！高级工程师团队正在准备适配方案，预计24小时内同步最新进展。", "触发关键词": ["参数错误", "性能问题", "安装指导", "规格不符"]},\n'
            '  "售后问题": {"emoji": "⚓", "响应模板": "售后护航舰已启航！区域服务总监将亲自带队处理，3个工作日内为您解决问题航标已点亮！", "触发关键词": ["退换货", "维修", "服务投诉", "质保查询"]},\n'
            '  "技术支持": {"emoji": "🛠️", "响应模板": "技术保障队进入战斗位置！系统架构师正在准备调试方案，请保持手机畅通，专属技术员即将致电！", "触发关键词": ["系统故障", "技术调试", "方案定制", "代码错误"]},\n'
            '  "其他问题": {"emoji": "📌", "响应模板": "客户护航小组建立专属通道！跨部门联席负责人正在集结，今天下午茶时间前反馈路线图！", "触发关键词": ["发票问题", "合同条款", "合作咨询", "紧急事件"]}\n'
            "}\n\n"
            
            "## 知识库（供参考）\n\n"
            "产品信息:\n"
            "- DX12：12V 100Ah 密封铅酸蓄电池，支持深循环充放，适用于电动车辆、电力储能及 UPS 后备电源。\n"
            "- GP45：12V 45Ah 通用型蓄电池，采用 AGM 技术，适用于中小型 UPS、电信设备及安防备用电源。\n"
            "- AT70：12V 70Ah 污染汽车启动电池，适配轿车和SUV。\n"
            "- LFP48-100：48V 100Ah 磷酸铁锂电池模组，循环寿命长，适用于家庭储能系统、电信基站等对长寿命电源有需求的场景。\n\n"
            
            "售后政策:\n"
            "- 质保期：所有蓄电池产品自购买日起享有 12 个月质保服务。\n"
            "- 退换货：产品到货 7 日内若出现非人为故障，可申请退换货；超过 7 日则按质保流程提供维修或更换服务。\n"
            "- 保修范围：质保涵盖正常使用下的性能故障，不包括人为损坏、进水等非产品质量原因的问题。\n"
            "- 服务流程：质保期内如需保修服务，请联系 400 客服提供购买凭证和问题描述，经工程师确认后安排维修或换新。\n\n"
            
            "常见问题 FAQ:\n"
            "- 问：电池到货一般多久？ 答：正常情况下发货后 3～7 天内可送达，偏远地区可能需要更长时间。\n"
            "- 问：如何查询订单物流？ 答：每笔订单发货后都会提供运单号，可在公司官网或承运方官网查询物流状态。\n"
            "- 问：如何申请退换货？ 答：收到货物 7 天内如发现质量问题，可联系 400 客服申请退换货；超过 7 天则在质保期内享受维修或更换服务。\n"
            "- 问：蓄电池质保多久？ 答：新文蓄电池产品质保期为 12 个月，质保期内出现故障可享受免费维修或更换服务。\n\n"
            
            "经销政策:\n"
            "- 返点政策：经销商按当月采购额享受阶梯返点，采购额越高返利比例越高（约 3%～10%）。返利金额通常在次月初结算返还。\n"
            "- 采购门槛：成为正式经销商需完成首批不低于￥5 万元的进货额；长期合作中每年度采购额需达标以维持经销授权资格。\n"
            "- 月度结算：公司与经销商采用月度对账结算，每月初核对上月订单和返利金额，并在核对无误后 7 个工作日内发放返利。\n"
            "- 账期政策：新经销商原则上款到发货；合作满 6 个月且信用良好后，可申请 30 天左右的月结账期。\n\n"
            
            "技术支持:\n"
            "- 故障判断流程：首先检查蓄电池外观和接线是否正常，然后测试电池电压，尝试重新充电；若仍无法正常工作，请联系技术支持处理。\n"
            "- 蓄电池无法充电：建议先检查充电器输出和电池连接是否良好；如果确认无误但仍无法充电，请联系技术支持协助排查处理。\n"
            "- 蓄电池放电时间短：请确保电池已完全充电，并检查电池是否因长期闲置导致容量下降；若完全充电后放电依旧不足，请寻求技术支持进一步诊断。\n"
            "- 技术服务联系方式：400-XXX-XXX（7×24 小时热线），或发送邮件至 support@xinwenbattery.com 获取技术支持。\n\n"
            
            "订单物流:\n"
            "- 发货周期：订单确认后 1～3 个工作日内发货；小批量现货一般在 24 小时内发出，大批量订单可能需要约 1 周备货时间。\n"
            "- 快递公司：默认使用顺丰速运、德邦物流等快递服务，大件或特殊订单将安排专线物流公司配送。\n"
            "- 物流追踪：每次发货后提供运单号，可在公司官网的“订单查询”页面或快递公司官网输入运单号查询配送进度。\n"
            "- 运费政策：单笔订单满 5000 元免运费；未满 5000 元的订单运费根据距离和重量计算，由经销商承担。\n\n"
            
            "## 格式规范\n"
            "生成的回复必须严格符合如下格式：\n"
            "{emoji} {响应模板} {基于知识库的智能生成回复}\n\n"
            "对于电池到货、退换货等问题，AI 会根据常见问题FAQ生成详细答案。"
        )
    },
    {
        "role": "user",
        "content": "{user_message}"  # 用户的具体反馈信息将会替换这里
    }
]

    logging.debug("调用 Azure OpenAI API，发送 prompt:")
    logging.debug(prompt_messages)

    try:
        response = client.chat.completions.create(
            model=AZURE_OPENAI_ENGINE,
            messages=prompt_messages,
            max_tokens=150,
            temperature=0.7
        )
        gpt_reply = response.choices[0].message.content.strip()
        logging.debug(f"Azure OpenAI 返回: {gpt_reply}")
        reply_content = gpt_reply
        if reply_content.startswith("【") and "\n" in reply_content:
            reply_content = reply_content.split("\n", 1)[1].strip()
    except Exception as e:
        logging.error(f"调用 Azure OpenAI API 出错: {e}")
        reply_content = "抱歉，暂时无法获取回复，请稍后再试。"

    # 如果是客服事件消息，则下发回复
    if msg_type == "event" and event == "kf_msg_or_event":
        open_kf_id = xml_root.findtext("OpenKfId")
        if not cs_info:
            logging.error("客服事件消息中缺少 cs_info")
            return
        external_userid = cs_info.get("external_userid")
        if not external_userid:
            logging.error("客服消息中未返回 external_userid")
            return
        send_result = send_kf_message(open_kf_id, external_userid, "text", reply_content)
        logging.debug(f"send_kf_message 返回: {send_result}")
    # 对于文本消息，这里仅保存记录，不主动下发回复

    logging.debug("消息处理完成")
    return

@app.route("/work", methods=["GET", "POST"])
def work_route():
    logging.debug("收到企业微信请求 (路径 /work)")
    
    # GET 请求：验证 URL 有效性
    if request.method == "GET":
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        echo_str = request.args.get("echostr", "")
        msg_signature = request.args.get("msg_signature", "")
        encrypt_type = request.args.get("encrypt_type", "raw")
        
        logging.debug(f"GET参数: timestamp={timestamp}, nonce={nonce}, encrypt_type={encrypt_type}, echostr={echo_str}, msg_signature={msg_signature}")
        
        if echo_str:
            try:
                arr = [WECHAT_TOKEN, timestamp, nonce, echo_str]
                arr.sort()
                computed = hashlib.sha1("".join(arr).encode("utf-8")).hexdigest()
                logging.debug(f"计算得到的签名: {computed}")
                if computed != msg_signature:
                    logging.error("签名校验失败")
                    abort(403)
            except Exception as e:
                logging.error(f"签名验证出错: {e}")
                abort(403)
            try:
                crypto = WeChatCrypto(WECHAT_TOKEN, WECHAT_AES_KEY, WECHAT_APPID)
                decrypted_echo = crypto.decrypt_message(echo_str, msg_signature, timestamp, nonce)
                logging.debug(f"GET 请求，解密后的 echostr: {decrypted_echo}")
                return Response(decrypted_echo, mimetype='text/xml')
            except Exception as e:
                logging.error(f"解密 echostr 失败: {e}")
                abort(403)
        else:
            return Response("success", mimetype="text/plain")
    
    # POST 请求：快速响应后异步处理
    try:
        raw_data = request.data.decode("utf-8")
        logging.debug(f"POST 请求原始数据: {raw_data}")
        root = ET.fromstring(raw_data)
        crypto = WeChatCrypto(WECHAT_TOKEN, WECHAT_AES_KEY, WECHAT_APPID)
        if root.find("Encrypt") is not None:
            decrypted_xml = crypto.decrypt_message(raw_data,
                                                    request.args.get("msg_signature", ""),
                                                    request.args.get("timestamp", ""),
                                                    request.args.get("nonce", ""))
            logging.debug(f"解密后的 XML: {decrypted_xml}")
        else:
            decrypted_xml = raw_data
            logging.debug("消息未加密，直接使用原始数据")
    except Exception as e:
        logging.error(f"处理 POST 数据失败: {e}")
        abort(403)

    # 异步处理消息，立即返回 "success" 避免重传
    threading.Thread(target=process_message, args=(decrypted_xml,)).start()
    return Response("success", mimetype="text/plain")

if __name__ == "__main__":
    logging.debug("启动 Flask 应用 (监听 80 端口)")
    app.run(host="0.0.0.0", port=80, debug=True)
