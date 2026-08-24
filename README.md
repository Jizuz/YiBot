# YiBot
基于langchain、langgraph，集成RAG、MCP、tavily等工具的医疗问诊、购药、专业咨询玩具平台

## 核心特征
- 🤖 **AI智能体引擎**：基于LangChain、LangGraph的Agent框架，具备自主推理和决策能力
- 🛠️ **工具调用系统**：支持多种工具动态调用
- 💬 **自然语言交互**：支持多轮对话，理解上下文
- 📚 **知识库检索**：RAG技术，提供专业医疗建议

## 技术栈

### 后端
- python 3.13+
- FastAPI - Web框架
- LangChain - Agent框架
- LangGraph - 流程编排框架
- **LLM**:
  - ✅ 智谱AI GLM-4（推荐，免费）⭐
  - ✅ 千问向量模型 text-embedding-v3
- ChromaDB - 向量数据库
- SQLAlchemy - ORM
- SQLite - 数据库

### 前端
- react
- node.js

## 快速开始

### 环境依赖
```bash
# Python 3.13+
# Node.js 24+
```

### 后端启动
```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install langchain langchain_openai langchain_tavily langgraph dotenv fastapi sqlalchemy chroma

# 配置环境变量
.env.dev

# 启动服务
python -m uvicorn app.main:app --reload --port 8000
```

### 前端启动
```bash
# 安装依赖
npm install

# 启动开发服务器
npm run dev

# node版本低，切换高版本
nvm use 24
```

### 4. 访问应用

- 前端：http://localhost:5173
- 后端API文档：http://localhost:8000

## 数据库信息

- **主机**: 127.0.0.1 (本地)
- **端口**: 3306
- **数据库**: chatte
- **用户名**: chatte
- **密码**: chatte
- **版本**: MySQL 8.0.31

#### 库表
```sql
use chatte;

-- 用户表
create table if not exists user (
    `id` bigint unsigned NOT NULL AUTO_INCREMENT comment '主键ID',
    `name` varchar(200) DEFAULT NULL comment '用户名字',
    `full_name` varchar(512) DEFAULT NULL comment '用户全名',
    `password` varchar(200) NOT NULL comment '用户密码',
    `hash_pwd` varchar(200) DEFAULT NULL comment 'hash密码',
    `mobile` varchar(20) NOT NULL comment '用户手机号',
    `email` varchar(32) DEFAULT NULL comment '用户邮箱',
    `id_type` tinyint DEFAULT 0 comment '证件类型 取值：0-身份证 1-护照',
    `id_number` varchar(100) DEFAULT NULL comment '证件号码',
    `disabled` tinyint DEFAULT 0 comment '是否被禁用 取值：0-未禁用 1-已禁用',
    `gmt_create` datetime DEFAULT CURRENT_TIMESTAMP comment '创建时间',
    `gmt_update` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP comment '更新时间',
    `is_delete` tinyint DEFAULT 0 comment '删除标记 取值：0-未删除 1-已删除',
    `version` int DEFAULT 0 comment '版本号',
    PRIMARY KEY (`id`),
    KEY `idx_mobile` (`mobile`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '用户表';
```
