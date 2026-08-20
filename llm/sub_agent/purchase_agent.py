from langgraph.graph import MessagesState

def get_agent_response(state: MessagesState) -> dict:
    return {"messages": [{"role": "assistant", "content": "为您推荐板蓝根颗粒"}]}