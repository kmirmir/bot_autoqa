import streamlit as st
import json
from bot_validator import (
    analyze_bot_json, validate_bot_json, suggest_fixes,
    export_excel, export_pdf, plot_error_types
)
import os
import pandas as pd
import plotly.express as px
import openai
from dotenv import load_dotenv
from fpdf import FPDF
from collections import defaultdict
import re
from io import BytesIO
import html  # html entity decoding
from openai import OpenAI  # Add for v1 API
from pydantic import BaseModel
import time  # For timing debug
from concurrent.futures import ThreadPoolExecutor, as_completed  # For parallel typo check

# .env 파일의 환경변수 자동 로드
load_dotenv()

st.sidebar.title("Auto QA 봇 검수")
menu = st.sidebar.radio("메뉴", [
    "대시보드",
    "JSON 구조 파악",
    "Response Text 검출",
    "QA 검수 결과"
])

st.markdown("""
    <style>
    .css-18e3th9 {padding-top: 0rem;}
    .css-1d391kg {padding-top: 0rem;}
    </style>
""", unsafe_allow_html=True)

# 업로드 파일을 세션 상태에 저장하여 모든 메뉴에서 공유
if 'shared_json_data' not in st.session_state:
    st.session_state['shared_json_data'] = None

uploaded_file = st.file_uploader("JSON 파일 업로드", type=["json"], key="main_json")
if uploaded_file is not None:
    try:
        st.session_state['shared_json_data'] = json.load(uploaded_file)
    except Exception as e:
        st.error(f"JSON 파일을 읽는 중 오류가 발생했습니다: {e}")

data = st.session_state['shared_json_data']

# 메뉴별 Title 동적 변경
if menu == "대시보드":
    st.title("🤖 봇 시나리오 자동 검수 대시보드")
elif menu == "QA 검수 결과":
    st.title("🤖 오류 상세 및 수정 제안")
elif menu == "JSON 구조 파악":
    st.title("🤖 봇 빌더 JSON 구조 파악하기")
elif menu == "Response Text 검출":
    st.title("🤖 Text 오타 검출 및 교정 제안")

def check_openai_key():
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            return False, "환경변수에 OPENAI_API_KEY가 없습니다."
        # 최신 openai 패키지(1.x) 방식
        openai.models.list()
        return True, "OpenAI API 키가 정상적으로 동작합니다."
    except Exception as e:
        return False, f"OpenAI API 키 오류: {e}"

if st.button("OpenAI API 키 정상동작 체크"):
    ok, msg = check_openai_key()
    if ok:
        st.success(msg)
    else:
        st.error(msg)

# Flow별 서비스 시나리오 요약 (Page간 이동 고려, 자연어)
def summarize_flow_service_natural(data):
    flows = data['context']['flows']
    summaries = []
    for flow in flows:
        flow_name = flow['name']
        pages = flow['pages']
        page_names = [page['name'] for page in pages]
        # page간 이동 해석
        page_links = {}
        for page in pages:
            for handler in page.get('handlers', []):
                target = handler.get('transitionTarget', {})
                if target.get('type') == 'CUSTOM' and target.get('page'):
                    page_links.setdefault(page['name'], set()).add(target['page'])
        # 주요 시나리오 흐름 추출 (DFS)
        def dfs(path, visited):
            cur = path[-1]
            if cur not in page_links or not page_links[cur]:
                return [path]
            flows = []
            for nxt in page_links[cur]:
                if nxt in visited:
                    continue
                flows.extend(dfs(path + [nxt], visited | {nxt}))
            return flows
        scenario_paths = []
        if pages:
            scenario_paths = dfs([pages[0]['name']], {pages[0]['name']})
        # 자연어 시나리오 요약
        scenario_desc = ""
        if scenario_paths:
            # 가장 긴 경로를 대표 시나리오로
            main_path = max(scenario_paths, key=len)
            scenario_desc = f"이 Flow는 '{main_path[0]}'에서 시작하여 "
            if len(main_path) > 2:
                scenario_desc += ", ".join(main_path[1:-1]) + f"를 거쳐 '{main_path[-1]}'로 이동하는 주요 시나리오를 포함합니다."
            elif len(main_path) == 2:
                scenario_desc += f"'{main_path[1]}'로 이동하는 시나리오를 포함합니다."
            else:
                scenario_desc += "단일 페이지로 구성되어 있습니다."
        else:
            scenario_desc = "시나리오 흐름을 해석할 수 없습니다."
        # 안내문 추출 (첫 Page의 record.text 또는 action.responses의 record.text)
        first_page = pages[0] if pages else None
        guide_text = None
        if first_page:
            if 'record' in first_page and first_page['record'] and 'text' in first_page['record']:
                guide_text = first_page['record']['text']
            elif 'action' in first_page and 'responses' in first_page['action']:
                for resp in first_page['action']['responses']:
                    if 'record' in resp and resp['record'] and 'text' in resp['record']:
                        guide_text = resp['record']['text']
                        break
        # 요약문 생성
        summary = f"**Flow: {flow_name}**\n"
        if guide_text:
            summary += f"- 주요 안내: {guide_text}\n"
        summary += f"- 주요 페이지: {', '.join(page_names[:3])}\n"
        summary += f"- {scenario_desc}"
        summaries.append(summary)
    return summaries

# 핸들러/변수 상세 요약 테이블 생성 (변수 상세 설명 포함)
def get_handler_variable_details(data):
    flows = data['context']['flows']
    handler_rows = []
    variable_rows = []
    variable_usage = {}  # 변수명: [dict(Flow, Page, Handler Type, Condition, Value, Where)]
    for flow in flows:
        flow_name = flow['name']
        for page in flow['pages']:
            page_name = page['name']
            for handler in page.get('handlers', []):
                handler_type = handler.get('type')
                cond = handler.get('conditionStatement', None)
                handler_rows.append({
                    'Flow': flow_name,
                    'Page': page_name,
                    'Handler Type': handler_type,
                    'Condition': cond if cond else ''
                })
                # 변수
                for preset in handler.get('action', {}).get('parameterPresets', []):
                    row = {
                        'Flow': flow_name,
                        'Page': page_name,
                        'Handler Type': handler_type,
                        'Condition': cond if cond else '',
                        'Variable': preset['name'],
                        'Value': preset.get('value', ''),
                        'Where': 'action.parameterPresets'
                    }
                    variable_rows.append(row)
                    variable_usage.setdefault(preset['name'], []).append(row)
                for preset in handler.get('parameterPresets', []):
                    row = {
                        'Flow': flow_name,
                        'Page': page_name,
                        'Handler Type': handler_type,
                        'Condition': cond if cond else '',
                        'Variable': preset['name'],
                        'Value': preset.get('value', ''),
                        'Where': 'parameterPresets'
                    }
                    variable_rows.append(row)
                    variable_usage.setdefault(preset['name'], []).append(row)
    handler_df = pd.DataFrame(handler_rows)
    variable_df = pd.DataFrame(variable_rows)
    return handler_df, variable_df, variable_usage

# Intent/Entity 요약 및 오류 검수 함수
def get_intent_entity_summary(data):
    # Intents
    intents = []
    intent_names = set()
    for intent in data['context'].get('openIntents', []) + data['context'].get('userIntents', []):
        name = intent.get('name')
        intent_names.add(name)
        example = ", ".join(intent.get('sentences', [])[:3])
        intents.append({
            'Intent명': name,
            '예시 문장': example
        })
    # Entities
    entities = []
    entity_names = set()
    for entity in data['context'].get('customEntities', []):
        name = entity.get('name')
        entity_names.add(name)
        for v in entity.get('entityValues', []):
            rep = v.get('representative')
            synonyms = ", ".join(v.get('synonyms', []))
            entities.append({
                'Entity명': name,
                '대표값': rep,
                '동의어': synonyms
            })
    # Intent 오류 검수(중복, 미사용 등)
    intent_errors = []
    if len(intent_names) != len(intents):
        intent_errors.append({'오류': '중복 Intent명 존재'})
    # Entity 오류 검수(중복, 미사용 등)
    entity_errors = []
    if len(entity_names) != len(data['context'].get('customEntities', [])):
        entity_errors.append({'오류': '중복 Entity명 존재'})
    # 미사용 Intent/Entity(플로우/핸들러에서 참조되지 않는 경우)
    # (간단히 플로우 내 intentTrigger, conditionStatement 등에서 참조되는지 확인)
    used_intents = set()
    used_entities = set()
    for flow in data['context'].get('flows', []):
        for page in flow.get('pages', []):
            for handler in page.get('handlers', []):
                # intentTrigger
                if 'intentTrigger' in handler:
                    used_intents.add(handler['intentTrigger'].get('name'))
                # conditionStatement 내 intent명/엔티티명
                cond = handler.get('conditionStatement', '')
                for iname in intent_names:
                    if iname and iname in str(cond):
                        used_intents.add(iname)
                for ename in entity_names:
                    if ename and ename in str(cond):
                        used_entities.add(ename)
    unused_intents = intent_names - used_intents
    unused_entities = entity_names - used_entities
    if unused_intents:
        intent_errors.append({'오류': f'미사용 Intent: {", ".join(unused_intents)}'})
    if unused_entities:
        entity_errors.append({'오류': f'미사용 Entity: {", ".join(unused_entities)}'})
    return pd.DataFrame(intents), pd.DataFrame(entities), pd.DataFrame(intent_errors), pd.DataFrame(entity_errors)

# 탭 스타일 커스텀 CSS 추가
st.markdown('''
    <style>
    /* 탭 바 전체 배경 및 구분선 */
    .stTabs [data-baseweb="tab-list"] {
        background: #fafaff;
        border-bottom: 2px solid #e0e0e0;
        padding: 1.2rem 2rem 0 2rem;
        border-radius: 2rem 2rem 0 0;
        box-shadow: 0 4px 16px rgba(108,71,255,0.06);
        margin-bottom: 0.5rem;
    }
    /* 탭 버튼 */
    .stTabs [data-baseweb="tab"] {
        font-size: 1.15rem;
        font-weight: 700;
        color: #888;
        padding: 0.7rem 2.2rem 0.7rem 2.2rem;
        margin-right: 1.2rem;
        border-radius: 1.5rem 1.5rem 0 0;
        background: #f5f6fa;
        transition: background 0.2s, color 0.2s;
        border: none;
        outline: none;
    }
    /* 활성 탭 */
    .stTabs [aria-selected="true"] {
        background: #fff;
        color: #2d2d3a;
        border-bottom: 3px solid #6c47ff;
        box-shadow: 0 2px 8px rgba(108,71,255,0.07);
        z-index: 2;
    }
    /* 비활성 탭 hover 효과 */
    .stTabs [data-baseweb="tab"]:hover {
        background: #ececff;
        color: #6c47ff;
    }
    /* 탭 내 제목 강조 */
    .tab-section-title {
        font-size: 2.1rem;
        font-weight: 900;
        color: #2d2d3a;
        margin-top: 1.2rem;
        margin-bottom: 1.2rem;
        letter-spacing: -1px;
        display: flex;
        align-items: center;
        gap: 0.7rem;
    }
    .tab-section-title .icon {
        font-size: 2.2rem;
        color: #6c47ff;
        vertical-align: middle;
    }
    </style>
''', unsafe_allow_html=True)

# --- JSON 구조 파악 기능 완전 내장 (structure.py 불필요) ---
def summarize_action(action):
    """핵심 key만 요약 텍스트로 변환"""
    if not isinstance(action, dict) or not action:
        return ""
    keys = [k for k in action.keys() if action[k]]
    summary = []
    for k in keys:
        v = action[k]
        if isinstance(v, list):
            summary.append(f"{k}: {len(v)}개")
        elif isinstance(v, dict):
            summary.append(f"{k}: dict")
        else:
            summary.append(f"{k}: {str(v)[:20]}")
    return ", ".join(summary) if summary else "-"

def summarize_list(val):
    if isinstance(val, list):
        if not val:
            return "-"
        # 리스트가 dict면 주요 key만 요약
        if all(isinstance(x, dict) for x in val):
            return "; ".join(
                [", ".join(f"{k}:{str(v)[:10]}" for k, v in x.items()) for x in val]
            )
        return ", ".join(str(x) for x in val)
    elif val is None or val == "":
        return "-"
    else:
        return str(val)

def parse_bot_structure_from_data(data):
    flows = data["context"].get("flows", [])
    intents = data["context"].get("openIntents", []) + data["context"].get("userIntents", [])
    entities = data["context"].get("customEntities", [])

    flow_rows = []
    for flow in flows:
        flow_name = flow.get("name")
        for page in flow.get("pages", []):
            page_name = page.get("name")
            action = page.get("action", {})
            parameters = page.get("parameters", [])
            for handler in page.get("handlers", []):
                handler_type = handler.get("type")
                handler_id = handler.get("id", "")
                handler_action = handler.get("action", {})
                handler_param_presets = handler_action.get("parameterPresets", [])
                condition = handler.get("conditionStatement", "")
                event_trigger = handler.get("eventTrigger", {})
                intent_trigger = handler.get("intentTrigger", {})
                transition_target = handler.get("transitionTarget", {})
                flow_rows.append({
                    "Flow": flow_name,
                    "Page": page_name,
                    "Page_Action": summarize_action(action),
                    "Page_Parameters": summarize_list(parameters),
                    "Handler_ID": handler_id,
                    "Handler_Type": handler_type,
                    "Handler_Condition": condition,
                    "Handler_Action": summarize_action(handler_action),
                    "Handler_ParameterPresets": summarize_list(handler_param_presets),
                    "Handler_EventTrigger": str(event_trigger) if event_trigger else "",
                    "Handler_IntentTrigger": str(intent_trigger) if intent_trigger else "",
                    "Handler_TransitionTarget": str(transition_target) if transition_target else "",
                })
            if not page.get("handlers"):
                flow_rows.append({
                    "Flow": flow_name,
                    "Page": page_name,
                    "Page_Action": summarize_action(action),
                    "Page_Parameters": summarize_list(parameters),
                    "Handler_ID": "",
                    "Handler_Type": "",
                    "Handler_Condition": "",
                    "Handler_Action": "",
                    "Handler_ParameterPresets": "",
                    "Handler_EventTrigger": "",
                    "Handler_IntentTrigger": "",
                    "Handler_TransitionTarget": "",
                })
    flow_df = pd.DataFrame(flow_rows)

    intent_rows = []
    for intent in intents:
        intent_rows.append({
            "Intent_Name": intent.get("name"),
            "Sentences": ", ".join(intent.get("sentences", [])),
            "RepresentativeSentences": ", ".join(intent.get("representativeSentences", [])),
        })
    intent_df = pd.DataFrame(intent_rows)

    entity_rows = []
    for entity in entities:
        entity_name = entity.get("name")
        for value in entity.get("entityValues", []):
            entity_rows.append({
                "Entity_Name": entity_name,
                "Representative": value.get("representative"),
                "Synonyms": ", ".join(value.get("synonyms", [])),
            })
    entity_df = pd.DataFrame(entity_rows)

    return flow_df, intent_df, entity_df

def extract_responses(data):
    """
    각 Flow/Page별로 Response 텍스트를 추출하여 리스트로 반환
    [{Flow, Page, Response Text, ...}]
    """
    rows = []
    for flow in data['context'].get('flows', []):
        flow_name = flow.get('name')
        for page in flow.get('pages', []):
            page_name = page.get('name')
            # Page-level action.responses
            responses = []
            if 'action' in page and 'responses' in page['action']:
                responses.extend(page['action']['responses'])
            # Handler-level action.responses
            for handler in page.get('handlers', []):
                if 'action' in handler and 'responses' in handler['action']:
                    responses.extend(handler['action']['responses'])
            for resp in responses:
                # response는 dict, text는 resp['record']['text'] 또는 resp['text']
                text = None
                if 'record' in resp and resp['record'] and 'text' in resp['record']:
                    text = resp['record']['text']
                elif 'text' in resp:
                    text = resp['text']
                if text:
                    rows.append({
                        'Flow': flow_name,
                        'Page': page_name,
                        'Response Text': text
                    })
    return rows

# 오타 검출(OpenAI)
def check_typo_openai(text):
    """
    OpenAI를 이용해 텍스트에 오타가 있는지 검사 (간단 프롬프트)
    오타가 있으면 True, 없으면 False 반환
    """
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        prompt = f"다음 문장에 맞춤법이나 오타가 있으면 '오타 있음', 없으면 '오타 없음'만 답해줘.\n문장: {text}"
        client = openai.OpenAI(api_key=openai.api_key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "너는 한국어 맞춤법 검사기야."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=10
        )
        result = response.choices[0].message.content.strip()
        return result
    except Exception as e:
        return f"오류: {e}"

def extract_response_texts_by_flow(data):
    """
    각 Flow/Page별로 action.responses의 <p>...</p> 텍스트만 추출하여 반환
    [{Flow, Page, 위치, Handler Type, Condition, Response Type, TemplateId, Response Text}]
    """
    rows = []
    for flow in data['context'].get('flows', []):
        flow_name = flow.get('name')
        for page in flow.get('pages', []):
            page_name = page.get('name')
            # Page-level action.responses
            if 'action' in page and 'responses' in page['action']:
                for resp in page['action']['responses']:
                    text_candidates = []
                    # 1. record.text
                    if 'record' in resp and resp['record'] and 'text' in resp['record']:
                        text_candidates.append(resp['record']['text'])
                    # 2. text
                    if 'text' in resp:
                        text_candidates.append(resp['text'])
                    # 3. MESSAGE 타입의 customPayload.content.item 내부 section/item/text.text
                    template_id = None
                    if resp.get('type') == 'MESSAGE':
                        custom_payload = resp.get('customPayload', {})
                        content = custom_payload.get('content', {})
                        template_id = content.get('templateId') or custom_payload.get('templateId')
                        items = content.get('item', [])
                        for section in items:
                            if isinstance(section, dict) and 'section' in section:
                                section_obj = section['section']
                                section_items = section_obj.get('item', [])
                                for section_item in section_items:
                                    if 'text' in section_item and isinstance(section_item['text'], dict):
                                        t = section_item['text'].get('text')
                                        if t:
                                            text_candidates.append(t)
                    for text in text_candidates:
                        if not text:
                            continue
                        p_texts = re.findall(r'<p>(.*?)</p>', text, re.DOTALL)
                        for p in p_texts:
                            clean_p = p.strip()
                            # <br> 및 <br/> 태그 제거
                            clean_p = re.sub(r'<br\s*/?>', '', clean_p, flags=re.IGNORECASE)
                            if clean_p:  # null/빈값 제외
                                clean_p = html.unescape(clean_p)  # HTML entity decode
                                rows.append({
                                    'Flow': flow_name,
                                    'Page': page_name,
                                    '위치': 'Page',
                                    'Handler Type': '',
                                    'Condition': '',
                                    'Response Type': resp.get('type', ''),
                                    'TemplateId': template_id,
                                    'Response Text': clean_p
                                })
            # Handler-level action.responses
            for handler in page.get('handlers', []):
                handler_type = handler.get('type', '')
                cond = handler.get('conditionStatement', '')
                if 'action' in handler and 'responses' in handler['action']:
                    for resp in handler['action']['responses']:
                        text_candidates = []
                        # 1. record.text
                        if 'record' in resp and resp['record'] and 'text' in resp['record']:
                            text_candidates.append(resp['record']['text'])
                        # 2. text
                        if 'text' in resp:
                            text_candidates.append(resp['text'])
                        # 3. MESSAGE 타입의 customPayload.content.item 내부 section/item/text.text
                        template_id = None
                        if resp.get('type') == 'MESSAGE':
                            custom_payload = resp.get('customPayload', {})
                            content = custom_payload.get('content', {})
                            template_id = content.get('templateId') or custom_payload.get('templateId')
                            items = content.get('item', [])
                            for section in items:
                                if isinstance(section, dict) and 'section' in section:
                                    section_obj = section['section']
                                    section_items = section_obj.get('item', [])
                                    for section_item in section_items:
                                        if 'text' in section_item and isinstance(section_item['text'], dict):
                                            t = section_item['text'].get('text')
                                            if t:
                                                text_candidates.append(t)
                        for text in text_candidates:
                            if not text:
                                continue
                            p_texts = re.findall(r'<p>(.*?)</p>', text, re.DOTALL)
                            for p in p_texts:
                                clean_p = p.strip()
                                # <br> 및 <br/> 태그 제거
                                clean_p = re.sub(r'<br\s*/?>', '', clean_p, flags=re.IGNORECASE)
                                if clean_p:  # null/빈값 제외
                                    clean_p = html.unescape(clean_p)  # HTML entity decode
                                    rows.append({
                                        'Flow': flow_name,
                                        'Page': page_name,
                                        '위치': 'Handler',
                                        'Handler Type': handler_type,
                                        'Condition': cond,
                                        'Response Type': resp.get('type', ''),
                                        'TemplateId': template_id,
                                        'Response Text': clean_p
                                    })
    # null/빈값 row 전체 제외 (혹시라도 남아있을 경우)
    rows = [row for row in rows if row.get('Response Text') not in [None, '', 'null']]
    return rows

# 오타 검출(OpenAI) - Flow 단위

def check_typo_openai_flow(flow_texts):
    """
    한 Flow의 모든 Response Text를 합쳐서 오타 검출(OpenAI) 요청
    오타가 있으면 '오타 있음', 없으면 '오타 없음' 반환
    """
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        joined = '\n'.join(flow_texts)
        prompt = f"다음 여러 문장에 맞춤법이나 오타가 있으면 '오타 있음', 없으면 '오타 없음'만 답해줘.\n문장들:\n{joined}"
        client = openai.OpenAI(api_key=openai.api_key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "너는 한국어 맞춤법 검사기야."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=10
        )
        result = response.choices[0].message.content.strip()
        return result
    except Exception as e:
        return f"오류: {e}"

class TypoCheckResult(BaseModel):
    text: str
    typo: bool
    reason: str = ""

# 오타 검출(OpenAI) - Response별 JSON 결과 반환

def check_typo_openai_responses_json(response_texts):
    """
    여러 Response Text를 받아 각각에 대해 오타 여부를 JSON으로 반환 (OpenAI + Pydantic)
    [{text, typo, reason} ...]
    무의미한 문자열(공백, 특수문자만, 매우 짧은 경우 등)은 OpenAI에 보내지 않고 바로 typo=True 처리
    """
    import re
    api_key = os.getenv("OPENAI_API_KEY")
    client = OpenAI(api_key=api_key)
    class TypoCheckList(BaseModel):
        results: list[TypoCheckResult]
    prompt = (
        "아래 여러 문장 각각에 대해 맞춤법/오타가 있으면 typo=true, 없으면 typo=false로, 이유(reason)와 함께 JSON 배열로 답해줘. "
        "형식: {\"results\":[{\"text\":..., \"typo\":true/false, \"reason\":...}, ...]}\n"
    )
    # 무의미한 문자열 판별 함수
    def is_meaningless(text):
        if not text or not str(text).strip():
            return True
        # 한글 자음/모음만 반복 (예: ㅇㅍㅇㅇㅇ...)
        if re.fullmatch(r'[ㄱ-ㅎㅏ-ㅣ]+', text.strip()):
            return True
        # 특수문자/공백만 (한글,영문,숫자, 완성형 한글 없으면)
        if not re.search(r"[A-Za-z0-9가-힣]", text):
            return True
        # 너무 짧은 경우 (예: 2글자 이하)
        if len(text.strip()) <= 2:
            return True
        return False
    # 분리: 무의미/의미있는 텍스트
    meaningless = [t for t in response_texts if is_meaningless(t)]
    meaningful = [t for t in response_texts if not is_meaningless(t)]
    results = []
    # 무의미한 텍스트는 바로 typo=True 처리
    for t in meaningless:
        results.append(TypoCheckResult(text=t, typo=True, reason="무의미한 문자열(공백/특수문자/너무 짧음)"))
    if meaningful:
        joined = "\n".join(f"- {t}" for t in meaningful)
        user_content = f"문장 목록:\n{joined}"
        response = client.responses.parse(
            model="gpt-4o-2024-08-06",
            input=[
                {"role": "system", "content": "너는 한국어 맞춤법 검사기야."},
                {"role": "user", "content": prompt + user_content},
            ],
            text_format=TypoCheckList,
        )
        results.extend(response.output_parsed.results)
    return results

if menu == "대시보드" and data is not None:
    flows, pages, handlers, variables = analyze_bot_json(data)
    errors = validate_bot_json(data)
    suggestions = suggest_fixes(errors, data)

    tab1, tab2, tab3 = st.tabs([
        "📄 서비스 요약",
        "🛠️ 핸들러/변수 상세",
        "🔎 인텐트/엔티티 요약"
    ])

    # 정확한 Page 수 집계 (모든 Flow의 Page 조합)
    def page_key(page_tuple):
        if isinstance(page_tuple, tuple) and len(page_tuple) == 2:
            flow, page = page_tuple
            if isinstance(page, dict):
                return (flow, page.get('name', str(page)))
            return (flow, str(page))
        return (None, str(page_tuple))
    unique_pages = set(page_key(p) for p in pages)

    with tab1:
        st.markdown("<div class='tab-section-title'><span class='icon'>📄</span> Flow별 서비스 시나리오 요약</div>", unsafe_allow_html=True)
        flow_summaries = summarize_flow_service_natural(data)
        flows_data = data['context']['flows']
        for i, flow in enumerate(flows_data):
            flow_name = flow['name']
            pages_in_flow = flow['pages']
            # page간 이동 해석
            page_links = []
            for page in pages_in_flow:
                for handler in page.get('handlers', []):
                    target = handler.get('transitionTarget', {})
                    if target.get('type') == 'CUSTOM' and target.get('page'):
                        page_links.append((page['name'], target['page']))
            # Graphviz 다이어그램 생성
            graph_lines = [f'digraph "{flow_name}" {{']
            for page in pages_in_flow:
                graph_lines.append(f'    "{page["name"]}";')
            for src, dst in page_links:
                graph_lines.append(f'    "{src}" -> "{dst}";')
            graph_lines.append('}')
            graph_str = '\n'.join(graph_lines)
            st.markdown(f"#### Flow: {flow_name}")
            st.graphviz_chart(graph_str)
            st.markdown(flow_summaries[i])
    with tab2:
        st.markdown("<div class='tab-section-title'><span class='icon'>🛠️</span> 핸들러/변수 상세 요약</div>", unsafe_allow_html=True)
        handler_df, variable_df, variable_usage = get_handler_variable_details(data)
        st.markdown("**[핸들러 요약]**")
        if not handler_df.empty:
            st.dataframe(handler_df, use_container_width=True)
        else:
            st.info("핸들러 정보가 없습니다.")
        st.markdown("**[변수 사용 요약]**")
        if not variable_df.empty:
            st.dataframe(variable_df, use_container_width=True)
            st.markdown("**[변수별 상세 설명]**")
            for var, usages in variable_usage.items():
                st.markdown(f"**변수명: {var}** (총 {len(usages)}회 사용)")
                usage_table = pd.DataFrame(usages)
                st.dataframe(usage_table, use_container_width=True)
                example_pages = usage_table['Page'].unique()
                example_handlers = usage_table['Handler Type'].unique()
                st.caption(f"- 사용 페이지: {', '.join(example_pages)}")
                st.caption(f"- 사용 핸들러: {', '.join(example_handlers)}")
                example_conds = usage_table['Condition'].unique()
                if any(example_conds):
                    st.caption(f"- 사용 조건문 예시: {', '.join([c for c in example_conds if c])}")
                example_values = usage_table['Value'].unique()
                if any(example_values):
                    st.caption(f"- 값 예시: {', '.join([str(v) for v in example_values if v])}")
                st.markdown("---")
        else:
            st.info("변수 정보가 없습니다.")
    with tab3:
        st.markdown("<div class='tab-section-title'><span class='icon'>🔎</span> 인텐트/엔티티 요약 및 오류 검수</div>", unsafe_allow_html=True)
        intent_df, entity_df, intent_err_df, entity_err_df = get_intent_entity_summary(data)
        st.markdown(f"**[Intent 요약 (총 {len(intent_df)}개)]**")
        if not intent_df.empty:
            st.dataframe(intent_df, use_container_width=True)
        else:
            st.info("등록된 Intent가 없습니다.")
        st.markdown(f"**[Entity 요약 (총 {len(entity_df)}개)]**")
        if not entity_df.empty:
            st.dataframe(entity_df, use_container_width=True)
        else:
            st.info("등록된 Entity가 없습니다.")
        st.markdown("**[Intent 오류 검수]**")
        if not intent_err_df.empty:
            st.dataframe(intent_err_df, use_container_width=True)
        else:
            st.info("Intent 오류 없음")
        st.markdown("**[Entity 오류 검수]**")
        if not entity_err_df.empty:
            st.dataframe(entity_err_df, use_container_width=True)
        else:
            st.info("Entity 오류 없음")

    # 오류 유형별 개수 및 비율
    error_types = [err['type'] for err in errors]
    error_type_counts = pd.Series(error_types).value_counts().reset_index()
    error_type_counts.columns = ['오류 유형', '건수']
    total_errors = len(errors)
    error_type_counts['비율(%)'] = (error_type_counts['건수'] / total_errors * 100).round(1)

    # Top N 오류 메시지
    top_n = 5
    top_errors = pd.DataFrame(errors)[:top_n] if errors else pd.DataFrame(columns=['type','message','location'])

    # 최근 오류 위치
    recent_locations = top_errors[['location','type','message']] if not top_errors.empty else pd.DataFrame(columns=['location','type','message'])

    # 카드형 요약
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Flow 수", len(flows))
    col2.metric("Page 수", len(unique_pages))  # 전체 Flow-Page 조합 기준
    col3.metric("핸들러 수", len(handlers))
    col4.metric("변수 수", len(variables))

    st.subheader(":bar_chart: 오류 유형별 분포 (plotly)")
    fig = plot_error_types(errors)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader(":clipboard: 오류 유형별 요약표")
    st.dataframe(error_type_counts, use_container_width=True)

    st.subheader(":trophy: Top 5 오류")
    if not top_errors.empty:
        st.table(top_errors[['type','message','location']])
    else:
        st.info("오류가 없습니다.")

    st.subheader(":round_pushpin: 최근 오류 발생 위치")
    if not recent_locations.empty:
        for idx, row in recent_locations.iterrows():
            st.write(f"- [{row['type']}] {row['location']} : {row['message']}")
    else:
        st.info("최근 오류가 없습니다.")

    st.subheader("리포트 다운로드")
    # 업로드한 파일명에서 확장자 제거
    uploaded_filename = uploaded_file.name if uploaded_file else "uploaded"
    base_filename = os.path.splitext(uploaded_filename)[0]

    # 엑셀 리포트 다운로드 버튼
    excel_filename = f"{base_filename}_bot_report.xlsx"
    if st.button("엑셀 리포트 다운로드"):
        excel_buffer = export_excel(errors, suggestions, filename=excel_filename)
        st.download_button("엑셀 파일 다운로드", excel_buffer, file_name=excel_filename)

    # PDF 리포트 다운로드 버튼
    pdf_filename = f"{base_filename}_bot_report.pdf"
    if st.button("PDF 리포트 다운로드"):
        pdf_buffer = export_pdf(errors, suggestions, filename=pdf_filename)
        st.download_button("PDF 리포트 다운로드", pdf_buffer, file_name=pdf_filename)

if menu == "QA 검수 결과" and data is not None:
    flows, pages, handlers, variables = analyze_bot_json(data)
    errors = validate_bot_json(data)

    # 디자인 업그레이드 CSS (오류 상세 카드/배지/박스)
    st.markdown("""
    <style>
    .flow-section {
        background: #f7f6ff;
        border-radius: 1.5rem;
        box-shadow: 0 2px 12px rgba(108,71,255,0.07);
        margin-bottom: 1.1rem;
        padding: 0.7rem 1.2rem 0.7rem 1.2rem;
        border: 1px solid #e0e0ff;
    }
    .flow-title {
        font-size: 1.25rem;
        font-weight: 900;
        color: #7c3aed;
        margin-bottom: 0.7rem;
        display: flex;
        align-items: center;
        gap: 0.7rem;
    }
    .page-error-table-header {
        display: flex;
        align-items: center;
        font-size: 1.01rem;
        font-weight: 700;
        color: #7c3aed;
        margin-bottom: 0.3rem;
        margin-left: 0.2rem;
    }
    .page-error-table-header .page-header-col {
        flex: 1 1 0;
        min-width: 120px;
    }
    .page-error-table-header .type-header-col {
        flex: 1 1 0;
        text-align: center;
    }
    .page-error-table-header .suggest-header-col {
        flex: 2 1 0;
        text-align: center;
        padding-left: 0.5rem;
        padding-right: 0.5rem;
    }
    .page-error-row {
        display: flex;
        align-items: stretch;
        background: #fff;
        border-radius: 1.1rem;
        box-shadow: 0 1px 4px rgba(108,71,255,0.04);
        margin-bottom: 0.4rem;
        padding: 0.7rem 1.1rem 0.7rem 1.1rem;
        border: 1px solid #ececff;
        width: 100%;
        min-height: 2.2rem;
    }
    .page-name {
        flex: 1 1 0;
        font-weight: 700;
        color: #2d2d3a;
        min-width: 120px;
        margin-right: 0.7rem;
        display: flex;
        align-items: center;
    }
    .error-type-badge {
        flex: 1 1 0;
        text-align: center;
        font-weight: 700;
        padding: 0.2rem 0.8rem;
        border-radius: 1rem;
        font-size: 1.02rem;
        color: #fff;
        margin: 0 auto;
        display: inline-block;
        min-width: 120px;
        max-width: 180px;
    }
    .HandlerMissing { background: #ff5c5c; }
    .ConditionError { background: #ffb300; color: #222; }
    .CustomCheck { background: #6c47ff; }
    .PageLinkError { background: #00b894; }
    .error-suggestion {
        flex: 2 1 0;
        color: #2d5fff;
        font-size: 0.98rem;
        font-weight: 600;
        margin-left: 0.2rem;
        display: flex;
        align-items: center;
        word-break: break-all;
        justify-content: center;
        text-align: center;
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("<div class='tab-section-title'><span class='icon'>📝</span> 오류 상세 및 수정 제안</div>", unsafe_allow_html=True)
    use_openai = st.checkbox("OpenAI 기반 자동 수정 제안 보기", value=False)
    suggestions = suggest_fixes(errors, data, use_openai=use_openai)

    # 오류를 Flow별로 그룹핑
    flow_errors = defaultdict(list)
    for i, err in enumerate(errors):
        flow = err['location'].split('>')[0].strip() if '>' in err['location'] else err['location']
        flow_errors[flow].append((i, err))

    for flow, err_list in flow_errors.items():
        st.markdown(f"<div class='flow-section'>", unsafe_allow_html=True)
        st.markdown(f"<div class='flow-title'>📁 Flow: {flow}</div>", unsafe_allow_html=True)
        # 타이틀 행 추가
        st.markdown("""
            <div class='page-error-table-header'>
                <div class='page-header-col'>Page명</div>
                <div class='type-header-col'>오류유형</div>
                <div class='suggest-header-col'>수정제안</div>
            </div>
        """, unsafe_allow_html=True)
        for i, err in err_list:
            emoji = {
                "HandlerMissing": "🔴",
                "ConditionError": "🟡",
                "PageLinkError": "🟢",
                "CustomCheck": "🟣"
            }.get(err['type'], "⚠️")
            badge_class = f"error-type-badge {err['type']}"
            page_name = err['location'].split('>')[1].strip() if '>' in err['location'] else err['location']
            st.markdown(f"""
                <div class='page-error-row'>
                    <span class='page-name'>{page_name}</span>
                    <span class='{badge_class}'>{emoji} {err['type']}</span>
                    <span class='error-suggestion'>{err['suggestion'] or ''}</span>
                </div>
            """, unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    # 자동 수정 제안 요약 - Page별 표
    summary_rows = []
    for err, sug in zip(errors, suggestions):
        page = err['location']
        summary_rows.append({
            "Page": page,
            "오류 유형": err['type'],
            "오류 메시지": err['message'],
            "수정 제안": err['suggestion'] or sug
        })
    import pandas as pd
    summary_df = pd.DataFrame(summary_rows)
    st.markdown("<div class='tab-section-title'><span class='icon'>📋</span> 자동 수정 제안 요약 (Page별)</div>", unsafe_allow_html=True)
    st.dataframe(summary_df, use_container_width=True)
    # 엑셀 다운로드 버튼 추가
    import io
    def to_excel_bytes_summary(df):
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False)
        output.seek(0)
        return output
    excel_bytes_summary = to_excel_bytes_summary(summary_df)
    # 업로드 파일명에서 확장자 제거
    uploaded_filename = uploaded_file.name if uploaded_file else "uploaded"
    base_filename = os.path.splitext(uploaded_filename)[0]
    summary_excel_filename = f"{base_filename}_summary.xlsx"
    st.download_button(
        label="자동 수정 제안 요약 엑셀 다운로드",
        data=excel_bytes_summary,
        file_name=summary_excel_filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

if menu == "JSON 구조 파악" and data is not None:
    flow_df, intent_df, entity_df = parse_bot_structure_from_data(data)
    st.subheader("Flow/Page/Handler 구조")
    for flow_name in flow_df["Flow"].unique():
        st.markdown(f"### 🗂️ Flow: {flow_name}")
        flow_part = flow_df[flow_df["Flow"] == flow_name].copy()
        show_cols = ["Page", "Handler_ID", "Handler_Type", "Handler_Condition", "Handler_Action", "Handler_TransitionTarget", "Page_Action", "Page_Parameters", "Handler_ParameterPresets"]
        st.dataframe(flow_part[show_cols].reset_index(drop=True), use_container_width=True)
    st.subheader("Intent 정보")
    st.dataframe(intent_df, use_container_width=True)
    st.subheader("Entity 정보")
    st.dataframe(entity_df, use_container_width=True)
    import io
    if st.button("엑셀 파일로 변환"):
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            flow_df.to_excel(writer, sheet_name="Flow_Page_Handler", index=False)
            intent_df.to_excel(writer, sheet_name="Intent", index=False)
            entity_df.to_excel(writer, sheet_name="Entity", index=False)
        output.seek(0)
        st.success("엑셀 파일로 변환이 완료되었습니다!")
        st.download_button(
            label="엑셀 파일 다운로드",
            data=output,
            file_name="bot_structure.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

if menu == "Response Text 검출" and data is not None:
    st.write("각 Flow/Page별 Response 텍스트(<p>...</p>)를 추출하여 표로 보여주고, 각 Response별 오타를 OpenAI로 검사합니다.")
    rows = extract_response_texts_by_flow(data)
    rows = [row for row in rows if row.get('Response Text') not in [None, '', 'null']]
    if not rows:
        st.info("Response 텍스트가 없습니다.")
    else:
        import pandas as pd
        df = pd.DataFrame(rows)
        typo_results = {}
        if st.button("Response Text 오타 검수 실행(by OpenAI, JSON, 병렬)"):
            flow_groups = list(df.groupby('Flow'))
            total = len(flow_groups)
            progress = st.progress(0, text="오타 분석 진행 중...")
            start_time = time.time()
            def typo_check_for_flow(flow, group):
                texts = group['Response Text'].tolist()
                return flow, check_typo_openai_responses_json(texts)
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(typo_check_for_flow, flow, group) for flow, group in flow_groups]
                for idx, future in enumerate(as_completed(futures)):
                    flow, results = future.result()
                    for r in results:
                        typo_results[(flow, r.text)] = (r.typo, r.reason)
                    progress.progress((idx + 1) / total, text=f"오타 분석: {idx + 1}/{total} Flow 완료")
            st.success(f"Response Text 오타 검출이 완료되었습니다! (총 소요: {time.time() - start_time:.1f}s)")
        # 표에 오타 결과 컬럼 추가
        def get_typo_result(row):
            key = (row['Flow'], row['Response Text'])
            if key in typo_results:
                typo, reason = typo_results[key]
                return f"오타 있음: {reason}" if typo else "오타 없음"
            return '(검사 전)'
        df['오타 검출 결과(Response별)'] = df.apply(get_typo_result, axis=1)
        # Handler_ID 컬럼이 있으면 모두 문자열로 변환 (Arrow 오류 방지)
        if 'Handler_ID' in df.columns:
            df['Handler_ID'] = df['Handler_ID'].astype(str)
        st.dataframe(df, use_container_width=True)
        # 엑셀 다운로드 버튼
        def to_excel_bytes(df):
            output = BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                df.to_excel(writer, index=False)
            output.seek(0)
            return output
        excel_bytes = to_excel_bytes(df)
        st.download_button(
            label="엑셀로 다운하기",
            data=excel_bytes,
            file_name="response_typo_check.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
