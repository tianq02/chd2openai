import base64
import os
import json
import time
import uuid
import requests
from flask import Flask, request, Response, jsonify, stream_with_context
import logging
from urllib.parse import urlparse, parse_qs

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 全局配置
SCHOOL_BASE_URL = "https://agi.chd.edu.cn"
SCHOOL_API_URL = f"{SCHOOL_BASE_URL}/chat/api/chat-messages"
SCHOOL_DELETE_URL = f"{SCHOOL_BASE_URL}/chat/api/conversations"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0"
AUTO_DELETE_CONVERSATIONS = os.getenv("AUTO_DELETE_CONVERSATIONS", "true").lower() == "true"


# 认证信息管理器
class AuthManager:
    def __init__(self):
        self.config_url = None
        self.user_token = None
        self.app_id = None
        self.uid = None
        self.model = None # 模型由CONFIG_URL决定
        self.cookies = {}
        self.headers = {}

    def initialize_from_url(self, config_url):
        """从配置URL初始化所有认证信息"""
        self.config_url = config_url

        # 解析URL获取userToken和appId
        parsed = urlparse(config_url)
        query_params = parse_qs(parsed.query)

        self.user_token = query_params.get("userToken", [None])[0]
        self.app_id = query_params.get("appId", [None])[0]

        if not self.user_token or not self.app_id:
            raise ValueError("Invalid config URL - missing userToken or appId")

        # 获取用户信息(uid)
        self._fetch_user_info()

        # 获取应用配置cookie
        self._fetch_app_config()

        # 构建通用请求头
        self.headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "DNT": "1",
            "Origin": SCHOOL_BASE_URL,
            "Pragma": "no-cache",
            "Referer": self.config_url,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": USER_AGENT,
            "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Microsoft Edge";v="138"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "x-ai-portal-token": self.user_token,
            "x-ai-portal-uid": self.uid
        }

    def _fetch_user_info(self):
        """获取用户UID"""
        url = f"{SCHOOL_BASE_URL}/chat/api/ai-portal/user-info"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.config_url,
            "User-Agent": USER_AGENT,
            "x-ai-portal-token": self.user_token
        }

        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            raise ValueError(f"Failed to fetch user info: {response.status_code}")

        data = response.json()
        self.uid = data.get("uid")
        if not self.uid:
            raise ValueError("No UID found in user info response")
        # pure nonsense, i don't know why, but it seems session_id always matches uid
        #self.cookies["session_id"] = self.uid

    def _fetch_app_config(self):
        """获取应用配置cookie"""
        url = f"{SCHOOL_BASE_URL}/chat/api/ai-portal/app-info?appId={self.app_id}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.config_url,
            "User-Agent": USER_AGENT,
            "x-ai-portal-token": self.user_token,
            "x-ai-portal-uid": self.uid
        }

        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            raise ValueError(f"Failed to fetch app config: {response.status_code}")

        if 'set-cookie' in response.headers:
            cookie_strings = response.headers['set-cookie'].split(', ')
            for cookie_str in cookie_strings:
                # 分割每个cookie字符串，取第一个等号前的部分作为key，之后的部分作为value
                parts = cookie_str.split(';')[0].split('=', 1)
                if len(parts) == 2:
                    key, value = parts
                    self.cookies[key] = value
        try:
            self.model = response.json()['appName']
        except:
            logger.warning("Failed to fetch app model, CONFIG_URL may have expired")
            pass

    def get_cookie_header(self):
        """获取完整的Cookie头部值"""
        return "; ".join(f"{k}={v.split('=')[1]}" for k, v in self.cookies.items())

    def get_headers(self, content_type=None):
        """获取请求头，可选的content_type"""
        headers = self.headers.copy()
        if content_type:
            headers["content-type"] = content_type
        return headers


# 初始化认证管理器
auth_manager = AuthManager()


# 会话管理器
class SessionManager:
    def __init__(self):
        self.sessions = {}

    def get_conversation_id(self, session_id):
        return self.sessions.get(session_id)

    def update_session(self, session_id, conversation_id):
        self.sessions[session_id] = conversation_id


session_manager = SessionManager()


def convert_to_school_api(payload):
    """将OpenAI格式的请求转换为学校API格式"""
    messages = payload.get("messages", [])

    # 提取最后一条用户消息
    user_message = ""
    for msg in reversed(messages):
        if msg["role"] == "user":
            user_message = msg["content"]
            break

    # 从自定义头部获取会话ID（如果存在）
    session_id = request.headers.get("X-Session-Id", str(uuid.uuid4()))
    conversation_id = session_manager.get_conversation_id(session_id)

    return {
        "inputs": {"web_search": ""},
        "query": user_message,
        "conversation_id": conversation_id,
        "response_mode": "streaming"
    }, session_id


def convert_to_openai_chunk(data, message_id, created):
    """将学校API的事件转换为OpenAI格式的流式块"""
    try:
        event_data = json.loads(data[6:])  # 去掉"data: "前缀

        if event_data.get("event") == "message":
            return {
                "id": message_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": auth_manager.model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": event_data.get("answer", "")},
                    "finish_reason": None
                }]
            }

        elif event_data.get("event") == "workflow_finished":
            return {
                "id": message_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": auth_manager.model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }]
            }
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse event data: {data}")

    return None


def convert_to_openai_complete(response_data, message_id, created):
    """将完整的学校API响应转换为OpenAI格式"""
    content = ""
    for line in response_data.splitlines():
        if line.startswith("data:") and len(line) > 15:  # 过滤ping事件
            try:
                event_data = json.loads(line[6:])
                if event_data.get("event") == "message":
                    content += event_data.get("answer", "")
            except json.JSONDecodeError:
                continue

    return {
        "id": message_id,
        "object": "chat.completion",
        "created": created,
        "model": auth_manager.model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content
            },
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }
    }


def delete_conversation(conversation_id):
    """删除指定的对话"""
    if not conversation_id or not AUTO_DELETE_CONVERSATIONS:
        return False

    try:
        headers = auth_manager.get_headers()
        headers["Accept"] = "application/json, text/plain, */*"

        response = requests.delete(
            f"{SCHOOL_DELETE_URL}/{conversation_id}",
            headers=headers,
            cookies=auth_manager.cookies
        )

        if response.status_code == 200:
            logger.info(f"Successfully deleted conversation: {conversation_id}")
            return True
        else:
            logger.error(f"Failed to delete conversation {conversation_id}: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error deleting conversation {conversation_id}: {str(e)}")
        return False

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    """OpenAI兼容API端点"""
    # 解析请求
    payload = request.json
    stream = payload.get("stream", False)

    # 转换为学校API格式
    school_payload, session_id = convert_to_school_api(payload)
    created = int(time.time())

    # 准备学校API请求
    headers = auth_manager.get_headers("application/json")
    cookies = auth_manager.cookies

    # 发送请求到学校API
    response = requests.post(
        SCHOOL_API_URL,
        headers=headers,
        cookies=cookies,
        json=school_payload,
        stream=stream
    )

    # 检查响应状态
    if response.status_code != 200:
        return jsonify({
            "error": f"School API returned {response.status_code}",
            "details": response.text
        }), 500

    # 获取消息ID用于响应
    message_id = str(uuid.uuid4())
    conversation_id = None

    # 流式响应处理
    if stream:
        def generate():
            nonlocal conversation_id
            try:
                for line in response.iter_lines():
                    if line and len(line) > 15:  # 过滤ping事件
                        decoded_line = line.decode('utf-8')

                        # 检测并更新会话ID
                        if "conversation_id" in decoded_line:
                            try:
                                event_data = json.loads(decoded_line[6:])
                                if "conversation_id" in event_data:
                                    conversation_id = event_data["conversation_id"]
                                    session_manager.update_session(session_id, conversation_id)
                            except:
                                pass

                        # 转换为OpenAI格式
                        chunk = convert_to_openai_chunk(decoded_line, message_id, created)
                        if chunk:
                            yield f"data: {json.dumps(chunk)}\n\n"
            finally:
                # 流式结束后删除对话
                delete_conversation(conversation_id)

            # 流式结束标记
            yield "data: [DONE]\n\n"

        return Response(stream_with_context(generate()), mimetype='text/event-stream')

    # 非流式响应处理
    else:
        content = ""
        try:
            for line in response.iter_lines():
                if line and len(line) > 15:  # 过滤ping事件
                    decoded_line = line.decode('utf-8')

                    # 检测并更新会话ID
                    if "conversation_id" in decoded_line:
                        try:
                            event_data = json.loads(decoded_line[6:])
                            if "conversation_id" in event_data:
                                conversation_id = event_data["conversation_id"]
                                session_manager.update_session(session_id, conversation_id)
                        except:
                            pass

                    # 提取消息内容
                    if decoded_line.startswith("data:") and '"event": "message"' in decoded_line:
                        try:
                            event_data = json.loads(decoded_line[6:])
                            content += event_data.get("answer", "")
                        except json.JSONDecodeError:
                            continue
        finally:
            # 非流式结束后删除对话
            delete_conversation(conversation_id)

        return jsonify({
            "id": message_id,
            "object": "chat.completion",
            "created": created,
            "model": auth_manager.model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        })


if __name__ == '__main__':
    # 检查配置URL
    config_url = os.getenv("CONFIG_URL")
    
    if config_url:
        logger.info(f"CONFIG_URL(env):   {config_url}")
    else:
        print("cannot find CONFIG_URL in environment variable")
        print("get one here: https://agi.chd.edu.cn")
        config_url = input("CONFIG_URL:")
        
    if not config_url:
        raise ValueError("CONFIG_URL is required")

    logger.info("Authentication initializing")
    auth_manager.initialize_from_url(config_url)
    try:
        logger.info(f"appName(model):    {auth_manager.model}")
        logger.info(f"dify_app_id:       {auth_manager.cookies['dify_app_id']}")
        logger.info(f"dify_app_config:   {auth_manager.cookies['dify_app_config']}")
        logger.info(f"dify_app_config:   {base64.b64decode(auth_manager.cookies['dify_app_config']).decode('utf-8')}")
        logger.info(f"x-ai-portal-token: {auth_manager.user_token}")
        logger.info(f"x-ai-portal-uid:   {auth_manager.uid}")
        logger.info("Authentication initialized successfully")
    except Exception as e:
        logger.error(f"Authentication done, but failed with {e}")
        exit(1)

    # auth api would be called twice if debug=True
    app.run(host='0.0.0.0', port=5000, debug=False)
