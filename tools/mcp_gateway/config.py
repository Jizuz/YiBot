"""MCP 网关配置：集中管理 Nacos 订阅 / MCP 服务 / 网关行为的全部环境变量（技改 §4.4 工具层）。"""
import os

from dotenv import load_dotenv

load_dotenv()

# ==================== Nacos 订阅 ====================
NACOS_SERVER_ADDR = os.getenv("NACOS_SERVER_ADDR", "127.0.0.1:8848")
NACOS_USERNAME = os.getenv("NACOS_USERNAME", "").strip()
NACOS_PASSWORD = os.getenv("NACOS_PASSWORD", "").strip()
NACOS_NAMESPACE = os.getenv("NACOS_NAMESPACE", "").strip()
NACOS_GROUP = os.getenv("NACOS_GROUP", "DEFAULT_GROUP").strip()
# Nacos accessToken 缓存秒数(开启鉴权时避免每次轮询都登录)
NACOS_TOKEN_TTL = float(os.getenv("NACOS_TOKEN_TTL", "300"))

# ==================== MCP 服务 ====================
MCP_SERVICE_NAME = os.getenv("MCP_SERVICE_NAME", "spring-ai-mcp-server")
MCP_SSE_PATH = os.getenv("MCP_SSE_PATH", "/sse")
# 连接 / 调用超时(秒)
MCP_TIMEOUT = float(os.getenv("MCP_TIMEOUT", "30"))

# ==================== 网关行为 ====================
# 订阅轮询间隔(秒)，实际执行时附加 ±20% 抖动避免惊群
MCP_SUBSCRIBE_INTERVAL = float(os.getenv("MCP_SUBSCRIBE_INTERVAL", "10"))
# 负载均衡策略: round_robin(默认) / random / least_conn
MCP_LB_POLICY = os.getenv("MCP_LB_POLICY", "round_robin").strip().lower()
# 连续失败多少次熔断该实例
MCP_CIRCUIT_FAIL_THRESHOLD = int(os.getenv("MCP_CIRCUIT_FAIL_THRESHOLD", "3"))
# 熔断冷却时间(秒)，到期转半开允许探测
MCP_CIRCUIT_COOLDOWN = float(os.getenv("MCP_CIRCUIT_COOLDOWN", "30"))
# callTool 失败后换实例重试次数(额外尝试次数)
MCP_CALL_RETRIES = int(os.getenv("MCP_CALL_RETRIES", "2"))
# 工具缓存 TTL(秒)，实例集合变化时立即失效
MCP_TOOL_CACHE_TTL = float(os.getenv("MCP_TOOL_CACHE_TTL", "300"))

VALID_LB_POLICIES = ("round_robin", "random", "least_conn")
if MCP_LB_POLICY not in VALID_LB_POLICIES:
    # 非法配置回退默认策略而非启动崩溃
    MCP_LB_POLICY = "round_robin"

# ==================== MCP Server 暴露的工具名(按当前 SpringAI MCP Server listTools 实际返回配置) ====================
TOOL_GET_WEATHER = "queryWeather"            # 查天气,参数: city
TOOL_GET_RIGHT_LIST = "get_right_list"       # 查用户权益,参数: userId(int)
TOOL_GET_NEARBY_STORES = "get_nearby_stores"  # 查附近门店,参数: location
TOOL_SEARCH_KNOWLEDGE_BASE = "search_knowledge_base"  # 健康科普知识库检索

# search_knowledge_base 的工具描述(与 MCP Server 端注册保持一致)
SEARCH_KB_TOOL_DESC = (
    "健康科普知识库检索工具（向量+BM25关键词双路召回，RRF融合排序，支持按健康分组过滤），仅用于健康科普助手场景。"
    "当用户咨询疾病、症状、用药、饮食养生、就医建议等健康科普问题时调用，从知识库检索权威科普片段作为回答依据；"
    "若用户明确限定某类健康分组（如心血管、呼吸、儿童健康等）可通过docGroup过滤。"
    "超出健康科普范围的问题不要调用本工具，也不要编造知识库中没有的医学内容。"
)
