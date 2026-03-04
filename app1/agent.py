from flask import Response, stream_with_context
import json
import openai
from app1.tools import OPENAI_TOOLS, execute_tool
from . import render_multi_course_media, render_product_media

def handle_agentic_ask(user_query, session):
    """
    Core Agent Loop for Phase 2 & 3. 
    Maintains memory in the session, executes tools natively, and yields SSE streams.
    """
    # 1. Initialize or load Conversation Memory
    if "messages" not in session:
        session["messages"] = [
            {"role": "system", "content": "Du er en professionel, dansk AI-assistent for AiLead, der hjælper brugerne med at finde og forstå kurser og uddannelser. Brug værktøjerne 'search_courses' til at finde kurser via klog søgning, og 'get_course_details' hvis brugeren spørger specifikt til et kursus. Svar altid på dansk. Vær hjælpsom og opsummer resultaterne elegant."}
        ]
        
    messages = session["messages"]
    messages.append({"role": "user", "content": user_query})

    # Keep memory size bounded to avoid token limit exceptions (last 10 messages + system prompt)
    if len(messages) > 11:
        session["messages"] = [messages[0]] + messages[-10:]

    def stream_generator():
        # Agent Loop
        while True:
            response = openai.chat.completions.create(
                model="gpt-4o",
                messages=session["messages"],
                tools=OPENAI_TOOLS,
                stream=False # We use true continuous streaming internally, but step-by-step for the agent loop
            )
            
            message = response.choices[0].message
            session["messages"].append(message.model_dump())

            # If the LLM wants to use a tool
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    # Execute tool natively
                    tool_result_str = execute_tool(tool_call)
                    tool_result_dict = json.loads(tool_result_str)
                    
                    # Store tool response
                    session["messages"].append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "content": json.dumps({k: v for k,v in tool_result_dict.items() if not k.startswith("raw_")})
                    })

                    # ---> UI Interceptor <---
                    # If the tool returned raw product data, we proactively stream the HTML UI component back to the frontend immediately!
                    if tool_call.function.name == "search_courses" and "raw_products" in tool_result_dict:
                        raw_products = tool_result_dict["raw_products"]
                        if raw_products:
                            ui_html = render_multi_course_media(raw_products)
                            yield f"data: {json.dumps({'type': 'product', 'html': ui_html})}\n\n"
                    
                    elif tool_call.function.name == "get_course_details" and "raw_product" in tool_result_dict:
                        ui_html = render_product_media(tool_result_dict["raw_product"])
                        yield f"data: {json.dumps({'type': 'product', 'html': ui_html})}\n\n"

                # Loop continues, allowing LLM to read the tool output and stream its final answer
                continue
            
            # If the LLM just wants to talk, stream the text to the client
            break
            
        # Final Text Streaming Output from LLM
        stream = openai.chat.completions.create(
            model="gpt-4o",
            messages=session["messages"][:-1], # pop the static message and regenerate as a stream
            tools=OPENAI_TOOLS,
            stream=True
        )
        
        full_assistant_reply = ""
        for chunk in stream:
            if chunk.choices[0].delta.content:
                txt = chunk.choices[0].delta.content
                full_assistant_reply += txt
                # Yield text chunk to frontend SSE
                yield f"data: {json.dumps({'type': 'chunk', 'content': txt})}\n\n"
        
        # Save final text to memory
        session["messages"][-1] = {"role": "assistant", "content": full_assistant_reply}
        
        # End stream
        yield "data: [DONE]\n\n"

    response = Response(stream_with_context(stream_generator()), mimetype="text/event-stream")
    # Crucial for PythonAnywhere (uWSGI/Nginx) to prevent buffering and Broken Pipe errors
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    return response
