import json
from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware # <-- New import for CORS
from agent.controller import agent_controller, agent 

chat_memory = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting MCP Server and connecting to Agent...")
    async with agent:
        yield
    print("🛑 Shutting down MCP Server...")

app = FastAPI(lifespan=lifespan)

# <-- Add CORS settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Set this to specific domains in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/business-agent")
async def business_agent(req: dict):
    user_message = req.get("message", "")
    session_id = req.get("session_id", "default_user")
    
    history = chat_memory.get(session_id, [])
    response = await agent_controller(user_message, history)
    
    history.append({"role": "user", "content": user_message})
    
    history.append({"role": "assistant", "content": response["response"]})  
  
    if len(history) > 10:
        history = history[-10:]
        
    chat_memory[session_id] = history
    return response