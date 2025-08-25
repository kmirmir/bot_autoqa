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
    """봇 JSON 데이터를 분석하여 flows, pages, handlers, variables를 반환"""
    try:
        if not data or 'context' not in data or 'flows' not in data['context']:
            raise ValueError("유효하지 않은 데이터 구조입니다.")
        
        flows = data['context']['flows']
        if not isinstance(flows, list):
            raise ValueError("flows가 리스트가 아닙니다.")
        
        pages = []
        handlers = []
        variables = set()
        
        for flow in flows:
            if not isinstance(flow, dict) or 'name' not in flow or 'pages' not in flow:
                continue
                
            flow_name = flow['name']
            for page in flow.get('pages', []):
                if not isinstance(page, dict) or 'name' not in page:
                    continue
                    
                # Flow명 + Page명 조합으로 저장
                pages.append((flow_name, page['name']))
                
                for handler in page.get('handlers', []):
                    if isinstance(handler, dict):
                        handlers.append(handler)
                        
                        # action.parameterPresets에서 변수 추출
                        action = handler.get('action', {})
                        if isinstance(action, dict):
                            for preset in action.get('parameterPresets', []):
                                if isinstance(preset, dict) and 'name' in preset:
                                    variables.add(preset['name'])
                        
                        # parameterPresets에서 변수 추출
                        for preset in handler.get('parameterPresets', []):
                            if isinstance(preset, dict) and 'name' in preset:
                                variables.add(preset['name'])
        
        return flows, pages, handlers, list(variables)
        
    except Exception as e:
        # 기본값 반환
        return [], [], [], []

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
    # FPDF의 output(dest='S')로 PDF 바이트를 얻어 BytesIO에 저장
    pdf_bytes = pdf.output(dest='S').encode('latin1')
    output = io.BytesIO(pdf_bytes)
    output.seek(0)
    return output

# plotly 그래프

def plot_error_types(errors):
    error_types = [err['type'] for err in errors]
    fig = px.pie(names=error_types, title="오류 유형별 분포")
    return fig

# validate_bot_json: 오타 검수 완전 제거
def validate_bot_json(data, custom_checks=None):
    """봇 JSON 데이터의 유효성을 검증하여 오류 목록을 반환"""
    errors = []
    
    try:
        if not data or 'context' not in data or 'flows' not in data['context']:
            errors.append({
                'type': 'DataStructureError',
                'message': "유효하지 않은 데이터 구조입니다.",
                'location': "전체",
                'suggestion': "올바른 봇 빌더 JSON 파일을 업로드하세요."
            })
            return errors
        
        flows = data['context']['flows']
        if not isinstance(flows, list):
            errors.append({
                'type': 'DataStructureError',
                'message': "flows가 리스트가 아닙니다.",
                'location': "전체",
                'suggestion': "데이터 구조를 확인하세요."
            })
            return errors
        
        page_names = set()
        # --- INTENT/ENTITY/이벤트/연산자/함수/예약어 목록 추출 ---
        all_intents = set()
        for intent in data['context'].get('openIntents', []) + data['context'].get('userIntents', []):
            if isinstance(intent, dict) and intent.get('name'):
                all_intents.add(intent['name'])
        
        allowed_operators = set(['/','*','+','-','==','!=','>=','<=','>','<','EXISTS','NOT EXISTS','IN','NOT','AND','OR'])
        allowed_functions = set(['sum','getNumber','isValidDatetime','getLength','convertDateFormat','addDays','isBefore','trim','replaceAll','mergeStrings','toUpper','toLower'])
        reserved_words = set(['True','{$USER_TEXT_INPUT}','{$__NLU_INTENT__}', '{$NLU_INTENT}','SLOT_FILLING_COMPLETED','ASKING_SLOT'])
        allowed_event_types = set([
            'NO_MATCH_EVENT','PAUSE_EVENT','WAKE_EVENT','WEBHOOK_FAILED_EVENT','USER_DIALOG_START','USER_DIALOG_END',
            'USER_DIALOG_WAIT_TIMEOUT','USER_BUTTON_CLICK','USER_FILE_UPLOAD_SUCCESS','USER_FILE_UPLOAD_FAIL','USER_TRANSFER_AGENT','BOT_TRANSITION_NOT_ALLOWED'
        ])
        
        # 페이지명 수집
        for flow in flows:
            if not isinstance(flow, dict) or 'name' not in flow or 'pages' not in flow:
                continue
            for page in flow.get('pages', []):
                if isinstance(page, dict) and 'name' in page:
                    page_names.add(page['name'])
        
        # 기본 검수
        for flow in flows:
            if not isinstance(flow, dict) or 'name' not in flow or 'pages' not in flow:
                continue
                
            flow_name = flow['name']
            for page in flow.get('pages', []):
                if not isinstance(page, dict) or 'name' not in page:
                    continue
                    
                page_name = page['name']
                
                # transitionTarget 오류
                for handler in page.get('handlers', []):
                    if not isinstance(handler, dict):
                        continue
                        
                    target = handler.get('transitionTarget', {})
                    if isinstance(target, dict) and target.get('type') == 'CUSTOM':
                        if target.get('page') and target['page'] not in page_names:
                            errors.append({
                                'type': 'PageLinkError',
                                'message': f"존재하지 않는 페이지로 이동: {target['page']}",
                                'location': f"{flow_name} > {page_name}",
                                'suggestion': f"'{target['page']}' 페이지가 실제로 존재하는지 확인하거나, 올바른 페이지명으로 수정하세요."
                            })
                
                # 핸들러 누락
                if not page.get('handlers'):
                    errors.append({
                        'type': 'HandlerMissing',
                        'message': "핸들러가 없는 페이지",
                        'location': f"{flow_name} > {page_name}",
                        'suggestion': "필수 이벤트 핸들러를 추가하세요."
                    })
                
                # 핸들러별 상세 검수
                for handler in page.get('handlers', []):
                    if not isinstance(handler, dict):
                        continue
                        
                    handler_type = handler.get('type','')
                    cond = handler.get('conditionStatement','')
                    
                    # --- INTENT handler 검수 ---
                    if handler_type == 'INTENT':
                        intent_trigger = handler.get('intentTrigger', {})
                        if isinstance(intent_trigger, dict):
                            intent_name = intent_trigger.get('name')
                            if intent_name and intent_name not in all_intents:
                                errors.append({
                                    'type': 'IntentError',
                                    'message': f"등록되지 않은 Intent명 사용: {intent_name}",
                                    'location': f"{flow_name} > {page_name}",
                                    'suggestion': f"{intent_name}는 등록된 Intent명이 아닙니다.",
                                    'used_intent': intent_name
                                })
                    
                    # --- CONDITION handler 검수 ---
                    if handler_type == 'CONDITION' and cond:
                        try:
                            # True 대소문자 구분
                            if cond.strip() and cond.strip() not in reserved_words:
                                if cond.strip() == 'true' or cond.strip() == 'TRUE' or cond.strip() == 'True ':
                                    errors.append({
                                        'type': 'ConditionError',
                                        'message': f"조건문 True는 반드시 대소문자 구분하여 'True'로 입력해야 합니다. 현재: '{cond}'",
                                        'location': f"{flow_name} > {page_name}",
                                        'suggestion': f"조건문 '{cond}'를 'True'로 수정하세요.",
                                        'used_condition': cond
                                    })
                            
                            # 파라미터 참조 형식 및 parameterPresets, intents, entities와 비교
                            import re
                            param_refs = re.findall(r'\{\$([a-zA-Z0-9_]+)\}', cond)
                            preset_names = set()
                            
                            # parameterPresets에서 이름 추출
                            for p in handler.get('parameterPresets', []):
                                if isinstance(p, dict) and 'name' in p:
                                    preset_names.add(p['name'])
                            
                            # intent/entity name 목록 추출
                            all_entities = set()
                            for entity in data['context'].get('customEntities', []):
                                if isinstance(entity, dict) and entity.get('name'):
                                    all_entities.add(entity['name'])
                            
                            missing_vars = []
                            for ref in param_refs:
                                # '__NLU_INTENT__' 또는 'NLU_INTENT'는 제외
                                if ref in ('__NLU_INTENT__', 'NLU_INTENT'):
                                    continue
                                if ref not in preset_names and ref not in all_intents and ref not in all_entities:
                                    missing_vars.append(ref)
                            
                            if missing_vars:
                                errors.append({
                                    'type': 'ConditionWarning',
                                    'message': f"조건문에서 참조한 파라미터명(들) {', '.join(missing_vars)}이(가) parameterPresets, Intent, Entity에 없습니다.",
                                    'location': f"{flow_name} > {page_name}",
                                    'suggestion': f"{', '.join(missing_vars)} 변수가 등록되어 있지 않습니다.",
                                    'missing_vars': missing_vars
                                })
                            
                            # 연산자 체크
                            ops = re.findall(r'(==|!=|>=|<=|>|<|EXISTS|NOT EXISTS|IN|NOT|AND|OR|\+|\-|\*|/)', cond)
                            for op in ops:
                                if op not in allowed_operators:
                                    errors.append({
                                        'type': 'ConditionError',
                                        'message': f"허용되지 않은 연산자 사용: {op}",
                                        'location': f"{flow_name} > {page_name}",
                                        'suggestion': f"조건문에서 허용되지 않은 연산자 '{op}'를 사용했습니다. 조건문: '{cond}'",
                                        'used_condition': cond
                                    })
                            
                            # 함수 체크
                            funcs = re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', cond)
                            for func in funcs:
                                if func not in allowed_functions and func not in reserved_words:
                                    errors.append({
                                        'type': 'ConditionError',
                                        'message': f"허용되지 않은 함수 사용: {func}",
                                        'location': f"{flow_name} > {page_name}",
                                        'suggestion': f"조건문에서 허용되지 않은 함수 '{func}'를 사용했습니다. 조건문: '{cond}'",
                                        'used_condition': cond
                                    })
                        except Exception as e:
                            # 정규식 처리 중 오류 발생 시
                            errors.append({
                                'type': 'ConditionError',
                                'message': f"조건문 파싱 오류: {str(e)}",
                                'location': f"{flow_name} > {page_name}",
                                'suggestion': "조건문 형식을 확인하세요.",
                                'used_condition': cond
                            })
                    
                    # --- EVENT handler 검수 ---
                    if handler_type == 'EVENT':
                        event_trigger = handler.get('eventTrigger', {})
                        if isinstance(event_trigger, dict):
                            event_type = event_trigger.get('type')
                            if event_type and event_type not in allowed_event_types:
                                errors.append({
                                    'type': 'EventWarning',
                                    'message': f"허용되지 않은 이벤트 타입: {event_type}",
                                    'location': f"{flow_name} > {page_name}",
                                    'suggestion': f"허용 이벤트 타입만 사용하세요: {', '.join(allowed_event_types)}"
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
        
    except Exception as e:
        errors.append({
            'type': 'ValidationError',
            'message': f"검증 중 오류 발생: {str(e)}",
            'location': "전체",
            'suggestion': "데이터 구조를 확인하고 다시 시도하세요."
        })
    
    return errors

def suggest_fixes(errors, data, use_openai=False):
    suggestions = []
    for err in errors:
        # IntentError도 AI 제안 적용
        if use_openai and err['type'] in ['ConditionError', 'PageLinkError', 'IntentError']:
            # 오류 맥락과 프롬프트 생성
            context = f"오류 설명: {err['message']}\n위치: {err['location']}"
            prompt = "이 오류를 어떻게 고치면 좋을지 제안해줘."
            ai_suggestion = openai_suggest_fix(context, prompt)
            # AI 제안 접두어 보장
            if not ai_suggestion.strip().startswith("AI 제안:"):
                ai_suggestion = f"AI 제안: {ai_suggestion}"
            suggestions.append(ai_suggestion)
        elif err['type'] == 'PageLinkError':
            suggestions.append(f"{err['location']}에서 '{err['message']}' 오류가 있습니다. '{err['suggestion']}'")
        elif err['type'] == 'HandlerMissing':
            suggestions.append(f"{err['location']}에 핸들러가 없습니다. 'USER_DIALOG_START' 등 기본 핸들러를 추가하세요.")
        elif err['type'] == 'ConditionError':
            suggestions.append(f"{err['location']}의 조건문을 점검하세요. '{err['suggestion']}'")
        elif err['type'] == 'CustomCheck':
            suggestions.append(f"사용자 정의 검수 항목: {err['message']}")
    return suggestions
