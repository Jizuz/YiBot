import logging
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import MessagesState

from tools.tavily import tavily_extract, tavily_search
from orchestration.model_provider import get_orch_model as _get_model
from orchestration.prompts.search import PROFESSIONAL_PROMPT
logger = logging.getLogger("orchestration.sub_agent.search_agent")

model = _get_model()


agent = create_agent(
    model,
    tools=[tavily_search, tavily_extract],
    system_prompt=PROFESSIONAL_PROMPT
)

def ask_professional(state: MessagesState) -> dict:
    last_msg = state["messages"][-1].content
    logger.info(f"=== professional agent start, question: {last_msg} ===")

    user_msg = HumanMessage(content=last_msg)
    response = agent.invoke({"messages": user_msg})

    # logger.info(f"=======> professional response: {response}")
    if not response or not response["messages"]:
        return {"messages": [AIMessage(content="对不起，暂时无法回答你的问题")]}

    
    ai_response = response["messages"][-1]
    logger.info(f"=======> professional ai response: {ai_response}")
    
    total_tokens = 0
    metadata = ai_response.response_metadata
    if metadata and metadata["token_usage"] and metadata["token_usage"]["total_tokens"]:
        total_tokens = metadata["token_usage"]["total_tokens"]

    logger.info(f"=======> professional tokens: {total_tokens}")

    if total_tokens > 0:
        ai_msg = ai_response.content + f"\n\n【共消耗{total_tokens}个token】"
    else:
        ai_msg = ai_response.content
    
    logger.info("=== professional agent end ===")
    return {"messages": [{"role": "assistant", "content": ai_msg}]}