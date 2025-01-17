from typing import Literal
from langchain_openai import ChatOpenAI
from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore
from langchain.chains.retrieval_qa.base import RetrievalQA
from langchain_core.tools import tool, StructuredTool
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

class RAGQuery(BaseModel):
    query: str = Field(..., description="The query to retrieve relevant content for")

class RAGTool:
    def __init__(self):
        self.embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
        self.index_name = "india-spend"
        self.vectorstore = PineconeVectorStore(
            index_name=self.index_name,
            embedding=self.embeddings
        )
        self.llm = ChatOpenAI(model_name="gpt-4o", temperature=0)
        self.retriever = self.vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs={"k": 5}
        )
        self.rag_chain = RetrievalQA.from_chain_type(
            llm=self.llm,
            chain_type="stuff",
            retriever=self.retriever,
            return_source_documents=True  # Enable source document return
        )

    def retrieve(self, query: RAGQuery) -> dict:
        print(f"Retrieving for query: {query.query}")
        similar_docs = self.retriever.get_relevant_documents(query.query)
        source_links = [doc.metadata.get('source', 'No source') for doc in similar_docs]
        
        result = self.rag_chain.invoke(query.query)
        if isinstance(result, dict):
            response_result = result.get('result', str(result))
            source_documents = result.get('source_documents', [])
            source_links.extend([doc.metadata.get('source', 'No source') for doc in source_documents])
        else:
            response_result = str(result)
            
        # Remove duplicates while preserving order
        source_links = list(dict.fromkeys(source_links))
        
        return {
            "result": response_result,
            "sources": source_links
        }

class Chatbot:
    def __init__(self):
        self.llm = ChatOpenAI(model_name="gpt-4o", temperature=0)
        self.memory = MemorySaver()
        self.rag_tool = RAGTool()
        self.tool_node = None
        self.app = None

        # Define the chatbot's system message
        self.system_message = SystemMessage(
            content=(
                "You are IndiaSpend AI, an expert chatbot designed to answer questions related to IndiaSpend articles, reports, and data analysis. "
                "Your responses should be fact-based, sourced from IndiaSpend's database, and align with IndiaSpend's journalistic style. "
                "You should provide clear, well-structured answers and cite sources where applicable. "
                "Website: [IndiaSpend](https://www.indiaspend.com/)."
            )
        )

    def setup_tools(self):
        rag_tool = StructuredTool.from_function(
            func=self.rag_tool.retrieve,
            name="RAG",
            description="Retrieve relevant content from IndiaSpend's knowledge base",
            args_schema=RAGQuery
        )
        self.tool_node = ToolNode(tools=[rag_tool])

    def should_use_rag(self, query: str) -> bool:
        decision_prompt = f"Does this query require retrieving external information to answer accurately? Answer only 'yes' or 'no'. Query: {query}"
        decision = self.llm.invoke([self.system_message, HumanMessage(content=decision_prompt)])
        return "yes" in decision.content.lower()

    def call_model(self, state: MessagesState) -> dict:
        messages = state['messages']
        last_message = messages[-1]
        query = last_message.content
        sources = []

        if self.should_use_rag(query):
            print(f"Triggering RAG tool for query: {query}")
            rag_result = self.rag_tool.retrieve(RAGQuery(query=query))
            result_text = rag_result['result']
            sources = rag_result['sources']
            
            # Format response with context and sources
            context = f"Context: {result_text}"
            prompt = (
                f"Question: {query}\n\n"
                f"{context}\n\n"
                "Provide a detailed response using IndiaSpend's reporting style, ensuring accuracy and data-backed insights."
            )

            response = self.llm.invoke([self.system_message, HumanMessage(content=prompt)])
            formatted_response = f"{response.content}\n\nSources:\n" + "\n".join(sources)
            return {"messages": [AIMessage(content=formatted_response)]}
        
        # For non-RAG queries, process normally
        response = self.llm.invoke([self.system_message] + messages)
        return {"messages": [AIMessage(content=response.content)]}

    def router_function(self, state: MessagesState) -> Literal["tools", END]:
        messages = state['messages']
        last_message = messages[-1]
        return "tools" if getattr(last_message, 'tool_calls', None) else END

    def __call__(self):
        self.setup_tools()
        workflow = StateGraph(MessagesState)
        
        workflow.add_node("agent", self.call_model)
        workflow.add_node("tools", self.tool_node)
        workflow.add_edge(START, "agent")
        workflow.add_conditional_edges(
            "agent",
            self.router_function,
            {"tools": "tools", END: END}
        )
        workflow.add_edge("tools", "agent")
        
        self.app = workflow.compile(checkpointer=self.memory)
        return self.app
