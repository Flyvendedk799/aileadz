from flask import Response, stream_with_context
import json
import openai
from app1.tools import OPENAI_TOOLS, execute_tool
from . import render_multi_course_media, render_product_media

import uuid

# Server-side memory buffer. Flask session cookies cannot be mutated mid-stream because headers are already sent.
CHAT_MEMORY = {}

def handle_agentic_ask(user_query, session):
    """
    Core Agent Loop for Phase 2 & 3. 
    Maintains memory Server-Side, executes tools natively, and yields SSE streams.
    """
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
        
    sid = session["session_id"]

    # 1. Initialize or load Conversation Memory
    if sid not in CHAT_MEMORY:
        CHAT_MEMORY[sid] = [
            {"role": "system", "content": "Du er en AI-uddannelsesrådgiver for AiLead. VIGTIG RESPEKTER DETTE: Når du bruger tools til at finde kurser, vises de fundne kurser automatisk på skærmen som interaktive visuelle kort ved siden af din tekst. Du må derfor ALDRIG, ALDRIG, ALDRIG skrive lister eller bulletpoints over kursusnavne, priser, lokationer eller beskrivelser i dit eget tekstsvar! At gentage disse informationer ødelægger brugeroplevelsen.\n\nDin opgave er KUN at være selve samtalepartneren. Skriv i et naturligt, hjælpende sprog (maks 2-3 sætninger ad gangen). Sig f.eks.: 'Her er et par muligheder til at håndtere dit behov. Hvor i landet foretrækker du at deltage?', og lad kortene på skærmen tale for sig selv."}
        ]
        
    messages = CHAT_MEMORY[sid]
    messages.append({"role": "user", "content": user_query})

    # Keep memory size bounded to avoid token limit exceptions
    if len(messages) > 11:
        CHAT_MEMORY[sid] = [messages[0]] + messages[-10:]
        messages = CHAT_MEMORY[sid]

    def stream_generator():
        # Ping Nginx immediately to keep the HTTP connection alive while the LLM thinks
        yield f"data: {json.dumps({'type': 'ping', 'content': 'ok'})}\n\n"

        buffered_ui_html = []

        # Agent Loop
        while True:
            response = openai.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=OPENAI_TOOLS,
                stream=False # We use true continuous streaming internally, but step-by-step for the agent loop
            )
            
            message = response.choices[0].message
            messages.append(message.model_dump())

            # If the LLM wants to use a tool
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    # Execute tool natively
                    tool_result_str = execute_tool(tool_call)
                    tool_result_dict = json.loads(tool_result_str)
                    
                    # Store tool response
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "content": json.dumps({k: v for k,v in tool_result_dict.items() if not k.startswith("raw_")})
                    })

                    # ---> UI Interceptor <---
                    # If the tool returned raw product data, we buffer the HTML UI component to stream after the text!
                    if tool_call.function.name == "search_courses" and "raw_products" in tool_result_dict:
                        raw_products = tool_result_dict["raw_products"]
                        if raw_products:
                            ui_html = render_multi_course_media(raw_products)
                            buffered_ui_html.append(ui_html)
                    
                    elif tool_call.function.name == "get_course_details" and "raw_product" in tool_result_dict:
                        ui_html = render_product_media(tool_result_dict["raw_product"])
                        buffered_ui_html.append(ui_html)

                # Loop continues, allowing LLM to read the tool output and stream its final answer
                continue
            
            # If the LLM just wants to talk, stream the text to the client
            break
            
        # Final Text Streaming Output from LLM
        stream = openai.chat.completions.create(
            model="gpt-4o",
            messages=messages[:-1], # pop the static message and regenerate as a stream
            tools=OPENAI_TOOLS,
            stream=True
        )
        
        full_assistant_reply = ""
        for chunk in stream:
            txt = chunk.choices[0].delta.content
            if txt:
                full_assistant_reply += txt
                # Yield text chunk to frontend SSE
                yield f"data: {json.dumps({'type': 'chunk', 'content': txt})}\n\n"
        
        # After text has finished typing out, yield any buffered product HTML cards!
        for html_chunk in buffered_ui_html:
            yield f"data: {json.dumps({'type': 'product', 'html': html_chunk})}\n\n"
        
        # Save final text to memory
        messages[-1] = {"role": "assistant", "content": full_assistant_reply}
        
        # End stream
        yield "data: [DONE]\n\n"

    response = Response(stream_with_context(stream_generator()), mimetype="text/event-stream")
    # Crucial for PythonAnywhere (uWSGI/Nginx) to prevent buffering and Broken Pipe errors
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    return response
