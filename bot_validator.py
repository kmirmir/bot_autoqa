# bot_validator.py

import openai
import os
import pandas as pd
from fpdf import FPDF
import plotly.express as px
from dotenv import load_dotenv
import io

# .env 환경변수 로드
def ensure_env_loaded():
    if not os.environ.get("ENV_LOADED"):
        load_dotenv()
        os.environ["ENV_LOADED"] = "1"

# 기존 분석 함수

def analyze_bot_json(data):
    flows = data['context']['flows']
    pages = []
    handlers = []
    variables = set()
    for flow in flows:
        flow_name = flow['name']
        for page in flow['pages']:
            # 기존: pages.append(page)
            # 수정: Flow명 + Page명 조합으로 저장
            pages.append((flow_name, page['name']))
            for handler in page.get('handlers', []):
                handlers.append(handler)
                for preset in handler.get('action', {}).get('parameterPresets', []):
                    variables.add(preset['name'])
                for preset in handler.get('parameterPresets', []):
                    variables.add(preset['name'])
    return flows, pages, handlers, list(variables)

# OpenAI 자동 수정 제안

def openai_suggest_fix(error_context, prompt):
    ensure_env_loaded()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "[OpenAI API 키가 설정되지 않았습니다.]"
    import openai
    openai.api_key = api_key
    try:
        # openai>=1.0.0 방식
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "너는 봇 QA 전문가야. 오류를 고쳐줘."},
                {"role": "user", "content": prompt + "\n" + error_context}
            ],
            max_tokens=200
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"[OpenAI 오류: {e}]"

# PDF/Excel 리포트

def export_excel(errors, suggestions, filename="bot_report.xlsx"):
    df = pd.DataFrame(errors)
    df2 = pd.DataFrame({"suggestion": suggestions})
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name="Errors", index=False)
        df2.to_excel(writer, sheet_name="Suggestions", index=False)
    output.seek(0)
    return output

def export_pdf(errors, suggestions, filename="bot_report.pdf"):
    pdf = FPDF()
    pdf.add_page()
    # 한글 폰트 등록 (폰트 파일이 프로젝트 폴더에 있어야 함)
    font_path = "NanumGothic.ttf"  # 실제 파일명에 맞게 수정
    pdf.add_font('Nanum', '', font_path, uni=True)
    pdf.set_font('Nanum', '', 12)
    pdf.cell(200, 10, txt="Bot QA Report", ln=True, align='C')
    pdf.ln(10)
    for err, sug in zip(errors, suggestions):
        pdf.multi_cell(0, 10, f"[{err['type']}] {err['message']} (위치: {err['location']})")
        if err['suggestion']:
            pdf.multi_cell(0, 10, f"수정 제안: {err['suggestion']}")
        pdf.ln(5)
    output = io.BytesIO()
    pdf.output(output)
    output.seek(0)
    return output

# plotly 그래프

def plot_error_types(errors):
    error_types = [err['type'] for err in errors]
    fig = px.pie(names=error_types, title="오류 유형별 분포")
    return fig

# validate_bot_json: 오타 검수 완전 제거
def validate_bot_json(data, custom_checks=None):
    errors = []
    flows = data['context']['flows']
    page_names = set()
    for flow in flows:
        for page in flow['pages']:
            page_names.add(page['name'])
    # 기본 검수
    for flow in flows:
        for page in flow['pages']:
            # transitionTarget 오류
            for handler in page.get('handlers', []):
                target = handler.get('transitionTarget', {})
                if target.get('type') == 'CUSTOM':
                    if target.get('page') and target['page'] not in page_names:
                        errors.append({
                            'type': 'PageLinkError',
                            'message': f"존재하지 않는 페이지로 이동: {target['page']}",
                            'location': f"{flow['name']} > {page['name']}",
                            'suggestion': f"'{target['page']}' 페이지가 실제로 존재하는지 확인하거나, 올바른 페이지명으로 수정하세요."
                        })
            # 핸들러 누락
            if not page.get('handlers'):
                errors.append({
                    'type': 'HandlerMissing',
                    'message': "핸들러가 없는 페이지",
                    'location': f"{flow['name']} > {page['name']}",
                    'suggestion': "필수 이벤트 핸들러를 추가하세요."
                })
            # 조건문 오류
            for handler in page.get('handlers', []):
                cond = handler.get('conditionStatement')
                if cond is not None and cond.strip() in ["", "True", "False"]:
                    errors.append({
                        'type': 'ConditionError',
                        'message': f"비어있거나 의미 없는 조건문: '{cond}'",
                        'location': f"{flow['name']} > {page['name']}",
                        'suggestion': "실제 조건을 입력하거나, 불필요하다면 조건문을 제거하세요."
                    })
    # 커스텀 검수 항목
    if custom_checks:
        for check in custom_checks:
            errors.append({
                'type': 'CustomCheck',
                'message': f"사용자 정의 검수: {check}",
                'location': "전체",
                'suggestion': "직접 검수 로직을 추가하세요."
            })
    return errors

def suggest_fixes(errors, data, use_openai=False):
    suggestions = []
    for err in errors:
        if use_openai and err['type'] in ['ConditionError', 'PageLinkError']:
            # 오류 맥락과 프롬프트 생성
            context = f"오류 설명: {err['message']}\n위치: {err['location']}"
            prompt = "이 오류를 어떻게 고치면 좋을지 제안해줘."
            ai_suggestion = openai_suggest_fix(context, prompt)
            suggestions.append(f"AI 제안: {ai_suggestion}")
        elif err['type'] == 'PageLinkError':
            suggestions.append(f"{err['location']}에서 '{err['message']}' 오류가 있습니다. '{err['suggestion']}'")
        elif err['type'] == 'HandlerMissing':
            suggestions.append(f"{err['location']}에 핸들러가 없습니다. 'USER_DIALOG_START' 등 기본 핸들러를 추가하세요.")
        elif err['type'] == 'ConditionError':
            suggestions.append(f"{err['location']}의 조건문을 점검하세요. '{err['suggestion']}'")
        elif err['type'] == 'CustomCheck':
            suggestions.append(f"사용자 정의 검수 항목: {err['message']}")
    return suggestions 
