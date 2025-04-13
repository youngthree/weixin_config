import os
import logging
from datetime import datetime
from flask import Flask, request, abort
from wechatpy import parse_message, create_reply
from wechatpy.crypto import WeChatCrypto
from wechatpy.utils import check_signature
from wechatpy.exceptions import InvalidSignatureException, InvalidAppIdException
import openai
import pymysql

# 配置日志，输出到终端，同时写入文件（可选）
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
        # 如果需要写入文件，取消下面一行的注释
        # logging.FileHandler("app.log", encoding="utf-8")
    ]
)

# 配置微信参数（请根据实际情况填写或通过环境变量传入）
WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "FR7jRcTe8XuERyaDlAE8x1O")
WECHAT_AES_KEY = os.getenv("WECHAT_ENCODING_AES_KEY", "5Q23GoLfUradvWbxWoaNEXJ3OU1Q2js88THWoufo9M7")
WECHAT_APPID = os.getenv("WECHAT_APP_ID", "ww26ba103d4950e0cb")

# Azure OpenAI 配置（请根据实际情况填写）
openai.api_type = "azure"
openai.api_base = os.getenv("AZURE_ENDPOINT_GPT4", "https://edgenesis-openai-sc-01.openai.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2024-08-01-preview")
openai.api_version = os.getenv("AZURE_API_VERSION_GPT4", "2024-08-01-preview")
openai.api_key = os.getenv("AZURE_API_KEY_GPT4", "de7dd2fbb8404f08ad04ac22d515df87")
AZURE_OPENAI_ENGINE = os.getenv("AZURE_DEPLOYMENT_GPT4", "gpt-4o")

# 数据库配置（请根据实际情况填写）
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASSWORD", "hello!edgenesis")
DB_NAME = os.getenv("DB_NAME", "wechat_data")
DB_PORT = int(os.getenv("DB_PORT", 13306))

app = Flask(__name__)

@app.route("/work", methods=["GET", "POST"])
def work():
    logging.debug("收到企业微信请求 (路径 /work)")
    
    # 1. 获取 URL 参数
    signature = request.args.get("signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    echo_str = request.args.get("echostr", "")
    msg_signature = request.args.get("msg_signature", "")
    encrypt_type = request.args.get("encrypt_type", "raw")
    
    logging.debug(f"signature={signature}, timestamp={timestamp}, nonce={nonce}, encrypt_type={encrypt_type}, echostr={echo_str}")

    # 2. 签名校验（POST 和 GET 均适用）
    try:
        check_signature(WECHAT_TOKEN, signature, timestamp, nonce)
        logging.debug("签名校验通过")
    except InvalidSignatureException as e:
        logging.error(f"签名校验失败: {e}")
        abort(403)
    
    # 创建 WeChatCrypto 对象，用于解密消息或 echostr
    crypto = WeChatCrypto(WECHAT_TOKEN, WECHAT_AES_KEY, WECHAT_APPID)

    # 3. GET 请求：企业微信 URL 验证
    if request.method == "GET":
        try:
            decrypted_echo = crypto.decrypt_message(echo_str, msg_signature, timestamp, nonce)
            logging.debug(f"GET 请求，解密后的 echostr: {decrypted_echo}")
            return decrypted_echo
        except Exception as e:
            logging.error(f"解密 echostr 失败: {e}")
            abort(403)

    # 4. POST 请求：接收企业微信推送的消息
    try:
        raw_data = request.data.decode("utf-8")
        logging.debug(f"POST 请求原始数据: {raw_data}")
        if encrypt_type == "raw":
            decrypted_xml = raw_data
            logging.debug("消息未加密，直接使用原始数据")
        else:
            decrypted_xml = crypto.decrypt_message(raw_data, msg_signature, timestamp, nonce)
            logging.debug(f"解密后的 XML: {decrypted_xml}")
    except Exception as e:
        logging.error(f"处理 POST 数据失败: {e}")
        abort(403)

    try:
        msg = parse_message(decrypted_xml)
        logging.debug(f"解析后的消息: type={msg.type}, content={getattr(msg, 'content', '')}")
    except Exception as e:
        logging.error(f"解析 XML 失败: {e}")
        abort(400)

    user_message = ""
    reply_content = "抱歉，目前无法处理您的消息。"

    if msg.type == "text":
        user_message = msg.content.strip()
        logging.debug(f"用户文本消息: {user_message}")

        # 调用 Azure OpenAI API，对用户消息进行分类，并返回预定义的回复模板
        prompt_messages = [
            {
                "role": "system",
                "content": (
                    "# 新文蓄电池AI客服执行协议\n\n"
                    "## 核心指令\n"
                    "你作为分类应答机器人，必须严格按以下流程工作：\n"
                    "1. 输入文本→匹配分类→调用对应模板→禁止任何改编\n\n"
                    "## 分类-模板绑定表\n"
                    "json{\n"
                    '  "订单问题": {"emoji": "📦", "响应模板": "销售客服部已接收您的指令！核查专员正在调取全链路数据，保证在太阳下山前通过专属通道给您明确答复！🌱", "触发关键词": ["订单号", "发货时间", "付款异常", "修改订单"]},\n'
                    '  "物流问题": {"emoji": "🚛", "响应模板": "物流守护者已上线！运输情况已由特勤组接手，最迟24小时内推送最新进展路线图~ 💨", "触发关键词": ["物流延迟", "单号查询", "货物破损", "路线变更"]},\n'
                    '  "产品问题": {"emoji": "🔋", "响应模板": "技术中心已启动参数解析！高级工程师团队正在准备适配方案，预计24小时内同步最新进展。", "触发关键词": ["参数错误", "性能问题", "安装指导", "规格不符"]},\n'
                    '  "售后问题": {"emoji": "⚓", "响应模板": "售后护航舰已启航！区域服务总监将亲自带队处理，3个工作日内为您解决问题航标已点亮！", "触发关键词": ["退换货", "维修", "服务投诉", "质保查询"]},\n'
                    '  "技术支持": {"emoji": "🛠️", "响应模板": "技术保障队进入战斗位置！系统架构师正在准备调试方案，请保持手机畅通，专属技术员即将致电！", "触发关键词": ["系统故障", "技术调试", "方案定制", "代码错误"]},\n'
                    '  "其他问题": {"emoji": "📌", "响应模板": "客户护航小组建立专属通道！跨部门联席负责人正在集结，今天下午茶时间前反馈路线图！", "触发关键词": ["发票问题", "合同条款", "合作咨询", "紧急事件"]}\n'
                    "}\n\n"
                    "## 格式规范\n"
                    "回复格式必须为:\n"
                    'response = f"【{ {category} }】\\n{ {emoji} } { {响应模板} }"\n\n'
                    "请严格按照上述要求生成回复，不允许修改模板内容。"
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
            reply_content = gpt_reply
        except Exception as e:
            logging.error(f"调用 Azure OpenAI API 出错: {e}")
            reply_content = "抱歉，暂时无法获取回复，请稍后再试。"
    else:
        logging.debug(f"非文本消息类型: {msg.type}")
        reply_content = "抱歉，目前仅支持文本消息。"

    # 将收到的用户消息保存到 MySQL 数据库中
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
        cur.execute(
            "INSERT INTO chat_records (`chat_time`, `user_id`, `message`) VALUES (%s, %s, %s)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg.source, user_message)
        )
        conn.commit()
        cur.close()
        conn.close()
        logging.debug("聊天记录保存成功")
    except Exception as db_err:
        logging.error(f"数据库保存错误: {db_err}")

    # 构造回复消息并加密返回
    reply = create_reply(reply_content, msg)
    try:
        encrypted_reply = crypto.encrypt_message(reply.render(), nonce, timestamp)
        logging.debug("构造并加密回复成功")
    except Exception as e:
        logging.error(f"加密回复失败: {e}")
        encrypted_reply = reply.render()
    return encrypted_reply

if __name__ == "__main__":
    logging.debug("启动 Flask 应用 (监听 80 端口)")
    app.run(host="0.0.0.0", port=80, debug=True)
