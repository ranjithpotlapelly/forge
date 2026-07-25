"""Phase 21 acceptance check: hold two separate conversations, restart the
app (two genuinely separate OS subprocesses, per this session's established
pattern for proving state survives a process dying -- see
app/interrupt_resume_smoke_test.py), and confirm both appear in /history's
data source and resume correctly with their context intact.

This tests the data layer app/chainlit_app.py's /history command and
resume_thread/delete_thread action callbacks sit on top of -- not those
callbacks themselves, which need a live Chainlit session (cl.Message/
cl.AskActionMessage require one) that only `chainlit run` + a browser
provides. What's verified here is exactly what those callbacks would read:
core.run_history.RunHistory.list_threads()/list_runs_for_thread()/
list_steps(), and LangGraphEngine.resume_task()/delete_thread().

Conversation 1 (Q&A): real retriever.search() calls (fast, no LLM) for both
an answered and a declined turn; the answer text itself is a short
synthetic stand-in rather than a real ~2-3min LLM generation -- what's under
test here is whether the recorded (question, answer) round-trips correctly
through run_history's JSON detail column, not LLM output quality (already
covered by Phase 16's own smoke tests). Conversation 2 (task): a real task
against the throwaway forge-test-repo (same override every other task-flow
smoke test uses), paused at the plan-approval interrupt -- this one has to
be real, since "resume a task paused mid-approval" is exactly the novel
thing being proven.

Uses the real, default checkpoint path (.data/checkpoints.db), the same one
app/chainlit_app.py uses, since the point is to prove this survives an
actual app restart -- cleaned up via delete_thread() at the end.

Run from the repo root:  python -m app.history_ui_smoke_test
"""
from __future__ import annotations
import json
import subprocess
import sys
import uuid
from pathlib import Path
from app.config_loader import load_config
from app.wiring import build_engine, build_run_history

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_REPO = r"C:\Users\Ranjith\AI-Space\forge-test-repo"
TASK = "Add a short comment above the greet function"

FIRST_QUESTION = "How does the human approval gate for a tool call work?"
FIRST_ANSWER = (
    "Tools marked requires_approval=True go through product/approval.py's "
    "run_tool(), which calls the supplied approve() callback and raises "
    "ApprovalDenied if it returns false."
)
SECOND_QUESTION = "What's the weather like today?"
DECLINE_ANSWER = "I don't have enough indexed context to answer that."

# A standalone `python -c` script -- genuinely independent of this file's own
# process, no shared in-memory state. Args: qa_thread_id, task_thread_id, task, task_repo.
_START_SCRIPT = f"""
import sys, json
from app.config_loader import load_config
from app.wiring import build_engine, build_retriever, build_run_history

cfg = load_config()
cfg["forge"]["repo_path"] = sys.argv[4]
retriever = build_retriever(cfg)
history = build_run_history(cfg)
qa_thread_id, task_thread_id, task = sys.argv[1], sys.argv[2], sys.argv[3]

# --- conversation 1: two Q&A turns sharing one thread_id, like one Chainlit session ---
run1 = history.start_run(qa_thread_id, "qa", {FIRST_QUESTION!r})
chunks1 = retriever.search({FIRST_QUESTION!r}, k=8)
history.record_step(run1, "retrieve", "ok", json.dumps({{"chunks": len(chunks1)}}), 5.0)
history.record_step(run1, "answer", "ok", json.dumps({{"answer": {FIRST_ANSWER!r}, "citations": len(chunks1)}}), 10.0)
history.finish_run(run1, "completed")

run2 = history.start_run(qa_thread_id, "qa", {SECOND_QUESTION!r})
chunks2 = retriever.search({SECOND_QUESTION!r}, k=8)
history.record_step(run2, "retrieve", "ok", json.dumps({{"chunks": len(chunks2)}}), 5.0)
history.record_step(run2, "decline", "ok", json.dumps({{"answer": {DECLINE_ANSWER!r}}}), None)
history.finish_run(run2, "completed")
print(f"QA_RECORDED runs={{run1[:8]}},{{run2[:8]}}")

# --- conversation 2: a real task, paused at the plan-approval interrupt ---
engine = build_engine(cfg, retriever=retriever)
result = engine.run_task(task, thread_id=task_thread_id, approve=None, approve_pr=None)
if not result.get("__interrupt__"):
    print("FAIL: expected the task to pause at the plan-approval interrupt")
    sys.exit(1)
print("TASK_PAUSED")
"""

def main() -> int:
    cfg = load_config()
    history = build_run_history(cfg)
    qa_thread_id = f"history-smoke-qa-{uuid.uuid4()}"
    task_thread_id = f"history-smoke-task-{uuid.uuid4()}"

    # --- process 1: hold both conversations, then exit ("restart the app") ---
    print(f"[..] process 1: qa_thread={qa_thread_id}, task_thread={task_thread_id}")
    proc1 = subprocess.run(
        [sys.executable, "-c", _START_SCRIPT, qa_thread_id, task_thread_id, TASK, TASK_REPO],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
    )
    print(proc1.stdout)
    if proc1.stderr.strip():
        print("--- process 1 stderr ---\n" + proc1.stderr)
    if proc1.returncode != 0 or "TASK_PAUSED" not in proc1.stdout or "QA_RECORDED" not in proc1.stdout:
        print("[!!] process 1 did not set up both conversations as expected")
        return 1
    print("[ok] process 1 held two conversations, then exited")

    # --- process 2 (this process, but a fresh RunHistory/engine -- exactly
    # what app/chainlit_app.py builds on its own next startup) ------------
    threads = {t["thread_id"]: t for t in history.list_threads(limit=50)}

    if qa_thread_id not in threads:
        print("[!!] QA thread missing from /history's data source after restart")
        return 1
    qa_thread = threads[qa_thread_id]
    if qa_thread["kind"] != "qa" or qa_thread["run_count"] != 2:
        print(f"[!!] expected kind=qa, run_count=2, got {qa_thread}")
        return 1
    if qa_thread["title"] != FIRST_QUESTION:
        print(f"[!!] expected title to be the FIRST question, got {qa_thread['title']!r}")
        return 1
    print(f"[ok] QA thread appears after restart: {qa_thread['title']!r}, {qa_thread['run_count']} run(s)")

    if task_thread_id not in threads:
        print("[!!] task thread missing from /history's data source after restart")
        return 1
    task_thread = threads[task_thread_id]
    if task_thread["kind"] != "task" or task_thread["status"] != "running":
        print(f"[!!] expected kind=task, status=running (paused), got {task_thread}")
        return 1
    print(f"[ok] task thread appears after restart, still paused: {task_thread['title']!r}")

    # --- reconstruct the QA transcript exactly as _replay_qa_transcript() would ---
    runs = history.list_runs_for_thread(qa_thread_id)
    if len(runs) != 2:
        print(f"[!!] expected 2 runs for the QA thread, got {len(runs)}")
        return 1
    transcript = []
    for run in runs:
        steps = history.list_steps(run["id"])
        reply_step = next((s for s in steps if s["node"] in ("answer", "decline")), None)
        answer = json.loads(reply_step["detail"])["answer"] if reply_step and reply_step["detail"] else None
        transcript.append((run["task_text"], answer))
    expected = [(FIRST_QUESTION, FIRST_ANSWER), (SECOND_QUESTION, DECLINE_ANSWER)]
    if transcript != expected:
        print(f"[!!] reconstructed transcript doesn't match what was recorded:\n  got={transcript}\n  want={expected}")
        return 1
    print("[ok] QA transcript reconstructed correctly from run_history alone, full context intact:")
    for q, a in transcript:
        print(f"       Q: {q}\n       A: {a}")

    # --- resume the paused task from a FRESH engine, in THIS process ------
    from product.approval import PlanDecision
    task_cfg = load_config()
    task_cfg["forge"]["repo_path"] = TASK_REPO
    engine = build_engine(task_cfg)
    result = engine.resume_task(
        task_thread_id, approve=lambda plan: PlanDecision("approve"), approve_pr=lambda *_: False,
    )
    if result.get("__interrupt__"):
        print(f"[!!] expected the resumed task to complete, got another unresolved interrupt: "
              f"{result['__interrupt__']}")
        return 1
    if not result.get("commit_result") or result["commit_result"].get("status") != "ok":
        print(f"[!!] expected a successful commit after resuming, got {result.get('commit_result')!r}")
        return 1
    print(f"[ok] resumed the paused task from a fresh engine instance -- "
          f"commit_result={result['commit_result']}")

    # --- delete both threads: checkpoints + run_history rows -------------
    engine.delete_thread(qa_thread_id)  # a QA thread has no checkpoint, just run_history rows
    engine.delete_thread(task_thread_id)
    remaining = {t["thread_id"] for t in history.list_threads(limit=50)}
    if qa_thread_id in remaining or task_thread_id in remaining:
        print(f"[!!] expected both threads gone from /history after delete, still present: "
              f"{remaining & {qa_thread_id, task_thread_id}}")
        return 1
    if history.get_run(runs[0]["id"]) is not None:
        print("[!!] expected the QA thread's run rows to be gone after delete_thread")
        return 1
    try:
        engine.resume_task(task_thread_id, approve=lambda plan: PlanDecision("approve"))
        print("[!!] expected resume_task to fail after delete_thread removed its checkpoint")
        return 1
    except RuntimeError:
        pass
    print("[ok] delete_thread removed both threads' checkpoints and run_history rows -- "
          "gone from /history, and the task thread can no longer be resumed")

    print("\nPhase 21 OK. Two separate conversations survive the app being restarted: "
          "both appear in /history's data source, the Q&A transcript replays with full "
          "context, and the task paused mid-approval resumes and completes from a fresh "
          "process. Deleting a thread removes both its checkpoints and its run_history rows.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
