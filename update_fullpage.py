import re

with open("app1/templates/index.html", "r", encoding="utf-8") as f:
    text = f.read()

# 1. Override .content and #chatApp layout natively
pattern_app = r"#chatApp\s*\{[\s\S]*?\/\* accounting for header.*?}"
replacement_app = """/* OVERRIDE BASE LAYOUT FOR FULL-PAGE CHAT */
  .content {
    padding: 0 !important;
    display: flex;
    flex-direction: column;
    height: calc(100vh - 142px); /* strict height to force internal scroll */
    overflow: hidden;
  }
  
  #chatApp {
    margin: 0;
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    display: flex;
    flex-direction: column;
    flex-grow: 1;
    height: 100%;
  }"""
text = re.sub(pattern_app, replacement_app, text)

# 2. Transparent chat container borderless
pattern_container = r"#chatApp \.chat-container\s*\{[\s\S]*?animation: containerFadeIn[^\n]*\n\s*\}"
replacement_container = """#chatApp .chat-container {
    width: 100%;
    max-width: 100%;
    flex-grow: 1;
    background: transparent; /* blend with dashboard */
    border: none;
    border-radius: 0;
    box-shadow: none;
    margin: 0;
    display: flex;
    flex-direction: column;
    animation: containerFadeIn 0.5s ease-out;
  }"""
text = re.sub(pattern_container, replacement_container, text)

# 3. Suppress the redundant dark glass header
pattern_header = r"#chatApp \.chat-header\s*\{[\s\S]*?z-index: 10;\n\s*\}"
replacement_header = """#chatApp .chat-header {
    display: none; /* Let the dashboard's header serve as the page title */
  }"""
text = re.sub(pattern_header, replacement_header, text)

# 4. Enhance chat box flush padding
pattern_box = r"#chatApp \.chat-box\s*\{[\s\S]*?gap: 10px;\n\s*\}"
replacement_box = """#chatApp .chat-box {
    flex-grow: 1;
    overflow-y: auto;
    padding: 24px 40px; /* match dashboard padding horizontally */
    display: flex;
    flex-direction: column;
    gap: 16px;
  }"""
text = re.sub(pattern_box, replacement_box, text)

# 5. Make the input area flush
pattern_input = r"#chatApp \.chat-input-container\s*\{[\s\S]*?border-top[^\n]*\n\s*\}"
replacement_input = """#chatApp .chat-input-container {
    display: flex;
    padding: 24px 40px;
    background-color: transparent; /* sits directly on the dashboard page */
    align-items: center;
    border-top: 1px solid rgba(255, 255, 255, 0.1); /* subtle separator */
  }"""
text = re.sub(pattern_input, replacement_input, text)


with open('app1/templates/index.html', 'w', encoding='utf-8') as f:
    f.write(text)
