"""쇼핑 리스트 — Streamlit 버전.

웹 버전(index.html / app.js)과 같은 기능을 Streamlit으로 옮긴 것이다.
브라우저 localStorage 대신 앱 옆의 JSON 파일에 저장한다.

실행: streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

import streamlit as st

DATA_FILE = Path(__file__).with_name("shopping_list.json")
MAX_NAME_LENGTH = 50
UNDO_SECONDS = 6
SYNC_SECONDS = float(os.environ.get("SHOPPING_SYNC_SECONDS", "2")) or None  # 다른 탭에서 바꾼 내용을 가져오는 주기 (0이면 끔)

# 아이템 형식: {"id": str, "name": str, "checked": bool, "createdAt": int(ms)}


# ---------- 글자 수 ----------

def char_length(text: str) -> int:
    """화면에 보이는 글자 단위로 센다 (이모지·결합 문자를 1글자로)."""
    count = 0
    after_zwj = False
    for ch in text:
        code = ord(ch)
        if ch == "‍":  # 이모지 결합(ZWJ): 다음 글자를 앞 글자에 붙인다
            after_zwj = True
            continue
        if after_zwj:
            after_zwj = False
            continue
        if (
            unicodedata.combining(ch)
            or 0xFE00 <= code <= 0xFE0F  # 이체자 선택자
            or 0x1F3FB <= code <= 0x1F3FF  # 피부색 수정자
        ):
            continue
        count += 1
    return count


def normalize_name(name: str) -> str | None:
    """앞뒤 공백을 제거하고, 1~50자가 아니면 None을 반환한다."""
    trimmed = str(name).strip()
    if not 0 < char_length(trimmed) <= MAX_NAME_LENGTH:
        return None
    return trimmed


# ---------- 저장소 ----------

def is_valid_item(item) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("checked"), bool)
        and isinstance(item.get("createdAt"), (int, float))
        and not isinstance(item.get("createdAt"), bool)
    )


def load_items() -> list[dict]:
    """파일에서 목록을 읽는다. 없거나 깨졌으면 빈 목록, 형식이 틀린 항목은 버린다."""
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if is_valid_item(item)] if isinstance(data, list) else []


def save_items(items: list[dict]) -> bool:
    """임시 파일에 쓴 뒤 교체해서, 저장 도중 끊겨도 파일이 깨지지 않게 한다."""
    try:
        fd, tmp = tempfile.mkstemp(dir=DATA_FILE.parent, prefix=".shopping-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
        return True
    except OSError:
        st.session_state.save_error = True
        return False


def mutate(change) -> None:
    """항상 파일을 새로 읽고 → 바꾸고 → 저장한다. 다른 탭의 변경을 덮어쓰지 않기 위함."""
    items = load_items()
    change(items)
    if save_items(items):
        st.session_state.save_error = False


# ---------- 동작 ----------

DUPLICATE_WINDOW = 1.0  # 같은 이름이 이 시간(초) 안에 다시 제출되면 연타로 보고 무시


def add_item() -> None:
    # 추가 폼은 clear_on_submit=True라 브라우저가 제출 즉시 입력창을 비운다.
    # 여기서 new_item을 ""로 다시 설정하면 그 사이 사용자가 이어서 입력한 글자까지 지워지므로 건드리지 않는다.
    raw = st.session_state.new_item
    if raw.strip() == "":
        return
    name = normalize_name(raw)
    if name is None:
        st.session_state.add_error = f"{MAX_NAME_LENGTH}자까지 입력할 수 있어요. (지금 {char_length(raw.strip())}자)"
        # 너무 긴 입력은 지우지 않고 돌려놓아 고칠 수 있게 한다
        st.session_state.new_item = raw
        return

    last = st.session_state.get("last_added")
    if last and last[0] == name and time.monotonic() - last[1] < DUPLICATE_WINDOW:
        return
    st.session_state.last_added = (name, time.monotonic())

    def change(items):
        items.append({
            "id": str(uuid.uuid4()),
            "name": name,
            "checked": False,
            "createdAt": int(time.time() * 1000),
        })

    mutate(change)
    st.session_state.add_error = None


def toggle_item(item_id: str) -> None:
    checked = st.session_state[f"chk_{item_id}"]

    def change(items):
        for item in items:
            if item["id"] == item_id:
                item["checked"] = checked

    mutate(change)


def remove_items(predicate, label) -> None:
    """조건에 맞는 아이템을 지우고, 되돌릴 수 있게 원래 위치와 함께 기억한다."""
    removed = []

    def change(items):
        for index, item in enumerate(items):
            if predicate(item):
                removed.append((index, item))
        items[:] = [item for item in items if not predicate(item)]

    mutate(change)
    if removed:
        st.session_state.last_deleted = {
            "removed": removed,
            "message": label(removed),
            "at": time.time(),
        }


def delete_item(item_id: str) -> None:
    remove_items(lambda item: item["id"] == item_id, lambda r: f"'{r[0][1]['name']}' 삭제됨")


def clear_checked() -> None:
    remove_items(lambda item: item["checked"], lambda r: f"완료 항목 {len(r)}개 삭제됨")


def undo_delete() -> None:
    deleted = st.session_state.pop("last_deleted", None)
    if not deleted:
        return

    def change(items):
        existing = {item["id"] for item in items}
        for index, item in deleted["removed"]:
            if item["id"] not in existing:
                items.insert(min(index, len(items)), item)

    mutate(change)


def start_edit(item_id: str) -> None:
    st.session_state.editing_id = item_id
    st.session_state.edit_error = None
    # 편집 중에는 자동 새로고침을 꺼야 하므로 앱 전체를 다시 실행한다
    st.session_state.need_full_rerun = True


def finish_edit(item_id: str, save: bool) -> None:
    if save:
        raw = st.session_state.get(f"edit_{item_id}", "")
        name = normalize_name(raw)
        if name is None and raw.strip() != "":
            # 50자 초과: 편집 모드를 유지하고 알려준다 (빈 값은 원래 이름 유지로 처리)
            st.session_state.edit_error = f"{MAX_NAME_LENGTH}자까지 입력할 수 있어요. (지금 {char_length(raw.strip())}자)"
            return
        if name is not None:
            def change(items):
                for item in items:
                    if item["id"] == item_id:
                        item["name"] = name

            mutate(change)
    st.session_state.editing_id = None
    st.session_state.edit_error = None
    st.session_state.need_full_rerun = True


# ---------- 화면 ----------

MARKDOWN_SPECIALS = set("\\`*_{}[]()#+-.!|~<>:$")


def escape_markdown(text: str) -> str:
    """아이템 이름이 마크다운·색상 문법으로 해석되지 않게 한다. (화면에만 보이는 글에 사용)"""
    return "".join("\\" + ch if ch in MARKDOWN_SPECIALS else ch for ch in text)


def escape_label(text: str) -> str:
    """위젯 라벨용 이스케이프. 실제로 서식이 되는 경우에만 '\\'를 붙인다.

    Streamlit은 위젯 라벨 원문을 그대로 aria-label에 넣어서, 이스케이프한 '\\'까지
    화면 읽기 프로그램이 읽는다. 그래서 '우유 1L', '계란 30-1' 같은 평범한 이름은 손대지 않는다.
    """
    out = []
    for ch in text:
        paired = ch in "*_~`$" and text.count(ch) >= 2  # 강조·취소선·코드·수식은 짝이 있어야 서식이 됨
        bracket = ch in "[]" and "[" in text and "]" in text  # 링크
        out.append("\\" + ch if ch == "\\" or paired or bracket else ch)
    s = "".join(out)
    s = re.sub(r"<(?=[A-Za-z/!?])", r"\\<", s)  # HTML 태그·자동 링크
    s = re.sub(r"&(?=#?\w+;)", r"\\&", s)  # HTML 엔티티
    s = re.sub(r":(?=[A-Za-z0-9_+-]+\\?\[)", r"\\:", s)  # :red[...] 색상 (대괄호는 위에서 이미 이스케이프됨)
    s = re.sub(r":([A-Za-z0-9_+-]+):", r"\\:\1\\:", s)  # :apple: 이모지 코드는 양쪽 콜론 모두
    # 제목·인용·목록 기호 (번호 목록 "1."은 숫자가 아니라 마침표 앞에 '\\'를 붙여야 한다)
    s = re.sub(r"^(\s*)([#>+*-]|\d+[.)])(?=\s|$)", lambda m: m.group(1) + m.group(2)[:-1] + "\\" + m.group(2)[-1], s)
    return s


CSS = """
<style>
  .block-container { max-width: 560px; padding-top: 2.5rem; }
  /* 좁은 화면에서도 체크박스 | 수정 | 삭제를 한 줄로 유지 */
  [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 0.25rem; }
  [data-testid="stColumn"] { min-width: 0 !important; }
  [data-testid="stColumn"]:not(:first-child) { flex: 0 0 auto !important; width: auto !important; }
  /* 편집 폼의 저장·취소 버튼은 같은 너비로 */
  [class*="st-key-editbox_"] [data-testid="stColumn"] { flex: 1 1 0 !important; }
  /* 긴 이름은 "…"로 자르지 않고 줄바꿈 (Streamlit 기본값은 한 줄 말줄임) */
  [data-testid="stCheckbox"] [data-testid="stMarkdownContainer"],
  [data-testid="stCheckbox"] [data-testid="stMarkdownContainer"] p {
    white-space: normal !important; overflow: visible !important; text-overflow: clip !important;
    overflow-wrap: anywhere; word-break: keep-all;
  }
  /* 체크된 아이템: 취소선 + 흐린 색 */
  [class*="st-key-row_done_"] [data-testid="stCheckbox"] p { text-decoration: line-through; color: #5b6760; }
  /* 수정·삭제 버튼: 라벨("우유 수정")은 화면 읽기 프로그램용, 화면에는 짧은 글자만 */
  [class*="st-key-editbtn_"] button [data-testid="stMarkdownContainer"],
  [class*="st-key-delbtn_"] button [data-testid="stMarkdownContainer"],
  [class*="st-key-editbtn_"] button p,
  [class*="st-key-delbtn_"] button p { font-size: 0 !important; line-height: 0 !important; margin: 0 !important; }
  [class*="st-key-editbtn_"] button, [class*="st-key-delbtn_"] button { white-space: nowrap; padding: 0 0.5rem; }
  [class*="st-key-editbtn_"] button::after { content: "수정"; content: "수정" / ""; font-size: 0.9375rem; line-height: 1.6; }
  [class*="st-key-delbtn_"] button::after { content: "삭제"; content: "삭제" / ""; font-size: 0.9375rem; line-height: 1.6; color: #c92a2a; }
  /* 터치 영역 44px 이상 */
  .stButton button, [data-testid="stFormSubmitButton"] button { min-height: 44px; min-width: 44px; }
  [data-testid="stCheckbox"] label { min-height: 44px; align-items: center; }
  [data-testid="stTextInputRootElement"] input { min-height: 44px; }
  /* 입력창 안의 영어 안내("Press Enter to submit form")는 placeholder와 겹쳐서 숨김 */
  [data-testid="InputInstructions"] { display: none; }
  /* 좁은 화면에서는 제목을 한 줄에 */
  @media (max-width: 480px) {
    [data-testid="stHeading"] h1 { font-size: 1.75rem; }
  }
</style>
"""


def render_add_form() -> None:
    with st.form("add_form", border=False, clear_on_submit=True):
        cols = st.columns([5, 1], vertical_alignment="bottom")
        cols[0].text_input(
            "아이템 이름",
            key="new_item",
            placeholder="아이템을 입력하세요...",
            label_visibility="collapsed",
        )
        cols[1].form_submit_button("추가", type="primary", on_click=add_item, width="stretch")
    if st.session_state.get("add_error"):
        st.warning(st.session_state.add_error, icon="⚠️")


def render_item(item: dict) -> None:
    item_id = item["id"]

    if st.session_state.get("editing_id") == item_id:
        with st.container(key=f"editbox_{item_id}"), st.form(f"edit_form_{item_id}", border=True):
            st.text_input(
                f"{item['name']} 이름 수정",
                value=item["name"],
                key=f"edit_{item_id}",
                label_visibility="collapsed",
            )
            if st.session_state.get("edit_error"):
                st.warning(st.session_state.edit_error, icon="⚠️")
            cols = st.columns(2)
            cols[0].form_submit_button(
                "저장", type="primary", on_click=finish_edit, args=(item_id, True), width="stretch"
            )
            cols[1].form_submit_button(
                "취소", on_click=finish_edit, args=(item_id, False), width="stretch"
            )
        return

    # 다른 탭에서 바뀐 체크 상태를 위젯에 반영 (위젯을 만들기 전에만 설정 가능)
    st.session_state[f"chk_{item_id}"] = item["checked"]
    label = escape_label(item["name"])

    # 체크된 줄의 취소선·회색은 마크다운이 아니라 CSS로 그린다 (라벨 원문이 aria-label이 되므로)
    state = "done" if item["checked"] else "todo"
    with st.container(key=f"row_{state}_{item_id}"):
        cols = st.columns([6, 1, 1], vertical_alignment="center")
        cols[0].checkbox(label, key=f"chk_{item_id}", on_change=toggle_item, args=(item_id,))
        # 라벨은 "우유 수정"처럼 아이템 이름을 포함해 화면 읽기 프로그램이 구분할 수 있게 하고,
        # 화면에는 CSS로 "수정"/"삭제"만 보인다
        cols[1].button(f"{label} 수정", key=f"editbtn_{item_id}", type="tertiary",
                       on_click=start_edit, args=(item_id,))
        cols[2].button(f"{label} 삭제", key=f"delbtn_{item_id}", type="tertiary",
                       on_click=delete_item, args=(item_id,))


def render_undo_bar() -> None:
    deleted = st.session_state.get("last_deleted")
    if not deleted:
        return
    if time.time() - deleted["at"] > UNDO_SECONDS:
        st.session_state.pop("last_deleted", None)
        return
    with st.container(border=True):
        cols = st.columns([4, 1], vertical_alignment="center")
        cols[0].write(escape_markdown(deleted["message"]))
        cols[1].button("되돌리기", key="undo", on_click=undo_delete, type="primary")


def render_list() -> None:
    # 콜백에서는 st.rerun()을 부를 수 없어서, 편집 시작/종료 후 여기서 앱 전체를 다시 실행한다
    if st.session_state.pop("need_full_rerun", False):
        st.rerun(scope="app")

    items = load_items()

    if not items:
        st.markdown(
            "<p style='text-align:center; color:gray; padding:2rem 0;'>"
            "쇼핑 리스트가 비어 있어요. 아이템을 추가해 보세요.</p>",
            unsafe_allow_html=True,
        )
    else:
        with st.container(border=True):
            for item in items:
                render_item(item)

    checked_count = sum(item["checked"] for item in items)
    cols = st.columns([3, 2], vertical_alignment="center")
    cols[0].caption(f"전체 {len(items)}개 · 완료 {checked_count}개")
    cols[1].button(
        "완료 항목 삭제",
        on_click=clear_checked,
        disabled=checked_count == 0,
        width="stretch",
    )

    render_undo_bar()

    if st.session_state.get("save_error"):
        st.error("저장하지 못했어요. 저장 공간이나 파일 권한을 확인해 주세요. 방금 변경한 내용이 사라질 수 있어요.", icon="🚫")


def main() -> None:
    st.set_page_config(page_title="쇼핑 리스트", page_icon="🛒", layout="centered")
    st.html(CSS)
    st.title("🛒 쇼핑 리스트")

    render_add_form()

    # 편집 중이 아닐 때만 주기적으로 다시 읽어서 다른 탭의 변경을 보여준다
    editing = st.session_state.get("editing_id") is not None
    st.fragment(render_list, run_every=None if editing else SYNC_SECONDS)()


main()
