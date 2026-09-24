"""POST /events/{event_id}/extend 受约束截止时间延长测试。

SQLite 文件库 + 模拟时钟（与 test_api.py 相同手法），覆盖：
  - commit 阶段成功延长：响应含旧值/新值/reason，事件截止时间被更新
  - 完全相同的请求幂等重放：返回同一结果，不重复写入审计记录
  - 只延不缩：缩短/不变 commit 或 reveal、reveal 不晚于新 commit 均 409
  - 进入 reveal / closed 阶段后延长返回 409
  - 无时区时间 422、未知事件 404
  - 任何失败不改变截止时间；延长记录出现在时间线中
"""

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_extend_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/extend.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import DeadlineExtension  # noqa: E402

T0 = datetime(2030, 5, 1, 0, 0, 0, tzinfo=timezone.utc)

COMMIT_DL = T0 + timedelta(hours=1)
REVEAL_DL = T0 + timedelta(hours=2)


class MutableClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t


clock = MutableClock(T0)

client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    clock.t = T0
    monkeypatch.setattr(clock_module, "now_utc", clock)
    with client:  # 触发 lifespan 建表（幂等）
        yield


@pytest.fixture
def event_id() -> str:
    r = client.post("/events", json={
        "title": "待延长事件",
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def iso(dt: datetime) -> str:
    return dt.isoformat()


def dt(s: str) -> datetime:
    """解析响应中的时间字符串（兼容 Z 与 +00:00），按时刻比较。"""
    return datetime.fromisoformat(s)


def get_event(event_id: str) -> dict:
    r = client.get(f"/events/{event_id}")
    assert r.status_code == 200, r.text
    return r.json()


def extension_count(event_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(DeadlineExtension).filter(
            DeadlineExtension.event_id == UUID(event_id)
        ).count()
    finally:
        db.close()


def test_extend_success_returns_old_new_and_reason(event_id):
    new_commit = COMMIT_DL + timedelta(hours=3)
    new_reveal = REVEAL_DL + timedelta(hours=4)
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(new_commit),
        "reveal_deadline": iso(new_reveal),
        "reason": "数据延迟，顺延提交窗口",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_id"] == event_id
    assert dt(body["old_commit_deadline"]) == COMMIT_DL
    assert dt(body["old_reveal_deadline"]) == REVEAL_DL
    assert dt(body["new_commit_deadline"]) == new_commit
    assert dt(body["new_reveal_deadline"]) == new_reveal
    assert body["reason"] == "数据延迟，顺延提交窗口"
    assert "extended_at" in body

    # 事件截止时间确实被更新，且仍处于 commit 阶段
    event = get_event(event_id)
    assert dt(event["commit_deadline"]) == dt(body["new_commit_deadline"])
    assert dt(event["reveal_deadline"]) == dt(body["new_reveal_deadline"])
    assert event["status"] == "commit"
    assert extension_count(event_id) == 1


def test_extend_idempotent_replay_returns_same_result(event_id):
    new_commit = COMMIT_DL + timedelta(hours=1)
    new_reveal = REVEAL_DL + timedelta(hours=1)
    payload = {
        "commit_deadline": iso(new_commit),
        "reveal_deadline": iso(new_reveal),
        "reason": "幂等测试",
    }
    r1 = client.post(f"/events/{event_id}/extend", json=payload)
    assert r1.status_code == 200, r1.text

    # 时钟推进后重放完全相同的请求：仍返回首次结果，不重复写入
    clock.t = T0 + timedelta(minutes=30)
    r2 = client.post(f"/events/{event_id}/extend", json=payload)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()
    assert extension_count(event_id) == 1

    event = get_event(event_id)
    assert dt(event["commit_deadline"]) == dt(r1.json()["new_commit_deadline"])
    assert dt(event["reveal_deadline"]) == dt(r1.json()["new_reveal_deadline"])


def test_extend_idempotency_requires_same_reason(event_id):
    new_commit = COMMIT_DL + timedelta(hours=1)
    new_reveal = REVEAL_DL + timedelta(hours=1)
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(new_commit),
        "reveal_deadline": iso(new_reveal),
        "reason": "首次延长",
    })
    assert r.status_code == 200

    # 相同截止时间但 reason 不同：不是同一请求，按普通校验处理（未再延长）-> 409
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(new_commit),
        "reveal_deadline": iso(new_reveal),
        "reason": "另一个理由",
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "INVALID_EXTENSION"
    assert extension_count(event_id) == 1


def test_extend_sequential_extensions(event_id):
    first_commit = COMMIT_DL + timedelta(hours=1)
    first_reveal = REVEAL_DL + timedelta(hours=1)
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(first_commit),
        "reveal_deadline": iso(first_reveal),
        "reason": "第一次",
    })
    assert r.status_code == 200

    # 在新窗口内再次延长（基于更新后的旧值校验）
    clock.t = COMMIT_DL + timedelta(minutes=30)  # 旧 commit 截止已过，新截止未到
    second_commit = first_commit + timedelta(hours=1)
    second_reveal = first_reveal + timedelta(hours=2)
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(second_commit),
        "reveal_deadline": iso(second_reveal),
        "reason": "第二次",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert dt(body["old_commit_deadline"]) == first_commit
    assert dt(body["old_reveal_deadline"]) == first_reveal
    assert extension_count(event_id) == 2


@pytest.mark.parametrize("new_commit,new_reveal,case", [
    (COMMIT_DL - timedelta(hours=1), REVEAL_DL + timedelta(hours=1), "缩短 commit"),
    (COMMIT_DL, REVEAL_DL + timedelta(hours=1), "commit 不变"),
    (COMMIT_DL + timedelta(minutes=15), REVEAL_DL - timedelta(minutes=30), "缩短 reveal"),
    (COMMIT_DL + timedelta(minutes=30), REVEAL_DL, "reveal 不变"),
    (COMMIT_DL + timedelta(hours=2), COMMIT_DL + timedelta(hours=2), "reveal 等于新 commit"),
    (COMMIT_DL + timedelta(hours=2), COMMIT_DL + timedelta(hours=1), "reveal 早于新 commit"),
])
def test_extend_rejects_non_extension(event_id, new_commit, new_reveal, case):
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(new_commit),
        "reveal_deadline": iso(new_reveal),
        "reason": case,
    })
    assert r.status_code == 409, f"{case}: {r.text}"
    assert r.json()["detail"]["code"] == "INVALID_EXTENSION"

    # 失败不改变截止时间，也不留下审计记录
    event = get_event(event_id)
    assert dt(event["commit_deadline"]) == COMMIT_DL
    assert dt(event["reveal_deadline"]) == REVEAL_DL
    assert extension_count(event_id) == 0


def test_extend_rejected_in_reveal_and_closed_phases(event_id):
    # 进入 reveal 阶段
    clock.t = COMMIT_DL
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(COMMIT_DL + timedelta(hours=5)),
        "reveal_deadline": iso(REVEAL_DL + timedelta(hours=5)),
        "reason": "太迟了",
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "EXTENSION_NOT_IN_COMMIT_PHASE"

    # 进入 closed 阶段
    clock.t = REVEAL_DL
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(COMMIT_DL + timedelta(hours=5)),
        "reveal_deadline": iso(REVEAL_DL + timedelta(hours=5)),
        "reason": "更迟了",
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "EXTENSION_NOT_IN_COMMIT_PHASE"

    # 截止时间未被改变
    event = get_event(event_id)
    assert dt(event["commit_deadline"]) == COMMIT_DL
    assert dt(event["reveal_deadline"]) == REVEAL_DL
    assert extension_count(event_id) == 0


def test_extend_naive_datetime_returns_422(event_id):
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": "2030-05-01T05:00:00",  # 无时区
        "reveal_deadline": iso(REVEAL_DL + timedelta(hours=5)),
        "reason": "无时区",
    })
    assert r.status_code == 422

    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(COMMIT_DL + timedelta(hours=5)),
        "reveal_deadline": "2030-05-01T08:00:00",  # 无时区
        "reason": "无时区",
    })
    assert r.status_code == 422

    # reason 缺失或为空同样 422
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(COMMIT_DL + timedelta(hours=5)),
        "reveal_deadline": iso(REVEAL_DL + timedelta(hours=5)),
    })
    assert r.status_code == 422
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(COMMIT_DL + timedelta(hours=5)),
        "reveal_deadline": iso(REVEAL_DL + timedelta(hours=5)),
        "reason": "",
    })
    assert r.status_code == 422

    event = get_event(event_id)
    assert dt(event["commit_deadline"]) == COMMIT_DL
    assert extension_count(event_id) == 0


def test_extend_unknown_event_returns_404():
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.post(f"/events/{missing}/extend", json={
        "commit_deadline": iso(COMMIT_DL + timedelta(hours=5)),
        "reveal_deadline": iso(REVEAL_DL + timedelta(hours=5)),
        "reason": "不存在的事件",
    })
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "EVENT_NOT_FOUND"


def test_extend_non_uuid_returns_422():
    r = client.post("/events/not-a-uuid/extend", json={
        "commit_deadline": iso(COMMIT_DL + timedelta(hours=5)),
        "reveal_deadline": iso(REVEAL_DL + timedelta(hours=5)),
        "reason": "非法 ID",
    })
    assert r.status_code == 422


def test_extend_accepts_non_utc_timezone(event_id):
    # +08:00 表示的时刻与 UTC 等价比较
    new_commit = COMMIT_DL + timedelta(hours=2)
    new_reveal = REVEAL_DL + timedelta(hours=2)
    plus8 = timezone(timedelta(hours=8))
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": new_commit.astimezone(plus8).isoformat(),
        "reveal_deadline": new_reveal.astimezone(plus8).isoformat(),
        "reason": "带偏移时区",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert dt(body["new_commit_deadline"]) == new_commit
    assert dt(body["new_reveal_deadline"]) == new_reveal


def test_extend_appears_in_timeline_and_created_keeps_original(event_id):
    new_commit = COMMIT_DL + timedelta(hours=1)
    new_reveal = REVEAL_DL + timedelta(hours=1)
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(new_commit),
        "reveal_deadline": iso(new_reveal),
        "reason": "时间线可见",
    })
    assert r.status_code == 200

    r = client.get(f"/events/{event_id}/timeline")
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    assert [e["type"] for e in entries] == ["event_created", "deadline_extended"]

    # event_created 仍展示创建时的原始截止时间
    created = entries[0]["detail"]
    assert dt(created["commit_deadline"]) == COMMIT_DL
    assert dt(created["reveal_deadline"]) == REVEAL_DL

    extended = entries[1]["detail"]
    assert dt(extended["old_commit_deadline"]) == COMMIT_DL
    assert dt(extended["new_commit_deadline"]) == new_commit
    assert extended["reason"] == "时间线可见"


def _invert_same_instant_extension_ids(event_id: str) -> None:
    """把同刻写入的两条延长记录的 UUID 改成业务倒挂：首次延长 UUID 大、
    第二次延长 UUID 小（经临时 UUID 两步改键，避开主键唯一约束）。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(DeadlineExtension)
            .filter(DeadlineExtension.event_id == UUID(event_id))
            .all()
        )
        assert len(rows) == 2
        rows.sort(key=lambda e: e.new_commit_deadline)
        first, second = rows
        # 每次取互不相同的随机 UUID 并强制大小关系，避免跨用例主键冲突
        a, b = uuid.uuid4(), uuid.uuid4()
        while a == b:
            b = uuid.uuid4()
        high_id, low_id = (a, b) if a > b else (b, a)
        first.id = uuid.uuid4()  # 临时键，腾出目标值
        db.flush()
        second.id = low_id
        db.flush()
        first.id = high_id
        db.commit()
    finally:
        db.close()


def test_same_instant_two_extensions_with_inverted_uuids(event_id):
    """同一时刻连续成功延长两次、且第二条记录 UUID 小于第一条：
    延长链仍按新截止时间严格递增识别先后。"""
    first_commit = COMMIT_DL + timedelta(hours=1)
    first_reveal = REVEAL_DL + timedelta(hours=1)
    second_commit = COMMIT_DL + timedelta(hours=2)
    second_reveal = REVEAL_DL + timedelta(hours=2)
    second_payload = {
        "commit_deadline": iso(second_commit),
        "reveal_deadline": iso(second_reveal),
        "reason": "同刻第二次",
    }

    # 时钟不动：两次延长写在同一 created_at 时刻
    r1 = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(first_commit),
        "reveal_deadline": iso(first_reveal),
        "reason": "同刻第一次",
    })
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/events/{event_id}/extend", json=second_payload)
    assert r2.status_code == 200, r2.text

    # 制造「第二条记录 UUID 小于第一条」的同刻倒挂
    _invert_same_instant_extension_ids(event_id)

    # 1) 最新请求（第二次延长）重放仍幂等返回首次响应，不误报 409
    r3 = client.post(f"/events/{event_id}/extend", json=second_payload)
    assert r3.status_code == 200, r3.text
    assert r3.json() == r2.json()
    assert extension_count(event_id) == 2

    # 事件当前截止时间为两次延长后的值
    event = get_event(event_id)
    assert dt(event["commit_deadline"]) == second_commit
    assert dt(event["reveal_deadline"]) == second_reveal

    # 2) 时间线同刻记录顺序与延长链一致：第一次 -> 第二次
    tl = client.get(f"/events/{event_id}/timeline")
    assert tl.status_code == 200, tl.text
    entries = tl.json()["entries"]
    ext_entries = [e for e in entries if e["type"] == "deadline_extended"]
    assert len(ext_entries) == 2
    assert dt(ext_entries[0]["detail"]["new_commit_deadline"]) == first_commit
    assert dt(ext_entries[1]["detail"]["new_commit_deadline"]) == second_commit
    # 链中第二次延长的旧值是第一次延长的新值，链关系不被倒挂打乱
    assert dt(ext_entries[1]["detail"]["old_commit_deadline"]) == first_commit
    # event_created 始终展示创建时的原始截止时间，不回退为中间值
    created = next(e for e in entries if e["type"] == "event_created")
    assert dt(created["detail"]["commit_deadline"]) == COMMIT_DL
    assert dt(created["detail"]["reveal_deadline"]) == REVEAL_DL

    # 3) 恰好该时刻的历史快照应用两次延长后的截止时间
    snap = client.get(f"/events/{event_id}/snapshot", params={"at": iso(T0)})
    assert snap.status_code == 200, snap.text
    body = snap.json()
    assert dt(body["commit_deadline"]) == second_commit
    assert dt(body["reveal_deadline"]) == second_reveal
    assert body["status"] == "commit"
