#!/usr/bin/env python3
"""PostToolUse nudge: write-batch (0036) — правило в точке действия + стоп-кран.

Чекпоинт обязан выписывать все правки памяти ОДНИМ сообщением (0036), но текст
скилла давит не всегда: live write-фаза дробилась на 4 захода. Этот хук — тот
самый «механический сигнал» из вердикта 0036: напоминание вспыхивает у первой
записи, стоп-кран — у второй. Цель: срезать частоту дробления и поставить
потолок урона в 2 захода.

Заход = одно assistant-сообщение с Edit/Write в память (`.memory/wiki/` или
`.memory/STATE.md`; brainstorms/specs/research НЕ считаются — grill-me пишет
их помногу через сообщения by design). Параллельные вызовы одного сообщения
делят message.id и считаются ОДНИМ заходом; счётчик обнуляется на настоящем
промпте пользователя (tool_result-записи промптом не считаются, sidechain —
не в счёт). Каждый текст уходит в LLM один раз на заход (dedup через
state-файл в %TEMP% по session_id).

Fail-OPEN: любая ошибка -> молчание; нудж не имеет права ломать запись.
Self-test: python write_batch_nudge.py --selfcheck.
"""
import json
import os
import sys
import tempfile

from consult_gate import _is_prompt  # same-dir reuse: "real prompt" boundary

# ── тексты, уходящие в LLM (правь смело — всё поведение целиком в них) ─────────
MSG_ROUND_1 = (
    "WRITE-BATCH (0036): пошла запись в память. ВСЕ оставшиеся правки этого хода — "
    "wiki-Edit'ы, Write STATE, log-append — добавляй в ЭТО ЖЕ сообщение, одним "
    "заходом. Сверься с описью: не заканчивай сообщение, пока не выписано всё."
)
MSG_ROUND_2 = (
    "WRITE-BATCH СТОП-КРАН (0036): это ВТОРОЙ заход записи за ход — потолок. "
    "Перечисли про себя все ещё не записанные файлы из описи и выпиши их ВСЕ "
    "в этом сообщении. Третьего захода быть не должно."
)
MSG_ROUND_OVER = (
    "WRITE-BATCH: потолок 2 заходов ПРЕВЫШЕН (заход №{n}) — это то самое дробление, "
    "которое 0036 запрещает. Прямо сейчас, этим сообщением, выпиши ВСЕ оставшиеся "
    "правки памяти разом и больше к записи не возвращайся."
)

_WIKI_MARK = ".memory/wiki/"
_STATE_MARK = ".memory/STATE.md"


def _is_mem_write(block):
    """tool_use block, пишущий durable-память (wiki-страница / STATE)."""
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return False
    if block.get("name") not in ("Edit", "Write"):
        return False
    fp = ((block.get("input") or {}).get("file_path") or "").replace("\\", "/")
    return _WIKI_MARK in fp or fp.endswith(_STATE_MARK)


def memory_write_round(entries):
    """(round_n, message_id) для ПОСЛЕДНЕГО сообщения с memory-write.

    round_n = число РАЗЛИЧНЫХ assistant-сообщений с memory-write после последнего
    настоящего промпта (0 = ни одного). Сообщение = message.id (fallback uuid).
    """
    start = 0
    for i, o in enumerate(entries):
        if _is_prompt(o):
            start = i + 1
    ids = []
    for o in entries[start:]:
        if o.get("type") != "assistant" or o.get("isSidechain"):
            continue
        m = o.get("message") or {}
        if any(_is_mem_write(b) for b in m.get("content") or []):
            mid = m.get("id") or o.get("uuid")
            if mid and mid not in ids:
                ids.append(mid)
    return (len(ids), ids[-1] if ids else None)


def pick_message(n):
    if n <= 0:
        return None
    if n == 1:
        return MSG_ROUND_1
    if n == 2:
        return MSG_ROUND_2
    return MSG_ROUND_OVER.format(n=n)


def _state_path(session_id):
    sid = "".join(c for c in str(session_id) if c.isalnum() or c == "-") or "unknown"
    return os.path.join(tempfile.gettempdir(), f"write-batch-{sid}.json")


def main():
    try:
        data = json.load(sys.stdin)
        probe = {"type": "tool_use", "name": data.get("tool_name"),
                 "input": data.get("tool_input") or {}}
        if not _is_mem_write(probe):
            return
        entries = []
        with open(data["transcript_path"], encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
        n, mid = memory_write_round(entries)
        msg = pick_message(n)
        if not msg or not mid:
            return
        sp = _state_path(data.get("session_id"))
        try:
            with open(sp, encoding="utf-8") as f:
                if json.load(f).get("last_id") == mid:
                    return  # это сообщение уже отнуджено (параллельный вызов)
        except Exception:
            pass
        with open(sp, "w", encoding="utf-8") as f:
            json.dump({"last_id": mid}, f)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": msg}},
            ensure_ascii=False))
    except Exception:
        return  # fail-OPEN: нудж не имеет права ломать запись


# --- selfcheck ---------------------------------------------------------------

def _u(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tr():
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x"}]}}


def _aw(mid, path, name="Edit", side=False):
    return {"type": "assistant", "isSidechain": side, "uuid": "u-" + mid,
            "message": {"id": mid, "role": "assistant", "content": [
                {"type": "tool_use", "name": name, "input": {"file_path": path}}]}}


def _ao(mid):
    return {"type": "assistant", "uuid": "u-" + mid,
            "message": {"id": mid, "role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}}


def selfcheck():
    W = "D:\\p\\.memory\\wiki\\concepts\\a.md"   # backslash path — must still match
    S = "D:/p/.memory/STATE.md"
    C = "D:/p/code/m.py"
    B = "D:/p/.memory/brainstorms/x.md"

    assert memory_write_round([_u("q")]) == (0, None), "no writes -> 0"
    assert memory_write_round([_u("q"), _aw("m1", W)]) == (1, "m1"), "one msg -> round 1"
    two_par = {"type": "assistant", "uuid": "u-m1", "message": {
        "id": "m1", "role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": W}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": S}}]}}
    assert memory_write_round([_u("q"), two_par]) == (1, "m1"), "parallel same id -> 1"
    assert memory_write_round([_u("q"), _aw("m1", W), _tr(), _aw("m2", S)]) == (2, "m2"), \
        "second message -> round 2 (tool_result no reset)"
    assert memory_write_round([_u("q"), _aw("m1", W), _ao("mx"), _aw("m2", W),
                               _aw("m3", S)]) == (3, "m3"), \
        "non-write assistant msg does not reset the count"
    assert memory_write_round([_u("q"), _aw("m1", W), _u("q2"), _aw("m2", W)]) == (1, "m2"), \
        "real user prompt resets"
    assert memory_write_round([_u("q"), _aw("m1", C)]) == (0, None), "code write ignored"
    assert memory_write_round([_u("q"), _aw("m1", B)]) == (0, None), "brainstorm ignored"
    assert memory_write_round([_u("q"), _aw("m1", W, side=True)]) == (0, None), \
        "sidechain ignored"
    assert memory_write_round([_u("q"), _aw("m1", W, name="Read")]) == (0, None), \
        "Read is not a write"

    assert pick_message(0) is None
    assert pick_message(1) is MSG_ROUND_1
    assert pick_message(2) is MSG_ROUND_2
    assert "№3" in pick_message(3), pick_message(3)

    assert _is_mem_write({"type": "tool_use", "name": "Write", "input": {"file_path": S}})
    assert not _is_mem_write({"type": "tool_use", "name": "Write", "input": {}})
    assert not _is_mem_write({"type": "text"})
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        main()
