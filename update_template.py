with open("app1/__init__.py", "r", encoding="utf-8") as f:
    content = f.read()

import re

new_template = '''MULTIPLE_COURSES_TEMPLATE = """
<div style="display: flex; flex-direction: column; gap: 12px; max-width: 450px;">
  {% for course in courses %}
    <div class="course-card" onclick="this.classList.toggle('expanded');">
      
      {# Header / Always Visible Part #}
      <div class="course-card-header">
        
        {# Left Icon/Logo #}
        <div style="background-color: #f8f6f2; border-radius: 8px; width: 60px; height: 60px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; overflow: hidden; position: relative;">
          {% if course.image_url %}
              <img src="{{ course.image_url | e }}" style="max-width: 80%; max-height: 80%; object-fit: contain;">
          {% else %}
              <div style="font-size: 10px; color: #aaa;">Logo</div>
          {% endif %}
        </div>
        
        {# Right Details Summary #}
        <div style="flex-grow: 1; min-width: 0; font-family: 'Inter', Arial, sans-serif;">
          <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 4px;">
              <h4 style="margin: 0; font-size: 15px; font-weight: 700; color: #1a1a1a; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 70%;">
                  {{ course.title | e }}
              </h4>
              <div style="font-size: 13px; font-weight: 700; color: #1a1a1a; white-space: nowrap;">
                  {% if course.price in ['0', '0.00'] %}
                      Gratis
                  {% else %}
                      kr {{ course.price | e }}
                  {% endif %}
              </div>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: #666;">
              <div style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 60%;">{{ course.vendor | e }}</div>
              <div style="display: flex; align-items: center; gap: 6px;">
                  {% if course.price not in ['0', '0.00'] %}
                      <span style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; opacity: 0.8;">Ekskl. moms</span>
                  {% endif %}
                  <svg class="course-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
              </div>
          </div>
        </div>
      </div>
      
      {# Collapsible Details Body #}
      <div class="course-details-wrapper">
        <div class="course-details-inner">
          <div class="course-details-content" style="font-family: 'Inter', Arial, sans-serif;">
            <div style="margin-top: 12px; font-size: 13px; color: #444; line-height: 1.6;">
              {{ get_short_description(course) | e }}
            </div>
            
            <div style="display: flex; gap: 24px; margin-top: 20px; margin-bottom: 20px;">
              <div>
                <div style="display: flex; align-items: center; gap: 6px; color: #222; font-weight: 700; font-size: 12px; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px;">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                  Varighed
                </div>
                <div style="color: #666; font-size: 13px; font-weight: 500;">
                  {% if course.variants and course.variants|length > 1 %}
                    Flere muligheder
                  {% else %}
                    Ikke angivet
                  {% endif %}
                </div>
              </div>
              <div>
                <div style="display: flex; align-items: center; gap: 6px; color: #222; font-weight: 700; font-size: 12px; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px;">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1 4-10z"></path></svg>
                  Lokation
                </div>
                <div style="color: #666; font-size: 13px; font-weight: 500;">
                  {% if course.location %}
                    {{ course.location | e }}
                  {% else %}
                    Online/Flere
                  {% endif %}
                </div>
              </div>
            </div>

            <button onclick="event.stopPropagation(); window.open(\'{{ course.url | e }}\', \'_blank\')" style="width: 100%; background-color: #111; color: #fff; border: 1px solid #111; padding: 12px 0; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1); outline: none;" onmouseover="this.style.backgroundColor=\'#fff\'; this.style.color=\'#111\'; this.style.boxShadow=\'0 4px 12px rgba(0,0,0,0.1)\';" onmouseout="this.style.backgroundColor=\'#111\'; this.style.color=\'#fff\'; this.style.boxShadow=\'none\';">
              Vælg kursus
            </button>
          </div>
        </div>
      </div>
      
    </div>
  {% endfor %}
</div>
"""
'''

pattern = r'MULTIPLE_COURSES_TEMPLATE = """(.*?)"""\n'
content = re.sub(pattern, new_template + "\n", content, flags=re.DOTALL)

with open("app1/__init__.py", "w", encoding="utf-8") as f:
    f.write(content)
