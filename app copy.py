import os
import logging
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, request, abort, Response
from wechatpy import parse_message, create_reply
from wechatpy.crypto import WeChatCrypto
from wechatpy.exceptions import InvalidSignatureException, InvalidAppIdException
import openai
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

# Azure OpenAI 配置（请根据实际情况修改）
openai.api_type = "azure"
openai.api_base = os.getenv("AZURE_ENDPOINT_GPT4", "https://edgenesis-openai-sc-01.openai.azure.com/")
openai.api_version = os.getenv("AZURE_API_VERSION_GPT4", "2024-08-01-preview")
openai.api_key = os.getenv("AZURE_API_KEY_GPT4", "de7dd2fbb8404f08ad04ac22d515df87")
AZURE_OPENAI_ENGINE = os.getenv("AZURE_DEPLOYMENT_GPT4", "gpt-4o")

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
            "1. 接收到用户输入文本后，根据内容匹配对应分类；\n"
            "2. 使用下列模板生成回复，但仅生成回复内容，不输出任何分类名称或其他前缀。\n\n"
            "## 分类-模板绑定表\n"
            "json{\n"
            '  "订单问题": {"emoji": "📦", "响应模板": "订单特工组已接收您的指令！核查专员正在调取全链路数据，保证在太阳下山前通过专属通道给您明确答复！🌱", "触发关键词": ["订单号", "发货时间", "付款异常", "修改订单"]},\n'
            '  "物流问题": {"emoji": "🚛", "响应模板": "物流守护者已上线！运输情况已由特勤组接手，最迟24小时内推送最新进展路线图~ 💨", "触发关键词": ["物流延迟", "单号查询", "货物破损", "路线变更"]},\n'
            '  "产品问题": {"emoji": "🔋", "响应模板": "技术中心已启动参数解析！高级工程师团队正在准备适配方案，预计24小时内同步最新进展。", "触发关键词": ["参数错误", "性能问题", "安装指导", "规格不符"]},\n'
            '  "售后问题": {"emoji": "⚓", "响应模板": "售后护航舰已启航！区域服务总监将亲自带队处理，3个工作日内为您解决问题航标已点亮！", "触发关键词": ["退换货", "维修", "服务投诉", "质保查询"]},\n'
            '  "技术支持": {"emoji": "🛠️", "响应模板": "技术保障队进入战斗位置！系统架构师正在准备调试方案，请保持手机畅通，专属技术员即将致电！", "触发关键词": ["系统故障", "技术调试", "方案定制", "代码错误"]},\n'
            '  "其他问题": {"emoji": "📌", "响应模板": "客户护航小组建立专属通道！跨部门联席负责人正在集结，今天下午茶时间前反馈路线图！", "触发关键词": ["发票问题", "合同条款", "合作咨询", "紧急事件"]}\n'
            "}\n\n"
            "## 格式规范\n"
            "生成的回复必须严格符合如下格式：\n"
            "{ {emoji} } { {响应模板} }\n\n"
            "请注意：只输出模板内容部分，不要输出任何分类名称或其他额外文本。"
            )
        },
        {"role": "user", "content": user_message}
    ]
    logging.debug("调用 Azure OpenAI API，发送 prompt:")
    logging.debug(prompt_messages)

    try:
        response = openai.ChatCompletion.create(
            engine=AZURE_OPENAI_ENGINE,
            messages=prompt_messages,
            max_tokens=150,
            temperature=0.7
        )
        gpt_reply = response.choices[0].message["content"].strip()
        logging.debug(f"Azure OpenAI 返回: {gpt_reply}")
        # 后处理：去掉返回回复中首行的类别前缀
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
