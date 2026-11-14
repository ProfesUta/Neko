import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

import config
from core.league import LeagueClient
from core.league_knowledge import LeagueKnowledge
from core.league_events import MatchEvent
from core.league_journal import MatchJournal


@pytest.fixture
def knowledge(tmp_path,monkeypatch):
    monkeypatch.setattr(config,"LEAGUE_KNOWLEDGE_ENABLED",True)
    monkeypatch.setattr(config,"LEAGUE_STATIC_DOWNLOADS_ENABLED",True)
    names = SimpleNamespace(names={64:"Lee Sin",103:"Ahri"})
    calls = []
    def response(request):
        calls.append(request.url.path)
        assert request.url.host == "ddragon.leagueoflegends.com" and request.method == "GET"
        path = request.url.path
        if path.endswith("versions.json"):
            return httpx.Response(200,json=["99.1.1"])
        if path.endswith("/champion.json"):
            return httpx.Response(200,json={"data":{"LeeSin":{"name":"Lee Sin"},"Ahri":{"name":"Ahri"}}})
        if path.endswith("/item.json"):
            return httpx.Response(200,json={"data":{"1001":{"name":"Example Boots","description":"<b>Speed</b>: Move faster."}}})
        key = path.rsplit("/",1)[-1].split(".")[0]
        return httpx.Response(200,json={"data":{key:{"name":"Lee Sin" if key=="LeeSin" else "Ahri","tags":["Fighter"],
            "passive":{"name":"Energy","description":"<b>Energy</b> after a spell."},
            "spells":[{"name":slot+" skill","description":slot+" description {{ scaling }}."} for slot in "QWER"],
            "allytips":["General playing tip."],"enemytips":["General weakness tip."]}}})
    service = LeagueKnowledge(names,tmp_path/"static",httpx.Client(transport=httpx.MockTransport(response)))
    yield service,calls
    service.close()


def test_specific_ability_patch_source_and_repeated_memory_cache(knowledge,monkeypatch):
    service,calls = knowledge
    answer = service.answer("What does Lee Sin Q do?")
    assert "99.1.1" in answer and "Q skill" in answer and "R skill" not in answer
    assert "<b>" not in answer and "[unresolved scaling]" in answer
    assert len(calls)==3
    monkeypatch.setattr(service,"_disk",lambda *args:pytest.fail("Warm query read disk"))
    assert service.answer("What does Lee Sin Q do?")==answer and len(calls)==3
    context=service.context_for("What does Lee Sin Q do?")
    assert context[0]["source"].startswith("https://ddragon.")
    context[0]["champion"]="tamper"
    assert service.context_for("What does Lee Sin Q do?")[0]["champion"]=="Lee Sin"
    assert service.context_for("Unrelated question") is None


def test_pinned_disk_cache_works_without_network_or_lcu_names(knowledge,monkeypatch):
    service,_=knowledge
    expected=service.answer("What does Lee Sin Q do?")
    monkeypatch.setattr(config,"LEAGUE_STATIC_DOWNLOADS_ENABLED",False)
    offline=LeagueKnowledge(SimpleNamespace(names={}),service.root)
    assert offline.answer("What does Lee Sin Q do?")==expected
    assert offline._http is None


def test_items_classes_tips_and_uncertain_interactions_are_not_invented(knowledge):
    service,_=knowledge
    service.data("item")
    assert "Move faster" in service.answer("What does Example Boots do?")
    assert "not assigned lane roles" in service.answer("What role does Lee Sin play?")
    assert "General weakness tip" in service.answer("What are Lee Sin weaknesses?")
    result=service.answer("Does Lee Sin Q interact with Ahri E?")
    assert "don't verify that exact interaction" in result and "E skill" in result
    assert "current location" in service.answer("Explain jungle pathing")
    assert "team buffs" in service.answer("What does Baron do?")


@pytest.mark.parametrize("text",["Do you have the ability to watch my screen?","What does this Python code do?","What's on your mind?","I like cooking"])
def test_unrelated_conversations_never_fetch_references(knowledge,text):
    service,calls=knowledge
    assert service.answer(text) is None and calls==[]


def test_concurrent_cache_misses_coalesce_and_malformed_responses_fail_honestly(knowledge):
    service,calls=knowledge
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers=list(pool.map(lambda _:service.answer("What does Lee Sin Q do?"),range(8)))
    assert len(set(answers))==1 and len(calls)==3
    bad=LeagueKnowledge(service.names,service.root/"bad",httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=[]))))
    assert "won't guess" in bad.answer("What does Lee Sin Q do?")
    bad.close()


def test_new_reference_route_does_not_call_model_or_screen(knowledge,monkeypatch):
    from core.context import Context
    from gui.workers import ChatWorker
    context=Context()
    context.league.knowledge=knowledge[0]
    monkeypatch.setattr(context,"refresh_screen",lambda *a,**k:pytest.fail("Unexpected screen inference"))
    monkeypatch.setattr("core.conversation.request_chat",lambda **k:pytest.fail("Unexpected model"))
    worker=ChatWorker([],context,"What does Lee Sin Q do?")
    responses,errors=[],[]
    worker.response_ready.connect(responses.append)
    worker.error.connect(errors.append)
    worker.run()
    assert not errors and "Q skill" in responses[0]


def test_reported_sightings_are_opt_in_timestamped_and_never_current_positions(monkeypatch):
    journal=MatchJournal()
    journal.reset(1)
    game={"game":{"gameTime":252}}
    assert "disabled" in journal.sighting_request("I saw Lee Sin top.",game,1,["Lee Sin"])
    monkeypatch.setattr(config,"LEAGUE_SIGHTING_NOTES_ENABLED",True)
    clock=[100.]
    monkeypatch.setattr("core.league_journal.time.monotonic",lambda:clock[0])
    assert "04:12" in journal.sighting_request("I saw Lee Sin top.",game,1,["Lee Sin"])
    clock[0]+=30
    answer=journal.sighting_request("Where did I last report Lee Sin?",game,1,["Lee Sin"])
    assert "30 seconds" in answer and "not their current location" in answer
    assert journal.notes[-1].source.startswith("user-reported")
    journal.reset(2)
    journal.reset(1)  # A racing old request must not restore the old epoch.
    assert journal.epoch==2 and not journal.notes
    assert "match changed" in journal.sighting_request("I saw Lee Sin top.",game,1,["Lee Sin"])


def test_snapshot_queries_and_teamfight_limits_are_deterministic(monkeypatch):
    monkeypatch.setattr(config,"LEAGUE_LIVE_DATA_ENABLED",True)
    monkeypatch.setattr(config,"LEAGUE_EVENTS_ENABLED",True)
    client=LeagueClient()
    monkeypatch.setattr(client,"read",lambda *a:{"items":["Boots"],"scores":{"kills":2,"deaths":1,"assists":5}})
    assert "Boots" in client.companion_answer("What items have I bought?")
    assert "2 kills" in client.companion_answer("What's my current KDA?")
    monkeypatch.setattr(client,"events",lambda:(MatchEvent(1,"ChampionKill",500,100,killer="Ahri",victim="Lee Sin"),))
    answer=client.companion_answer("What happened in the last teamfight?")
    assert "can't identify teamfight boundaries" in answer and "Ahri killed Lee Sin" in answer
    assert "can't verify current enemy positions" in client.companion_answer("Where is the enemy jungler?")


def test_postgame_objectives_survive_kill_history_eviction_and_are_reset():
    journal=MatchJournal()
    dragon=MatchEvent(1,"DragonKill",300,100,killer="Yuumi",side="ally")
    journal.update(1,{"champion":"Yuumi"})
    journal.observe_events(1,(dragon,),())
    end=MatchEvent(99,"GameEnd",1800,200,result="Win")
    journal.finish(1,(end,))
    answer=journal.postgame_answer("Review my last match")
    assert "victory" in answer and "05:00" in answer and "don't show positioning" in answer
    journal.reset(2)
    assert not journal.objectives and journal.last_game is None
    assert "victory" in journal.postgame_answer("Review my last match")


def test_full_item_catalog_does_not_recompile_names_on_every_warm_question(knowledge):
    from core.league_knowledge import name_pattern
    service,_=knowledge
    service.data("item")
    items={str(index):{"name":f"CatalogItem{index}","description":"Known static effect"} for index in range(700)}
    service._item_index={"data":items}
    service._memory[f"{service.version}-item.json"]=service._item_index
    name_pattern.cache_clear()
    assert "Known static effect" in service.answer("What does CatalogItem699 do?")
    misses=name_pattern.cache_info().misses
    assert "Known static effect" in service.answer("What does CatalogItem699 do?")
    assert name_pattern.cache_info().misses==misses


def test_mode_variant_items_require_inventory_or_map_evidence(knowledge):
    service,_=knowledge
    service.data("item")
    items={"1":{"name":"Example Boots","description":"Mode one effect","maps":{"11":True}},
           "2":{"name":"Example Boots","description":"Mode two effect","maps":{"12":True}}}
    service._item_index={"data":items}
    service._memory[f"{service.version}-item.json"]=service._item_index
    assert "different game-mode versions" in service.answer("What does Example Boots do?")
    assert "Mode one effect" in service.answer("What does Example Boots do?",map_id=11)
    assert "Mode two effect" in service.answer("What does Example Boots do?",item_ids=[2])


def test_public_enemy_role_reuses_the_existing_playerlist_without_more_live_requests(knowledge,monkeypatch):
    monkeypatch.setattr(config,"LEAGUE_LIVE_DATA_ENABLED",True)
    client=LeagueClient();client.knowledge=knowledge[0]
    data={"gamestats":{"gameTime":300,"mapNumber":11},"activeplayer":{"riotId":"own","championStats":{}},
          "playerlist":[{"riotId":"own","championName":"Yuumi","team":"ORDER","position":"UTILITY"},
                        {"riotId":"enemy","championName":"Lee Sin","team":"CHAOS","position":"JUNGLE"}]}
    requests=[]
    async def refresh():
        def response(request):
            requests.append(request.url.path)
            return httpx.Response(200,json=data[request.url.path.rsplit("/",1)[-1]])
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as http:
            await client.refresh_stats(http)
    asyncio.run(refresh())
    assert "Q skill" in client.companion_answer("What does the enemy jungler's champion do?")
    assert len(requests)==3
    assert all(set(item)=={"champion","side","position"} for item in client.roster())


def test_a_match_reset_during_note_request_cannot_attach_an_old_clock(monkeypatch):
    monkeypatch.setattr(config,"LEAGUE_LIVE_DATA_ENABLED",True)
    monkeypatch.setattr(config,"LEAGUE_SIGHTING_NOTES_ENABLED",True)
    client=LeagueClient();client.champ_select.names.names={64:"Lee Sin"}
    def read(*args):
        client.tracker.reset();client.journal.reset(client.tracker.epoch)
        return {"game":{"gameTime":1500}}
    monkeypatch.setattr(client,"read",read)
    assert "fresh match clock" in client.companion_answer("I saw Lee Sin top.")
    assert not client.journal.notes


def test_detailed_postgame_coaching_reuses_streaming_with_only_recorded_context(monkeypatch):
    from core.context import Context
    from gui.workers import ChatWorker
    context=Context()
    context.league.journal.update(context.league.tracker.epoch,{"champion":"Yuumi","scores":{"kills":1,"deaths":2,"assists":8}})
    context.league.journal.finish(context.league.tracker.epoch,(MatchEvent(1,"DragonKill",300,100,side="ally"),))
    monkeypatch.setattr(config,"CONVERSATION_STREAMING_ENABLED",True)
    monkeypatch.setattr("core.conversation.conversation_model",lambda:"qwen2.5:32b")
    monkeypatch.setattr(context,"refresh_screen",lambda *a,**k:pytest.fail("Post-game reasoning used live screen"))
    requests=[]
    def model(**options):
        requests.append(options)
        assert "game_clock_events" in options["messages"][0]["content"]
        options["on_text"]("The recorded objective is an observation; the log doesn't establish your positioning.")
        return SimpleNamespace(message=SimpleNamespace(content="Grounded review."))
    monkeypatch.setattr("core.conversation.request_chat",model)
    worker=ChatWorker([],context,"Give me detailed postgame coaching")
    errors=[];worker.error.connect(errors.append);worker.run()
    assert not errors and len(requests)==1
    assert requests[0]["model"]=="qwen2.5:32b"
