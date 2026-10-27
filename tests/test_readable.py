from fastapi.testclient import TestClient

from agrisea import readable
from agrisea.config import Settings
from agrisea.web.app import create_app


def U(idx, stype, name, text, **kw):
    return {"idx": idx, "speaker_type": stype, "speaker_name": name,
            "speaker_role": {"member": "위원", "chair": "위원장"}.get(stype, "농림축산식품부장관"),
            "text": text, "issues": kw.pop("issues", {}), "is_commitment": kw.pop("c", False),
            "is_data_request": False, "agenda_idx": 0, **kw}


def test_turns_group_member_questions_and_answers():
    utts = [
        U(1, "chair", "어기구", "다음은 서천호 위원님 질의해 주시기 바랍니다."),
        U(2, "member", "서천호", "장관님."),
        U(3, "official", "송미령", "예, 위원님."),
        U(4, "member", "서천호", "쌀 자급률은 84%인데 밀은 1.1%에 불과합니다. 전략작물 전환을 추진해야 되지 않습니까?",
          issues={"rice": 2}),
        U(5, "official", "송미령", "맞습니다, 위원님."),
        U(6, "member", "서천호", "유통비용이 너무 큽니다."),
        U(7, "member", "서천호", "사과 생산자는 2200원을 받는데 소비자는 6000원을 냅니다. 근본 대책이 있습니까?"),
        U(8, "official", "송미령", "위원님, 저희가 유통구조 효율화 방안을 발표했고 온라인 도매시장을 확대하겠습니다. 그 밖에도 여러 가지를 하고 있습니다.",
          c=True),
        U(9, "chair", "어기구", "시간 됐습니다."),          # 짧은 끼어들기는 차례를 끊지 않는다
        U(10, "member", "서천호", "좋아요."),               # 답 없는 짧은 말은 줄로 세우지 않는다
        U(11, "chair", "어기구", "서천호 위원님 수고하셨습니다. 다음은 윤준병 위원님 질의해 주시기 바랍니다."),
        U(12, "member", "윤준병", "쌀값 20만 원 약속은 지켜졌습니까?"),
        U(13, "official", "송미령", "지금은 안 됩니다."),
    ]
    ts = readable.turns(utts)
    assert [t["member"] for t in ts] == ["서천호", "윤준병"]
    first = ts[0]
    assert first["start_idx"] == 2 and first["end_idx"] == 10 and first["issues"] == ["쌀·식량안보"]
    items = first["items"]
    assert len(items) == 2  # '장관님.→예, 위원님.' 과 '좋아요.' 는 빠짐
    assert items[0]["q"].endswith("않습니까?") and items[0]["a"] == "맞습니다, 위원님." and items[0]["short"]
    # 답을 듣기 전 이어진 두 발언은 한 질문, 짧은 물음에는 앞 문장(맥락)을, 답은 약속 문장(호칭 뺌)
    assert items[1]["idx"] == 6 and items[1]["q"] == "사과 생산자는 2200원을 받는데 소비자는 6000원을 냅니다. 근본 대책이 있습니까?"
    assert items[1]["commitment"] and items[1]["a"].startswith("저희가") and items[1]["answerer"] == "농림축산식품부장관 송미령"
    assert first["commitments"] == 1 and ts[1]["items"][0]["a"] == "지금은 안 됩니다."


def test_rejoin_map_only_joins_rare_fragments():
    texts = ["활성화 방안을 마련하겠습니다."] * 30 + ["온라인 도매시장 활 성화뿐만 아니라", "그 사람이 그 사업을"] + ["그 사람"] * 5
    m = readable.rejoin_map(texts, min_joined=20)
    assert m.get("활 성화") == "활성화"
    assert "그 사람" not in m and "그 사업" not in m  # 흔한 낱말('그')은 잇지 않는다


def test_turns_and_readable_endpoints(store, tmp_path):
    c = TestClient(create_app(Settings(api_key="K", data_dir=tmp_path), store))
    mid = c.get("/api/meetings").json()[0]["id"]
    d = c.get(f"/api/meetings/{mid}/turns").json()
    assert d["turns"] and all(t["items"] for t in d["turns"])
    assert c.get("/api/meetings/nope/turns").status_code == 404
    g = c.get("/api/readable").json()
    assert set(g) == {"rejoin", "orgs", "laws"} and g["orgs"]["농진청"] == "농촌진흥청"
